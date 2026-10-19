from __future__ import annotations

import asyncio

from PIL import Image as PILImage
from rich.control import Control
from textual.app import App

from lowtide.widgets.album_art import _FixedSixelImage, _SixelImpl


def test_sixel_cursor_returns_to_image_last_row():
    """After drawing Sixel data, the cursor must end on the image's last row
    (where Textual continues writing that row), not the row below it, or the
    rest of that row is drawn one row too low and stale cells remain."""

    class ArtApp(App):
        def compose(self):
            yield _FixedSixelImage(PILImage.new("RGB", (40, 40)), id="art")

        def on_mount(self):
            art = self.query_one("#art")
            art.styles.width, art.styles.height = 10, 5

    async def main():
        app = ArtApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            impl = app.query_one(_SixelImpl)
            region = app.screen.find_widget(impl).visible_region
            segments = impl._get_sixel_segments("SIXELDATA")
            return region, segments

    region, segments = asyncio.run(main())
    assert region.height == 5
    assert segments[0].text == Control.move_to(region.x, region.y).segment.text
    assert segments[1].text == "SIXELDATA"
    assert segments[-1].text == Control.move_to(region.right, region.bottom - 1).segment.text


def test_column_beside_art_is_repainted_once_without_looping(monkeypatch):
    """Konsole erases one column past a Sixel image; the column right of the art
    must be repainted after each draw, without that repaint redrawing the image
    (which would erase it again, in a loop)."""
    from textual.containers import Horizontal
    from textual.geometry import Region
    from textual.screen import Screen

    draws, repaints = [], []
    original = _SixelImpl._get_sixel_segments
    monkeypatch.setattr(_SixelImpl, "_get_sixel_segments",
                        lambda self, data: (draws.append(1), original(self, data))[1])
    screen_refresh = Screen.refresh

    def spy(self, *regions, **kw):
        repaints.extend(regions)
        return screen_refresh(self, *regions, **kw)

    monkeypatch.setattr(Screen, "refresh", spy)

    class ArtApp(App):
        def compose(self):
            with Horizontal():
                yield _FixedSixelImage(PILImage.new("RGB", (40, 40)), id="art")

        def on_mount(self):
            art = self.query_one("#art")
            art.styles.width, art.styles.height = 10, 5

    async def main():
        app = ArtApp()
        async with app.run_test() as pilot:
            for _ in range(10):
                await pilot.pause(0.05)
            settled = len(draws)
            for _ in range(20):  # a quiet second: nothing should redraw now
                await pilot.pause(0.05)
            return app.screen.find_widget(app.query_one(_SixelImpl)).visible_region, settled

    region, settled = asyncio.run(main())
    assert draws, "image never drawn"
    beside = Region(region.right, region.y, 1, region.height)
    assert beside in repaints
    assert len(draws) == settled  # the repaint doesn't redraw the image (no loop)
