// Runs the SHIPPED renderActivity() / onNotice() / logLine() straight out of
// web/player.js against a stub DOM, with a clock and timers it controls.
// Extract-and-eval, as the other harnesses do, so these are the real
// functions and not copies that could drift from them.
//
// What it pins: a fresh notice wins the activity bar over idle and over
// playing; an expired one gets out of the way; its timer clears it; an empty
// notice cancels one; the ttl and the length are clamped; and everything
// lands through textContent — the stub has no innerHTML, so markup would have
// nowhere to go.  Run: node tools/activity_harness.js
"use strict";
const fs = require("fs");
const SRC = fs.readFileSync("web/player.js", "utf8");

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

const page = new Function(`
  const els = {};
  const $ = (id) => els[id] || (els[id] = { textContent: "", className: "" });
  let nowMs = 1000;
  const performance = { now: () => nowMs };
  const timers = [];
  const setTimeout = (cb, ms) => { timers.push({ cb, at: nowMs + ms }); return timers.length; };
  const clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].cb = null; };
  let current = null, armed = false;
  const dl = new Map(), decoding = new Set();
  const LOG_LINES = 40;
  const logLines = [];
  let notice = null, noticeTimer = null;
  ${fn("logLine")}
  ${fn("onNotice")}
  ${fn("renderActivity")}
  return {
    render: () => renderActivity(),
    say: (m) => onNotice(m),
    play: (on) => { current = on ? { trackId: "t" } : null; },
    arm: (on) => { armed = on; },
    advance: (ms) => {
      nowMs += ms;
      for (const t of timers) if (t.cb && t.at <= nowMs) { const cb = t.cb; t.cb = null; cb(); }
    },
    now: () => nowMs,
    text: () => els.activityText.textContent,
    cls: () => els.activity.className,
    pct: () => els.activityPct.textContent,
    log: () => els.log.textContent,
    state: () => notice,
    els,
  };
`)();

let fails = 0;
function check(name, ok, detail) {
  console.log((ok ? "ok   " : "FAIL ") + name + (ok || !detail ? "" : "  -- " + detail));
  if (!ok) fails++;
}

page.render();
check("idle to begin with", page.text() === "idle" && page.cls() === "");

page.say({ text: "clock still settling - joining automatically", ttlS: 40 });
check("a fresh notice wins over idle",
      page.text() === "clock still settling - joining automatically" && page.cls() === "note",
      page.text() + " / " + page.cls());
page.play(true); page.render();
check("...and over playing", page.text().startsWith("clock still settling"));
check("the percent cell is empty while a notice shows", page.pct() === "");
page.arm(true); page.render();
check("...and over arming", page.text().startsWith("clock still settling"));
page.arm(false);

page.advance(39_000); page.render();
check("still there at 39 s of a 40 s ttl", page.text().startsWith("clock still settling"));
page.advance(2_000);
check("its timer clears it: playing shows through", page.text() === "playing" && page.cls() === "play",
      page.text() + " / " + page.cls());
check("the notice state is gone", page.state() === null);

page.say({ text: "joining now", ttlS: 6 });
check("a second notice shows", page.text() === "joining now");
page.say({ text: "" });
check("an empty notice cancels it", page.text() === "playing" && page.state() === null);

page.say({ text: "<b>x</b> & <img src=x onerror=alert(1)>", ttlS: 1e9 });
check("ttl is clamped to 120 s", page.state().until <= page.now() + 120_000, String(page.state().until));
check("markup lands as text, not as markup",
      page.text() === "<b>x</b> & <img src=x onerror=alert(1)>" && page.els.activityText.innerHTML === undefined);
page.say({ text: "x".repeat(500), ttlS: 5 });
check("text is cut to 120 characters", page.text().length === 120, String(page.text().length));
page.say({ ttlS: 5 });
check("a notice with no text clears rather than showing 'undefined'", page.text() === "playing");

page.say({ text: "measuring - keep still", ttlS: "banana" });
check("a junk ttl falls back to the default", page.state().until === page.now() + 8_000);

check("the page log kept the notices, newest last",
      page.log().split("\n").slice(-1)[0].endsWith("notice: measuring - keep still")
      && page.log().includes("notice: joining now"));
for (let i = 0; i < 60; i++) page.say({ text: "n" + i, ttlS: 1 });
check("the page log is capped at 40 lines", page.log().split("\n").length === 40,
      String(page.log().split("\n").length));

console.log(fails ? `\n${fails} FAILED` : "\nall checks passed");
process.exit(fails ? 1 : 0);
