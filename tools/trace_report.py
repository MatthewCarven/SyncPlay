#!/usr/bin/env python
"""Read a SyncPlay trace and print the tables that used to be built by hand.

    python tools/trace_report.py                          # newest logs/trace-*.jsonl
    python tools/trace_report.py logs/trace-20260903-201500.jsonl
    python tools/trace_report.py --since 20:30 --until 21:15 --node tablet
    python tools/trace_report.py --csv steer.csv          # the steer lines, for a spreadsheet

Per node: n, mean, sd, min, max and zero-crossings of `err ms` from the steer
lines; sample survival, audio-clock ppm and its credibility from the node
lines; restarts. Across the fleet: the spread of the per-node means and its
metres of air. Then the starts — defers, catch-ups with the seconds waited,
timeouts — mesh closure best/worst per pair, and the warnings timeline. Any
evening becomes comparable with any other, which is the question the tablet
thread could never answer. Standard library only; reads nothing but the file.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

METRES_PER_MS = 0.343  # speed of sound, so a spread reads as a distance
MIN_ACKS = 8           # a mean of fewer acks than this is not a mean

STEER_COLUMNS = [
    "t", "wall", "node", "name", "track", "errMs", "rate", "runS", "offsetMs",
    "trustMs", "skewPpm", "nUsed", "lastRttMs", "mapMs",
    "sentPosMs", "sentAtNodeMs", "sentLeadS", "nudgeMs", "targetCtx", "anchorCtx", "anchorPos",
]
STEP_MS = 30.0         # a jump in err between consecutive acks worth explaining
MIRROR_MS = 200.0      # a restart's error past this, then the opposite sign next ack
MIRROR_WINDOW_S = 6.0  # ...within this long, is one bad sample costing two restarts


# --- reading -------------------------------------------------------------------


def load(path: Path) -> List[dict]:
    """Every line that parses. A file cut off mid-line (the process died
    between drains) loses that one line and nothing else."""
    rows: List[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def newest_trace(trace_dir: Path) -> Optional[Path]:
    paths = sorted(Path(trace_dir).glob("trace-*.jsonl"))
    return paths[-1] if paths else None


def wall_key(hhmm: str) -> str:
    """'20:30' / '20:30:15' / '8:05' -> 'HH:MM:SS', comparable as text."""
    parts = [p for p in hhmm.strip().split(":") if p != ""]
    while len(parts) < 3:
        parts.append("00")
    return ":".join(p.zfill(2) for p in parts[:3])


def _names(row: dict) -> List[str]:
    """Whatever names a line carries, for the --node filter."""
    out = []
    for k in ("name", "node", "aName", "bName", "a", "b", "peer"):
        v = row.get(k)
        if isinstance(v, str):
            out.append(v)
    nodes = row.get("nodes")
    if isinstance(nodes, list):
        out.extend(str(n) for n in nodes)
    return out


def select(rows: List[dict], since: Optional[str] = None, until: Optional[str] = None,
           node: Optional[str] = None) -> List[dict]:
    """Cut by wall time and by node. The header always survives; so does any
    event that names no node at all, because a fleet-wide line (a play, a
    stop) is context for every node."""
    lo = wall_key(since) if since else None
    hi = wall_key(until) if until else None
    want = node.lower() if node else None
    out = []
    for r in rows:
        if r.get("kind") == "start":
            out.append(r)
            continue
        wall = str(r.get("wall") or "")[:8]
        if lo and wall < lo:
            continue
        if hi and wall > hi:
            continue
        if want:
            names = _names(r)
            if names and not any(want in n.lower() for n in names):
                continue
        out.append(r)
    return out


# --- the arithmetic -------------------------------------------------------------


def zero_crossings(xs: List[float]) -> int:
    """Sign changes, zeros skipped. A node parked off-centre has few; a node
    swinging through zero has many — the single number that separates the
    two readings of a steady mean."""
    signs = [1 if x > 0 else -1 for x in xs if x != 0]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def steer_stats(rows: List[dict]) -> Dict[str, dict]:
    by: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        if r.get("kind") == "steer" and r.get("errMs") is not None:
            try:
                by[str(r.get("name") or r.get("node"))].append(float(r["errMs"]))
            except (TypeError, ValueError):
                continue
    out = {}
    for name, xs in by.items():
        out[name] = {
            "n": len(xs),
            "mean": statistics.fmean(xs),
            "sd": statistics.stdev(xs) if len(xs) > 1 else 0.0,
            "min": min(xs),
            "max": max(xs),
            "crossings": zero_crossings(xs),
        }
    return out


def node_facts(rows: List[dict]) -> Dict[str, dict]:
    """From the ten-second node lines: the last survival figure, the median
    audio-clock reading over the run and whether the last one was credible,
    and the build the node ran."""
    last: Dict[str, dict] = {}
    ppm: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        if r.get("kind") != "node":
            continue
        name = str(r.get("name") or r.get("id"))
        last[name] = r
        if r.get("audioClockPpm") is not None:
            try:
                ppm[name].append(float(r["audioClockPpm"]))
            except (TypeError, ValueError):
                pass
    out = {}
    for name, r in last.items():
        n_used, n_samples = r.get("nUsed"), r.get("nSamples")
        survival = None
        try:
            if n_samples:
                survival = 100.0 * float(n_used) / float(n_samples)
        except (TypeError, ValueError):
            pass
        out[name] = {
            "survival": survival,
            "audioPpm": statistics.median(ppm[name]) if ppm[name] else None,
            "audioCredible": bool(r.get("audioClockCredible")),
            "distSdPpm": r.get("distSdPpm"),
            "build": r.get("playerBuild"),
            "restarts": r.get("restarts"),
        }
    return out


def spread(stats: Dict[str, dict], min_n: int = MIN_ACKS) -> Optional[dict]:
    means = {k: v["mean"] for k, v in stats.items() if v["n"] >= min_n}
    if len(means) < 2:
        return None
    ms = max(means.values()) - min(means.values())
    return {"ms": ms, "metres": ms * METRES_PER_MS, "nodes": len(means)}


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _pos_at_target_ms(r: dict) -> Optional[float]:
    """The node's own posAt(targetCtx), in ms, from the fields it sent on the
    ack — the same line through the same anchors, so it reproduces the err the
    node computed (to within the rate trim over the ~0.3 s target lead)."""
    ap, ac, tc = _num(r.get("anchorPos")), _num(r.get("anchorCtx")), _num(r.get("targetCtx"))
    rate = _num(r.get("rate"))
    if ap is None or ac is None or tc is None:
        return None
    return (ap + max(0.0, tc - ac) * (rate if rate is not None else 1.0)) * 1000.0


def steps(rows: List[dict], thresh_ms: float = STEP_MS) -> List[dict]:
    """Every jump in err of more than `thresh_ms` between consecutive acks on
    one source (same node, same track, runS increasing — a restart is a new
    source and is not a step), with the jump laid against each input of err:

        err = posAt(target) - posMs,  target = (sentAt + nudge - map) / 1000

    so  d(err) = (d sentAt - d sentPos) + d nudge - d map + d book + rest,
    where `book` is the anchors' own movement beyond the target's and `rest` is
    whatever the columns this trace carries cannot account for. A trace from
    before the conductor half logged the target has only `map`; before the
    node half, no `nudge`/`book`. The column that jumped is the answer; a
    `rest` that carries the whole step is a column this trace does not have.
    """
    # A restart's own ack arrives a hair after its event, carrying the reading
    # that fired it: it is the last reading of the old source, and the pair
    # after it is the correction, not a step.
    restarted: Dict[str, List[float]] = defaultdict(list)
    for ev in events(rows, "restart"):
        t = _num(ev.get("t"))
        if t is not None:
            restarted[str(ev.get("node"))].append(t)

    def fired(key: str, r: dict) -> bool:
        t = _num(r.get("t"))
        return t is not None and any(0.0 <= t - t0 <= 0.25 for t0 in restarted.get(key, ()))

    prev: Dict[str, dict] = {}
    out: List[dict] = []
    for r in rows:
        if r.get("kind") != "steer":
            continue
        key = str(r.get("node") or r.get("name"))
        p = prev.get(key)
        prev[key] = r
        if p is None or p.get("track") != r.get("track") or fired(key, p):
            continue
        run_p, run_r, e_p, e_r = _num(p.get("runS")), _num(r.get("runS")), _num(p.get("errMs")), _num(r.get("errMs"))
        if None in (run_p, run_r, e_p, e_r) or run_r <= run_p:
            continue
        d_err = e_r - e_p
        if abs(d_err) <= thresh_ms:
            continue

        def delta(field):
            a, b = _num(p.get(field)), _num(r.get(field))
            return None if a is None or b is None else b - a

        d_at, d_pos = delta("sentAtNodeMs"), delta("sentPosMs")
        target = None if d_at is None or d_pos is None else d_at - d_pos
        nudge = delta("nudgeMs")
        map_ = delta("mapMs")
        pa_p, pa_r = _pos_at_target_ms(p), _pos_at_target_ms(r)
        d_tc = delta("targetCtx")
        book = None if None in (pa_p, pa_r, d_tc) else (pa_r - pa_p) - d_tc * 1000.0
        rest = d_err - sum(v for v in (target, nudge, (-map_ if map_ is not None else None), book) if v is not None)
        out.append({
            "wall": _short_wall(r), "name": str(r.get("name") or key), "track": r.get("track"),
            "runS": run_r, "errFrom": e_p, "errTo": e_r, "dErr": d_err,
            "target": target, "nudge": nudge, "map": None if map_ is None else -map_,
            "book": book, "rest": rest,
        })
    return out


def mirrors(rows: List[dict]) -> List[dict]:
    """Restarts that were one bad sample: a fault past MIRROR_MS, and the same
    node's next ack within MIRROR_WINDOW_S of the opposite sign and at least
    six-tenths the size. The audio was fine, the reading was not, and the
    restart moved it — the signature slice 2 of START_SHAPES_PLAN waits for."""
    steer = [r for r in rows if r.get("kind") == "steer"]
    out: List[dict] = []
    for ev in events(rows, "restart"):
        e = _num(ev.get("errMs"))
        t0 = _num(ev.get("t"))
        if e is None or t0 is None or abs(e) < MIRROR_MS:
            continue
        key = ev.get("node")
        for r in steer:
            t = _num(r.get("t"))
            # The restart's own ack follows its event by a hair, carrying the
            # reading that fired it; the one after is the reading that counts.
            if r.get("node") != key or t is None or t - t0 < 0.25:
                continue
            if t - t0 > MIRROR_WINDOW_S:
                break
            nxt = _num(r.get("errMs"))
            if nxt is None:
                continue
            if nxt * e < 0 and abs(nxt) >= 0.6 * abs(e):
                out.append({"wall": _short_wall(ev), "name": ev.get("name"), "errMs": e, "nextMs": nxt})
            break
    return out


def events(rows: List[dict], kind: str) -> List[dict]:
    return [r for r in rows if r.get("kind") == "event" and r.get("event") == kind]


def mesh_pairs(rows: List[dict]) -> Dict[str, dict]:
    by: Dict[str, dict] = {}
    for r in rows:
        if r.get("kind") != "mesh":
            continue
        key = f"{r.get('aName')} <-> {r.get('bName')}"
        d = by.setdefault(key, {"closures": [], "rtt": None, "n": 0})
        try:
            d["closures"].append(abs(float(r["closureMs"])))
        except (KeyError, TypeError, ValueError):
            continue
        d["rtt"] = r.get("rttMs")
        d["n"] += 1
    return by


# --- the report -------------------------------------------------------------------


def _f(v, fmt: str, dash: str = "-") -> str:
    if v is None:
        return dash
    try:
        return format(float(v), fmt)
    except (TypeError, ValueError):
        return dash


def _short_wall(r: dict) -> str:
    return str(r.get("wall") or "")[:8]


def report(rows: List[dict], source: str = "") -> str:
    out: List[str] = []
    header = next((r for r in rows if r.get("kind") == "start"), None)
    walls = [str(r["wall"])[:8] for r in rows if r.get("wall") and r.get("kind") != "start"]
    kinds = defaultdict(int)
    for r in rows:
        kinds[str(r.get("kind"))] += 1

    if source:
        out.append(f"trace: {source}")
    if header is not None:
        out.append(
            f"start: {_short_wall(header)}  build {header.get('build') or '?'}  "
            f"music {header.get('musicDir') or '?'}  "
            f"(play lead {_f(header.get('playLeadS'), '.1f')} s, "
            f"catch-up wait {_f(header.get('catchupWaitS'), '.0f')} s"
            f"{', +samples' if header.get('samples') else ''})"
        )
    if walls:
        out.append(
            f"lines: {sum(kinds.values())} over {walls[0]}-{walls[-1]}: "
            + ", ".join(f"{kinds[k]} {k}" for k in ("steer", "node", "mesh", "event", "sample") if kinds.get(k))
        )
    else:
        out.append("lines: none in range")
    out.append("")

    # --- err ms per node
    stats = steer_stats(rows)
    facts = node_facts(rows)
    # Each restart with its time and, once the node says (telemetry slice 3),
    # which re-anchor rule fired and how far out it was.
    restarts_by: Dict[str, List[str]] = defaultdict(list)
    for r in events(rows, "restart"):
        said = _short_wall(r)
        if r.get("reason"):
            said += f" {r['reason']}"
        if r.get("errMs") is not None:
            said += f" {_f(r.get('errMs'), '+.0f')} ms"
        restarts_by[str(r.get("name") or r.get("node"))].append(said)
    out.append("ERR MS per node (steer lines)")
    out.append(
        f"{'node':<18}{'n':>6}{'mean':>9}{'sd':>8}{'min':>9}{'max':>9}"
        f"{'cross':>7}{'surv':>7}{'audio':>9}{'cred':>6}{'restarts':>10}"
    )
    names = sorted(set(stats) | set(facts) | set(restarts_by))
    for name in names:
        s = stats.get(name)
        f = facts.get(name, {})
        out.append(
            f"{name:<18}"
            f"{(s['n'] if s else 0):>6}"
            f"{_f(s['mean'] if s else None, '.2f'):>9}"
            f"{_f(s['sd'] if s else None, '.2f'):>8}"
            f"{_f(s['min'] if s else None, '.2f'):>9}"
            f"{_f(s['max'] if s else None, '.2f'):>9}"
            f"{(s['crossings'] if s else '-'):>7}"
            f"{(_f(f.get('survival'), '.0f') + '%') if f.get('survival') is not None else '-':>7}"
            f"{_f(f.get('audioPpm'), '+.0f'):>9}"
            f"{('yes' if f.get('audioCredible') else ('no' if f.get('audioPpm') is not None else '-')):>6}"
            f"{len(restarts_by.get(name, [])):>10}"
        )
    sp = spread(stats)
    if sp:
        out.append(
            f"fleet spread of means: {sp['ms']:.2f} ms = {sp['metres']:.2f} m of air "
            f"({sp['nodes']} nodes with >= {MIN_ACKS} acks)"
        )
    elif stats:
        out.append(f"fleet spread: needs two nodes with >= {MIN_ACKS} acks")
    out.append("")

    # --- starts
    out.append("STARTS")
    defers = events(rows, "defer") + events(rows, "straggler")
    defers.sort(key=lambda r: str(r.get("t")))
    if defers:
        for r in defers:
            out.append(
                f"  {_short_wall(r)}  {r.get('event')}: {', '.join(map(str, r.get('nodes') or []))}"
            )
    else:
        out.append("  no deferred nodes")
    catchups = events(rows, "catchup")
    if catchups:
        out.append("  catch-ups: " + "; ".join(
            f"{_short_wall(r)} {r.get('name')} after {_f(r.get('waitedS'), '.1f')} s"
            for r in catchups
        ))
    timeouts = events(rows, "catchup-timeout")
    if timeouts:
        out.append("  catch-up TIMEOUTS: " + "; ".join(
            f"{_short_wall(r)} {r.get('name')}" for r in timeouts
        ))
    if restarts_by:
        out.append("  restarts: " + "; ".join(
            f"{name} {len(ws)} ({', '.join(ws)})" for name, ws in sorted(restarts_by.items())
        ))
    else:
        out.append("  restarts: none")
    plays = events(rows, "play")
    if plays:
        out.append(f"  plays: {len(plays)}, first {_short_wall(plays[0])}, last {_short_wall(plays[-1])}")
    mir = mirrors(rows)
    if mir:
        out.append("  mirror pairs (one bad sample, two restarts): " + "; ".join(
            f"{m['wall']} {m['name']} {m['errMs']:+.0f} -> {m['nextMs']:+.0f}" for m in mir
        ))
    else:
        out.append("  mirror pairs: none")
    out.append("")

    # --- steps: what moved
    st = steps(rows)
    out.append(f"STEPS in err > {STEP_MS:.0f} ms between consecutive acks on one source ({len(st)}) - what moved, ms")
    if st:
        out.append(f"  {'wall':<9} {'node':<20} {'err from -> to':>18}  {'target':>7} {'nudge':>7} {'-map':>7} {'book':>7} {'rest':>7}")
        for d in st:
            out.append(
                f"  {d['wall']:<9} {d['name']:<20} {d['errFrom']:>+8.1f} -> {d['errTo']:>+7.1f}  "
                f"{_f(d['target'], '+.1f'):>7} {_f(d['nudge'], '+.1f'):>7} {_f(d['map'], '+.1f'):>7} "
                f"{_f(d['book'], '+.1f'):>7} {_f(d['rest'], '+.1f'):>7}"
            )
        out.append("  (a column carrying the step is the input that moved; `rest` is what this trace's columns cannot see)")
    out.append("")

    # --- mesh
    out.append("MESH closure |ms| per pair (best / worst, lines, last rtt)")
    pairs = mesh_pairs(rows)
    if pairs:
        for key, d in sorted(pairs.items()):
            cl = d["closures"]
            out.append(
                f"  {key:<34}{_f(min(cl) if cl else None, '.2f'):>7} / "
                f"{_f(max(cl) if cl else None, '.2f'):<7} n={d['n']:<5} rtt {_f(d['rtt'], '.1f')}"
            )
    else:
        out.append("  no mesh lines")
    out.append("")

    # --- warnings
    warns = [r for r in rows if r.get("kind") == "event" and r.get("level") == "warning"]
    out.append(f"WARNINGS ({len(warns)})")
    for r in warns:
        who = f"{r.get('name')}  " if r.get("name") else ""
        out.append(f"  {_short_wall(r)}  {who}{r.get('text')}")
    return "\n".join(out) + "\n"


def write_csv(rows: List[dict], path: Path) -> int:
    """The steer lines, one per row, for a spreadsheet. Returns the row count."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(STEER_COLUMNS)
        for r in rows:
            if r.get("kind") != "steer":
                continue
            w.writerow([r.get(c) for c in STEER_COLUMNS])
            n += 1
    return n


# --- cli ------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("trace", nargs="?", type=Path, help="a trace file [default: newest in --dir]")
    p.add_argument("--dir", type=Path, default=Path("logs"), help="where traces live [default: ./logs]")
    p.add_argument("--since", help="wall time HH:MM[:SS] to start from")
    p.add_argument("--until", help="wall time HH:MM[:SS] to stop at")
    p.add_argument("--node", help="only lines naming this node (substring, case-insensitive)")
    p.add_argument("--csv", type=Path, help="also write the steer lines to this CSV file")
    args = p.parse_args(argv)

    path = args.trace or newest_trace(args.dir)
    if path is None or not Path(path).is_file():
        print(f"no trace found ({args.trace or args.dir})", file=sys.stderr)
        return 2
    rows = select(load(path), args.since, args.until, args.node)
    # Node names are whatever their owners typed; a console that cannot spell
    # one should print a '?' there, not die on it.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(report(rows, source=str(path)))
    if args.csv:
        n = write_csv(rows, args.csv)
        print(f"\n{n} steer rows -> {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
