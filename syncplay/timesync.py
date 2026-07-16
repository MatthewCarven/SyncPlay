"""Clock synchronization math for SyncPlay.

Pure functions and a per-node ClockModel. Standard library only, no I/O —
this module is deliberately extractable as its own tiny library.

Conventions
-----------
- All times are floats in **seconds** on some monotonic clock.
- "Conductor time" is the reference clock (``time.perf_counter()`` on the
  server). There is no NTP/UTC anywhere: every node syncs *to the conductor*.
- "Node time" is a player's own monotonic clock (``performance.now()/1000``
  in a browser).
- ``offset`` = node_time − conductor_time. Add it to a conductor time to get
  the same moment on the node's clock.
- ``skew`` = d(offset)/dt, dimensionless. ``skew * 1e6`` is ppm. Consumer
  crystals typically disagree by 10–100 ppm (≈ 6–24 ms over a 4-minute song),
  which is why drift matters and not just offset.

The measurement is NTP's four-timestamp exchange; the jitter defense is
min-RTT filtering (samples with the smallest round trip suffered the least
queueing delay, so their symmetric-path assumption is the least wrong);
the drift estimate is a least-squares slope of filtered offsets over time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Deque, Iterable, List, Optional

__all__ = ["PingSample", "ClockEstimate", "ClockModel", "filter_best"]


@dataclass(frozen=True)
class PingSample:
    """One four-timestamp ping/pong exchange (all values in seconds).

    t0: conductor clock when the ping left the conductor
    c1: node clock when the ping arrived at the node
    c2: node clock when the pong left the node
    t3: conductor clock when the pong arrived back at the conductor
    """

    t0: float
    c1: float
    c2: float
    t3: float

    @property
    def offset(self) -> float:
        """Estimated node_clock − conductor_clock, assuming symmetric path delay.

        The asymmetry error is (outbound_delay − return_delay) / 2, which is
        why low-RTT samples (least queueing) give the best estimates.
        """
        return ((self.c1 - self.t0) + (self.c2 - self.t3)) / 2.0

    @property
    def rtt(self) -> float:
        """Network round-trip time with the node's processing time excluded."""
        return (self.t3 - self.t0) - (self.c2 - self.c1)

    @property
    def midpoint(self) -> float:
        """Conductor-clock moment this offset estimate refers to."""
        return (self.t0 + self.t3) / 2.0


def filter_best(
    samples: Iterable[PingSample],
    base_tolerance: float = 0.002,
    rel_tolerance: float = 0.25,
) -> List[PingSample]:
    """Keep only samples whose RTT is close to the best RTT seen.

    "Close" = within ``base_tolerance`` seconds plus ``rel_tolerance`` × best.
    On a quiet LAN this keeps nearly everything; on spiky Wi-Fi it drops the
    queue-delayed outliers that would otherwise smear the offset estimate.
    """
    pool = [s for s in samples if s.rtt >= 0.0]
    if not pool:
        return []
    best = min(s.rtt for s in pool)
    cutoff = best + base_tolerance + rel_tolerance * best
    return [s for s in pool if s.rtt <= cutoff]


@dataclass(frozen=True)
class ClockEstimate:
    """Snapshot of a node's clock relationship to the conductor."""

    offset: float  # seconds, node − conductor, valid at conductor time `at`
    skew: float  # d(offset)/dt; 0.0 until enough span to trust a slope
    at: float  # conductor time the offset is anchored to
    best_rtt: float
    last_rtt: float
    n_samples: int  # samples currently in the window
    n_used: int  # samples that survived the RTT filter
    span: float  # conductor-time span covered by the used samples

    @property
    def skew_ppm(self) -> float:
        return self.skew * 1e6

    def offset_at(self, t: float) -> float:
        """Projected offset at conductor time ``t`` (drift-compensated)."""
        return self.offset + self.skew * (t - self.at)

    def to_node_time(self, t: float) -> float:
        """Conductor time → the same moment on the node's clock."""
        return t + self.offset_at(t)

    def to_conductor_time(self, node_t: float) -> float:
        """Node time → the same moment on the conductor's clock (exact inverse)."""
        # node_t = t + offset + skew*(t − at)  ⇒  solve for t
        return (node_t - self.offset + self.skew * self.at) / (1.0 + self.skew)


