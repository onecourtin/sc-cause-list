// Cloudflare Worker: the SCI display board, readable from onecourt.in and
// GitHub Actions.
//
// cdb.sci.gov.in has no CORS headers and doesn't answer GitHub's or
// Hostinger's servers, but it does answer Cloudflare. This Worker fetches the
// board, keeps a copy for 20 seconds (so many visitors make one request), and
// returns
//
//   { updated, courts: { "2": { item: "14", sitting: true, msg: "SEQUENCE 1 TO 32 ..." },
//                        "R1": { ... } } }
//
// `msg` is the court's message line — most mornings the hearing sequence.
// If SCI is slow or down, the last good copy (up to an hour old) is returned
// with "stale": true.
//
// Deploy: Cloudflare dashboard → Workers & Pages → Create → Worker, name it
// "sc-board", Deploy, then Edit code → paste this file → Deploy.

const FEED = "https://cdb.sci.gov.in/index.php?courtListCsv=1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,21,22" +
  "&request=display_full&requestType=ajax";
const FRESH = new Request("https://sc-board.cache/fresh");
const LAST_GOOD = new Request("https://sc-board.cache/last-good");

const HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Access-Control-Allow-Origin": "*",
  "Cache-Control": "no-store",
};

function clean(s) {
  return String(s ?? "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

// 1–18 are courtrooms; 21 and 22 are Registrar Courts 1 and 2.
function courtKey(no) {
  const n = parseInt(no, 10);
  if (!n) return null;
  return n >= 21 ? "R" + (n - 20) : String(n);
}

async function fetchBoard() {
  const r = await fetch(FEED, {
    headers: {
      "Accept": "application/json",
      "User-Agent": "Mozilla/5.0 (compatible; OneCourt display-board reader; +https://onecourt.in)",
    },
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error("SCI returned HTTP " + r.status);
  const d = await r.json();
  if (!Array.isArray(d.listedItemDetails)) throw new Error("Unexpected response from SCI");

  const courts = {};
  for (const row of d.listedItemDetails) {
    const key = courtKey(row.court_no);
    if (!key) continue;
    const reg = clean(row.registration_number_display);
    const rawItem = String(row.item_no ?? "");
    const sitting = !/not in session/i.test(reg) && !/<font/i.test(rawItem) && clean(rawItem) !== "";
    courts[key] = {
      item: sitting ? clean(rawItem) : null,
      sitting,
      msg: clean(row.court_message),
    };
  }
  return { updated: new Date().toISOString(), date: d.todayB || null, courts };
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: HEADERS });

    const cache = caches.default;
    const fresh = await cache.match(FRESH);
    if (fresh) return new Response(fresh.body, { headers: HEADERS });

    try {
      const body = JSON.stringify(await fetchBoard());
      ctx.waitUntil(Promise.all([
        cache.put(FRESH, new Response(body, { headers: { "Cache-Control": "max-age=20" } })),
        cache.put(LAST_GOOD, new Response(body, { headers: { "Cache-Control": "max-age=3600" } })),
      ]));
      return new Response(body, { headers: HEADERS });
    } catch (err) {
      const last = await cache.match(LAST_GOOD);
      if (last) {
        const data = await last.json();
        data.stale = true;
        data.error = String(err.message || err);
        return new Response(JSON.stringify(data), { headers: HEADERS });
      }
      return new Response(JSON.stringify({ error: String(err.message || err) }), { status: 502, headers: HEADERS });
    }
  },
};
