"""Shared fixtures.

`bare` is a Conductor built with `__new__` — no library scan, no state file,
no tasks — carrying just the attributes a single method needs to run. Half the
suite drives one method this way. The event ring and the control-socket set
are what `Conductor.event` touches, and every path that says anything now
goes through it, so a bare conductor needs both or the path under test raises
for a reason that has nothing to do with the test.
"""

from collections import deque

import pytest

from syncplay.conductor import EVENT_RING, Conductor


@pytest.fixture()
def bare():
    cond = Conductor.__new__(Conductor)
    cond.control_sockets = set()
    cond.events = deque(maxlen=EVENT_RING)
    cond.nodes = {}
    cond.playing = None
    cond.paused = None
    cond.mesh_pairs = {}
    cond.mesh_seen = {}
    cond.trace = None  # no file unless a test attaches one
    return cond
