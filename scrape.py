#!/usr/bin/env python3
"""Fetch Supreme Court of India daily cause lists and turn them into JSON.

SCI publishes each day's lists as PDFs at

    https://api.sci.gov.in/jonew/cl/<YYYY-MM-DD>/<M>_<T>_<N>.pdf

where T is the kind of list and N is 1 for the main list, 2+ for supplementary:

    J  Judges' courts (the main list — CJI's court, Court 2, 3, …)
    S  Single-judge benches
    C  Chamber matters
    R  Registrar courts

A missing file comes back as an empty body (sometimes with a 200), so a file
only counts if it actually starts with %PDF.

Output, all under data/:

    index.json          which dates exist, and per file its ETag/hash
    <YYYY-MM-DD>.json   the parsed list for that date

The PDFs have a fixed four-column layout (item no. | case no. | parties |
advocates), so the parser reads text by x-position with PyMuPDF rather than
trying to untangle pdftotext output, where the parties and advocates columns
interleave.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
import requests

BASE = "https://api.sci.gov.in/jonew/cl/{date}/{name}.pdf"
DATA = Path(__file__).resolve().parent / "data"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

LIST_KINDS = [
    ("J", "Courts"),
    ("S", "Single Judge"),
    ("C", "Chamber"),
    ("R", "Registrar"),
]
MAX_PARTS = 4  # _1 main, _2.._4 supplementary

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OneCourt cause-list reader; +https://onecourt.in)",
}

# ── PDF parsing ──────────────────────────────────────────────────────────────

# Column left edges in the PDF (points). Lines start within a couple of points
# of these; anything else is a centred heading or a free-text note.
COL_SNO, COL_CASE, COL_PARTY, COL_ADV = 43, 69, 185, 426
PAGE_MID = 297.6

BOILERPLATE = (
    "SUPREME COURT OF INDIA",
    "[ IT WILL BE APPRECIATED",
    "ON RECORD DO NOT SEEK",
    "LISTED BEFORE ALL THE COURTS",
)
NOTE_START = re.compile(r"^(IA No|I\.A|Crl\.?M\.?P|\{|\[|FOR |WITH |IN |TO BE |\(|\d+\.\s)", re.I)
JUDGE_RE = re.compile(r"^HON['’]BLE (THE|MR|MRS|MS|DR)\b")
# Centred header lines that describe the list itself rather than a notice.
TAG_RE = re.compile(r"^(SUPPLEMENTARY|ADVANCE|.*HEARING$|.*MATTERS$|.*\bLIST$|.*\bBENCH\b|THIS BENCH)")
SNO_RE = re.compile(r"^\d+(\.\d*)?$")


def _col(x: float) -> str | None:
    for name, edge in (("sno", COL_SNO), ("case", COL_CASE), ("party", COL_PARTY), ("adv", COL_ADV)):
        if abs(x - edge) <= 3.5:
            return name
    return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _lines(page):
    """Yield one dict per text line on a page: position, text, bold, size."""
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            text = "".join(s["text"] for s in ln["spans"])
            if not text.strip():
                continue
            span = ln["spans"][0]
            yield {
                "x0": ln["bbox"][0],
                "y0": ln["bbox"][1],
                "x1": ln["bbox"][2],
                "text": text,
                "bold": bool(span["flags"] & 16),
                "size": span["size"],
            }


def _words_in(page, line):
    """Words of one line with their x positions — used to split a line that
    holds both the item number and the start of the case number."""
    out = []
    for w in page.get_text("words", clip=fitz.Rect(line["x0"] - 1, line["y0"] - 0.5, line["x1"] + 1, line["y0"] + 8)):
        out.append((w[0], w[4]))
    return sorted(out)


class Item:
    def __init__(self, sno: str):
        self.sno = sno
        self.case: list[str] = []
        self.pet: list[str] = []
        self.res: list[str] = []
        self.padv: list[str] = []
        self.radv: list[str] = []
        self.notes: list[list] = []  # [text, last_x1]
        self.versus_y: float | None = None
        self.versus_page = -1
        self.sno_at = (-1, 0.0)  # (page, y) of the last item-number line

    def add_note(self, text: str, x1: float):
        if self.notes and self.notes[-1][1] > 340 and not NOTE_START.match(text):
            # The previous note line ran the full column width, so this one
            # is its wrapped continuation.
            self.notes[-1][0] += " " + text
            self.notes[-1][1] = x1
        else:
            self.notes.append([text, x1])

    def out(self) -> dict:
        case = list(self.case)
        conn = bool(case and case[0].lower() == "connected")
        if conn:
            case = case[1:]
        # The last short line in the case column is the dealing section
        # (e.g. "III-B", "PIL-W"); everything above it is the case number.
        sec = ""
        if len(case) >= 2 and re.fullmatch(r"[A-Z0-9\-/ ]{1,12}", case[-1]) and not re.search(r"\d{3,}", case[-1]):
            sec = case.pop()
        d = {
            "n": self.sno.rstrip("."),
            "c": _clean(" ".join(case)).replace("/ ", "/"),
            "p": _clean(" ".join(self.pet)),
            "r": _clean(" ".join(self.res)),
        }
        if sec:
            d["s"] = sec
        if conn:
            d["k"] = 1
        if self.padv:
            d["pa"] = _clean(" ".join(self.padv))
        if self.radv:
            d["ra"] = _clean(" ".join(self.radv))
        if self.notes:
            d["x"] = [_clean(t) for t, _ in self.notes]
        return d


def parse_pdf(path_or_bytes, list_name: str) -> tuple[list[dict], str | None]:
    """Return (benches, published-timestamp) for one cause-list PDF."""
    doc = fitz.open(stream=path_or_bytes, filetype="pdf") if isinstance(path_or_bytes, bytes) else fitz.open(path_or_bytes)
    benches: list[dict] = []
    published = None

    bench = None
    section = None
    item: Item | None = None
    mode = "content"  # header | content | footer
    expect_court = False
    new_bench_pending = False

    def close_item():
        nonlocal item
        if item is not None and section is not None:
            section["items"].append(item.out())
        item = None

    def add_note(text):
        # A bench note is a run of lines; a new one starts at a "NOTE",
        # bullet, bracket or judge's name, the rest are wrapped continuations.
        text = _clean(text)
        notes = bench["notes"]
        if notes and not re.match(r"^(NOTE|•|\[|\"|HON['’]BLE)", text, re.I) and not re.search(r"(:-?|\])$", notes[-1]):
            notes[-1] += " " + text
        else:
            notes.append(text)

    def ensure_section(title=""):
        nonlocal section
        if section is None:
            section = {"t": title, "items": []}
            bench["sections"].append(section)
        return section

    for pno, page in enumerate(doc):
        lines = list(_lines(page))
        lines.sort(key=lambda l: (round(l["y0"], 1), l["x0"]))
        for ln in lines:
            text = ln["text"].strip()
            col = _col(ln["x0"])

            if text.startswith("SUPREME COURT OF INDIA"):
                new_bench_pending = True
                continue
            if any(text.startswith(b) for b in BOILERPLATE):
                continue
            if text.startswith("DAILY CAUSE LIST FOR DATED"):
                expect_court = True
                continue
            if expect_court:
                expect_court = False
                if new_bench_pending or bench is None:
                    close_item()
                    new_bench_pending = False
                    bench = {
                        "list": list_name,
                        "court": _clean(text),
                        "judges": [],
                        "time": "",
                        "tags": [],
                        "notes": [],
                        "sections": [],
                    }
                    benches.append(bench)
                    section = None
                    mode = "header"
                # On continuation pages this line just repeats the court name.
                continue

            if bench is None:
                continue

            if mode == "footer":
                if re.match(r"\d{2}-\d{2}-\d{4} \d{2}:\d{2}", text):
                    published = published or text[:19]
                continue
            if text == "NEW DELHI" and ln["x0"] < 60:
                close_item()
                mode = "footer"
                continue

            if mode == "header":
                if text.startswith("SNo."):
                    mode = "content"
                    continue
                if text in ("Petitioner / Respondent", "Petitioner/Respondent", "Advocate"):
                    continue
                centred = abs((ln["x0"] + ln["x1"]) / 2 - PAGE_MID) < 20
                up = text.upper()
                if JUDGE_RE.match(up) or ("REGISTRAR" in up and "," in up):
                    bench["judges"].append(_clean(text))
                elif up.startswith("(TIME"):
                    bench["time"] = _clean(text.strip("() ").split(":", 1)[-1])
                elif centred and ln["bold"] and TAG_RE.match(up):
                    bench["tags"].append(_clean(text))
                else:
                    add_note(text)
                continue

            # ── content mode ──
            if text.startswith("SNo.") or text in ("Petitioner / Respondent", "Petitioner/Respondent", "Advocate"):
                continue

            if col is None:
                # Centred bold line: a section heading (e.g. "[BAIL MATTERS]")
                # or a hearing-type heading ("PART HEARD MATTERS").
                centred = abs((ln["x0"] + ln["x1"]) / 2 - PAGE_MID) < 25
                if centred and ln["bold"] and len(text) < 90:
                    close_item()
                    section = {"t": _clean(text), "items": []}
                    bench["sections"].append(section)
                else:
                    close_item()
                    add_note(text)
                continue

            if col == "sno":
                words = _words_in(page, ln)
                sno_words = [w for x, w in words if x < COL_CASE - 4]
                rest = [w for x, w in words if x >= COL_CASE - 4]
                token = "".join(sno_words).strip()
                wrapped = (
                    item is not None
                    and item.sno_at[0] == pno
                    and ln["y0"] - item.sno_at[1] < 13
                    and (re.fullmatch(r"\.\d*", token) or ("." in item.sno and token.isdigit()))
                )
                if wrapped:
                    # A long connected-matter number wraps in its narrow
                    # column: "1701" / ".1", or "50.1" / "0" for 50.10.
                    item.sno += token
                    item.sno_at = (pno, ln["y0"])
                elif SNO_RE.match(token):
                    close_item()
                    ensure_section()
                    item = Item(token)
                    item.sno_at = (pno, ln["y0"])
                elif token == "." or not sno_words:
                    continue
                else:
                    # Free text starting at the left margin: a "NOTE:-"
                    # block at the end of a court's list.
                    close_item()
                    add_note(text)
                    continue
                if rest and item is not None:
                    item.case.append(" ".join(rest))
                continue

            if item is None:
                # Text in the party column before the first item of a section
                # is a section-level note.
                if bench is not None and text:
                    add_note(text)
                continue

            if col == "case":
                if text.startswith("NOTE") or ln["x1"] > COL_PARTY - 3:
                    # Case numbers never run into the parties column, so a
                    # line this wide is a court note printed near that edge.
                    close_item()
                    add_note(text)
                else:
                    item.case.append(text)
            elif col == "party":
                if text == "Versus":
                    item.versus_y = ln["y0"]
                    item.versus_page = pno
                elif ln["bold"]:
                    item.add_note(_clean(text), ln["x1"])
                elif item.versus_y is None:
                    item.pet.append(text)
                else:
                    item.res.append(text)
            elif col == "adv":
                # Advocate lines sitting level with the respondent block belong
                # to the respondent. The respondent's first line is ~12pt below
                # "Versus", the petitioner's lines are always above it.
                # If the item ran over a page break after "Versus", everything
                # on the later page is on the respondent's side.
                if item.versus_y is None or (pno == item.versus_page and ln["y0"] < item.versus_y - 1):
                    item.padv.append(text)
                else:
                    item.radv.append(text)

    close_item()
    benches = _merge(benches)
    # Drop sections that ended up empty (e.g. a heading followed by a footer).
    for b in benches:
        b["sections"] = [s for s in b["sections"] if s["items"]]
        if not b["tags"]:
            del b["tags"]
        b["notes"] = [n for n in b["notes"] if not re.fullmatch(r"(NOTE\s*:?-?|\.)", n)]
        if not b["notes"]:
            del b["notes"]
    return benches, published


def _merge(benches: list[dict]) -> list[dict]:
    """The PDF reprints a court's full header partway through its list (the
    CJI's court comes out in four blocks). Fold consecutive blocks with the
    same court, judges and time back into one bench."""
    out: list[dict] = []
    for b in benches:
        p = out[-1] if out else None
        if p and (p["court"], p["judges"], p["time"]) == (b["court"], b["judges"], b["time"]):
            for s in b["sections"]:
                if p["sections"] and p["sections"][-1]["t"] == s["t"]:
                    p["sections"][-1]["items"].extend(s["items"])
                else:
                    p["sections"].append(s)
            p["tags"] += [t for t in b["tags"] if t not in p["tags"]]
            p["notes"] += [n for n in b["notes"] if n not in p["notes"]]
        else:
            out.append(b)
    return out


# ── Fetching ─────────────────────────────────────────────────────────────────

def fetch(session: requests.Session, url: str, prev: dict | None):
    """Return (status, body, meta). status is 'new', 'same' or 'missing'."""
    headers = {}
    if prev and prev.get("etag"):
        headers["If-None-Match"] = prev["etag"]
    for attempt in range(3):
        try:
            r = session.get(url, headers=headers, timeout=60)
            break
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  ! {url}: {e}", file=sys.stderr)
                return "error", None, None
            time.sleep(3 * (attempt + 1))
    if r.status_code == 304:
        return "same", None, prev
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return "missing", None, None
    meta = {
        "etag": r.headers.get("ETag", ""),
        "modified": r.headers.get("Last-Modified", ""),
        "sha": hashlib.sha256(r.content).hexdigest()[:16],
        "bytes": len(r.content),
    }
    if prev and prev.get("sha") == meta["sha"]:
        return "same", None, meta
    return "new", r.content, meta


def label_for(name: str) -> str:
    _, kind, part = name.split("_")
    base = dict(LIST_KINDS).get(kind, kind)
    return base if part == "1" else f"{base} — Supplementary" + ("" if part == "2" else f" {int(part) - 1}")


def scrape_date(session, date: str, index: dict, force: bool) -> bool:
    """Fetch every list for one date. Returns True if anything changed."""
    entry = index["dates"].get(date, {"files": {}})
    files = entry["files"]
    changed = False
    fresh: dict[str, bytes] = {}

    for kind, _ in LIST_KINDS:
        for part in range(1, MAX_PARTS + 1):
            name = f"M_{kind}_{part}"
            url = BASE.format(date=date, name=name)
            prev = None if force else files.get(name)
            status, body, meta = fetch(session, url, prev)
            if status == "missing":
                break
            if status == "error":
                continue
            if status == "new":
                fresh[name] = body
                files[name] = meta
                changed = True
                print(f"  + {date} {name} ({meta['bytes'] // 1024} KB)")
            elif meta:
                files[name] = {**files.get(name, {}), **meta}

    if not files:
        return False
    if not changed and (DATA / f"{date}.json").exists():
        return False

    # Re-parse every file for the date (not just the new one) so the date's
    # JSON is always built from one consistent set of PDFs.
    lists, benches = [], []
    for name in sorted(files, key=lambda n: ([k for k, _ in LIST_KINDS].index(n.split("_")[1]), n)):
        body = fresh.get(name)
        if body is None:
            status, body, meta = fetch(session, BASE.format(date=date, name=name), None)
            if status != "new":
                continue
            files[name] = meta
        try:
            b, published = parse_pdf(body, name)
        except Exception as e:  # keep the other lists if one PDF is odd
            print(f"  ! parse failed for {date} {name}: {e}", file=sys.stderr)
            b, published = [], None
        files[name]["published"] = published
        lists.append({
            "name": name,
            "label": label_for(name),
            "url": BASE.format(date=date, name=name),
            "published": published,
            "benches": len(b),
            "items": sum(len(s["items"]) for x in b for s in x["sections"]),
        })
        benches.extend(b)

    out = {
        "date": date,
        "fetched": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "lists": lists,
        "benches": benches,
    }
    (DATA / f"{date}.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    entry["files"] = files
    entry["items"] = sum(l["items"] for l in lists)
    entry["fetched"] = out["fetched"]
    index["dates"][date] = entry
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days-back", type=int, default=2)
    ap.add_argument("--days-ahead", type=int, default=7)
    ap.add_argument("--keep-days", type=int, default=60, help="delete parsed dates older than this")
    ap.add_argument("--date", action="append", help="scrape only this date (YYYY-MM-DD); repeatable")
    ap.add_argument("--force", action="store_true", help="re-download and re-parse even if unchanged")
    ap.add_argument("--parse", metavar="PDF", help="just parse a local PDF and print JSON")
    args = ap.parse_args()

    if args.parse:
        benches, published = parse_pdf(args.parse, Path(args.parse).stem)
        print(json.dumps({"published": published, "benches": benches}, ensure_ascii=False, indent=1))
        return

    DATA.mkdir(exist_ok=True)
    idx_path = DATA / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else {"dates": {}}

    today = dt.datetime.now(IST).date()
    if args.date:
        dates = args.date
    else:
        dates = [(today + dt.timedelta(days=d)).isoformat() for d in range(-args.days_back, args.days_ahead + 1)]

    session = requests.Session()
    session.headers.update(HEADERS)
    changed = False
    for date in dates:
        print(f"{date}")
        changed |= scrape_date(session, date, index, args.force)

    cutoff = (today - dt.timedelta(days=args.keep_days)).isoformat()
    for date in list(index["dates"]):
        if date < cutoff:
            del index["dates"][date]
            (DATA / f"{date}.json").unlink(missing_ok=True)
            changed = True

    # Only touch index.json when something changed, so a quiet run leaves
    # nothing for the workflow to commit.
    if not changed and idx_path.exists():
        print("No changes.")
        return
    index["dates"] = dict(sorted(index["dates"].items()))
    index["updated"] = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
