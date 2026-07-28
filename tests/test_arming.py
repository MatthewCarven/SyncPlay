"""When a start is cold enough to deserve a countdown.

`plan_start` is the whole decision, kept pure so the rule can be pinned without
a fleet, a network or a 70 MB decode. The rule is a conjunction, and both halves
matter: a countdown is only honest when the room can perceive the wait, and the
wait only exists when something still has to be fetched.
"""

import pytest

from syncplay.conductor import (
    ARM_SECONDS,
    LOAD_GATE_COLD,
    LOAD_GATE_TIMEOUT,
    plan_start,
)


# --- the cold case: the only one that arms ---------------------------------


def test_dead_start_with_nothing_loaded_arms():
    arm, gate = plan_start(playing_now=False, loaded=[False, False, False])
    assert arm is True
    assert gate == LOAD_GATE_COLD


def test_dead_start_arms_even_if_only_one_node_lags():
    """The fleet moves together, so one straggler makes it a cold start."""
    arm, gate = plan_start(playing_now=False, loaded=[True, True, False])
    assert arm is True
    assert gate == LOAD_GATE_COLD


# --- warm cases: no countdown, shorter gate --------------------------------


def test_resume_does_not_arm():
    """Paused -> resume: nothing is sounding, but everyone holds the buffer."""
    arm, gate = plan_start(playing_now=False, loaded=[True, True, True])
    assert arm is False
    assert gate == LOAD_GATE_TIMEOUT


def test_auto_advance_does_not_arm():
    """Mid-playlist the old track is still sounding — a countdown would madden."""
    arm, gate = plan_start(playing_now=True, loaded=[True, True])
    assert arm is False
    assert gate == LOAD_GATE_TIMEOUT


def test_skip_to_an_unprefetched_track_mid_playlist_does_not_arm():
    """Prefetch missed, but audio is still going, so the wait isn't silence."""
    arm, gate = plan_start(playing_now=True, loaded=[False, False])
    assert arm is False
    assert gate == LOAD_GATE_TIMEOUT


def test_no_nodes_connected_does_not_arm():
    arm, gate = plan_start(playing_now=False, loaded=[])
    assert arm is False
    assert gate == LOAD_GATE_TIMEOUT


def test_single_node_cases():
    assert plan_start(playing_now=False, loaded=[False])[0] is True
    assert plan_start(playing_now=False, loaded=[True])[0] is False


@pytest.mark.parametrize("playing_now", [True, False])
@pytest.mark.parametrize("loaded", [[], [True], [False], [True, False], [False, True]])
def test_cold_gate_is_never_shorter_than_warm(playing_now, loaded):
    """Whichever branch we take, the cold path must buy more time, not less."""
    _, gate = plan_start(playing_now, loaded)
    assert gate >= LOAD_GATE_TIMEOUT


# --- the constants have to leave room for each other -----------------------


def test_countdown_fits_inside_the_cold_gate():
    """A countdown longer than the gate would promise a start we'd abandon."""
    assert ARM_SECONDS < LOAD_GATE_COLD


def test_cold_gate_is_longer_than_warm():
    assert LOAD_GATE_COLD > LOAD_GATE_TIMEOUT
