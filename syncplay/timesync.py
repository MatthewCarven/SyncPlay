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
    skew: float  # d(offset)/dt; the prior (or 0.0) until enough span to fit
    at: float  # conductor time the offset is anchored to
    best_rtt: float
    last_rtt: float
    n_samples: int  # samples currently in the window
    n_used: int  # samples that survived the RTT filter
    span: float  # conductor-time span covered by the used samples
    skew_fitted: bool = False  # True only if `skew` came from this window's fit
    # Widest round trip that survived the RTT filter, i.e. the worst sample the
    # offset was actually computed from. Read-only bookkeeping for `trust`.
    worst_rtt: float = 0.0
    # Worst-case error on `skew`, same units, 0.0 when no slope was fitted.
    # See `skew_credible_at` for what it is for.
    skew_bound: float = 0.0
    # True when the fit hit `max_skew` and had to be clamped. That is not a fast
    # crystal, it is a broken fit — the usual cause is a step in the offset
    # series (a phone suspending its AudioContext and waking again), and a
    # least-squares line through a step has an enormous slope. Recorded because
    # the credibility gate cannot see it: `skew_credible_at` rejects slopes too
    # *small* to separate from their own error bound, and a saturated slope is
    # the opposite failure — it dwarfs every bound and sails straight through.
    skew_saturated: bool = False

    @property
    def skew_ppm(self) -> float:
        return self.skew * 1e6

    @property
    def trust_s(self) -> float:
        """Worst-case error on this offset, in seconds — a certificate, not a guess.

        A ping measures the offset with an error equal to the *asymmetry* of the
        path, and asymmetry is bounded by the round trip: |d_out − d_ret| <= rtt,
        so any single sample carries |offset error| <= rtt/2. The estimate is a
        median (or a fit) over every sample that survived the filter, so it lies
        between them and inherits the bound of the *widest* one admitted — not
        the tightest. Hence worst_rtt, not best_rtt: `best_rtt/2` is the floor
        this node could reach if the filter admitted nothing else, and quoting it
        as the answer would flatter a node whose filter gate is wide open.

        Bounds the offset at `at`. Projecting it forward by `skew` adds error
        this number does not attempt to model.
        """
        return self.worst_rtt / 2.0

    @property
    def trust_ms(self) -> float:
        return self.trust_s * 1000.0

    @property
    def skew_bound_ppm(self) -> float:
        return self.skew_bound * 1e6

    def skew_credible_at(self, ratio: float = 2.0) -> bool:
        """Could this slope be an artefact of measurement error alone?

        The same certificate that bounds the offset bounds the *slope* fitted
        through a window of them. Perturbing each offset by e_i moves the
        least-squares slope by `sum((t_i - t_mean) * e_i) / sum((t_i - t_mean)^2)`,
        and every |e_i| is bounded by rtt_i/2 — so the worst case an adversary
        could manufacture is `skew_bound`, computed exactly from the same sums
        the fit already needs.

        This matters because the dangerous bad fit is not a noisy one. A device
        being *carried across the room* produces a beautifully clean trend: its
        path asymmetry changes steadily, the offsets follow, and R^2 comes out
        high. Residual-based quality checks wave that through. But a walk cannot
        move the offset further than asymmetry allows, so an apparent drift that
        exceeds `ratio x skew_bound` is one that measurement error *cannot*
        fake — it has to be the crystal. That is the property worth testing
        before believing a slope, and it catches every impostor, not just the
        moving kind.

        A perfect (rtt 0) window has `skew_bound == 0` and certifies any slope,
        which is exactly right: with no measurement error there is nothing to
        explain the trend away.
        """
        if not self.skew_fitted:
            return False
        return abs(self.skew) >= ratio * self.skew_bound

    @property
    def floor_ms(self) -> float:
        """The tightest certificate any surviving sample carries, in ms. The gap
        between this and `trust_ms` is the price of the filter's tolerance."""
        return self.best_rtt * 1000.0 / 2.0

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

    ``prior_skew`` seeds the drift estimate for a device we have measured
    before. It is used only until this window can fit a slope of its own; from
    then on live data wins and ``ClockEstimate.skew_fitted`` goes True.
    """

    def __init__(
        self,
        window: float = 600.0,
        min_slope_samples: int = 8,
        min_slope_span: float = 30.0,
        max_skew: float = 500e-6,
        base_tolerance: float = 0.002,
        rel_tolerance: float = 0.25,
        prior_skew: Optional[float] = None,
    ) -> None:
        self.window = window
        self.min_slope_samples = min_slope_samples
        self.min_slope_span = min_slope_span
        self.max_skew = max_skew
        self.base_tolerance = base_tolerance
        self.rel_tolerance = rel_tolerance
        # A skew carried over from a previous session with this same device.
        # Offset cannot survive a reconnect (the node's clock epoch resets) but
        # skew is a property of its crystal, not of the session — so a returning
        # node need not spend `min_slope_span` seconds pretending it has none.
        self.prior_skew = (
            None if prior_skew is None
            else max(-max_skew, min(max_skew, float(prior_skew)))
        )
        self._samples: Deque[PingSample] = deque()
        self._latest_mid = float("-inf")
        self._cache: Optional[ClockEstimate] = None

    def __len__(self) -> int:
        return len(self._samples)

    def forget_prior(self) -> None:
        """Drop an inherited drift this window has not confirmed for itself.

        A no-op once the window fits its own slope — the fit wins over the prior
        anyway — so this only bites in the one case it is for: a node still
        coasting on a remembered number that turned out to be wrong.
        """
        if self.prior_skew is None:
            return
        self.prior_skew = None
        self._cache = None  # the cached estimate may have been built on it

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
        fitted = False
        slope_bound = 0.0
        saturated = False
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
                # Clamping keeps it from poisoning today's timing; `saturated`
                # is what stops it being carried into tomorrow's.
                saturated = abs(slope) >= self.max_skew
                slope = max(-self.max_skew, min(self.max_skew, slope))
                fitted = True
                # Worst-case slope error given each sample's own rtt/2 bound:
                # the adversary aligns every error with (t - t_mean). Recorded,
                # never acted on here — the live model uses the slope either
                # way; only what gets *persisted* is gated on it.
                slope_bound = sum(
                    abs(t - t_mean) * s.rtt / 2.0 for t, s in zip(times, used)
                ) / var
            anchor_t, anchor_y = t_mean, y_mean
        else:
            # Not enough span to trust a slope of our own. Fall back to the prior
            # if this device left us one, and de-trend the window by it so the
            # anchor is a clean offset at `latest_mid` rather than a smear. With
            # no prior this is exactly the old behaviour: flat median offset.
            slope = self.prior_skew or 0.0
            anchor_t = self._latest_mid
            anchor_y = median(s.offset - slope * (s.midpoint - anchor_t) for s in used)

        self._cache = ClockEstimate(
            offset=anchor_y,
            skew=slope,
            at=anchor_t,
            best_rtt=min(s.rtt for s in used),
            worst_rtt=max(s.rtt for s in used),
            last_rtt=self._samples[-1].rtt,
            n_samples=len(self._samples),
            n_used=n,
            span=span,
            skew_fitted=fitted,
            skew_bound=slope_bound,
            skew_saturated=saturated,
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
