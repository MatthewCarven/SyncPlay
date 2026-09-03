"""A trace that outlives the process: JSON lines, one file per conductor start.

The conductor's log dies with its console window and the control page's
events ring dies with the conductor. This is the copy that does neither:
every event, every steer ack, a stats line per node and a closure line per
mesh pair every ten seconds, and — opt-in — the raw four-timestamp exchanges
themselves, so a different RTT filter can be argued offline by replaying real
pongs instead of by a live experiment.

Two rules, from the plan this came from:

* **It must not perturb the measurement.** Nothing here asks a node for
  anything; every line rides a message that already flows.
* **It must never be the reason nobody can play.** `write` appends a string
  to a list and returns. One task drains the list to disk every few seconds
  with a single write and flush. Any `OSError` — a full disk, a closed file, a
  directory that vanished — is logged once and turns the trace off for the
  rest of the run. Nothing in a message handler ever awaits the disk.

Line shape: `{"t": <conductor seconds>, "wall": "HH:MM:SS.mmm", "kind": ...,
...}`. `t` is the same clock as every number in the snapshot; `wall` is for
humans, and for lining a line up against what was heard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("syncplay")

FLUSH_S = 2.0  # how often the buffer is drained to disk


def wall_clock() -> str:
    """Wall-clock HH:MM:SS.mmm — the same clock the log stamps."""
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int(t % 1 * 1000)


def trace_path(trace_dir: Path, at: Optional[float] = None) -> Path:
    """`<dir>/trace-YYYYMMDD-HHMMSS.jsonl`: the name says when."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(at))
    return Path(trace_dir) / f"trace-{stamp}.jsonl"


def _plain(o):
    """json.dumps fallback: anything exotic becomes its str()."""
    return str(o)


class Trace:
    def __init__(
        self, path: Path, *, samples: bool = False,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.path = Path(path)
        self.samples = samples  # the opt-in raw-pong tier
        self._clock = clock
        self._buf: List[str] = []
        self._fh = None
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self.failed: Optional[str] = None  # why it turned itself off, once set
        self.lines = 0  # written to disk so far

    @property
    def enabled(self) -> bool:
        return self.failed is None and not self._closed

    def write(self, kind: str, **fields) -> None:
        """Queue one line. Never blocks, never raises.

        `fields` may carry their own `t` and `wall` (an event row does); they
        win, so the ring and the trace stamp the same instant.
        """
        if not self.enabled:
            return
        row = {"t": self._clock(), "wall": wall_clock(), "kind": kind}
        row.update(fields)
        try:
            self._buf.append(json.dumps(row, default=_plain, separators=(",", ":")))
        except (TypeError, ValueError) as e:  # a field json cannot spell at all
            log.warning("trace: dropped a %s line: %s", kind, e)

    async def start(self, _app=None) -> None:
        """Open the file and start the drain. An aiohttp on_startup handler."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
        except OSError as e:
            self._fail(e)
            return
        self._task = asyncio.create_task(self._run())
        log.info("trace: writing %s%s", self.path, " (+samples)" if self.samples else "")

    async def stop(self, _app=None) -> None:
        """Drain what is left and close. An aiohttp on_cleanup handler."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.flush()
        self._closed = True
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_S)
            self.flush()

    def flush(self) -> None:
        """Drain the buffer to disk: one write, one flush. Lines queued before
        the file is open wait for it; nothing is lost across startup."""
        if not self._buf or self._fh is None:
            return
        chunk, self._buf = self._buf, []
        try:
            self._fh.write("\n".join(chunk) + "\n")
            self._fh.flush()
            self.lines += len(chunk)
        except (OSError, ValueError) as e:  # ValueError: the file was closed
            self._fail(e)

    def _fail(self, err: BaseException) -> None:
        self.failed = f"{type(err).__name__}: {err}"
        self._buf.clear()
        log.warning("trace: off for the rest of this run - %s (%s)", self.failed, self.path)
