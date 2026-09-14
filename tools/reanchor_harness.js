// Runs the SHIPPED startSource()/onSteer() straight out of web/player.js, and
// compares them against the order they used to be written in. Extract-and-eval
// so they are the real functions, not copies that could drift from them.
//
// The bug: startSource() called stopCurrent() — which nulls `current` — and
// only then bailed on `seekS >= buf.duration`. A refused start therefore left
// the node silent with `onended` already detached, so it never reported
// `state`, the conductor went on steering a node that had stopped, and
// onSteer's own tail dereferenced the null and threw. Reachable at end of
// track, where the re-anchor's seek adjustment is largest, and on a short or
// truncated decode.
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

// The pre-fix order, kept here as the thing being guarded against.
const OLD_START = `function startSource(buf, trackId, title, whenCtx, seekS) {
  stopCurrent();
  if (seekS >= buf.duration) return;
  const src = ctx.createBufferSource();
  src.buffer = buf; src.connect(master); src.start(whenCtx, seekS);
  current = { src, trackId, title, rate: 1, anchorCtx: whenCtx, anchorPos: seekS, startedCtx: whenCtx };
  src.onended = () => { if (current && current.src === src) { current = null; send({type:"state",playing:null}); } };
  send({ type: "state", playing: trackId });
}`;

function build(startSrc, steerSrc = fn("onSteer")) {
  return new Function(`
    let current = null, nudgeMs = 0;
    const sent = [];
    const $ = () => ({ textContent: "" });      // the page log, stubbed
    const LOG_LINES = 40, logLines = [];
    const REANCHOR_S = 0.2, MAX_RATE_TRIM = 8e-4, STEER_HORIZON_S = 15;
    const SLEW_LIMIT_S = 2 * MAX_RATE_TRIM * STEER_HORIZON_S, SLEW_PATIENCE_S = 10;
    const eq = null, master = {};
    const ctx = {
      currentTime: 100.0,
      createBufferSource: () => ({ buffer: null, connect(){}, start(){}, stop(){},
                                   onended: null, playbackRate: { setValueAtTime(){} } }),
      getOutputTimestamp: () => ({ contextTime: 100.0, performanceTime: 100000.0 }),
    };
    const cache = new Map();
    const send = (m) => sent.push(m);
    const setNowPlaying = () => {};
    ${fn("logLine")}
    ${fn("describeCause")}
    ${fn("mapMs")}
    ${fn("renderAheadMs")}
    ${fn("perfToCtx")}
    ${fn("stopCurrent")}
    ${fn("posAt")}
    ${startSrc}
    ${steerSrc}
    return {
      // startCtx: when the source is scheduled to start - the anchors sit
      // there until the first steer moves them, exactly as startSource leaves them.
      seed(buf, id, anchorPos = 0, startCtx = 0) {
        current = { src: ctx.createBufferSource(), trackId: id, title: "t",
                    rate: 1, anchorCtx: startCtx, anchorPos, startedCtx: startCtx, slewSince: null };
        cache.set(id, buf);
      },
      steer: (m) => onSteer(m),
      advance: (s) => { ctx.currentTime += s; },
      now: () => ctx.currentTime,
      posAt: (t) => posAt(t),
      get current() { return current; },
      get rate() { return current ? current.rate : null; },
      get slewSince() { return current ? current.slewSince : null; },
      sent,
    };
  `)();
}

// The node holds a 60 s buffer; the conductor says the song is 181 s in, at a
// node time mapping to roughly now. onSteer takes the restart branch and
// computes a seek past the end of what this node actually has.
function trial(name, startSrc) {
  const h = build(startSrc);
  h.seed({ duration: 60.0 }, "trk");
  let threw = null;
  try { h.steer({ trackId: "trk", posMs: 181000, atNodeMs: 100000 }); }
  catch (e) { threw = e.constructor.name + ": " + e.message; }
  const out = {
    threw,
    alive: !!h.current,
    ack: !!h.sent.find((m) => m.type === "steerAck"),
    refusal: h.sent.find((m) => m.type === "startRefused") || null,
  };
  console.log(`--- ${name}`);
  console.log(`    threw:          ${out.threw || "no"}`);
  console.log(`    still playing:  ${out.alive ? "yes" : "NO — node is silent"}`);
  console.log(`    steerAck sent:  ${out.ack ? "yes" : "no"}`);
  console.log(`    refusal voiced: ${out.refusal ? JSON.stringify(out.refusal) : "no"}`);
  return out;
}

