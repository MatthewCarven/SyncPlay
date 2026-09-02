"""Which player.js is a node actually running?

"Needs a fleet reload" has been a standing note in the worklog for a month, and
until this existed there was no way to check whether one had happened. A capture
taken right after a reload could prove the *conductor* was on new code and say
nothing at all about the *players*, because the new player paths only become
visible when something goes wrong.

Two design choices are worth pinning rather than explaining:

**No hand-bumped constant.** A build marker that depends on someone remembering
to update it reports "up to date" precisely when they forgot, which is the one
moment it was needed. The conductor hashes the file it is serving.

**The stamp rides on the page, not on the connection.** A node that drops its
WebSocket and reconnects has not reloaded and is still running old code — and a
connect-time check would call it fresh, which is the exact failure this is for.
"""

import asyncio
import logging

import pytest

from syncplay.conductor import PLAYER_SCRIPT_TAG, Conductor, Node, now


@pytest.fixture()
def cond(tmp_path, monkeypatch):
    import syncplay.conductor as C

    web = tmp_path / "web"
    web.mkdir()
    (web / "player.js").write_text("// v1\n", encoding="utf-8")
    (web / "player.html").write_text(
        f"<html><head></head><body>\n  {PLAYER_SCRIPT_TAG}\n</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(C, "WEB_DIR", web)
    c = Conductor.__new__(Conductor)
    c._build = None
    c._build_key = None
    return c, web


# --- the hash ---------------------------------------------------------------


def test_build_is_stable_while_the_file_is(cond):
    c, _ = cond
    assert c.player_build() == c.player_build()
    assert len(c.player_build()) == 8


def test_build_moves_when_player_js_does(cond):
    c, web = cond
    before = c.player_build()
    (web / "player.js").write_text("// v2 — one character more\n", encoding="utf-8")
    assert c.player_build() != before


def test_a_missing_player_js_is_no_build_rather_than_a_crash(cond):
    c, web = cond
    (web / "player.js").unlink()
    c._build = c._build_key = None
    assert c.player_build() is None


# --- the stamp --------------------------------------------------------------


def test_the_page_is_served_with_the_build_stamped_in(cond):
    c, _ = cond
    resp = asyncio.run(c.handle_player_page(None))
    body = resp.text
    assert f'window.PLAYER_BUILD="{c.player_build()}"' in body
    # Before the script tag, so the player can read it as it initialises.
    assert body.index("PLAYER_BUILD") < body.index(PLAYER_SCRIPT_TAG)


def test_a_page_without_the_expected_tag_still_serves(cond):
    """The stamp is a diagnostic. It must never be the reason nobody can play."""
    c, web = cond
    (web / "player.html").write_text("<html>no script here</html>", encoding="utf-8")
    resp = asyncio.run(c.handle_player_page(None))
    assert "no script here" in resp.text
    assert "PLAYER_BUILD" not in resp.text


# --- what a node reports ----------------------------------------------------


def test_a_node_carries_the_build_it_loaded():
    n = Node("id", "n")
    assert n.build is None            # before hello, and from a page too old
    n.build = "abc12345"
    assert n.stats("t")["playerBuild"] == "abc12345"


def test_a_stale_node_is_named_in_the_log(cond, caplog):
    """The control page shows it; the log is what you still have an hour later."""
    c, web = cond
    node = Node("id", "Mums-Tablet")
    node.build = c.player_build()
    assert c.note_build(node) is False, "a node on the served build is not stale"

    (web / "player.js").write_text("// v2 changed", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert c.note_build(node) is True
    assert "Mums-Tablet" in caplog.text
    assert "has not reloaded" in caplog.text
    assert node.build in caplog.text


def test_a_node_that_reports_no_build_is_not_accused(cond, caplog):
    """A page served before the stamp existed has nothing to compare against.
    Saying nothing is right: the control page renders that case separately, and
    a warning per hello would be noise on every old client."""
    c, _ = cond
    node = Node("id", "old-page")
    with caplog.at_level(logging.WARNING):
        assert c.note_build(node) is False
    assert caplog.text == ""


def test_a_reconnect_does_not_launder_a_stale_node(cond):
    """The failure this whole thing is for. The stamp rides on the *page*, so a
    node that drops its socket and comes back is still running old code and must
    still be reported — where a check keyed to connection time would call it
    fresh precisely when it is not."""
    c, web = cond
    node = Node("id", "phone")
    node.build = c.player_build()
    (web / "player.js").write_text("// v2 changed", encoding="utf-8")
    for _ in range(3):
        # no page reload: same build stamp, socket comes and goes
        assert c.note_build(node) is True
