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