let fails = 0;
function check(name, ok) {
  if (!ok) fails++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}`);
}

const before = trial("OLD order (stopCurrent, then bail)", OLD_START);
const after = trial("SHIPPED (bail, then stopCurrent)", fn("startSource"));

console.log("");
check("old order throws on the null", !!before.threw);
check("old order leaves the node silent", !before.alive);
check("old order never reports anything", !before.ack && !before.refusal);
check("shipped order does not throw", !after.threw);
check("shipped order keeps playing what it had", after.alive);
check("shipped order still acks the steer", after.ack);
check("shipped order voices the refusal", !!after.refusal);
check("refusal carries the numbers that explain it",
      !!after.refusal && after.refusal.seekMs === 181080 && after.refusal.durationMs === 60000);


// --- the slew dead zone ----------------------------------------------------
// A node stranded past what the servo can pull must not be left slewing for
// minutes; a node swinging through zero must never be restarted at all. Both
// are driven here through the shipped onSteer.

function steerAt(h, errMs) {
  // Build a steer whose implied error is exactly errMs, by asking the SHIPPED
  // posAt() where we will be and subtracting. Reimplementing posAt here is what
  // broke the first draft of this: it has a Math.max(0, ...) clamp that a
  // hand-rolled copy quietly omitted.
  const t = h.now();
  const atNodeMs = 100000 + (t - 100) * 1000;   // maps through perfToCtx to t
  const m = { trackId: "trk", posMs: (h.posAt(t) - errMs / 1000) * 1000, atNodeMs };
  h.steer(m);
  return m;
}

// 1. stranded: 120 ms out and staying there
{
  const h = build(fn("startSource"));
  h.seed({ duration: 600.0 }, "trk", 100.0);
  const before = h.current.src;
  steerAt(h, 120);
  check("stranded: does not restart immediately (below REANCHOR_S)", h.current.src === before);
  check("stranded: starts counting how long it has been out", h.slewSince !== null);
  h.advance(4); steerAt(h, 120);
  check("stranded: still patient at 4 s", h.current.src === before);
  h.advance(8); steerAt(h, 120);
  check("stranded: restarts once patience runs out", h.current.src !== before);
  check("stranded: the fresh source starts un-trimmed", h.rate === 1);
}

// 2. swinging: +/-12 ms crossing zero — the live Android 6 tablet
{
  const h = build(fn("startSource"));
  h.seed({ duration: 600.0 }, "trk", 100.0);
  const before = h.current.src;
  let restarted = false;
  for (let i = 0; i < 40; i++) {            // 80 s of 2 s steers
    steerAt(h, i % 2 ? 12 : -12);
    h.advance(2);
    if (h.current.src !== before) restarted = true;
  }
  check("swinging +/-12 ms for 80 s never triggers a restart", !restarted);
  check("swinging: the out-of-range timer keeps being cleared", h.slewSince === null);
}

// 3. a real fault still restarts at once
{
  const h = build(fn("startSource"));
  h.seed({ duration: 600.0 }, "trk", 100.0);
  const before = h.current.src;
  steerAt(h, 400);                          // past REANCHOR_S
  check("400 ms restarts immediately, without waiting", h.current.src !== before);
}


// --- what each restart says about itself (telemetry slice 3) ---------------
// The conductor used to see "a source started" and nothing else. Now the
// patience path and the fault path each name themselves and carry the error
// that fired them, and a swinging node - which never restarts - says nothing.
function starts(h) { return h.sent.filter((m) => m.type === "state" && m.playing); }

{
  const h = build(fn("startSource"));
  h.seed({ duration: 600.0 }, "trk", 100.0);
  steerAt(h, 120); h.advance(4); steerAt(h, 120); h.advance(8); steerAt(h, 120);
  const st = starts(h);
  check("patience restart says cause reanchor, reason patience",
        st.length === 1 && st[0].cause === "reanchor" && st[0].reason === "patience");
  check("...and carries the error that fired it (~120 ms)",
        st.length === 1 && Math.abs(st[0].errMs - 120) < 2);
}
{
  const h = build(fn("startSource"));
  h.seed({ duration: 600.0 }, "trk", 100.0);
  steerAt(h, 400);
  const st = starts(h);
  check("fault restart says reason fault at ~400 ms",
        st.length === 1 && st[0].reason === "fault" && Math.abs(st[0].errMs - 400) < 2);
}
{
  const h = build(fn("startSource"));
  h.seed({ duration: 600.0 }, "trk", 100.0);
  for (let i = 0; i < 40; i++) { steerAt(h, i % 2 ? 12 : -12); h.advance(2); }
  check("a swinging node reports no start at all", starts(h).length === 0);
  const acks = h.sent.filter((m) => m.type === "steerAck");
  check("every steerAck carries mapMs from the output-timestamp pair",
        acks.length === 40 && acks.every((m) => typeof m.mapMs === "number"));
  // The inputs ride with the reading, and they reproduce it: err is what the
  // shipped posAt() gives at the target minus the posMs the steer carried.
  // The harness knows posMs because steerAt() built it; the ack's anchors are
  // the post-steer ones, and posAt is the same line through either anchor.
  check("every steerAck carries nudgeMs, targetCtx and the anchors",
        acks.every((m) => ["nudgeMs", "targetCtx", "anchorCtx", "anchorPos"]
                             .every((k) => typeof m[k] === "number")));
  check("every steerAck carries renderAheadMs",
        acks.every((m) => typeof m.renderAheadMs === "number"));
  const m = steerAt(h, 7);
  const last = h.sent[h.sent.length - 1];
  const rebuilt = last.anchorPos + (last.targetCtx - last.anchorCtx) * last.rate;
  check("the ack's fields rebuild its own err: posAt(target) - posMs, via the shipped posAt",
        last.type === "steerAck" && Math.abs((rebuilt - m.posMs / 1000) * 1000 - last.errMs) < 1e-6
        && Math.abs(last.errMs - 7) < 1e-6);
  // The stub's output timestamp is pinned at contextTime 100 while currentTime
  // advances, so render-ahead on this ack is exactly how far the harness has come.
  check("renderAheadMs is the render position minus the output position",
        Math.abs(last.renderAheadMs - (h.now() - 100) * 1000) < 1e-6);
}

// --- the pre-start anchor ---------------------------------------------------
// A steer whose target is past the scheduled start, arriving while now is not.
// The old branch re-anchored at now: posAt(now) clamps to seekS, so the anchor
// then said "seekS at now" for a source that starts later - bookkeeping ahead
// of the audio by the remaining lead, for the rest of the source. Observed
// live (START_SHAPES_PLAN, Shape A): laptop and pc, 45-130 ms, then patience.
//
// The old branch is the shipped text with its two anchor lines put back, and
// the splice has to match: if the shipped function drifts, this fails loudly.
const NEW_ANCHOR = `    const at = Math.max(nowCtx, current.startedCtx);
    current.anchorPos = posAt(at);
    current.anchorCtx = at;`;
const OLD_ANCHOR = `    current.anchorPos = posAt(nowCtx); // re-anchor bookkeeping at the old rate
    current.anchorCtx = nowCtx;`;
const SHIPPED_STEER = fn("onSteer").replace(/\r\n/g, "\n");  // template literals are LF; the file is CRLF
if (!SHIPPED_STEER.includes(NEW_ANCHOR)) { console.error("FAIL: onSteer no longer carries the guarded anchor"); process.exit(1); }
const OLD_STEER = SHIPPED_STEER.replace(NEW_ANCHOR, OLD_ANCHOR);

// The conductor's truth for a source at `seekS` scheduled at `startCtx`: the
// song is at seekS + (target - startCtx). Built from the truth, not from the
// node's posAt, so the ack's err is the bookkeeping's error and nothing else.
function steerTruth(h, seekS, startCtx, lead) {
  const target = h.now() + lead;
  const atNodeMs = 100000 + (target - 100) * 1000;
  h.steer({ trackId: "trk", posMs: (seekS + (target - startCtx)) * 1000, atNodeMs });
  const acks = h.sent.filter((m) => m.type === "steerAck");
  return acks[acks.length - 1].errMs;
}

function preStart(steerSrc) {
  const h = build(fn("startSource"), steerSrc);
  const start = h.now() + 0.2;                 // scheduled 200 ms from now
  h.seed({ duration: 600.0 }, "trk", 100.0, start);
  const first = steerTruth(h, 100.0, start, 0.3);   // target past the start, now before it
  const anchorCtx = h.current.anchorCtx;
  h.advance(2.0);                              // well into the source
  const second = steerTruth(h, 100.0, start, 0.3);
  return { first, second, anchorCtx, start };
}

const old = preStart(OLD_STEER);
const nu = preStart(SHIPPED_STEER);
console.log("");
console.log(`--- pre-start anchor: old first ${old.first.toFixed(1)} ms, second ${old.second.toFixed(1)} ms; shipped first ${nu.first.toFixed(1)} ms, second ${nu.second.toFixed(1)} ms`);
check("old: the pre-start steer itself reads 0 (the reading is right, the anchor it leaves is not)",
      Math.abs(old.first) < 1e-6);
check("old: the anchor is moved to before the start", old.anchorCtx < old.start);
check("old: the next steer then reads the remaining lead as being ahead (+200 ms)",
      Math.abs(old.second - 200) < 1e-6);
check("shipped: the pre-start steer reads 0", Math.abs(nu.first) < 1e-6);
check("shipped: the anchor stays at the start", Math.abs(nu.anchorCtx - nu.start) < 1e-9);
check("shipped: the next steer reads 0", Math.abs(nu.second) < 1e-6);
{
  // And after the start, both anchor identically: a steer at runS 2 s and one
  // at runS 4 s read 0 on either text - the guard is a no-op once running.
  const a = build(fn("startSource"), OLD_STEER), b = build(fn("startSource"), SHIPPED_STEER);
  for (const h of [a, b]) { h.seed({ duration: 600.0 }, "trk", 100.0, h.now() - 2.0); }
  const ea = [steerTruth(a, 100.0, a.now() - 2.0, 0.3)], eb = [steerTruth(b, 100.0, b.now() - 2.0, 0.3)];
  a.advance(2); b.advance(2);
  ea.push(steerTruth(a, 100.0, a.now() - 4.0, 0.3)); eb.push(steerTruth(b, 100.0, b.now() - 4.0, 0.3));
  check("once running, old and shipped anchor identically (both read 0, 0)",
        ea.every((e) => Math.abs(e) < 1e-6) && eb.every((e) => Math.abs(e) < 1e-6));
}

console.log(fails ? `\n${fails} CHECK(S) FAILED` : "\nall checks passed");
process.exit(fails ? 1 : 0);
