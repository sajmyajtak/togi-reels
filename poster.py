"""Postování TOGI reelů z GitHubu — Mac nemusí být zapnutý.

Klipy vyrábí ClipForge na Macu (YouTube stahování z cloudu blokuje) a
nahraje je sem: video jako asset releasu `klipy`, popis do queue.json,
pravidla do rules.json. Tenhle skript v GitHub Actions:

  1. počká na čas z plánu (rules.json: 15:00 / 18:30 / 22:00 Praha),
  2. vezme nejlépe hodnocený klip, který ještě nevyšel,
  3. vystaví video přes KLAPKU (/media) — jen tak Instagram připojí hudbu
     z knihovny (u nahrání souboru ji tiše zahodí, ověřeno 10. 11. 2026),
  4. zveřejní Reel s hudbou, zapíše ho do posted.json (hned commit),
  5. propíše stav do KLAPKY (Airtable): klip „venku“ + nový plán.

Proč čeká uvnitř běhu: GitHub pouští naplánované běhy jen jednou za
několik hodin, ať je cron jakýkoli. Běh proto čeká na okna sám a když
další okno připadne až za jeho konec, spustí další běh (viz workflow).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
GRAPH = "https://graph.facebook.com/v26.0"
AIRTABLE = "https://api.airtable.com/v0"
UA = {"User-Agent": "ClipForge/1.0 (KLAPKA autoclipping)"}

# Airtable — tabulky a pole KLAPKY (base autoclippingu)
T_CLIPS, T_SETTINGS = "tbltIcs134lco0QB7", "tblcYY4LXKPFEHeoc"
F = {"stage": "fld3C6ZuxKzd4LFeE", "url": "fld5zhNU1qjBAG7y2", "published": "fldYnFT2LkXXEyPdH",
     "platform": "fldhSpE5DN1LH9zvq", "account": "fldFR4CaswZUbLKcs"}
F_SET_KEY, F_SET_VAL, F_SET_AT = "fld0Qr7YclciePaxM", "fldPb8fy7pqsdBBs1", "fldzGa2AZJHyyakW8"
T_ACCOUNTS = "tbl13UIOw9q07Od7H"
F_METRIC = {"views": "fldHkXs7nmukhKe81", "likes": "fldFgDCEXB3HuAJF9",
            "comments": "fldf4Sj8xKm8scHkc", "shares": "fldxdJ9PM6uLhZacj"}
F_FOLLOWERS = "fldDCRPWlLqsKgFP6"
F_CATEGORY = "fldMamMhz9aN8PFev"
METRICS_EVERY_H = 6      # Airtable free: 1 000 volání/měsíc na celý workspace (sdílí ho i leady)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def load(name: str, default):
    p = ROOT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name: str, data) -> None:
    (ROOT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def parse(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def git_commit(msg: str) -> None:
    """Každý post hned do repozitáře — souběžný běh ho pak uvidí."""
    if os.environ.get("POSTER_GIT_COMMIT") != "1":
        return
    run = lambda *a: subprocess.run(["git", *a], cwd=ROOT, check=False, capture_output=True, text=True)
    run("add", "posted.json", "state.json")
    if run("diff", "--cached", "--quiet").returncode == 0:
        return
    run("commit", "-m", msg)
    run("pull", "--rebase", "origin", os.environ.get("GITHUB_REF_NAME", "main"))
    r = run("push", "origin", f"HEAD:{os.environ.get('GITHUB_REF_NAME', 'main')}")
    log("commit " + ("✓" if r.returncode == 0 else f"selhal: {r.stderr[:160]}"))


def git_pull() -> None:
    if os.environ.get("POSTER_GIT_COMMIT") == "1":
        subprocess.run(["git", "pull", "--rebase", "origin", os.environ.get("GITHUB_REF_NAME", "main")],
                       cwd=ROOT, check=False, capture_output=True)


# ── pravidla a výběr ─────────────────────────────────────────
class Rules:
    def __init__(self, d: dict):
        self.on = bool(d.get("on"))
        self.tz = ZoneInfo(d.get("tz", "Europe/Prague"))
        self.slots = list(d.get("slots") or [])
        self.min_gap = int(d.get("min_gap_minutes", 120))
        self.min_score = int(d.get("min_ai_score", 55))
        self.music = d.get("music") or {}
        self.account = d.get("account", "")
        self.account_rec = d.get("account_rec", "")
        self.attribution = d.get("attribution", "")
        self.banned = {str(b).lower().lstrip("#") for b in d.get("banned_tags") or []}
        self.plan_rec = d.get("plan_rec", "")      # řádek plánu v Nastavení (založil ho Mac)

    def slots_on(self, day: dt.date) -> list[dt.datetime]:
        out = []
        for hhmm in self.slots:
            h, m = (int(x) for x in hhmm.split(":"))
            out.append(dt.datetime.combine(day, dt.time(h, m), tzinfo=self.tz))
        return sorted(out)


def pool(queue: list[dict], posted: list[dict], rules: Rules) -> list[dict]:
    done = {p["id"] for p in posted}
    # Přestřih už zveřejněného momentu má nové ID, ale stejný nadpis — ven podruhé nesmí.
    titles = {(p.get("title") or "").strip().lower() for p in posted} - {""}
    items = [c for c in queue if c["id"] not in done and int(c.get("score") or 0) >= rules.min_score
             and c.get("ok", True) and (c.get("header") or "").strip().lower() not in titles]
    return sorted(items, key=lambda c: (int(c.get("score") or 0), c.get("created", "")), reverse=True)


def last_post_time(posted: list[dict], ig_last: dt.datetime | None) -> dt.datetime | None:
    times = [parse(p["at"]) for p in posted] + ([ig_last] if ig_last else [])
    return max(times) if times else None


def due(rules: Rules, posted: list[dict], ig_last: dt.datetime | None, now: dt.datetime) -> str:
    """Prázdný řetězec = teď se smí postovat, jinak důvod."""
    if not rules.on:
        return "vypnuto"
    local = now.astimezone(rules.tz)
    today = [p for p in posted if parse(p["at"]).astimezone(rules.tz).date() == local.date()]
    slots = rules.slots_on(local.date())
    if len(today) >= len([s for s in slots if s <= local]):
        return "žádný čas teď nenastal"
    last = last_post_time(posted, ig_last)
    if last and (now - last).total_seconds() < rules.min_gap * 60:
        return f"rozestup: od posledního postu {int((now - last).total_seconds() // 60)} min"
    return ""


def next_slot(rules: Rules, posted: list[dict], now: dt.datetime) -> dt.datetime | None:
    """Nejbližší dnešní čas, na který ještě nic nevyšlo."""
    local = now.astimezone(rules.tz)
    today = [p for p in posted if parse(p["at"]).astimezone(rules.tz).date() == local.date()]
    future = [s for s in rules.slots_on(local.date())[len(today):] if s > local]
    return future[0] if future else None


BLOCK = ("phonk", "orchestra", "philharmonic", "sibelius", "etude", "suicid", "lullaby", "meditation",
         "sleep", "christmas", "xmas", "santa", "noel", "jingle", "snow", "winter", "lullaby", "holiday",
         "navidad", "weihnacht", "vánoc", "december", "silent night")
_SONG_CACHE: dict = {}


def _norm(s: str | None) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (s or "").lower()).split())


def niche_list(rules: Rules) -> list[dict]:
    """Schválené niche písničky z configu: „Název — Interpret“ nebo {title, artist, audio_id}."""
    out = []
    for t in rules.music.get("tracks") or []:
        if isinstance(t, dict):
            w = {"title": str(t.get("title") or ""), "artist": str(t.get("artist") or ""),
                 "audio_id": str(t.get("audio_id") or "")}
        else:
            title, _, artist = str(t).replace(" – ", " — ").partition(" — ")
            w = {"title": title.strip(), "artist": artist.strip(), "audio_id": ""}
        if w["title"] or w["audio_id"]:
            out.append(w)
    return out


def fits(a: dict, w: dict) -> bool:
    """Skladba z katalogu IG = schválená písnička (ID, nebo název + interpret)."""
    if w["audio_id"] and str(a.get("audio_id")) == w["audio_id"]:
        return True
    if not w["title"] or not _norm(a.get("title")).startswith(_norm(w["title"])):
        return False
    return not w["artist"] or _norm(w["artist"]) in _norm(a.get("display_artist"))


def song_pool(ig: "IG", rules: Rules) -> list[dict]:
    """Katalog se drží hodinu (poster se budí každých 20 min); změna seznamu v rules.json platí hned."""
    key = json.dumps(rules.music, sort_keys=True, ensure_ascii=False)
    hit = _SONG_CACHE.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    pool = _song_pool(ig, rules)
    _SONG_CACHE.clear()
    _SONG_CACHE[key] = (time.time(), pool)
    return pool


def _song_pool(ig: "IG", rules: Rules) -> list[dict]:
    """JEN niche písničky, které jde k reelu právě připojit.

    Simon 12. 11. večer (po „Work hard, dream big – Blues369“): „používej fakt
    jenom niche písničky“. Niche = whitegirl pop (Tate McRae, Katy Perry…),
    žádný phonk. Bere se jen: 1) písnička ze seznamu `tracks`, 2) trend od
    interpreta ze `niche_artists`. Žádné náhodné trendy, žádná volná hudba
    Mety (Sound Collection) — z toho byly „Ethiopia“ a „Blues369“.
    Pozor: API Instagramu nabízí jen to, co zrovna trenduje, plus volnou
    hudbu — písnička ze seznamu jde připojit jen ve chvíli, kdy ji IG nabízí.
    """
    wants = niche_list(rules)
    artists = [_norm(x) for x in rules.music.get("niche_artists") or [] if _norm(x)]
    seen, out = set(), []

    def add(a: dict, kind: str) -> None:
        aid = str(a.get("audio_id") or "")
        name = f"{a.get('title', '')} {a.get('display_artist', '')}".lower()
        if not aid or aid in seen or a.get("download_url") or any(b in name for b in BLOCK):
            return                                             # download_url = volná hudba Mety
        seen.add(aid)
        out.append({"audio_id": aid, "title": a.get("title", ""), "artist": a.get("display_artist", ""),
                    "kind": kind})
    try:
        trends = ig.get("ig_audio", ig_user_id=ig.user, audio_type="music").get("audio") or []
    except Exception as exc:
        trends = []
        log(f"trendy hudby nejdou načíst: {str(exc)[:120]}")
    for w in wants:
        hits = [a for a in trends if fits(a, w)]
        if not hits and w["title"]:
            try:
                q = f"{w['title']} {w['artist']}".strip()
                hits = [a for a in ig.get("ig_audio", ig_user_id=ig.user, audio_type="music",
                                          search_query=q).get("audio") or [] if fits(a, w)]
            except Exception:
                hits = []
        for a in hits[:1]:
            add(a, "niche")
    for a in trends:
        if artists and any(x in _norm(a.get("display_artist")) for x in artists):
            add(a, "niche-interpret")
    log("niche hudba k dispozici: " + (", ".join(f"{a['title']} – {a['artist']}" for a in out) or "žádná"))
    return out


def pick_song(ig: "IG", rules: Rules, posted: list[dict], skip: int = 0) -> dict | None:
    """Niche písnička pro momentku (`skip` = kolik dalších přeskočit — pro plán).

    Napřed ty, co ještě nehrály. Když dojdou: s `repeat` ta, co hrála
    nejdávněji, ale ne z posledních `avoid_last` postů. Nic → None a
    momentka počká (cizí hudbu radši ne).
    """
    if not rules.music.get("enabled"):
        return None
    pool = song_pool(ig, rules)
    hist = sorted(posted, key=lambda p: p.get("at", ""))
    last: dict[str, int] = {}
    for i, p in enumerate(hist):
        for k in (str(p.get("audio_id") or ""), _norm(p.get("music"))):
            if k:
                last[k] = i
    def used(a: dict) -> int:
        return max(last.get(a["audio_id"], -1), last.get(_norm(a["title"]), -1))
    fresh = [a for a in pool if used(a) < 0]
    order = list(fresh)
    if rules.music.get("repeat", True):
        cut = len(hist) - int(rules.music.get("avoid_last", 1))
        order += sorted((a for a in pool if 0 <= used(a) < cut), key=used)
    return order[skip] if skip < len(order) else None


# ── Instagram ────────────────────────────────────────────────
class IG:
    def __init__(self):
        self.tok = os.environ["IG_USER_TOKEN"]
        self.user = os.environ["IG_USER_ID"]
        self.h = {"Authorization": f"OAuth {self.tok}", **UA}

    def get(self, path: str, **params):
        r = requests.get(f"{GRAPH}/{path}", headers=self.h, params=params, timeout=60)
        d = r.json()
        if r.status_code >= 400 or "error" in d:
            raise RuntimeError(f"Graph {path}: {str(d.get('error', d))[:300]}")
        return d

    def post(self, path: str, **data):
        r = requests.post(f"{GRAPH}/{path}", headers=self.h, data=data, timeout=120)
        d = r.json()
        if r.status_code >= 400 or "error" in d:
            raise RuntimeError(f"Graph {path}: {str(d.get('error', d))[:300]}")
        return d

    def last_post(self) -> dt.datetime | None:
        d = self.get(f"{self.user}/media", fields="timestamp", limit=1)
        rows = d.get("data") or []
        return parse(rows[0]["timestamp"].replace("+0000", "+00:00")) if rows else None

    def find_song(self, title: str) -> dict | None:
        d = self.get("ig_audio", ig_user_id=self.user, audio_type="music", search_query=title)
        return next((a for a in d.get("audio") or [] if a.get("title", "").lower().startswith(title.lower())), None)

    def wait_ready(self, cid: str) -> None:
        for _ in range(100):
            st = self.get(cid, fields="status_code,status")
            if st.get("status_code") in ("FINISHED", "PUBLISHED"):
                return
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram video odmítl: {st.get('status')}")
            time.sleep(6)
        raise RuntimeError("Instagram video nezpracoval do 10 minut")

    def publish(self, video: Path, caption: str, audio: dict | None, video_url: str | None) -> dict:
        if video_url:
            data = {"media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "true"}
            if audio:
                data["audio_configuration"] = json.dumps(audio)
            cid = self.post(f"{self.user}/media", **data)["id"]
        else:                                   # záloha: soubor rovnou, bez hudby
            c = self.post(f"{self.user}/media", media_type="REELS", upload_type="resumable", caption=caption)
            r = requests.post(c["uri"], data=video.read_bytes(), timeout=600,
                              headers={**self.h, "offset": "0", "file_size": str(video.stat().st_size)})
            if not r.ok or not r.json().get("success"):
                raise RuntimeError(f"nahrání videa: {r.status_code} {r.text[:200]}")
            cid = c["id"]
        self.wait_ready(cid)
        mid = self.post(f"{self.user}/media_publish", creation_id=cid)["id"]
        link = self.get(mid, fields="permalink").get("permalink") or f"https://www.instagram.com/reel/{mid}/"
        return {"media_id": mid, "url": link}


def host_on_klapka(video: Path) -> str:
    base = os.environ.get("KLAPKA_URL", "https://klapka.onrender.com").rstrip("/")
    last = ""
    for _ in range(3):                         # Render na free plánu spí — první dotaz ho budí
        try:
            r = requests.post(f"{base}/media", data=video.read_bytes(), timeout=240,
                              headers={"X-ClipForge-Key": os.environ["KLAPKA_MEDIA_KEY"],
                                       "Content-Type": "video/mp4", **UA})
            if r.ok:
                return r.json()["url"]
            last = f"{r.status_code} {r.text[:120]}"
        except requests.RequestException as exc:
            last = str(exc)[:120]
        time.sleep(10)
    raise RuntimeError(f"KLAPKA video nepřijala: {last}")


def download(clip: dict) -> Path:
    repo = os.environ.get("GITHUB_REPOSITORY", "sajmyajtak/togi-reels")
    url = f"https://github.com/{repo}/releases/download/klipy/{clip['asset']}"
    dst = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / clip["asset"]
    with requests.get(url, stream=True, timeout=300, headers=UA) as r:
        r.raise_for_status()
        with dst.open("wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    return dst


def caption_for(clip: dict, rules: Rules) -> str:
    text = clip.get("caption") or ""
    if rules.attribution and rules.attribution.lower() not in text.lower():
        text = f"{text}\n\n{rules.attribution}".strip()
    tags = [t.lstrip("#") for t in clip.get("hashtags") or [] if t.lstrip("#").lower() not in rules.banned][:8]
    return (f"{text}\n\n" + " ".join("#" + t for t in tags)).strip()[:2200]


# ── KLAPKA (Airtable) ────────────────────────────────────────
def airtable(method: str, table: str, records: list[dict]) -> list[dict]:
    tok, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("KLAPKA_BASE_ID")
    if not tok or not base or not records:
        return []
    r = requests.request(method, f"{AIRTABLE}/{base}/{table}", timeout=45,
                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                         json={"records": records, "typecast": True, "returnFieldsByFieldId": True})
    if not r.ok:
        log(f"Airtable {r.status_code}: {r.text[:200]}")
        return []
    return r.json().get("records", [])


def plan(rules: Rules, queue: list[dict], posted: list[dict], now: dt.datetime, days: int = 3,
         ig: "IG | None" = None) -> dict:
    """Stejný tvar, jaký čte KLAPKA (Autoclipping → Plán)."""
    local = now.astimezone(rules.tz)
    left = pool(queue, posted, rules)
    total = len(left)
    music_hist = [p.get("music", "") for p in posted]
    out = []
    for k in range(days):
        day = local.date() + dt.timedelta(days=k)
        done = [p for p in posted if parse(p["at"]).astimezone(rules.tz).date() == day]
        rows = []
        for i, at in enumerate(rules.slots_on(day)):
            if i < len(done):
                p = done[i]
                rows.append({"at": iso(at), "status": "published", "clipId": p["id"], "title": p.get("title", ""),
                             "url": p.get("url", ""), "postedAt": p["at"], "music": p.get("music", ""),
                             "kategorie": p.get("kategorie", "momentka"),
                             "views": int((p.get("metrics") or {}).get("views") or 0)})
                continue
            c = left.pop(0) if left else None
            song = ""
            if c and c.get("zvuk") == "puvodni":
                song = f"{c.get('hudba', '')} (ve videu)"
            elif c and ig is not None:
                nxt = pick_song(ig, rules, posted, skip=music_hist.count("__planned__"))
                song = f"{nxt['title']} – {nxt['artist']}" if nxt else "čeká na niche písničku"
                music_hist.append("__planned__")
            rows.append({"at": iso(at), "status": "planned" if c else "empty", "clipId": c["id"] if c else "",
                         "kategorie": (c or {}).get("kategorie", "momentka") if c else "",
                         "title": c.get("header", "") if c else "", "score": int(c.get("score") or 0) if c else 0,
                         "music": song})
        out.append({"date": day.isoformat(), "perDay": len(rows), "slots": rows})
    return {"target": "togi", "platform": "instagram", "account": rules.account, "on": rules.on,
            "tz": str(rules.tz.key), "times": rules.slots, "minScore": rules.min_score,
            "pool": total, "leftAfter": len(left), "via": "github", "days": out,
            "reels": reels_summary(posted)}


REEL_METRICS = ("views", "reach", "likes", "comments", "shares", "saved")


def reels_summary(posted: list[dict], limit: int = 60) -> list[dict]:
    """Všechny zveřejněné reely i s čísly — KLAPKA z nich staví „Videa“ v Přehledu účtu
    (Simon 12. 11.: „úspěšnost konkrétních videí a možnost je rozkliknout“). Jde to
    s plánem v jednom řádku Nastavení, takže to nestojí žádné volání Airtable navíc."""
    out = []
    for p in sorted(posted, key=lambda e: e.get("at", ""), reverse=True)[:limit]:
        m = p.get("metrics") or {}
        out.append({"clipId": p["id"], "title": p.get("title", ""), "at": p["at"], "url": p.get("url", ""),
                    "kategorie": p.get("kategorie", "momentka"), "score": int(p.get("score") or 0),
                    "music": p.get("music", ""), "artist": p.get("artist", ""),
                    "baked": bool(p.get("baked_audio")), "caption": (p.get("caption") or "")[:600],
                    "m": {k: int(m.get(k) or 0) for k in REEL_METRICS}, "mAt": m.get("at", "")})
    return out


def push_plan(rules: Rules, queue: list[dict], posted: list[dict], now: dt.datetime, state: dict,
              ig: "IG | None" = None) -> None:
    p = plan(rules, queue, posted, now, ig=ig)
    fp = hashlib.sha1(json.dumps(p, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if state.get("plan_fp") == fp:
        return
    rec = state.get("plan_rec") or rules.plan_rec
    fields = {F_SET_KEY: "clipforge.plan.togi.instagram",
              F_SET_VAL: json.dumps({**p, "at": iso(now)}, ensure_ascii=False), F_SET_AT: iso(now)}
    done = airtable("PATCH", T_SETTINGS, [{"id": rec, "fields": fields}]) if rec else []
    if not done:
        done = airtable("POST", T_SETTINGS, [{"fields": fields}])
    if done:
        state.update(plan_fp=fp, plan_rec=done[0]["id"])
        log(f"plán v KLAPCE aktualizovaný (zásoba {p['pool']}, po plánu zbude {p['leftAfter']})")


# ── statistiky ───────────────────────────────────────────────
F_CFID = "fldM38mOpEAVbay8h"     # Klipy → ClipForge ID


def link_missing_recs(posted: list[dict], now: dt.datetime) -> set[str]:
    """Reel, který šel ven dřív, než Mac poslal jeho řádek do KLAPKY, nemá `klipy_rec`
    — a čísla by se k němu nikdy nezapsala („god exposed“ 11. 11.). Dohledá řádek
    podle ClipForge ID. Jen u reelů z posledních 3 dnů, ať smazaný řádek nežere
    volání Airtable navždy. Vrací ID reelů, které se právě napojily."""
    missing = [e for e in posted if not e.get("klipy_rec")
               and (now - parse(e["at"])).total_seconds() < 3 * 86400]
    tok, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("KLAPKA_BASE_ID")
    if not missing or not tok or not base:
        return set()
    by_cf, offset = {}, None
    try:
        for _ in range(5):
            params = {"fields[]": F_CFID, "returnFieldsByFieldId": "true", "pageSize": 100}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{AIRTABLE}/{base}/{T_CLIPS}", params=params, timeout=45,
                             headers={"Authorization": f"Bearer {tok}"})
            if not r.ok:
                log(f"Airtable {r.status_code}: {r.text[:200]}")
                return set()
            js = r.json()
            for rec in js.get("records", []):
                cf = (rec.get("fields") or {}).get(F_CFID)
                if cf:
                    by_cf[cf] = rec["id"]
            offset = js.get("offset")
            if not offset:
                break
    except requests.RequestException as exc:
        log(f"řádky klipů nejdou načíst: {str(exc)[:100]}")
        return set()
    out = set()
    for e in missing:
        if by_cf.get(e["id"]):
            e["klipy_rec"] = by_cf[e["id"]]
            out.add(e["id"])
            log(f"reel {e['id']} napojen na řádek v KLAPCE ({e['klipy_rec']})")
    return out


def refresh_metrics(ig: "IG", rules: Rules, posted: list[dict], now: dt.datetime, state: dict) -> bool:
    """Čísla reelů z Instagramu → posted.json a KLAPKA (Views, Lajky, …).

    Měří se tady, ne na Macu — Mac nemusí být zapnutý a reely postuje
    GitHub. Jednou za METRICS_EVERY_H hodin; do KLAPKY jen to, co se
    změnilo (po 10 řádcích na jedno volání Airtable).
    """
    last = state.get("metrics_at")
    if last and (now - parse(last)).total_seconds() < METRICS_EVERY_H * 3600:
        return False
    changed = []
    linked = link_missing_recs(posted, now)
    for e in posted:
        if not e.get("media_id"):
            continue
        try:
            d = ig.get(f"{e['media_id']}/insights", metric="views,reach,likes,comments,shares,saved")
        except Exception as exc:
            log(f"statistiky {e['id']} nejdou: {str(exc)[:100]}")
            continue
        m = {x["name"]: (x.get("values") or [{}])[0].get("value", 0) for x in d.get("data", [])}
        old = {k: v for k, v in (e.get("metrics") or {}).items() if k != "at"}
        if m != old or e["id"] in linked:
            changed.append(e)
        e["metrics"] = {**m, "at": iso(now)}
    recs = []
    for e in changed:
        if not e.get("klipy_rec"):
            continue
        fields = {F_METRIC[k]: int(e["metrics"].get(k) or 0) for k in F_METRIC}
        if e["id"] in linked:      # řádek dosud o postu nevěděl — doplnit i to, co by zapsal post_one
            fields.update({F["stage"]: "published", F["url"]: e.get("url", ""), F["published"]: e["at"],
                           F["platform"]: "instagram", F_CATEGORY: e.get("kategorie", "momentka"),
                           **({F["account"]: rules.account_rec} if rules.account_rec else {})})
        recs.append({"id": e["klipy_rec"], "fields": fields})
    for i in range(0, len(recs), 10):
        airtable("PATCH", T_CLIPS, recs[i:i + 10])
    try:
        fol = int(ig.get(ig.user, fields="followers_count").get("followers_count") or 0)
        if rules.account_rec and fol != state.get("followers"):
            airtable("PATCH", T_ACCOUNTS, [{"id": rules.account_rec, "fields": {F_FOLLOWERS: fol}}])
            state["followers"] = fol
    except Exception:
        pass
    state["metrics_at"] = iso(now)
    total = sum(int((e.get("metrics") or {}).get("views") or 0) for e in posted)
    log(f"statistiky: {len(posted)} reelů, celkem {total} zhlédnutí, změna u {len(changed)}")
    return bool(changed or linked)


# ── hlavní smyčka ────────────────────────────────────────────
HOLD = "pockat"          # post_one: teď nepostovat (chybí hudba), zkusit za chvíli
HOLD_S = 10 * 60


def post_one(ig: IG, rules: Rules, queue: list[dict], posted: list[dict], dry: bool) -> dict | str | None:
    items = pool(queue, posted, rules)
    if not items:
        log("zásoba je prázdná — nic k postování")
        return None
    clip = items[0]
    # Kombo/edit má písničku zapečenou ve videu („původní zvuk“) — druhou
    # z knihovny Instagramu by hrála přes ni (Simon 11. 11.).
    baked = clip.get("zvuk") == "puvodni"
    song = None if baked else pick_song(ig, rules, posted)
    log(f"na řadě: {clip['id']} „{clip.get('header', '')}“ ({clip.get('score')}/100), "
        f"hudba: {clip.get('hudba') + ' (ve videu)' if baked else (song or {}).get('title', '—')} – {(song or {}).get('artist', '')}")
    if dry:
        return None
    # POJISTKA (Simon 12. 11.: „UŽ AŤ SE TO NEOPAKUJE“): momentka nikdy bez hudby.
    # Radši počkat a zkusit znovu než zveřejnit reel jen s hlasem — zvuk už po
    # zveřejnění změnit nejde. Kombo/edit má hudbu ve videu, to jde vždycky.
    if not baked and not song:
        log("❌ NEPOSTUJU: momentka by byla bez hudby (žádná niche písnička k dispozici) — zkusím znovu")
        return HOLD
    video = download(clip)
    cap = caption_for(clip, rules)
    audio, note = None, ""
    if baked:
        res = ig.publish(video, cap, None, None)
    else:
        try:
            video_url = host_on_klapka(video)
        except Exception as exc:
            log(f"❌ NEPOSTUJU: video pro hudbu nejde vystavit přes KLAPKU ({str(exc)[:160]}) — zkusím znovu")
            return HOLD
        res = None
        for k in range(3):                   # když Instagram písničku nepřijme, zkusit další
            s = song if k == 0 else pick_song(ig, rules, posted, skip=k)
            if not s:
                break
            audio = {"audio_id": s["audio_id"], "audio_volume": int(rules.music.get("audio_volume", 22)),
                     "video_volume": int(rules.music.get("video_volume", 100))}
            try:
                res = ig.publish(video, cap, audio, video_url)
                song = s
                break
            except Exception as exc:
                note = f"hudbu {s.get('title', '')} Instagram nepřijal: {str(exc)[:160]}"
                log(note)
        if not res:
            log("❌ NEPOSTUJU: Instagram nepřijal žádnou hudbu — zkusím znovu")
            return HOLD
    entry = {"id": clip["id"], "at": iso(dt.datetime.now(dt.timezone.utc)), "url": res["url"],
             "media_id": res["media_id"], "title": clip.get("header", ""), "score": clip.get("score"),
             "music": (clip.get("hudba", "") if baked else (song["title"] if audio else "")),
             "artist": song.get("artist", "") if audio else "", "baked_audio": baked,
             "audio_id": song["audio_id"] if audio else "", "caption": cap, "note": note,
             "klipy_rec": clip.get("klipy_rec", ""), "kategorie": clip.get("kategorie", "momentka")}
    log(f"✅ venku: {res['url']}")
    if clip.get("klipy_rec"):
        airtable("PATCH", T_CLIPS, [{"id": clip["klipy_rec"], "fields": {
            F["stage"]: "published", F["url"]: res["url"], F["published"]: entry["at"],
            F["platform"]: "instagram", F_CATEGORY: clip.get("kategorie", "momentka"),
            **({F["account"]: rules.account_rec} if rules.account_rec else {})}}])
    return entry


NAP_S = 20 * 60


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=0, help="kolik minut čekat na okna plánu")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    deadline = time.time() + args.wait * 60
    state = load("state.json", {})
    ig = IG()
    while True:
        git_pull()
        rules, queue, posted = Rules(load("rules.json", {})), load("queue.json", []), load("posted.json", [])
        now = dt.datetime.now(dt.timezone.utc)
        if refresh_metrics(ig, rules, posted, now, state):
            save("posted.json", posted)
            save("state.json", state)
            git_commit("statistiky reelů")
        push_plan(rules, queue, posted, now, state, ig)
        why = due(rules, posted, ig.last_post(), now)
        if not why:
            entry = post_one(ig, rules, queue, posted, args.dry_run)
            if entry == HOLD:
                state["held"] = {"at": iso(now), "why": "momentka by byla bez hudby"}
                save("state.json", state)
                if time.time() + HOLD_S > deadline:
                    break                          # další běh to zkusí znovu
                time.sleep(HOLD_S)
                continue
            if entry:
                posted.append(entry)
                save("posted.json", posted)
                push_plan(rules, queue, posted, dt.datetime.now(dt.timezone.utc), state, ig)
                save("state.json", state)
                git_commit(f"reel: {entry['title'][:50]}")
                continue
            if args.dry_run:
                break
        else:
            log(f"teď ne: {why}")
        save("state.json", state)
        nxt = next_slot(rules, posted, dt.datetime.now(dt.timezone.utc)) if rules.on else None
        if not nxt:
            log("dnes už žádný další čas — konec")
            break
        wait_s = (nxt - dt.datetime.now(dt.timezone.utc)).total_seconds() + 20
        if time.time() + wait_s > deadline:
            # Nejdřív vyčerpat vlastní čas běhu, teprve pak navázat dalším.
            # Dřív se navazovalo hned → v noci (další čas až odpoledne) se
            # běhy řetězily naprázdno jeden za druhým (11. 11. ~00:50).
            rest = deadline - time.time() - 90
            if rest > 60:
                log(f"další čas {nxt.astimezone(rules.tz):%H:%M} je až po konci běhu — čekám {int(rest // 60)} min, pak navážu")
                time.sleep(min(NAP_S, rest))      # po kouscích: mezitím obnovit frontu a plán v KLAPCE
                continue
            log(f"další čas {nxt.astimezone(rules.tz):%H:%M} — navazuji dalším během")
            out = os.environ.get("GITHUB_OUTPUT")
            if out and args.wait > 0:
                with open(out, "a") as fh:
                    fh.write(f"next={iso(nxt)}\n")
            break
        log(f"čekám na {nxt.astimezone(rules.tz):%H:%M} ({int(wait_s // 60)} min)")
        # Nejdéle 20 min v kuse: Mac mezitím mění frontu (přestřižené klipy,
        # komba) a plán v KLAPCE by jinak ukazoval smazané klipy jako „klip“.
        time.sleep(min(NAP_S, max(30, wait_s)))
    save("state.json", state)
    git_commit("stav plánu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
