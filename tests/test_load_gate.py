"""When to stop waiting for the fleet and just start the song.

`hold_gate` is the whole rule, kept pure so it can be pinned without a network,
a decode or a party. It answers one question — *keep waiting?* — from three
numbers, and the caller's hard timeout remains the outer bound in every case.

The rule is a **quiet period rather than a deadline**, and that distinction is
what most of this file exists to protect. A fixed grace measured from the first
ready node strands three nodes behind one that happens to hold a cached copy; a
fixed grace measured from the start punishes a fleet that is merely uniformly
slow. Waiting only while nodes are still *arriving* handles both, because it
responds to progress instead of to a clock.

Underneath it all is the fact that makes starting early cheap: a node left
behind is not left out. It sends `loaded` when it decodes and `_catchup` drops
it into the song from there. The choice is never "with or without that node" —
it is "everyone waits in silence" versus "that node joins a moment late".
"""

import pytest

from syncplay.conductor import STRAGGLER_GRACE, hold_gate


QUIET = STRAGGLER_GRACE + 1.0   # long enough that the grace has certainly gone
BUSY = STRAGGLER_GRACE - 1.0    # a node arrived recently enough to still count


# --- the two ends: nothing to wait for, or everyone already here -----------


@pytest.mark.parametrize("quiet_for", [0.0, BUSY, QUIET, 3600.0])
def test_a_full_fleet_never_waits(quiet_for):
    """No amount of quiet or noise matters once everyone holds the track."""
    assert hold_gate(n_ready=4, n_connected=4, quiet_for=quiet_for) is False


@pytest.mark.parametrize("quiet_for", [0.0, QUIET])
def test_an_empty_fleet_never_waits(quiet_for):
    """Nobody connected: the caller's "no node managed to load this" path owns
    this case, and holding here would just delay that message."""
    assert hold_gate(n_ready=0, n_connected=0, quiet_for=quiet_for) is False


def test_more_ready_than_connected_is_not_a_deadlock():
    """A node can disconnect between the two counts being taken."""
    assert hold_gate(n_ready=3, n_connected=2, quiet_for=0.0) is False


# --- the floor: a minority is not a straggler ------------------------------


@pytest.mark.parametrize("n_ready,n_connected", [(0, 4), (1, 4), (1, 3), (0, 1)])
@pytest.mark.parametrize("quiet_for", [0.0, QUIET, 3600.0])
def test_below_half_holds_however_quiet_it_gets(n_ready, n_connected, quiet_for):
    """Starting for a minority is playing to an empty room. Below half we wait
    regardless, and the hard timeout is the only thing that can release us."""
    assert hold_gate(n_ready, n_connected, quiet_for) is True


def test_exactly_half_is_enough_to_be_released_by_quiet():
    assert hold_gate(n_ready=2, n_connected=4, quiet_for=QUIET) is False
    assert hold_gate(n_ready=2, n_connected=4, quiet_for=BUSY) is True


# --- the quiet period itself -----------------------------------------------


def test_still_arriving_means_still_waiting():
    """Three of four in and somebody landed a moment ago — the fourth is
    probably seconds behind, so the wait is buying something."""
    assert hold_gate(n_ready=3, n_connected=4, quiet_for=BUSY) is True


def test_gone_quiet_means_go():
    assert hold_gate(n_ready=3, n_connected=4, quiet_for=QUIET) is False


def test_the_grace_boundary():
    """Pinned exactly, because this is the number that decides how long a room
    stands in silence for one phone."""
    assert hold_gate(3, 4, STRAGGLER_GRACE - 1e-6) is True
    assert hold_gate(3, 4, STRAGGLER_GRACE) is False


# --- the scenarios the rule was actually designed against ------------------


def test_a_cached_node_cannot_strand_the_others():
    """The failure a grace measured from the *first* ready node would have had.

    One node already holds the track from a prefetch and reports instantly; the
    other three need eight seconds. Even after a long silence we must still be
    waiting, because one of four is below the floor.
    """
    assert hold_gate(n_ready=1, n_connected=4, quiet_for=3600.0) is True


def test_a_uniformly_slow_fleet_is_never_cut():
    """Everyone is slow but everyone is coming: an arrival every second or two
    keeps resetting the quiet period, so the gate holds through all of it."""
    for n_ready in (2, 3):
        assert hold_gate(n_ready, 4, quiet_for=1.5) is True
    assert hold_gate(4, 4, quiet_for=1.5) is False  # ...and releases on the last


def test_one_straggler_stops_the_drip():
    """Three arrived, the fourth is on a bad radio. The drip stops, the quiet
    period elapses, and the room starts instead of waiting out the timeout."""
    assert hold_gate(3, 4, quiet_for=0.0) is True
    assert hold_gate(3, 4, quiet_for=QUIET) is False


def test_a_two_node_fleet_still_gets_bounded():
    """Half of two is one, so a pair with one straggler waits the grace and no
    longer — the case where the old all-or-timeout rule cost the most."""
    assert hold_gate(1, 2, quiet_for=BUSY) is True
    assert hold_gate(1, 2, quiet_for=QUIET) is False


def test_a_lone_node_waits_for_itself():
    assert hold_gate(0, 1, quiet_for=QUIET) is True
    assert hold_gate(1, 1, quiet_for=0.0) is False


# --- properties ------------------------------------------------------------


@pytest.mark.parametrize("n_connected", range(1, 9))
@pytest.mark.parametrize("quiet_for", [0.0, BUSY, QUIET])
def test_readiness_is_monotone(n_connected, quiet_for):
    """More nodes ready may never turn a release into a hold. Without this a
    node arriving could *extend* the wait, which is precisely backwards."""
    decisions = [hold_gate(r, n_connected, quiet_for) for r in range(n_connected + 1)]
    released = [i for i, held in enumerate(decisions) if not held]
    assert released, "a full fleet must always release"
    # Once released, it stays released as more arrive.
    assert all(not decisions[i] for i in range(min(released), n_connected + 1))


@pytest.mark.parametrize("n_connected", range(1, 9))
def test_waiting_longer_is_monotone(n_connected):
    """More quiet may never turn a release back into a hold."""
    for n_ready in range(n_connected + 1):
        held = [hold_gate(n_ready, n_connected, q) for q in (0.0, BUSY, QUIET, 3600.0)]
        assert held == sorted(held, reverse=True), (n_ready, n_connected, held)


@pytest.mark.parametrize("n_connected", range(1, 9))
def test_a_full_fleet_always_releases_immediately(n_connected):
    assert hold_gate(n_connected, n_connected, quiet_for=0.0) is False


def test_the_grace_is_shorter_than_either_hard_gate():
    """A quiet period longer than the timeout it sits inside would be dead
    code — the timeout would always win and nothing would have changed."""
    from syncplay.conductor import LOAD_GATE_COLD, LOAD_GATE_TIMEOUT

    assert STRAGGLER_GRACE < LOAD_GATE_TIMEOUT < LOAD_GATE_COLD
