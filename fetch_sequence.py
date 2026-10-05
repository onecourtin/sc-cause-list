#!/usr/bin/env python3
"""Save the day's hearing sequences from the SCI display board.

Most mornings each court posts a "sequence" on the display board, e.g.

    SEQUENCE 1 TO 32 79 84 PASS OVER IF ANY 33 TO 77 80 TO 83

telling the bar the order in which it will take up the matters (usually where
the supplementary list slots in, and when passed-over matters are called).
The board's JSON feed carries it as `court_message`. The feed has no CORS
headers, so this script fetches it on a schedule and writes

    data/seq-<YYYY-MM-DD>.json   {date, updated, courts: {"2": {msg, seen, changed}}}

The page parses the message itself (see parseSequence in index.html), so the
raw text is all that's stored here.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

import requests

FEED = ("https://cdb.sci.gov.in/index.php?courtListCsv=1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,21,22"
        "&request=display_full&requestType=ajax")
DATA = Path(__file__).resolve().parent / "data"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
KEEP_DAYS = 60


def court_key(name: str) -> str | None:
    """'C2' → '2', 'RC1' → 'R1'. Court names sometimes arrive wrapped in HTML."""
    name = re.sub(r"<[^>]+>", "", str(name)).strip().upper()
    m = re.fullmatch(r"(R?)C(\d+)", name)
    return (m.group(1) + m.group(2)) if m else None


def main():
    now = dt.datetime.now(IST)
    r = requests.get(FEED, timeout=60, headers={"User-Agent": "Mozilla/5.0 (compatible; OneCourt cause-list reader; +https://onecourt.in)"})
    r.raise_for_status()
    feed = r.json()

    # The board's own date, so a run just after midnight can't file
    # yesterday's messages under today.
    date = feed.get("todayB") or now.date().isoformat()
    path = DATA / f"seq-{date}.json"
    old = json.loads(path.read_text()) if path.exists() else {"date": date, "courts": {}}
    courts = old.get("courts", {})
    stamp = now.strftime("%H:%M")

    changed = False
    for c in feed.get("listedItemDetails", []):
        key = court_key(c.get("court_name", ""))
        msg = html.unescape(re.sub(r"<[^>]+>", " ", c.get("court_message") or ""))
        msg = re.sub(r"\s+", " ", msg).strip()
        if not key or not msg:
            # A message that disappears later in the day is kept: the
            # sequence still describes how the morning was taken up.
            continue
        prev = courts.get(key)
        if prev and prev["msg"] == msg:
            continue
        courts[key] = {"msg": msg, "seen": prev["seen"] if prev else stamp, "changed": stamp}
        changed = True
        print(f"  {key}: {msg}")

    # Prune old sequence files whatever happens today.
    cutoff = (now.date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for f in DATA.glob("seq-*.json"):
        if f.stem[4:] < cutoff:
            f.unlink()
            changed = True

    if not changed:
        print(f"{date}: no new sequences ({len(courts)} on file).")
        return
    DATA.mkdir(exist_ok=True)
    out = {"date": date, "updated": now.strftime("%Y-%m-%d %H:%M IST"), "courts": dict(sorted(courts.items(), key=lambda kv: (kv[0][0] == "R", int(re.sub(r"\D", "", kv[0]) or 0))))}
    if out["courts"]:
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"{date}: {len(courts)} sequences saved.")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"::warning::Display board unreachable: {e}")
        sys.exit(0)  # a missed check isn't worth a failure email
