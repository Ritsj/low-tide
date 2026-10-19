"""App-level behaviour, using stand-in objects for the app where a full
LowTideApp (TIDAL session, mpv, D-Bus) isn't needed."""
from __future__ import annotations

import asyncio
import types

import pytest
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import DataTable, Label, ListItem, ListView, TabbedContent, TabPane, Tabs

from lowtide.app import ContentArea, LowTideApp
from lowtide.local_track import LocalTrack

from conftest import make_track

NS = types.SimpleNamespace


# ── Queue save / restore ─────────────────────────────────────────────────


def _restore(queue, current_idx, get_track):
    """Save `queue`, then run the restore worker body and return
    (restored tracks, index playback starts at)."""
    LowTideApp._save_queue(NS(_queue=queue, _current_idx=current_idx))
    result = {}

    def call_from_thread(fn, *args):
        result[fn.__name__] = args

    def _show_restored_ui(*a): pass
    def _apply_restored_queue(*a): pass

    app = NS(client=NS(get_track=get_track), call_from_thread=call_from_thread,
             _show_restored_ui=_show_restored_ui, _apply_restored_queue=_apply_restored_queue)
    LowTideApp._restore_queue.__wrapped__(app)
    return result.get("_apply_restored_queue")


def _fetch_except(failing):
    def get_track(track_id):
        if track_id in failing:
            raise RuntimeError("not available")
        return make_track(track_id)
    return get_track


@pytest.mark.parametrize("failing, expected_ids, expected_current", [
    (set(), [1, 2, 3, 4, 5], 4),
    ({1, 2}, [3, 4, 5], 4),        # failures before the saved track shift it
    ({4}, [1, 2, 3, 5], 5),        # saved track itself failed: next one plays
    ({4, 5}, [1, 2, 3], 1),        # nothing after it: start from the top
])
def test_restore_resumes_on_saved_track(conf_dir, failing, expected_ids, expected_current):
    queue = [make_track(i) for i in range(1, 6)]
    tracks, idx = _restore(queue, 3, _fetch_except(failing))
    assert [t.id for t in tracks] == expected_ids
    assert tracks[idx].id == expected_current


def test_restore_rebuilds_local_tracks_from_files(conf_dir, flac_file):
    from lowtide.local_library import LocalLibrary

    path = flac_file("01 Song.flac", "Local Song")
    missing = flac_file("02 Gone.flac", "Gone")
    queue = [LocalLibrary.track_from_path(path), make_track(7), LocalLibrary.track_from_path(missing)]
    import os
    os.remove(missing)

    tracks, idx = _restore(queue, 0, _fetch_except(set()))
    assert isinstance(tracks[0], LocalTrack)
    assert tracks[0].name == "Local Song"
    assert tracks[0].artist.name == "Test Artist"
    assert [getattr(t, "id", None) for t in tracks[1:]] == [7]  # missing file dropped
    assert idx == 0


# ── Crossfade volume ─────────────────────────────────────────────────────


class FakePlayer:
    crossfade_secs = 5

    def __init__(self):
        self.volume, self.pos, self.paused = 80, 0.0, False

    async def get_position(self): return self.pos
    async def get_duration(self): return 100.0
    async def get_paused(self): return self.paused
    async def _cmd(self, command): self.volume = command[2]
    async def set_volume(self, level): self.volume = level


def test_crossfade_volume_restored_after_seeking_back():
    player = FakePlayer()
    ui = NS()
    app = NS(player=player, _target_volume=80, _crossfading=False, query_one=lambda *_: ui,
             mpris=NS(update_position=lambda *_: None, update_playback_status=lambda *_: None),
             scrobbler=NS(update=lambda *_: None))
    app._end_crossfade = lambda: LowTideApp._end_crossfade(app)

    async def poll(pos, paused=False):
        player.pos, player.paused = pos, paused
        await LowTideApp._poll_player(app)
        return player.volume

    async def run():
        assert await poll(50) == 80
        assert await poll(97) < 80                 # fading
        assert await poll(30, paused=True) < 80    # stays faded while paused
        assert await poll(30) == 80                # restored on resume

    asyncio.run(run())


# ── Keyboard navigation and focus frame ─────────────────────────────────


class Library(Widget):
    def compose(self):
        with TabbedContent():
            with TabPane("Playlists"):
                yield ListView(*[ListItem(Label(f"Playlist {i}")) for i in range(5)], id="playlists")
            with TabPane("Tracks"):
                yield DataTable(id="table")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_column("Title")
        for i in range(3):
            table.add_row(f"Song {i}")


class PlaylistView(Widget):
    def compose(self):
        yield DataTable(id="tracks")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_column("Title")
        table.add_row("Song")
        table.focus()  # like TrackList.load


class ContentForTest(ContentArea):
    def compose(self):
        yield Library()


class NavApp(App):
    CSS = LowTideApp.CSS
    BINDINGS = [Binding("escape", "back")]

    def __init__(self, focus_frame=True):
        super().__init__()
        self.set_class(focus_frame, "-focus-frame")

    def compose(self):
        with Horizontal():
            yield ListView(ListItem(Label("Library")), id="nav")
            yield ContentForTest()

    async def action_back(self):
        await self.query_one(ContentArea).pop()


def run_pilot(test):
    async def main():
        app = NavApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await test(app, pilot)
    asyncio.run(main())


def test_escape_returns_focus_to_the_list_it_came_from():
    async def test(app, pilot):
        playlists = app.query_one("#playlists", ListView)
        playlists.focus()
        await pilot.press("down", "down", "down")
        row = playlists.index
        await app.query_one(ContentArea).push(PlaylistView())
        await pilot.pause()
        assert app.focused.id == "tracks"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is playlists
        assert playlists.index == row  # cursor kept on the same playlist
    run_pilot(test)


def test_arrow_keys_move_between_tabs_and_list():
    async def test(app, pilot):
        app.query_one(Tabs).focus()
        await pilot.press("down")
        assert app.focused.id == "playlists"
        await pilot.press("down", "up")          # not on the top row: stays in list
        assert app.focused.id == "playlists"
        await pilot.press("up")                  # top row: back to the tabs
        assert isinstance(app.focused, Tabs)
        await pilot.press("right", "down")       # Tracks tab holds a DataTable
        await pilot.pause()
        assert app.focused.id == "table"
        await pilot.press("up")
        assert isinstance(app.focused, Tabs)
    run_pilot(test)


@pytest.mark.parametrize("enabled", [True, False])
def test_focus_frame_setting(enabled):
    async def main():
        app = NavApp(focus_frame=enabled)
        async with app.run_test() as pilot:
            app.query_one("#playlists").focus()
            await pilot.pause()
            focused = app.query_one("#playlists").styles.border.top[0]
            other = app.query_one("#nav").styles.border.top[0]
            tabs_height = app.query_one(Tabs).styles.height.value
        return focused, other, tabs_height

    focused, other, tabs_height = asyncio.run(main())
    if enabled:
        assert (focused, other, tabs_height) == ("round", "blank", 4)
    else:
        assert (focused, other, tabs_height) == ("", "", 2)
