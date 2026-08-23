"""The arithmetic that decides how the room sounds — tested without a room.

`plan_nudges` is deliberately pure, so every judgement it makes (which reps to
believe, how to combine them, which speaker sets the pace) can be pinned here.
The acoustic loop still needs real hardware; this file exists so that when the
hardware finally disagrees with us, we know it isn't the arithmetic.
"""

import pytest

from syncplay.conductor import CAL_MIN_PEAK, MAX_NUDGE_MS, plan_nudges


def probe(tof, peak=0.9, snr=20.0):
    return {"tofMs": tof, "peak": peak, "snr": snr}


def by_id(plan):
    return {r["id"]: r for r in plan}


def test_aligns_every_speaker_to_the_latest_arrival():
    """The furthest speaker sets the pace and gets 0; everyone else is delayed
    to match it. No proposal may be negative — you cannot un-delay sound."""
    plan = by_id(plan_nudges({
        "near": [probe(10.0)] * 3,
        "far": [probe(35.0)] * 3,
        "mid": [probe(20.0)] * 3,
    }))
    assert plan["far"]["proposedMs"] == 0.0
    assert plan["mid"]["proposedMs"] == 15.0
    assert plan["near"]["proposedMs"] == 25.0
    assert all(r["proposedMs"] >= 0 for r in plan.values())


def test_median_ignores_a_single_wild_rep():
    """One mis-picked correlation peak (a reflection, a slammed door) must not
    move the answer. This is the entire reason for taking reps."""
    plan = by_id(plan_nudges({
        "a": [probe(10.0), probe(410.0), probe(10.4)],   # middle rep is nonsense
        "b": [probe(30.0), probe(30.0), probe(30.0)],
    }))
    assert plan["a"]["tofMs"] == pytest.approx(10.4)     # not the ~143 ms mean
    assert plan["a"]["proposedMs"] == pytest.approx(19.6)
    assert plan["a"]["spreadMs"] == pytest.approx(400.0)  # ...but the spread confesses


def test_reps_below_the_peak_gate_are_discarded():
    weak = probe(99.0, peak=CAL_MIN_PEAK - 0.01)
    plan = by_id(plan_nudges({
        "a": [probe(10.0), probe(10.0), weak],
        "b": [probe(25.0), probe(25.0), probe(25.0)],
    }))
    assert plan["a"]["nGood"] == 2 and plan["a"]["nTotal"] == 3
    assert plan["a"]["tofMs"] == pytest.approx(10.0)     # the weak rep is gone
    assert plan["a"]["proposedMs"] == pytest.approx(15.0)


def test_a_speaker_we_could_not_hear_is_excluded_not_guessed():
    """A speaker with too few clean reps must not receive a proposal *and* must
    not drag the target — otherwise one deaf measurement re-times the room."""
    plan = by_id(plan_nudges({
        "heard": [probe(10.0)] * 3,
        "loud": [probe(30.0)] * 3,
        "silent": [probe(500.0, peak=0.01)] * 3,
    }))
    assert plan["silent"]["proposedMs"] is None
    assert plan["silent"]["note"] == "no usable capture"
    # target came from 'loud' (30), not the 500 ms phantom
    assert plan["heard"]["proposedMs"] == pytest.approx(20.0)


def test_one_clean_rep_is_not_enough():
    plan = by_id(plan_nudges({
        "a": [probe(10.0), probe(10.0, peak=0.0), probe(10.0, peak=0.0)],
        "b": [probe(30.0)] * 3,
    }))
    assert plan["a"]["proposedMs"] is None
    assert "only 1 clean rep" in plan["a"]["note"]


def test_implausible_spacing_is_flagged_rather_than_clamped():
    """>500 ms apart is ~170 m of air. Far likelier a bad peak than a real room,
    so it must say so — a silent clamp would look like a valid calibration."""
    plan = by_id(plan_nudges({
        "a": [probe(10.0)] * 3,
        "b": [probe(10.0 + MAX_NUDGE_MS + 50.0)] * 3,
    }))
    assert plan["a"]["proposedMs"] is None
    assert "implausible" in plan["a"]["note"]
    assert plan["b"]["proposedMs"] == 0.0     # the reference itself is fine


def test_single_speaker_gets_a_readout_and_an_honest_note():
    plan = by_id(plan_nudges({"only": [probe(12.0)] * 3}))
    assert plan["only"]["proposedMs"] == 0.0
    assert "nothing to align against" in plan["only"]["note"]


def test_no_usable_data_at_all_proposes_nothing():
    plan = plan_nudges({"a": [], "b": [probe(5.0, peak=0.0)]})
    assert all(r["proposedMs"] is None for r in plan)
    assert all(r["tofMs"] is None or r["note"] for r in plan)


def test_constant_mic_latency_cancels_out():
    """The ToF carries the mic's own input latency. It's constant across
    speakers, so adding it to every reading must not change any proposal."""
    base = {"a": [probe(10.0)] * 3, "b": [probe(30.0)] * 3, "c": [probe(21.0)] * 3}
    shifted = {k: [probe(p["tofMs"] + 137.0) for p in v] for k, v in base.items()}
    assert ([r["proposedMs"] for r in plan_nudges(base)]
            == [r["proposedMs"] for r in plan_nudges(shifted)])


