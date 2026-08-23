// Runs the SHIPPED captureLevel() straight out of web/player.js against signals
// whose level is known by hand. Extract-and-eval so it is the real function,
// not a copy that could drift from it.
const fs = require("fs");
const src = fs.readFileSync("web/player.js", "utf8");
const m = src.match(/function captureLevel\(sig\) \{[\s\S]*?\n\}/);
if (!m) { console.error("FAIL: could not find captureLevel in player.js"); process.exit(1); }
eval(m[0]);

let fails = 0;
function check(name, got, want, tol = 0.05) {
  const ok = Math.abs(got - want) <= tol;
  if (!ok) fails++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${name}: got ${got.toFixed(2)}, want ${want.toFixed(2)}`);
}

// 1. digital silence — the July symptom, taken to its limit
const silence = new Float32Array(4800);
let r = captureLevel(silence);
check("silence rmsDb", r.rmsDb, -120);
check("silence peakDb", r.peakDb, -120);
check("silence clipPct", r.clipPct, 0);

// 2. a full-scale square: RMS and peak both 0 dBFS, every sample clipped
const square = Float32Array.from({length: 4800}, (_, i) => (i % 2 ? 1 : -1));
r = captureLevel(square);
check("square rmsDb", r.rmsDb, 0);
check("square peakDb", r.peakDb, 0);
check("square clipPct", r.clipPct, 100);

// 3. a sine at amplitude 0.1: RMS = 0.1/sqrt(2) = 0.0707 -> -23.0 dBFS,
//    peak 0.1 -> -20.0 dBFS. The 3 dB gap between them is the giveaway that
//    rms and peak are genuinely being computed separately.
const sine = Float32Array.from({length: 48000}, (_, i) => 0.1 * Math.sin(2 * Math.PI * 440 * i / 48000));
r = captureLevel(sine);
check("sine rmsDb", r.rmsDb, -23.0);
check("sine peakDb", r.peakDb, -20.0);
check("sine clipPct", r.clipPct, 0);

// 4. a realistic quiet-but-alive room: -43 dBFS was what the laptop mic read
//    before it went dead, and it must land clear of the -60 silence threshold.
const amp = Math.pow(10, -43 / 20) * Math.SQRT2;
const quiet = Float32Array.from({length: 48000}, (_, i) => amp * Math.sin(2 * Math.PI * 200 * i / 48000));
r = captureLevel(quiet);
check("alive-but-quiet rmsDb", r.rmsDb, -43.0);
console.log(`${r.rmsDb > -60 ? "ok  " : "FAIL"}  alive-but-quiet clears the -60 dBFS silence gate`);
if (!(r.rmsDb > -60)) fails++;

// 5. the dead reading itself must fall below it
const dead = Float32Array.from({length: 48000}, (_, i) => Math.pow(10, -74 / 20) * Math.SQRT2 * Math.sin(i));
r = captureLevel(dead);
console.log(`${r.rmsDb < -60 ? "ok  " : "FAIL"}  -74 dBFS capture is caught by the silence gate (${r.rmsDb.toFixed(1)})`);
if (!(r.rmsDb < -60)) fails++;

// 6. an empty capture must not produce NaN
r = captureLevel(new Float32Array(0));
const clean = Number.isFinite(r.rmsDb) && Number.isFinite(r.peakDb) && Number.isFinite(r.clipPct);
console.log(`${clean ? "ok  " : "FAIL"}  empty capture stays finite: ${JSON.stringify(r)}`);
if (!clean) fails++;

console.log(fails ? `\n${fails} CHECK(S) FAILED` : "\nall checks passed");
process.exit(fails ? 1 : 0);
