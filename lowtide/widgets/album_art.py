from __future__ import annotations

from io import BytesIO

import requests
from PIL import Image as PILImage
from rich.control import Control
from rich.segment import Segment
from textual import work
from textual.app import ComposeResult
from textual.widget import Widget
from textual_image.widget import Image as TImage
from textual_image.widget import SixelImage
from textual_image.widget.sixel import _ImageSixelImpl, _NoopRenderable


class _SixelImpl(_ImageSixelImpl):
    """Works around an off-by-one in textual-image (up to at least 0.14.1).

    The Sixel data is injected while Textual writes the image's last row, and
    afterwards the cursor is moved to (right, bottom) -- the row *below* the
    image. On full-screen redraws Textual writes each row after a single cursor
    move, so the rest of that row (everything right of the art) landed one row
    too low and the real row kept stale content, e.g. leftover focus frames.
    Move back to the image's last row instead."""

    def _get_sixel_segments(self, sixel_data: str):
        segments = list(super()._get_sixel_segments(sixel_data))
        region = self.screen.find_widget(self).visible_region
        segments[-1] = Segment(
            Control.move_to(region.right, region.bottom - 1).segment.text, style=segments[-1].style
        )
        return segments


class _FixedSixelImage(SixelImage, Renderable=_NoopRenderable):
    def compose(self) -> ComposeResult:
        yield _SixelImpl(self.image, self._sixel_options)


# textual-image picks the protocol at import time; only Sixel needs the fix.
if TImage is SixelImage:
    TImage = _FixedSixelImage


class AlbumArt(Widget):
    """Downloads a TIDAL album art URL and renders it using the best available protocol."""

    DEFAULT_CSS = """
    AlbumArt {
        width: 14;
        height: 7;
        background: transparent;
    }
    AlbumArt #art-img {
        width: 100%;
        height: 100%;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._current_url: str | None = None

    def compose(self) -> ComposeResult:
        yield TImage(id="art-img")

    def load(self, url: str | None) -> None:
        if not url or url == self._current_url:
            return
        self._current_url = url
        self._fetch(url)

    def clear(self) -> None:
        self._current_url = None
        try:
            self.query_one(TImage).image = None
        except Exception:
            pass

    @work(thread=True)
    def _fetch(self, url: str) -> None:
        try:
            if url.startswith("file://"):
                img = PILImage.open(url[7:]).convert("RGB")
            else:
                resp = requests.get(url, timeout=5)
                resp.raise_for_status()
                img = PILImage.open(BytesIO(resp.content)).convert("RGB")
            self.app.call_from_thread(self._apply, img, url)
        except Exception:
            pass

    def _apply(self, img: PILImage.Image, url: str) -> None:
        if url != self._current_url:
            return  # a newer track started before this fetch completed
        widget = self.query_one(TImage)
        widget.image = img
        widget.refresh(layout=True)
