// Runs the SHIPPED eventRowHtml()/eventClass()/renderEvents()/pushEvent()
// straight out of web/control.js against a stub DOM. Extract-and-eval, as
// reanchor_harness.js does for player.js, so these are the real functions and
// not copies that could drift from them.
//
// What it pins: newest first; the cap; every string through esc(); the classes
// the CSS keys on (warning amber, restart red, debug dim); a row with no node
// has an empty node cell; the wall clock is cut to HH:MM:SS; clear empties the
// view.  Run: node tools/events_harness.js
"use strict";
const fs = require("fs");
const SRC = fs.readFileSync("web/control.js", "utf8");

function fn(name) {                    // extract a top-level function by braces
  const i = SRC.indexOf("function " + name + "(");
  if (i < 0) { console.error("FAIL: could not find " + name); process.exit(1); }
  let d = 0;
  for (let k = SRC.indexOf("{", i); k < SRC.length; k++) {
    if (SRC[k] === "{") d++;
    else if (SRC[k] === "}" && --d === 0) return SRC.slice(i, k + 1);
  }
  console.error("FAIL: unbalanced braces in " + name); process.exit(1);
}

const CAP = Number((SRC.match(/const EVENT_CAP = (\d+);/) || [])[1]);
if (!CAP) { console.error("FAIL: EVENT_CAP not found in control.js"); process.exit(1); }

const page = new Function(`
  const els = {};
  const $ = (id) => els[id] || (els[id] = { innerHTML: "", textContent: "", style: {} });
  const EVENT_CAP = ${CAP};
  let events = [];
  ${fn("esc")}
  ${fn("pushEvent")}
  ${fn("eventClass")}
  ${fn("eventRowHtml")}
  ${fn("renderEvents")}
  return {
    push: (e) => pushEvent(e),
    set: (items) => { events = items; renderEvents(); },
    clear: () => { events = []; renderEvents(); },
    html: () => $("eventRows").innerHTML,
    empty: () => $("eventsEmpty").style.display,
    count: () => $("evCount").textContent,
    row: eventRowHtml,
    cls: eventClass,
  };
`)();

let fails = 0;
function check(name, ok, detail) {
  console.log((ok ? "ok   " : "FAIL ") + name + (ok || !detail ? "" : "  -- " + detail));
  if (!ok) fails++;
}
const ev = (o) => Object.assign({ wall: "12:34:56.789", kind: "k", level: "info", name: "n", text: "t" }, o);

// empty state
page.set([]);
check("empty state shown with no events", page.empty() === "block" && page.html() === "");
check("count reads 'newest first' when empty", page.count() === "newest first");

// order
for (let i = 1; i <= 3; i++) page.push(ev({ wall: `00:00:0${i}.123`, text: `e${i}` }));
const rows = page.html().match(/<tr[\s\S]*?<\/tr>/g) || [];
check("three rows", rows.length === 3, "got " + rows.length);
check("newest first", rows[0].includes(">e3 <") && rows[2].includes(">e1 <"));
check("wall cut to HH:MM:SS", rows[0].includes(">00:00:03<") && !rows[0].includes(".123"));
check("count reads the size", page.count() === "3 · newest first", page.count());
check("empty state hidden", page.empty() === "none");

// cap
page.set([]);
for (let i = 0; i < CAP + 40; i++) page.push(ev({ text: "x" + i }));
const n = (page.html().match(/<tr/g) || []).length;
check(`capped at ${CAP}`, n === CAP, "got " + n);
check("cap keeps the newest, drops the oldest",
      page.html().includes(">x" + (CAP + 39) + " <") && !page.html().includes(">x0 <"));
// and a history bigger than the cap (cannot happen, ring is the same size — but)
page.set(Array.from({ length: CAP + 10 }, (_, i) => ev({ text: "h" + i })));
check("an oversized history is cut to the cap", (page.html().match(/<tr/g) || []).length === CAP);

// escaping: every string a hostile node could influence goes through esc()
const hostile = page.row(ev({
  wall: "<b>", kind: "<i>", name: "<script>alert(1)</script>",
  text: "\"quoted\" & <img src=x onerror=alert(1)>",
}));
check("text escaped", !hostile.includes("<img") && hostile.includes("&lt;img src=x onerror=alert(1)&gt;"));
check("name escaped", !hostile.includes("<script>") && hostile.includes("&lt;script&gt;"));
check("kind and wall escaped", hostile.includes("&lt;i&gt;") && hostile.includes("&lt;b&gt;"));
check("quotes and ampersands escaped", hostile.includes("&quot;quoted&quot; &amp;"));

// classes the CSS keys on
check("warning is amber", page.cls({ level: "warning", kind: "catchup-timeout" }) === "evWarn");
check("restart is red as well", page.cls({ level: "warning", kind: "restart" }) === "evWarn evBad");
check("debug is dim", page.cls({ level: "debug", kind: "cadence" }) === "evDim");
check("info is plain", page.cls({ level: "info", kind: "join" }) === "");

// rows without a node, and rows missing fields
const bare = page.row(ev({ kind: "toast", name: null, text: "hi" }));
check("no node: empty node cell", bare.includes('<td class="evN"></td>'));
const partial = page.row({ kind: "k", level: "info" });
check("missing text/wall/name never print 'undefined'", !partial.includes("undefined"));

// clear
page.clear();
check("clear empties the view", page.html() === "" && page.empty() === "block");

console.log(fails ? `\n${fails} FAILED` : "\nall checks passed");
process.exit(fails ? 1 : 0);
