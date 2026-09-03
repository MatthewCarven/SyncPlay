"""The trace: a JSONL sidecar that outlives the process, and its report.

The log dies with the console window and the events ring dies with the
conductor. The trace is the copy that does neither, and it is on by default
because the evening worth measuring is the one nobody planned to. Two things
are pinned hardest here: that nothing in a message handler ever waits on the
disk, and that a disk that fails - full, closed, gone - turns the trace off
and changes nothing else. A diagnostic must never be the reason nobody can
play.

The report tool is checked the way `plan_nudges` was: a synthetic trace with
planted numbers, and the tool has to print them back.
"""

import asyncio
import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import syncplay.conductor as C
import syncplay.trace as T
from syncplay.conductor import (
    CATCHUP_WAIT_S,
    PLAY_LEAD,
    Conductor,
    Node,
    build_app,
    now,
)
from syncplay.timesync import PingSample
from syncplay.trace import Trace, trace_path, wall_clock

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "tools" / "trace_report.py"
sys.path.insert(0, str(ROOT / "tools"))
import trace_report as R  # noqa: E402


def lines(path: Path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def feed(model, n=16, span=60.0):
    base = now() - span
    for i in range(n):
        t = base + span * i / max(1, n - 1)
        model.add(PingSample(t0=t, c1=t + 1.0, c2=t + 1.0, t3=t))


# --- the writer ----------------------------------------------------------------


def test_the_name_says_when_and_wall_is_hhmmss_mmm(tmp_path):
    p = trace_path(tmp_path, at=0.0)
    assert p.parent == tmp_path and p.name.startswith("trace-") and p.suffix == ".jsonl"
    w = wall_clock()
    assert len(w) == 12 and w[2] == w[5] == ":" and w[8] == "."


def test_header_first_and_every_kind_serialises(tmp_path):
    tr = Trace(tmp_path / "logs" / "t.jsonl")

    async def run():
        await tr.start()
        tr.write("start", build="abc")
        tr.write("event", event="join", name="n", text="joined")
        tr.write("steer", node="id", errMs=0.5, rate=1.0001)
        tr.write("node", id="id", nUsed=3)
        tr.write("mesh", aName="a", bName="b", closureMs=0.1)
        tr.write("sample", node="id", t0=1.0, c1=2.0, c2=2.0, t3=3.0)
        await tr.stop()

    asyncio.run(run())
    rows = lines(tr.path)
    assert [r["kind"] for r in rows] == ["start", "event", "steer", "node", "mesh", "sample"]
    for r in rows:
        assert isinstance(r["t"], float) and len(r["wall"]) == 12
    assert rows[1]["event"] == "join" and rows[2]["rate"] == 1.0001
    assert tr.lines == 6 and tr.enabled is False, "closed after stop"


def test_a_field_json_cannot_spell_becomes_its_str(tmp_path):
    tr = Trace(tmp_path / "t.jsonl")

    async def run():
        await tr.start()
        tr.write("k", path=Path("x/y"), thing=object(), fine=1)
        await tr.stop()

    asyncio.run(run())
    (row,) = lines(tr.path)
    assert row["path"] in ("x/y", "x\\y") and row["thing"].startswith("<object") and row["fine"] == 1


def test_lines_queued_before_the_file_opens_are_not_lost(tmp_path):
    """The conductor's header is written at its own start; the ordering of
    startup handlers must not decide whether it survives."""
    tr = Trace(tmp_path / "t.jsonl")
    tr.write("start", first=True)
    tr.flush()  # nothing to write to yet - keeps it

    async def run():
        await tr.start()
        tr.write("event", second=True)
        await tr.stop()

    asyncio.run(run())
    assert [r["kind"] for r in lines(tr.path)] == ["start", "event"]


def test_the_writer_survives_a_closed_file(tmp_path, caplog):
    tr = Trace(tmp_path / "t.jsonl")

    async def run():
        await tr.start()
        tr.write("k", n=1)
        tr.flush()
        tr._fh.close()  # the file goes away under it
        tr.write("k", n=2)
        tr.flush()      # must not raise
        assert tr.failed and "closed" in tr.failed
        assert tr.enabled is False
        tr.write("k", n=3)  # dropped silently from here on
        assert tr._buf == []
        await tr.stop()

    asyncio.run(run())
    assert [r["n"] for r in lines(tr.path)] == [1]
    assert "off for the rest of this run" in caplog.text


def test_a_directory_that_cannot_be_made_turns_the_trace_off(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    tr = Trace(blocker / "sub" / "t.jsonl")

    async def run():
        await tr.start()
        assert tr.failed is not None and tr.enabled is False
        tr.write("k")
        await tr.stop()

    asyncio.run(run())


@pytest.fixture()
def lib(tmp_path):
    with wave.open(str(tmp_path / "a.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 800)
    return tmp_path


def test_a_full_disk_turns_the_trace_off_and_a_play_still_goes_out(lib, monkeypatch):
    """The rule. The transport runs through `event()` and `event()` runs
    through the trace; a disk that is full must cost the trace, not the room."""
    monkeypatch.setattr(C, "LOAD_GATE_TIMEOUT", 0.05)
    monkeypatch.setattr(C, "LOAD_GATE_COLD", 0.05)
    monkeypatch.setattr(C, "STATE_FILE", lib / "state.json")
    cond = Conductor(lib)
    tr = Trace(lib / "logs" / "t.jsonl")
    cond.trace = tr
    track = cond.tracks[0]
    n = Node("id", "laptop")
    n.connected = True
    n.loaded.add(track.id)
    feed(n.model)
    sent = []

    async def send(payload):
        sent.append(payload)

    n.send = send
    cond.nodes[n.client_id] = n

    async def run():
        await tr.start()

        def full(_s):
            raise OSError(28, "No space left on device")

        tr._fh.write = full
        await cond._transport_play(track)
        cond._advance_task.cancel()
        tr.flush()
        await tr.stop()

    asyncio.run(run())
    assert any(m.get("type") == "play" for m in sent), "the play went out"
    assert cond.events[-1]["kind"] == "play", "and the ring still has it"
    assert tr.failed and "No space left" in tr.failed


# --- wired into the app -------------------------------------------------------------


def test_no_trace_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")

    async def run():
        app = build_app(tmp_path)
        assert app["conductor"].trace is None
        async with TestClient(TestServer(app)):
            await asyncio.sleep(0.1)

    asyncio.run(run())
    assert not list(tmp_path.rglob("*.jsonl"))


def test_the_header_carries_the_constants(lib, monkeypatch):
    cond = Conductor(lib)
    tr = Trace(lib / "t.jsonl")
    cond.trace = tr

    async def run():
        await tr.start()
        await cond.start(None)
        cond._pulse_task.cancel()
        await tr.stop()

    asyncio.run(run())
    head = lines(tr.path)[0]
    assert head["kind"] == "start"
    assert head["playLeadS"] == PLAY_LEAD and head["catchupWaitS"] == CATCHUP_WAIT_S
    assert head["build"] == cond.player_build() and head["samples"] is False
    assert head["musicDir"] == str(lib)


def test_the_periodic_lines_run_and_stop_with_the_app(tmp_path, monkeypatch):
    """Real app, real sockets: a node joins, the pulse writes its stats line
    every TRACE_PERIOD_S, and cleanup closes the file after the leave."""
    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(C, "TRACE_PERIOD_S", 1)
    monkeypatch.setattr(T, "FLUSH_S", 0.2)
    tr = Trace(tmp_path / "logs" / "t.jsonl")

    async def run():
        app = build_app(tmp_path, trace=tr)
        async with TestClient(TestServer(app)) as client:
            p = await client.ws_connect("/ws/player")
            await p.send_json({"type": "hello", "clientId": "n1", "name": "phone"})
            await asyncio.sleep(2.4)
            assert tr.lines > 0, "the drain ran"
            await p.close()
            await asyncio.sleep(0.3)
        assert tr._fh is None and tr._task is None, "closed at cleanup"

    asyncio.run(run())
    rows = lines(tr.path)
    assert rows[0]["kind"] == "start"
    kinds = [r["kind"] for r in rows]
    assert "node" in kinds and "event" in kinds
    node_lines = [r for r in rows if r["kind"] == "node"]
    assert all(r["name"] == "phone" and "nUsed" in r for r in node_lines)
    assert [r["event"] for r in rows if r["kind"] == "event"] == ["join", "leave"]


def test_steer_lines_carry_the_servo_numbers(bare, tmp_path):
    tr = Trace(tmp_path / "t.jsonl")
    bare.trace = tr
    n = Node("id", "tablet")
    n.playing_track = "trk"
    n.run_since = now() - 30.0
    feed(n.model)

    async def run():
        await tr.start()
        await bare._on_player_msg(n, {"type": "steerAck", "errMs": 1.5, "rate": 1.0002}, now())
        await bare._on_player_msg(n, {"type": "steerAck", "errMs": "junk", "rate": "inf"}, now())
        await tr.stop()

    asyncio.run(run())
    good, junk = lines(tr.path)
    assert good["kind"] == "steer" and good["name"] == "tablet" and good["track"] == "trk"
    assert good["errMs"] == 1.5 and good["rate"] == 1.0002
    assert 29.0 < good["runS"] < 31.0 and good["nUsed"] == 16
    assert isinstance(good["offsetMs"], float) and isinstance(good["trustMs"], float)
    assert junk["errMs"] is None and junk["rate"] is None, "client data, clamped or dropped"


def test_events_reach_the_trace_with_their_fields(bare, tmp_path):
    tr = Trace(tmp_path / "t.jsonl")
    bare.trace = tr
    sent = []

    async def relay(payload):
        sent.append(payload)

    bare._broadcast_control = relay

    async def run():
        await tr.start()
        await bare.event("join", "joined", node=Node("id", "n"), fresh=True, build="abc")
        await tr.stop()

    asyncio.run(run())
    (row,) = lines(tr.path)
    assert row["kind"] == "event" and row["event"] == "join"
    assert row["fresh"] is True and row["build"] == "abc" and row["name"] == "n"
    assert row["t"] == bare.events[-1]["t"], "the ring and the trace stamp the same instant"


def test_samples_are_opt_in(bare, tmp_path):
    a, b = Node("a-id", "laptop"), Node("b-id", "tablet")
    bare.nodes = {a.client_id: a, b.client_id: b}
    pong = {"type": "pong", "id": 1, "c1": 1000.0, "c2": 1000.0}
    mesh = {"type": "meshSample", "peer": "b-id", "t0": 0.0, "c1": 1.0, "c2": 1.0, "t3": 2.0}

    async def run(samples):
        tr = Trace(tmp_path / f"t-{samples}.jsonl", samples=samples)
        bare.trace = tr
        await tr.start()
        a.pending[1] = now()
        await bare._on_player_msg(a, pong, now())
        await bare._on_player_msg(a, mesh, now())
        await tr.stop()
        return lines(tr.path)

    off = asyncio.run(run(False))
    assert not any(r["kind"] == "sample" for r in off)
    on = asyncio.run(run(True))
    samples = [r for r in on if r["kind"] == "sample"]
    assert len(samples) == 2
    star, pair = samples
    assert star["peer"] is None and star["c1"] == 1.0 and star["t3"] > star["t0"] - 1
    assert pair["peer"] == "b-id" and pair["t3"] == 0.002


# --- the report ------------------------------------------------------------------


def planted_trace(path: Path) -> dict:
    """Three nodes with exact means and sds, one restart, a defer, a catch-up,
    a timeout, two mesh pairs, a warning. Returns what was planted."""
    rows = []
    t = 0.0

    def w(i):
        s = 20 * 3600 + 15 * 60 + i  # from 20:15:00
        return "%02d:%02d:%02d.000" % (s // 3600, (s // 60) % 60, s % 60)

    def line(kind, i, **f):
        rows.append({"t": float(i), "wall": w(i), "kind": kind, **f})

    line("start", 0, build="feedface", musicDir="M", playLeadS=1.8, catchupWaitS=35.0, samples=False)
    planted = {"laptop": (5.0, 1.0), "phone": (0.0, 2.0), "tablet": (-3.0, 0.5)}
    # An alternating +/-sd series has an exact mean and a sample sd of
    # sd * sqrt(n / (n - 1)); at 400 that factor vanishes at two decimals.
    n = 400
    for i in range(n):
        for j, (name, (mean, sd)) in enumerate(planted.items()):
            err = mean + sd * (1 if i % 2 == 0 else -1)
            line("steer", 10 + i, node=f"id-{name}", name=name, track="trk", errMs=err,
                 rate=1.0, runS=60.0 + i, offsetMs=1.0, trustMs=0.5, skewPpm=j * 1.0,
                 nUsed=100, lastRttMs=1.0)
    for name, used in (("laptop", 98), ("phone", 12), ("tablet", 40)):
        for k in range(3):
            line("node", 50 + k * 10, id=f"id-{name}", name=name, nUsed=used, nSamples=100,
                 audioClockPpm=(-100.0 if name == "tablet" else None),
                 audioClockCredible=(name == "tablet"), playerBuild="feedface", restarts=0)
    line("event", 5, event="defer", level="info", node=None, name=None,
         text="Starting without phone - clock still settling", nodes=["phone"])
    line("event", 8, event="play", level="info", node=None, name=None, text="play", nodes=["laptop", "tablet"])
    line("event", 34, event="catchup", level="info", node="id-phone", name="phone",
         text='joined "song"', waitedS=29.4)
    line("event", 120, event="restart", level="warning", node="id-tablet", name="tablet",
         text="source restarted (#1 this track)", restarts=1)
    line("event", 150, event="catchup-timeout", level="warning", node="id-phone", name="phone",
         text="could not join", waitedS=35.0)
    for k, cl in enumerate((0.2, 3.4, 1.0)):
        line("mesh", 60 + k * 10, a="id-laptop", b="id-tablet", aName="laptop", bName="tablet",
             directMs=1.0, closureMs=cl, rttMs=1.2, n=40)
    line("mesh", 60, a="id-laptop", b="id-phone", aName="laptop", bName="phone",
         directMs=1.0, closureMs=-0.7, rttMs=9.9, n=3)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return planted


def test_the_report_prints_the_planted_numbers(tmp_path):
    p = tmp_path / "trace-20260903-201500.jsonl"
    planted_trace(p)
    rows = R.load(p)
    text = R.report(rows, source=str(p))

    # per node: mean and sd exactly as planted (an alternating +/-sd series)
    assert "laptop" in text and "phone" in text and "tablet" in text
    st = R.steer_stats(rows)
    assert st["laptop"]["mean"] == pytest.approx(5.0) and st["laptop"]["sd"] == pytest.approx(1.0, abs=0.01)
    assert st["tablet"]["mean"] == pytest.approx(-3.0) and st["tablet"]["sd"] == pytest.approx(0.5, abs=0.01)
    assert st["phone"]["crossings"] == 399, "a series swinging through zero crosses every step"
    assert st["laptop"]["crossings"] == 0
    for needle in ("     5.00    1.00", "    -3.00    0.50", "     0.00    2.00"):
        assert needle in text, needle
    # fleet spread: 5.0 - (-3.0) = 8 ms = 2.74 m
    assert "fleet spread of means: 8.00 ms = 2.74 m of air (3 nodes" in text
    # survival, audio clock, credibility, restarts
    facts = R.node_facts(rows)
    assert facts["phone"]["survival"] == pytest.approx(12.0)
    assert facts["tablet"]["audioPpm"] == -100.0 and facts["tablet"]["audioCredible"] is True
    assert "restarts: tablet 1 (20:17:00)" in text
    assert "catch-ups: 20:15:34 phone after 29.4 s" in text
    assert "catch-up TIMEOUTS: 20:17:30 phone" in text
    assert "20:15:05  defer: phone" in text
    # mesh best / worst per pair
    assert "laptop <-> tablet" in text and "0.20 / 3.40" in text and "n=3" in text
    assert "laptop <-> phone" in text and "0.70 / 0.70" in text
    # warnings timeline, in order
    w = text.index("WARNINGS (2)")
    assert text.index("source restarted", w) < text.index("could not join", w)
    # header
    assert "build feedface" in text and "20:15:00" in text


def test_since_until_and_node_filters(tmp_path):
    p = tmp_path / "t.jsonl"
    planted_trace(p)
    rows = R.load(p)
    cut = R.select(rows, since="20:16", until="20:17")
    walls = [r["wall"][:8] for r in cut if r["kind"] != "start"]
    assert walls and min(walls) >= "20:16:00" and max(walls) <= "20:17:00"
    assert any(r["kind"] == "start" for r in cut), "the header always survives"

    only = R.select(rows, node="TAB")
    names = {r.get("name") for r in only if r["kind"] == "steer"}
    assert names == {"tablet"}
    assert any(r["kind"] == "event" and r.get("event") == "play" for r in only), \
        "fleet-wide events (no node) are context for every node"
    assert not any(r["kind"] == "event" and r.get("event") == "catchup" for r in only), \
        "another node's catch-up is not"
    assert {f"{r['aName']}-{r['bName']}" for r in only if r["kind"] == "mesh"} == {"laptop-tablet"}


def test_a_cut_off_last_line_loses_only_itself(tmp_path):
    p = tmp_path / "t.jsonl"
    planted_trace(p)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"t": 1, "wall": "20:20:00.000", "kind": "steer", "errMs": 1.')  # died mid-drain
    rows = R.load(p)
    assert rows and rows[-1]["kind"] == "mesh"


def test_csv_dumps_the_steer_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    planted_trace(p)
    out = tmp_path / "steer.csv"
    n = R.write_csv(R.load(p), out)
    assert n == 1200
    head, first = out.read_text(encoding="utf-8").splitlines()[:2]
    assert head.split(",") == R.STEER_COLUMNS
    assert first.split(",")[3] == "laptop" and first.split(",")[5] == "6.0"


def test_the_cli_runs_on_the_newest_trace(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    planted_trace(logs / "trace-20260901-200000.jsonl")
    planted_trace(logs / "trace-20260903-201500.jsonl")
    res = subprocess.run(
        [sys.executable, str(REPORT), "--dir", str(logs), "--node", "tablet",
         "--csv", str(tmp_path / "s.csv")],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "trace-20260903-201500.jsonl" in res.stdout
    assert "    -3.00    0.50" in res.stdout and "laptop" not in res.stdout.split("ERR MS")[1].split("fleet")[0]
    assert "400 steer rows -> " in res.stdout
    assert R.newest_trace(logs).name == "trace-20260903-201500.jsonl"
    missing = subprocess.run(
        [sys.executable, str(REPORT), "--dir", str(tmp_path / "nowhere")],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert missing.returncode == 2 and "no trace found" in missing.stderr
