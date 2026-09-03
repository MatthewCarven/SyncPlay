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
    "trustMs", "skewPpm", "nUsed", "lastRttMs",
]


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
    restarts_by: Dict[str, List[str]] = defaultdict(list)
    for r in events(rows, "restart"):
        restarts_by[str(r.get("name") or r.get("node"))].append(_short_wall(r))
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
