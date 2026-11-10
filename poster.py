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
    items = [c for c in queue if c["id"] not in done and int(c.get("score") or 0) >= rules.min_score
             and c.get("ok", True)]
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


BLOCK = ("christmas", "xmas", "santa", "noel", "jingle", "snow", "winter", "lullaby", "holiday",
         "navidad", "weihnacht", "vánoc", "december", "silent night")
TREND_ARTISTS = ("jay-z", "beyoncé", "travis scott", "drake", "kanye", "ye", "future", "metro boomin",
                 "kendrick", "eminem", "50 cent", "lil wayne", "21 savage", "playboi carti", "don toliver",
                 "tame impala", "black eyed peas", "tate mcrae", "doja cat", "bad bunny", "karol g", "post malone",
                 "the weeknd", "central cee", "lil baby", "gunna", "young thug", "a$ap", "tyga", "pitbull")
_POOL: list[dict] = []


def song_pool(ig: "IG", rules: Rules) -> list[dict]:
    """Písničky, které jde k reelu připojit — v pořadí, v jakém se berou.

    Hity bere Instagram přes API jen z aktuálních trendů (z 58 známých
    rap/gym hitů nebyl v katalogu ani jeden, trendy ano). Proto: nejdřív
    dnešní trendy bez vánočních apod., pak volné skladby Mety ve stylu
    TOGI niche (phonk, gym, hype). Každý post dostane jinou (Simon 11. 11.).
    """
    if _POOL:
        return _POOL
    seen, out = set(), []
    def add(a: dict, kind: str) -> None:
        name = f"{a.get('title', '')} {a.get('display_artist', '')}".lower()
        if a.get("audio_id") in seen or any(b in name for b in BLOCK):
            return
        seen.add(a["audio_id"])
        out.append({"audio_id": str(a["audio_id"]), "title": a.get("title", ""),
                    "artist": a.get("display_artist", ""), "kind": kind})
    for t in rules.music.get("tracks") or []:                 # oblíbené z configu, když jsou v katalogu
        try:
            hit = ig.find_song(t)
            if hit:
                add(hit, "hit")
        except Exception:
            pass
    for q in rules.music.get("searches") or ["phonk", "gym", "hype", "trap", "drift", "workout", "motivation"]:
        try:
            for a in ig.get("ig_audio", ig_user_id=ig.user, audio_type="music", search_query=q).get("audio") or []:
                add(a, "niche")
        except Exception:
            pass
    # Obecné trendy jen od interpretů z TOGI světa (rap, hype) — „What A
    # Wonderful World“ k posilovně nesedí. Seznam jde rozšířit v rules.json.
    artists = [x.lower() for x in rules.music.get("trend_artists") or TREND_ARTISTS]
    try:
        for a in ig.get("ig_audio", ig_user_id=ig.user, audio_type="music").get("audio") or []:
            if not a.get("download_url") and any(x in (a.get("display_artist") or "").lower() for x in artists):
                add(a, "trend")
    except Exception as exc:
        log(f"trendy hudby nejdou načíst: {str(exc)[:120]}")
    _POOL.extend(out)
    return _POOL


def pick_song(ig: "IG", rules: Rules, posted: list[dict], skip: int = 0) -> dict | None:
    """První písnička, která ještě nehrála (`skip` = kolik dalších přeskočit — pro plán)."""
    if not rules.music.get("enabled"):
        return None
    pool = song_pool(ig, rules)
    used = {p.get("audio_id") for p in posted if p.get("audio_id")} | \
           {p.get("music", "").lower() for p in posted if p.get("music")}
    fresh = [a for a in pool if a["audio_id"] not in used and a["title"].lower() not in used]
    if len(fresh) > skip:
        return fresh[skip]
    return pool[skip % len(pool)] if pool else None


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
                             "url": p.get("url", ""), "postedAt": p["at"], "music": p.get("music", "")})
                continue
            c = left.pop(0) if left else None
            song = ""
            if c and ig is not None:
                nxt = pick_song(ig, rules, posted, skip=music_hist.count("__planned__"))
                song = f"{nxt['title']} – {nxt['artist']}" if nxt else ""
                music_hist.append("__planned__")
            rows.append({"at": iso(at), "status": "planned" if c else "empty", "clipId": c["id"] if c else "",
                         "title": c.get("header", "") if c else "", "score": int(c.get("score") or 0) if c else 0,
                         "music": song})
        out.append({"date": day.isoformat(), "perDay": len(rows), "slots": rows})
    return {"target": "togi", "platform": "instagram", "account": rules.account, "on": rules.on,
            "tz": str(rules.tz.key), "times": rules.slots, "minScore": rules.min_score,
            "pool": total, "leftAfter": len(left), "via": "github", "days": out}


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


# ── hlavní smyčka ────────────────────────────────────────────
def post_one(ig: IG, rules: Rules, queue: list[dict], posted: list[dict], dry: bool) -> dict | None:
    items = pool(queue, posted, rules)
    if not items:
        log("zásoba je prázdná — nic k postování")
        return None
    clip = items[0]
    song = pick_song(ig, rules, posted)
    log(f"na řadě: {clip['id']} „{clip.get('header', '')}“ ({clip.get('score')}/100), "
        f"hudba: {(song or {}).get('title', '—')} – {(song or {}).get('artist', '')}")
    if dry:
        return None
    video = download(clip)
    audio, video_url, note = None, None, ""
    if song:
        try:
            audio = {"audio_id": song["audio_id"], "audio_volume": int(rules.music.get("audio_volume", 22)),
                     "video_volume": int(rules.music.get("video_volume", 100))}
            video_url = host_on_klapka(video)
        except Exception as exc:
            note = f"bez hudby: {str(exc)[:160]}"
            audio, video_url = None, None
            log(note)
    cap = caption_for(clip, rules)
    try:
        res = ig.publish(video, cap, audio, video_url)
    except Exception as exc:
        if not audio:
            raise
        note = f"hudbu Instagram nepřijal: {str(exc)[:160]}"
        log(note + " — posílám bez ní")
        audio = None
        res = ig.publish(video, cap, None, None)
    entry = {"id": clip["id"], "at": iso(dt.datetime.now(dt.timezone.utc)), "url": res["url"],
             "media_id": res["media_id"], "title": clip.get("header", ""), "score": clip.get("score"),
             "music": song["title"] if audio else "", "artist": song.get("artist", "") if audio else "",
             "audio_id": song["audio_id"] if audio else "", "caption": cap, "note": note}
    log(f"✅ venku: {res['url']}")
    if clip.get("klipy_rec"):
        airtable("PATCH", T_CLIPS, [{"id": clip["klipy_rec"], "fields": {
            F["stage"]: "published", F["url"]: res["url"], F["published"]: entry["at"],
            F["platform"]: "instagram", **({F["account"]: rules.account_rec} if rules.account_rec else {})}}])
    return entry


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
        push_plan(rules, queue, posted, now, state, ig)
        why = due(rules, posted, ig.last_post(), now)
        if not why:
            entry = post_one(ig, rules, queue, posted, args.dry_run)
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
            log(f"další čas {nxt.astimezone(rules.tz):%H:%M} je až po konci běhu — navážu dalším během")
            out = os.environ.get("GITHUB_OUTPUT")
            if out:
                with open(out, "a") as fh:
                    fh.write(f"next={iso(nxt)}\n")
            break
        log(f"čekám na {nxt.astimezone(rules.tz):%H:%M} ({int(wait_s // 60)} min)")
        time.sleep(max(30, wait_s))
    save("state.json", state)
    git_commit("stav plánu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
