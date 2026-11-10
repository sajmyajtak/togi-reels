# togi-reels

Automatické postování TOGI reelů na Instagram (@togis_lifestyle) z GitHub Actions.

- **Klipy vyrábí ClipForge na Macu** (stahování z YouTube, střih, posudek Claudem) a
  nahraje je sem: video do releasu `klipy`, popis do `queue.json`, pravidla do `rules.json`.
- **Postuje `poster.py`** v časech z `rules.json` (15:00 / 18:30 / 22:00 Praha), vždy
  nejlépe hodnocený klip, který ještě nevyšel, s hudbou z knihovny Instagramu.
- **Co vyšlo** je v `posted.json`; stav a plán se propisují do KLAPKY (Autoclipping → Plán).

Klíče (Instagram, Airtable, KLAPKA) jsou v GitHub Secrets, ne v kódu.
