// Checks the sequence parser in index.html against real board messages.
//   node test_sequence.js
const fs = require("fs");
const html = fs.readFileSync(__dirname + "/index.html", "utf8");
const src = html.match(/<script id="oc-sequence">([\s\S]*?)<\/script>/)[1];
const module_ = { exports: {} };
new Function("module", src)(module_);
const { parseSequence, applySequence, isSequence } = module_.exports;

// Board messages and the order we expect, as compact runs ("PO" = pass-over
// point, "FPO" = fresh pass-overs). Items are taken as 1..last unless listed.
const CASES = [
  ["SEQUENCE WOULD BE ITEM NOS 1 TO 30 53 PASS OVER THEN REST OF THE MATTERS", 56, "1-30 53 PO 31-52 54-56"],
  ["SEQUENCE 1 TO 32 79 84 PASS OVER IF ANY 33 TO 77 80 TO 83", 84, "1-32 79 84 PO 33-77 80-83 | 78"],
  ["SEQUENCE WOULD BE ITEM NOS. 1 TO 39 57 58 60 TO 62 PASSOVER IF ANY THEREAFTER ITEM NOS. 40 TO 56", 62, "1-39 57-58 60-62 PO 40-56 | 59"],
  ["sequence item nos. 1 to 40 57 to 64 68 fresh passover 41 to 56 65 67 66", 68, "1-40 57-64 68 FPO 41-56 65 67 66"],
  ["ITEM NO.67 WILL BE TAKEN UP AS FIRST ITEM AND REST OF THE MATTERS AS PER LIST", 67, "67 1-66"],
  ["SEQUENCE WOULD BE ITEM NOS.1 TO 11 56 57 60 51 WITH 61 PO IF ANY ITEM NO.12 ONWARDS AS PER CAUSELIST", 61, "1-11 56-57 60 51 61 PO 12-50 52-55 58-59"],
  ["Seq.Item Nos.1 to 27 56 58 28 to 55 61 62 and pass over matters", 62, "1-27 56 58 28-55 61-62 PO | 57 59-60"],
  ["Todays sequence 1 to 20 57 to 60 passovers 21 to 56 61 62", 62, "1-20 57-60 PO 21-56 61-62"],
  ["Sequence would be as Item Nos. 1 to 28 57 to 61 then rest of matters as per list...", 61, "1-28 57-61 29-56"],
  ["SEQUENCE WOULD BE ITEM NOS. 1 TO 41 PASSOVER IF ANY THEREAFTER ITEM NOS. 42 TO 51 60 54 55 REST AS PER LIST", 60, "1-41 PO 42-51 60 54-55 52-53 56-59"],
  ["SEQUENCE WOULD BE ITEM NOS.1 TO 16 56 58 60 PO IF ANY ITEM NO.17 ONWARDS AS PER CAUSELIST ITEM NO.41 AT 2 PM ITEM NO.59 AT 3 PM", 60, "1-16 56 58 60 PO 17-40 42-55 57", "41@2 PM 59@3 PM"],
  ["Seq.Item Nos.1 to8 54 55 56 9 to 32 57 58 62 33 to 53 59 60 61 and pass over matters", 63, "1-8 54-56 9-32 57-58 62 33-53 59-61 PO | 63"],
  ["SEQUENCE 1 TO 38 54 57 66 67 PASS OVER IF ANY 39 TO 53 59 TO 65", 58, "1-38 54 57 PO 39-53 | 55-56 58", "", "66,67,59–65"],
];

function runs(order) {
  const out = []; let run = null;
  const flush = () => { if (run) { out.push(run[0] === run[1] ? `${run[0]}` : `${run[0]}-${run[1]}`); run = null; } };
  for (const o of order) {
    if (o.divider) { flush(); out.push({ po: "PO", freshpo: "FPO", rest: "|" }[o.divider]); continue; }
    const n = o.entry.num;
    if (run && n === run[1] + 1) run[1] = n; else { flush(); run = [n, n]; }
  }
  flush();
  return out.join(" ");
}

function runs(order) {
  const out = []; let run = null;
  const flush = () => { if (run) { out.push(run[0] === run[1] ? `${run[0]}` : `${run[0]}-${run[1]}`); run = null; } };
  for (const o of order) {
    if (o.divider) { flush(); out.push({ po: "PO", freshpo: "FPO", rest: "|" }[o.divider]); continue; }
    const n = o.entry.num;
    if (run && n === run[1] + 1) run[1] = n; else { flush(); run = [n, n]; }
  }
  flush();
  return out.join(" ");
}

// Board notices that aren't sequences must not reorder anything.
let fail = 0;
// Board notices that aren't sequences must not reorder anything.
for (const [msg, want] of [
  ["Special Bench will sit at 1 PM", false],
  ["SINGLE JUDGE AND CHAMBER MATTERS TO BE TAKEN UP IMMEDIATELY AFTER NORMAL COURT WORK IS OVER", false],
  ["ITEM NO.67 WILL BE TAKEN UP AS FIRST ITEM AND REST OF THE MATTERS AS PER LIST", true],
  ["SEQUENCE 1 TO 32 79 84 PASS OVER IF ANY 33 TO 77 80 TO 83", true],
  ["SPECIAL BENCH AT 2 P.M. REST OF THE BOARD IS DISCHARGED", false],
  ["ITEM NOS. 301 TO 306 WILL BE TAKEN UP IN SPECIAL BENCH AT 2 PM", false],
  ["except item no. 101 rest of the board is discharged for the day", false],
  ["EXCEPT ITEM NO.38 AND P20 REST OF BOARD IS DISCHARGED FOR THE DAY", false],
  ["Kamal Mohan Gupta is required to appear in item no.102", false],
  ["Seq.at 2 p.m. Item Nos.13 to 28 31 and 101 to 120", true],
  ...CASES.map(c => [c[0], true]),
]) {
  const got = isSequence(parseSequence(msg), msg);
  if (got !== want) fail++;
  console.log(`${got === want ? "ok  " : "FAIL"} ${want ? "sequence" : "notice  "}: ${msg}`);
}
for (const [msg, last, want, wantTimed = "", wantMissing = ""] of CASES) {
  const entries = Array.from({ length: last }, (_, i) => ({ num: i + 1 }));
  const r = applySequence(parseSequence(msg), entries);
  const got = runs(r.order);
  const timed = r.timed.map(t => `${t.n}@${t.time}`).join(" ");
  const missing = r.missing.join(",");
  const ok = got === want && timed === wantTimed && missing === wantMissing;
  if (!ok) fail++;
  console.log(`${ok ? "ok  " : "FAIL"} ${msg}\n     → ${got}${timed ? "  [timed " + timed + "]" : ""}${missing ? "  [missing " + missing + "]" : ""}${ok ? "" : "\n     want " + want}`);
}
console.log(fail ? `\n${fail} failed` : `\nall ${CASES.length} passed`);
process.exit(fail ? 1 : 0);
