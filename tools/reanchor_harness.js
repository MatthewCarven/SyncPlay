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

function build(startSrc) {
  return new Function(`
    let current = null, nudgeMs = 0;
    const sent = [];
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
    ${fn("perfToCtx")}
    ${fn("stopCurrent")}
    ${fn("posAt")}
    ${startSrc}
    ${fn("onSteer")}
    return {
      seed(buf, id, anchorPos = 0) {
        current = { src: ctx.createBufferSource(), trackId: id, title: "t",
                    rate: 1, anchorCtx: 0, anchorPos, startedCtx: 0, slewSince: null };
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
  h.steer({ trackId: "trk", posMs: (h.posAt(t) - errMs / 1000) * 1000, atNodeMs });
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

console.log(fails ? `\n${fails} CHECK(S) FAILED` : "\nall checks passed");
process.exit(fails ? 1 : 0);
