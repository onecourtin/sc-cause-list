#!/usr/bin/env python3
"""Save the day's hearing sequences from the SCI display board.

Most mornings each court posts a "sequence" on the display board, e.g.

    SEQUENCE 1 TO 32 79 84 PASS OVER IF ANY 33 TO 77 80 TO 83

telling the bar the order in which it will take up the matters (usually where
the supplementary list slots in, and when passed-over matters are called).
The board's JSON feed carries it as `court_message`. cdb.sci.gov.in doesn't
answer GitHub's servers, so the script reads it through our Cloudflare Worker
(worker/sc-board.js), falling back to the board itself (which works from an
ordinary Indian connection, e.g. a Mac). It writes

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

WORKER = "https://sc-board.onecourtin.workers.dev/"
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


UA = {"User-Agent": "Mozilla/5.0 (compatible; OneCourt cause-list reader; +https://onecourt.in)"}


def read_board() -> tuple[str | None, dict[str, str]]:
    """Return (board date, {court key: message}) from the Worker, or from the
    board directly if the Worker isn't reachable."""
    try:
        r = requests.get(WORKER, timeout=40, headers=UA)
        d = r.json()
        if "courts" in d:
            if d.get("stale"):
                print(f"  (Worker served its last good copy: {d.get('error')})")
            return d.get("date"), {k: v.get("msg") or "" for k, v in d["courts"].items()}
        print(f"  Worker error: {d.get('error')}")
    except (requests.RequestException, ValueError) as e:
        print(f"  Worker unreachable: {e}")
    r = requests.get(FEED, timeout=30, headers=UA)
    r.raise_for_status()
    feed = r.json()
    msgs = {}
    for c in feed.get("listedItemDetails", []):
        key = court_key(c.get("court_name", ""))
        if key:
            msgs[key] = c.get("court_message") or ""
    return feed.get("todayB"), msgs


def main():
    now = dt.datetime.now(IST)
    board_date, messages = read_board()

    # The board's own date, so a run just after midnight can't file
    # yesterday's messages under today.
    date = board_date or now.date().isoformat()
    path = DATA / f"seq-{date}.json"
    old = json.loads(path.read_text()) if path.exists() else {"date": date, "courts": {}}
    courts = old.get("courts", {})
    stamp = now.strftime("%H:%M")

    changed = False
    for key, raw in messages.items():
        msg = html.unescape(re.sub(r"<[^>]+>", " ", raw))
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
