"""CLI entry point: python -m syncplay --music-dir <path> [--port 8927]"""

from __future__ import annotations

import argparse
import logging
import socket
from pathlib import Path

from aiohttp import web

from .conductor import build_app
from .trace import Trace, trace_path


def lan_ip() -> str:
    """Best-guess LAN address (no packets are actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="syncplay",
        description="Conductor for multi-node synchronized audio playback.",
    )
    parser.add_argument(
        "--music-dir", type=Path, default=Path("music"),
        help="folder scanned (recursively) for audio files [default: ./music]",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8927)
    parser.add_argument("-v", "--verbose", action="store_true")
    # The trace is on by default because the whole point of one is that it
    # exists on the evening nobody planned to measure.
    parser.add_argument(
        "--no-trace", action="store_true",
        help="do not write the JSONL trace (default: logs/trace-<stamp>.jsonl, "
             "one file per start; ~3 MB an hour for five nodes)",
    )
    parser.add_argument(
        "--trace-dir", type=Path, default=Path("logs"),
        help="where the trace goes [default: ./logs]",
    )
    parser.add_argument(
        "--trace-samples", action="store_true",
        help="also trace every raw ping exchange (about 4x the size; lets a "
             "different RTT filter be tried offline against real pongs)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    music_dir = args.music_dir.resolve()
    if not music_dir.is_dir():
        logging.warning("music dir %s does not exist yet — starting with an empty "
                        "library (drop files in and hit Rescan on the control page)",
                        music_dir)

    trace = None if args.no_trace else Trace(
        trace_path(args.trace_dir), samples=args.trace_samples
    )
    app = build_app(music_dir, trace=trace)
    ip = lan_ip()
    banner = (
        f"\n  SyncPlay conductor up.\n"
        f"    players - open on every device:  http://{ip}:{args.port}/\n"
        f"    control - your dashboard:        http://{ip}:{args.port}/control\n"
        + (
            f"    trace   - the evening, on disk:  {trace.path}"
            f"{' (+samples)' if trace.samples else ''}\n"
            if trace is not None else
            "    trace   - off (--no-trace)\n"
        )
    )
    web.run_app(app, host=args.host, port=args.port, print=lambda *_: print(banner))


if __name__ == "__main__":
    main()
