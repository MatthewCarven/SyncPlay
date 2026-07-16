"""Milestone 1 proof: recover known offset + skew from a synthetic noisy network.

A SimNode is a fake device whose clock runs at (1 + skew) × conductor time
plus a constant offset, reached over a network with exponential jitter.
If the math can't pull the truth back out of that, nothing downstream works.
"""

import random

import pytest

from syncplay.timesync import ClockModel, PingSample, filter_best


class SimNode:
    """Simulated player node behind a jittery network."""

    def __init__(
        self,
        offset0: float,
        skew_ppm: float,
        base_delay: float = 0.004,
        jitter_mean: float = 0.003,
        proc: float = 0.0004,
        seed: int = 42,
    ):
        self.offset0 = offset0
        self.skew = skew_ppm * 1e-6
        self.base_delay = base_delay
        self.jitter_mean = jitter_mean
        self.proc = proc
        self.rng = random.Random(seed)

    def node_clock(self, t: float) -> float:
        """The node's monotonic clock as a function of conductor time."""
        return (1.0 + self.skew) * t + self.offset0

    def true_offset(self, t: float) -> float:
        return self.node_clock(t) - t

    def exchange(self, t0: float) -> PingSample:
        """One ping/pong across the jittery network, conductor-initiated at t0."""
        d1 = self.base_delay + self.rng.expovariate(1.0 / self.jitter_mean)
        d2 = self.base_delay + self.rng.expovariate(1.0 / self.jitter_mean)
        arrive = t0 + d1
        c1 = self.node_clock(arrive)
        c2 = c1 + self.proc
        t3 = arrive + self.proc + d2
        return PingSample(t0=t0, c1=c1, c2=c2, t3=t3)

    def run_bursts(
        self,
        model: ClockModel,
        start: float,
        duration: float,
        burst_every: float = 5.0,
        burst_size: int = 8,
        gap: float = 0.08,
    ) -> float:
        """Feed the model bursts like the conductor would. Returns end time."""
        t = start
        end = start + duration
        while t < end:
            for i in range(burst_size):
                model.add(self.exchange(t + i * gap))
            t += burst_every
        return end


# --- PingSample arithmetic -------------------------------------------------


def test_pingsample_hand_computed():
    # True offset 2.5 s, outbound 5 ms, processing 1 ms, return 7 ms.
    s = PingSample(t0=100.0, c1=102.505, c2=102.506, t3=100.013)
    assert s.rtt == pytest.approx(0.012)
    # Asymmetry error is (d1 − d2)/2 = −1 ms → estimate 2.499.
    assert s.offset == pytest.approx(2.499)
    assert s.midpoint == pytest.approx(100.0065)


def test_symmetric_delay_recovers_offset_exactly():
    node = SimNode(offset0=2.5, skew_ppm=0.0, jitter_mean=1e-12)
    s = node.exchange(100.0)
    assert s.offset == pytest.approx(2.5, abs=1e-9)


def test_negative_rtt_sample_dropped():
    model = ClockModel()
    # Node claims 0.5 s of processing inside a 0.1 s round trip → rtt = −0.4.
    model.add(PingSample(t0=100.0, c1=200.0, c2=200.5, t3=100.1))
    assert len(model) == 0
    assert model.estimate() is None


# --- Jitter filtering ------------------------------------------------------


def test_filter_best_drops_spikes():
    node = SimNode(offset0=1.0, skew_ppm=0.0, seed=7)
    samples = [node.exchange(100.0 + i * 0.08) for i in range(40)]
    kept = filter_best(samples)
    assert 0 < len(kept) < len(samples)
    worst_kept = max(s.rtt for s in kept)
    best = min(s.rtt for s in samples)
    assert worst_kept <= best + 0.002 + 0.25 * best


def test_offset_recovery_through_jitter():
    node = SimNode(offset0=-3.7, skew_ppm=0.0, seed=3)
    model = ClockModel()
    for i in range(60):
        model.add(node.exchange(100.0 + i * 0.08))
    est = model.estimate()
    assert est is not None
    # Raw single-sample error can be several ms; filtered+averaged must be sub-ms.
    assert est.offset == pytest.approx(-3.7, abs=1e-3)


# --- Drift (skew) recovery -------------------------------------------------


def test_skew_recovery_50ppm():
    node = SimNode(offset0=1.0, skew_ppm=50.0, seed=11)
    model = ClockModel(window=600.0)
    end = node.run_bursts(model, start=1000.0, duration=600.0)
    est = model.estimate()
    assert est is not None
    assert est.skew_ppm == pytest.approx(50.0, abs=5.0)
    # Offset at the anchor should match the true line there.
    assert est.offset_at(end) == pytest.approx(node.true_offset(end), abs=1.5e-3)


def test_skew_zero_when_span_too_short():
    node = SimNode(offset0=0.5, skew_ppm=80.0, seed=5)
    model = ClockModel(min_slope_span=30.0)
    for i in range(20):  # only ~1.6 s of span — refuse to fit a slope
        model.add(node.exchange(100.0 + i * 0.08))
    est = model.estimate()
    assert est is not None
    assert est.skew == 0.0


def test_absurd_slope_is_clamped():
    model = ClockModel(min_slope_samples=2, min_slope_span=0.5, max_skew=500e-6)
    # Two clean samples faking a 10000 ppm "drift".
    for t, off in [(100.0, 1.0), (200.0, 2.0)]:
        model.add(PingSample(t0=t, c1=t + off, c2=t + off, t3=t))
    est = model.estimate()
    assert est is not None
    assert abs(est.skew) <= 500e-6


# --- Projection: the part the conductor actually uses ----------------------


def test_start_time_projection_two_minutes_out():
    node = SimNode(offset0=0.25, skew_ppm=-40.0, seed=13)
    model = ClockModel(window=600.0)
    end = node.run_bursts(model, start=500.0, duration=300.0)
    target = end + 120.0  # schedule a song start two minutes in the future
    predicted = model.to_node_time(target)
    truth = node.node_clock(target)
    # 5 ppm slope error × 120 s ≈ 0.6 ms; allow 2 ms total.
    assert predicted == pytest.approx(truth, abs=2e-3)


def test_conductor_time_roundtrip_is_exact():
    node = SimNode(offset0=5.0, skew_ppm=75.0, seed=17)
    model = ClockModel()
    node.run_bursts(model, start=0.0, duration=120.0)
    t = 500.0
    assert model.to_conductor_time(model.to_node_time(t)) == pytest.approx(
        t, abs=1e-9
    )


def test_model_raises_before_any_samples():
    model = ClockModel()
    with pytest.raises(ValueError):
        model.to_node_time(100.0)


# --- Windowing -------------------------------------------------------------


def test_window_pruning_discards_old_samples():
    node = SimNode(offset0=1.0, skew_ppm=20.0, seed=23)
    model = ClockModel(window=10.0)
    node.run_bursts(model, start=0.0, duration=100.0, burst_every=2.0, burst_size=4)
    horizon = max(s.midpoint for s in model._samples)
    assert all(s.midpoint >= horizon - 10.0 for s in model._samples)
    assert len(model) <= 4 * 6  # at most ~10s/2s bursts + slack
