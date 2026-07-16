"""CLI entry point: python -m syncplay --music-dir <path> [--port 8927]"""

from __future__ import annotations

import argparse
import logging
import socket
from pathlib import Path

from aiohttp import web

from .conductor import build_app


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

    app = build_app(music_dir)
    ip = lan_ip()
    banner = (
        f"\n  SyncPlay conductor up.\n"
        f"    players - open on every device:  http://{ip}:{args.port}/\n"
        f"    control - your dashboard:        http://{ip}:{args.port}/control\n"
    )
    web.run_app(app, host=args.host, port=args.port, print=lambda *_: print(banner))


if __name__ == "__main__":
    main()
