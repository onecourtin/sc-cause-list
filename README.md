# Supreme Court Cause List — OneCourt

`onecourt.in/sc/causelist/` — the SCI daily cause list as a searchable web page
instead of 250-page PDFs.

## How it works

```
GitHub Actions (cron)  ──►  scrape.py  ──►  data/*.json  ──►  index.html (on onecourt.in)  
   every 30 min eve/night      fetch PDFs     committed to       reads the JSON from
                               + parse        this repo          raw.githubusercontent.com
```

No server. The SCI site doesn't send CORS headers, so the browser can't read
its PDFs directly — the scraper does it on a schedule instead.

### Source PDFs

`https://api.sci.gov.in/jonew/cl/<YYYY-MM-DD>/M_<T>_<N>.pdf`

| T | List |
|---|---|
| `J` | Judges' courts (CJI, Court 2–17) |
| `S` | Single-judge benches |
| `C` | Chamber matters |
| `R` | Registrar courts |

Files start `M_` (miscellaneous hearing) or `F_` (regular hearing, Tue–Thu).
`N` = 1 for the main list, 2+ for supplementary lists. A file that doesn't
exist comes back as an empty body, so the scraper checks for `%PDF`.

Runs 9:00 AM – 11:00 PM IST (every 30 min morning and evening, hourly midday), none overnight. Each run checks 2 days back to 7 days ahead. Unchanged PDFs are skipped by
ETag (`304`), so a quiet run takes seconds and commits nothing. Revised lists
are picked up automatically. Dates older than 60 days are pruned.

### Parsing

The PDFs have a fixed four-column layout — item no. (x≈43pt), case no. (69),
parties (185), advocates (426) — so `parse_pdf()` reads lines by x-position
with PyMuPDF. Bold lines in the parties column are notes/IAs; advocate lines
above "Versus" are the petitioner's. A court's header is reprinted mid-list
sometimes, so consecutive blocks with the same court + judges + time are merged.

### JSON shape (`data/<date>.json`)

```
{ date, fetched, lists: [{name, label, url, published, benches, items}],
  benches: [{ list: "M_J_1", court, judges[], time, tags[], notes[],
              sections: [{ t: "[BAIL MATTERS]", items: [
                { n: "12", c: "SLP(C) No. 28926/2026", s: "IX",
                  p: petitioner, r: respondent, pa: pet. advocates,
                  ra: resp. advocates, x: [notes/IAs], k: 1 if connected } ] }] }] }
```

## Hearing sequences

Most mornings each court posts a sequence on the display board, e.g.
`SEQUENCE 1 TO 32 79 84 PASS OVER IF ANY 33 TO 77 80 TO 83`.
`fetch_sequence.py` saves the raw text to `data/seq-<date>.json` (source: the
`court_message` field of the `cdb.sci.gov.in` board feed).

The board doesn't answer GitHub's or Hostinger's servers, but it does answer
Cloudflare, so it's read through a Cloudflare Worker (`worker/sc-board.js`,
deployed as `sc-board.onecourtin.workers.dev`), which adds CORS and a
20-second cache. The `Save hearing sequences` workflow runs
`fetch_sequence.py` every 10 minutes from 9:30 AM to 1:20 PM IST via the
Worker; the page also asks the Worker directly for today's messages, so a new
or changed sequence shows within ~3 minutes. If the Worker is down,
`fetch_sequence.py` falls back to the board itself, which works from an
ordinary connection — `~/Desktop/Update SC sequences.command` does that from
the Mac as a manual backup.

The page parses it (the `oc-sequence` script in `index.html`) and shows the
court's matters in hearing order, with pass-over points, fixed-time items and
anything the sequence didn't mention at the end. It covers the court's regular
sitting (main + supplementary list) plus any other bench whose items it names.
`node test_sequence.js` checks the parser against real board messages.

## Running locally

```bash
pip install -r requirements.txt
python3 scrape.py                      # refresh data/
python3 scrape.py --date 2026-10-05 --force
python3 scrape.py --parse some.pdf     # parse one PDF, print JSON
python3 -m http.server 8771            # then open http://localhost:8771/
```

On localhost the page reads `data/`; anywhere else it reads this repo's
`data/` from `raw.githubusercontent.com` (see `DATA` in `index.html`).

## Deploying

- **Data:** push this repo to `github.com/onecourtin/sc-cause-list` (public —
  Actions minutes are free and raw files are readable). The workflow runs on
  its own; trigger it once by hand from the Actions tab.
- **Page:** upload `index.html` to `public_html/sc/causelist/index.html` on
  the host. Only that one file is needed there; the scraper, workflow and
  data stay on GitHub.
