from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from typing import Optional

import tidalapi
from tidalapi.media import Quality

log = logging.getLogger(__name__)

CONF_DIR = os.path.join(os.path.expanduser("~"), ".config", "low-tide")
CONF_PATH = os.path.join(CONF_DIR, "session.json")
CONFIG_PATH = os.path.join(CONF_DIR, "config.json")

_QUALITY_MAP = {
    "low": Quality.low_96k,
    "high": Quality.low_320k,
    "lossless": Quality.high_lossless,
    "hi_res": Quality.hi_res_lossless,
    "max": Quality.hi_res_lossless,
}

# Minimum seconds between API calls (shared across all threads)
_MIN_CALL_INTERVAL = 0.3
# Retry attempts before letting TooManyRequests propagate
_MAX_RETRIES = 2


class TidalClient:
    def __init__(self):
        self._api_lock = threading.Lock()
        self._last_call_time = 0.0
        quality = self._load_quality()
        self.session = tidalapi.Session(tidalapi.Config(quality=quality))
        self._try_load_tokens()

    # --- Rate limiting ---

    def _throttle(self) -> None:
        with self._api_lock:
            now = time.monotonic()
            wait = _MIN_CALL_INTERVAL - (now - self._last_call_time)
            if wait > 0:
                time.sleep(wait)
            self._last_call_time = time.monotonic()

    def _api_call(self, fn, *args, **kwargs):
        """Call fn with throttling and automatic retry on TooManyRequests."""
        from tidalapi.exceptions import TooManyRequests
        for attempt in range(_MAX_RETRIES):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except TooManyRequests as e:
                wait = max(1, getattr(e, "retry_after", None) or 5)
                log.warning(
                    "TIDAL rate limit; retrying in %ss (attempt %d/%d)",
                    wait, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(wait)
        # Final attempt – let any exception propagate
        self._throttle()
        return fn(*args, **kwargs)

    # --- Config ---

    def _load_config(self) -> dict:
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _load_quality(self) -> str:
        data = self._load_config()
        return _QUALITY_MAP.get(data.get("quality", "lossless"), Quality.high_lossless)

    @property
    def config(self) -> dict:
        return self._load_config()

    # --- Auth ---

    def _try_load_tokens(self) -> None:
        try:
            with open(CONF_PATH) as f:
                data = json.load(f)
            expiry = None
            if data.get("expiry_time"):
                expiry = datetime.datetime.fromisoformat(data["expiry_time"])
            self.session.load_oauth_session(
                token_type=data.get("token_type", "Bearer"),
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expiry_time=expiry,
            )
        except Exception:
            pass

    def _save_tokens(self) -> None:
        os.makedirs(CONF_DIR, exist_ok=True)
        expiry = getattr(self.session, "expiry_time", None)
        data = {
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "token_type": getattr(self.session, "token_type", "Bearer"),
            "expiry_time": expiry.isoformat() if expiry else None,
        }
        with open(CONF_PATH, "w") as f:
            json.dump(data, f)

    def ensure_login(self) -> None:
        if self.session.check_login():
            return
        print("==== TIDAL Login ====")
        self.session.login_oauth_simple()
        self._save_tokens()

    def me(self):
        return self.session.user

    # --- Search / resolve ---

    def search(self, query: str, limit: int = 50) -> dict:
        return self._api_call(self.session.search, query, limit=limit)

    def resolve_track(self, artist: str, title: str):
        """Search TIDAL for a track by artist + title. Returns the best match or None."""
        import difflib
        import re
        import unicodedata

        def _norm(s: str) -> str:
            return re.sub(r"[^\w\s]", "", s.lower())

        def _fold(s: str) -> str:
            s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
            return re.sub(r"[^\w\s]", "", s.lower())

        def _match(a: str, b: str) -> bool:
            return a in b or b in a

        def _fuzzy(a: str, b: str, threshold: float = 0.82) -> bool:
            return difflib.SequenceMatcher(None, a, b).ratio() >= threshold

        def _candidates(query: str) -> list:
            try:
                return self.search(query, limit=25).get("tracks") or []
            except Exception:
                return []

        def _find(tracks: list) -> object:
            a_fold = _fold(artist)
            t_low, t_norm, t_fold = title.lower(), _norm(title), _fold(title)
            for track in tracks:
                r_artist = getattr(getattr(track, "artist", None), "name", "").lower()
                r_title = getattr(track, "name", "").lower()
                r_artist_fold = _fold(r_artist)
                r_title_fold = _fold(r_title)
                title_exact = t_fold == r_title_fold
                title_ok = (title_exact or _match(t_low, r_title) or _match(t_norm, _norm(r_title))
                            or _match(t_fold, r_title_fold))
                artist_threshold = 0.76 if title_exact else 0.82
                artist_ok = (_match(artist.lower(), r_artist) or _match(_norm(artist), _norm(r_artist))
                             or _match(a_fold, r_artist_fold)
                             or _fuzzy(a_fold, r_artist_fold, artist_threshold))
                if artist_ok and title_ok:
                    return track
            return None

        try:
            track = _find(_candidates(f"{artist} {title}"))
            if track is None:
                track = _find(_candidates(title))
            return track
        except Exception:
            pass
        return None

    # --- Library ---

    def get_user_playlists(self) -> list:
        return self._api_call(self.session.user.playlists)

    def get_favorite_tracks(self) -> list:
        return self._api_call(self.session.user.favorites.tracks_paginated)

    def get_favorite_albums(self, order=None, order_direction=None) -> list:
        return self._api_call(
            self.session.user.favorites.albums_paginated,
            order=order,
            order_direction=order_direction,
        )

    def get_favorite_artists(self) -> list:
        return self._api_call(self.session.user.favorites.artists_paginated)

    def get_album_tracks(self, album) -> list:
        return self._api_call(album.tracks)

    def get_playlist_tracks(self, playlist) -> list:
        fn = playlist.tracks if hasattr(playlist, "tracks") else playlist.items
        return self._api_call(fn)

    def get_artist_albums(self, artist) -> list:
        return self._api_call(artist.get_albums)

    def get_artist_top_tracks(self, artist, limit: int = 20) -> list:
        return self._api_call(artist.get_top_tracks, limit=limit)

    def get_artist_ep_singles(self, artist) -> list:
        return self._api_call(artist.get_ep_singles)

    def get_mix_tracks(self, mix) -> list:
        return self._api_call(mix.items)

    def get_track(self, track_id: int):
        return self._api_call(self.session.track, track_id)

    def get_for_you(self):
        return self._api_call(self.session.for_you)

    def get_mixes(self):
        return self._api_call(self.session.mixes)

    def get_home(self):
        return self._api_call(self.session.home)

    def get_genres(self) -> list:
        return self._api_call(self.session.genre.get_genres)

    def get_genre_tracks(self, genre) -> list:
        import tidalapi.media as _media
        return self._api_call(genre.items, _media.Track)

    # --- Playback ---

    def get_track_url(self, track) -> Optional[str]:
        try:
            return self._api_call(track.get_url)
        except Exception:
            return None

    def get_lyrics(self, track) -> tuple[str, str]:
        """Returns (plain_text, lrc_subtitles). Either may be empty string."""
        try:
            lyr = self._api_call(track.lyrics)
            return lyr.text or "", lyr.subtitles or ""
        except Exception:
            return "", ""

    def get_track_info(self, track) -> dict:
        """Returns extended track metadata for display."""
        return {
            "bpm": getattr(track, "bpm", None),
            "explicit": getattr(track, "explicit", False),
            "audio_quality": getattr(track, "audio_quality", None),
            "isrc": getattr(track, "isrc", None),
        }

    # --- Favourites ---

    def add_favourite_track(self, track_id: int) -> bool:
        try:
            return self._api_call(self.session.user.favorites.add_track, track_id)
        except Exception:
            return False

    def remove_favourite_track(self, track_id: int) -> bool:
        try:
            return self._api_call(self.session.user.favorites.remove_track, str(track_id))
        except Exception:
            return False