def test_spread_is_reported_per_speaker():
    plan = by_id(plan_nudges({
        "tight": [probe(10.0), probe(10.1), probe(10.2)],
        "loose": [probe(20.0), probe(24.0), probe(28.0)],
    }))
    assert plan["tight"]["spreadMs"] == pytest.approx(0.2)
    assert plan["loose"]["spreadMs"] == pytest.approx(8.0)


# --- telling three different failures apart ---------------------------------


def probe_at(tof, peak, rms_db=None, clip_pct=None):
    p = {"tofMs": tof, "peak": peak, "snr": 2.0}
    if rms_db is not None:
        p["rmsDb"] = rms_db
    if clip_pct is not None:
        p["clipPct"] = clip_pct
    return p


def test_a_dead_input_is_named_as_one_rather_than_blamed_on_the_room():
    """July's failure, and the reason this exists: peak 0.02 against a mic at
    -74 dBFS. "no usable capture" is true and useless — the fault was Windows."""
    plan = by_id(plan_nudges({
        "a": [probe_at(0.0, 0.02, rms_db=-74.0)] * 3,
        "b": [probe_at(0.0, 0.03, rms_db=-76.0)] * 3,
    }))
    assert "mic heard nothing" in plan["a"]["note"]
    assert "-74 dBFS" in plan["a"]["note"]
    assert "check the input" in plan["a"]["note"]
    assert plan["a"]["proposedMs"] is None


def test_a_clipping_input_is_named_too():
    """The other end of the same axis: loud enough to destroy the correlation."""
    plan = by_id(plan_nudges({
        "a": [probe_at(0.0, 0.05, rms_db=-3.0, clip_pct=14.0)] * 3,
    }))
    assert "clipping" in plan["a"]["note"]
    assert "turn the mic gain down" in plan["a"]["note"]


def test_a_live_mic_that_simply_missed_keeps_the_old_wording():
    """Healthy level, no correlation: this one really is "we didn't hear it",
    and the fix is in the room — placement, volume, a door left open."""
    plan = by_id(plan_nudges({
        "a": [probe_at(0.0, 0.04, rms_db=-32.0, clip_pct=0.0)] * 3,
    }))
    assert plan["a"]["note"] == "no usable capture"


def test_level_is_reported_even_when_the_measurement_succeeds():
    """It is a diagnostic on every row, not just a failure message — a sweep
    that works at -55 dBFS is one bad evening away from not working."""
    plan = by_id(plan_nudges({
        "near": [probe_at(10.0, 0.8, rms_db=-30.0, clip_pct=0.0)] * 3,
        "far": [probe_at(35.0, 0.7, rms_db=-55.0, clip_pct=0.0)] * 3,
    }))
    assert plan["near"]["rmsDb"] == -30.0
    assert plan["far"]["rmsDb"] == -55.0
    assert plan["near"]["proposedMs"] == 25.0      # still proposes as before


def test_level_never_decides_whether_a_reading_is_accepted():
    """Diagnostics only. A quiet capture that correlates cleanly is still a
    measurement — the gate is `peak`, and this slice must not have moved it."""
    quiet = by_id(plan_nudges({
        "near": [probe_at(10.0, 0.9, rms_db=-70.0)] * 3,
        "far": [probe_at(35.0, 0.9, rms_db=-70.0)] * 3,
    }))
    assert quiet["near"]["proposedMs"] == 25.0
    assert quiet["near"]["note"] == ""


def test_rows_without_level_data_behave_exactly_as_before():
    """An older player page reports no level. It must not become an error."""
    plan = by_id(plan_nudges({"silent": [probe_at(0.0, 0.01)] * 3}))
    assert plan["silent"]["note"] == "no usable capture"
    assert plan["silent"]["rmsDb"] is None


# --- the level arrives over a socket, so it is untrusted input ---------------


@pytest.mark.parametrize("raw,want", [
    (-74.0, -74.0), ("-30.5", -30.5), (0.0, 0.0),
    (12.0, 0.0),            # above full scale is impossible; clamp, don't reject
    (-999.0, -120.0),       # floor
    (None, None), ("", None), ("abc", None), ([], None),
    (float("nan"), None), (float("inf"), None),
])
def test_clean_db_bounds_whatever_the_client_sends(raw, want):
    from syncplay.conductor import _clean_db

    got = _clean_db(raw)
    assert got is None if want is None else got == pytest.approx(want)


@pytest.mark.parametrize("raw,want", [
    (0.0, 0.0), (14.0, 14.0), (250.0, 100.0), (-5.0, 0.0),
    (None, None), ("nope", None), (float("nan"), None),
])
def test_clean_pct_bounds_whatever_the_client_sends(raw, want):
    from syncplay.conductor import _clean_pct

    got = _clean_pct(raw)
    assert got is None if want is None else got == pytest.approx(want)