class ClockModel:
    """Sliding-window offset + drift model for one node.

    Feed it every PingSample as it arrives (bursts are just a cadence choice
    upstream — the model filters by RTT across its whole window). Ask it for
    an estimate whenever you need to schedule something.
    """

    def __init__(
        self,
        window: float = 600.0,
        min_slope_samples: int = 8,
        min_slope_span: float = 30.0,
        max_skew: float = 500e-6,
        base_tolerance: float = 0.002,
        rel_tolerance: float = 0.25,
    ) -> None:
        self.window = window
        self.min_slope_samples = min_slope_samples
        self.min_slope_span = min_slope_span
        self.max_skew = max_skew
        self.base_tolerance = base_tolerance
        self.rel_tolerance = rel_tolerance
        self._samples: Deque[PingSample] = deque()
        self._latest_mid = float("-inf")
        self._cache: Optional[ClockEstimate] = None

    def __len__(self) -> int:
        return len(self._samples)

    def add(self, sample: PingSample) -> None:
        """Ingest one exchange. Corrupt samples (negative RTT) are dropped."""
        if sample.rtt < 0.0:
            return
        self._samples.append(sample)
        self._latest_mid = max(self._latest_mid, sample.midpoint)
        horizon = self._latest_mid - self.window
        while self._samples and self._samples[0].midpoint < horizon:
            self._samples.popleft()
        self._cache = None

    def estimate(self) -> Optional[ClockEstimate]:
        """Current best (offset, skew) — or None if no usable samples yet."""
        if self._cache is not None:
            return self._cache
        if not self._samples:
            return None

        used = filter_best(self._samples, self.base_tolerance, self.rel_tolerance)
        if not used:
            return None

        times = [s.midpoint for s in used]
        offsets = [s.offset for s in used]
        span = max(times) - min(times)
        n = len(used)

        slope = 0.0
        if n >= self.min_slope_samples and span >= self.min_slope_span:
            # Least squares, centered for numerical stability.
            t_mean = sum(times) / n
            y_mean = sum(offsets) / n
            var = sum((t - t_mean) ** 2 for t in times)
            if var > 0.0:
                cov = sum(
                    (t - t_mean) * (y - y_mean) for t, y in zip(times, offsets)
                )
                slope = cov / var
                # A "drift" beyond ±max_skew is a broken fit, not a real crystal.
                slope = max(-self.max_skew, min(self.max_skew, slope))
            anchor_t, anchor_y = t_mean, y_mean
        else:
            # Not enough span to trust a slope: robust flat offset instead.
            anchor_t, anchor_y = self._latest_mid, median(offsets)

        self._cache = ClockEstimate(
            offset=anchor_y,
            skew=slope,
            at=anchor_t,
            best_rtt=min(s.rtt for s in used),
            last_rtt=self._samples[-1].rtt,
            n_samples=len(self._samples),
            n_used=n,
            span=span,
        )
        return self._cache

    # Convenience passthroughs (raise if no data yet — callers check first).

    def offset_at(self, t: float) -> float:
        est = self.estimate()
        if est is None:
            raise ValueError("no samples in clock model yet")
        return est.offset_at(t)

    def to_node_time(self, t: float) -> float:
        est = self.estimate()
        if est is None:
            raise ValueError("no samples in clock model yet")
        return est.to_node_time(t)

    def to_conductor_time(self, node_t: float) -> float:
        est = self.estimate()
        if est is None:
            raise ValueError("no samples in clock model yet")
        return est.to_conductor_time(node_t)
