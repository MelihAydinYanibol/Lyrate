"""spotdl as a second lyrics source, alongside LRCLIB.

Two things come from here:

- **Canonical metadata.** Spotify knows what a track is really called and how
  long it runs. Plex tags are often messier - "Various Artists", a compilation
  album name, a slightly different title - and a cleaned-up title and artist
  makes the lyric search far more likely to land.
- **More lyric providers.** spotdl gets its synced lyrics from `syncedlyrics`,
  which covers Deezer, Lyricsify, Megalobiz, Musixmatch and NetEase as well as
  LRCLIB. That is the "bigger pool" - six sources we were not asking before.

Both are best effort. Everything here returns None rather than raising when
spotdl is missing, the network is slow, or nothing credible turns up.
"""

from __future__ import annotations

import difflib
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from lyric_engine import TIMESTAMP, lrc_span

# Set SPOTDL=0 to switch this source off entirely.
ENABLED = os.getenv("SPOTDL", "1").lower() not in ("0", "false", "no", "off")

# The Spotify search is slow - measured around 8s - so it is capped and its
# results are cached. Nothing here is allowed to hold up the web UI.
SEARCH_TIMEOUT = float(os.getenv("SPOTDL_TIMEOUT", "12"))

# How alike a Spotify result has to be before we believe it. Spotify always
# returns its top hit, even for a query that matches nothing at all, so
# without this a made-up song quietly comes back as somebody else's track.
TITLE_SIMILARITY = 0.55
ARTIST_SIMILARITY = 0.45
# A candidate whose length is this far from the file is a different recording.
DURATION_TOLERANCE = 7.0

# Artist tags that carry no information and should not be matched against.
PLACEHOLDER_ARTISTS = {"various artists", "various", "unknown artist", "unknown", ""}

_spotify_lock = threading.Lock()
_spotify_ready = False
_search_cache: dict = {}

# Bracketed suffixes and edition noise that differ between sources but do not
# make it a different song.
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_EDITION = re.compile(
    r"\b(remaster(ed)?|re-?master|radio edit|single version|album version|"
    r"extended( mix| version)?|mono|stereo|deluxe|bonus track|anniversary)\b"
)
_TRAILING_DASH = re.compile(r"\s+-\s+.*$")


def normalise(text: str) -> str:
    """Strip the differences that do not change which song this is."""
    text = (text or "").casefold()
    text = _BRACKETS.sub(" ", text)
    text = _TRAILING_DASH.sub(" ", text)   # "Title - 2011 Remaster"
    text = _EDITION.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def similarity(a: str, b: str) -> float:
    """0 to 1, how alike two titles or artist names are once normalised."""
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # One containing the other counts as a strong match: "Daddy Cool" inside
    # "Daddy Cool - Single Version" is the same song.
    if a in b or b in a:
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def _ensure_spotify():
    """Start spotdl's Spotify client once, using its bundled credentials."""
    global _spotify_ready
    with _spotify_lock:
        if _spotify_ready:
            return True
        try:
            from spotdl.utils.config import DEFAULT_CONFIG
            from spotdl.utils.spotify import SpotifyClient

            try:
                SpotifyClient.init(
                    client_id=os.getenv("SPOTIFY_CLIENT_ID")
                    or DEFAULT_CONFIG["client_id"],
                    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET")
                    or DEFAULT_CONFIG["client_secret"],
                    user_auth=False,
                    cache_path=None,
                    no_cache=True,
                )
            except Exception:
                # Already initialised by something else in this process.
                pass
            _spotify_ready = True
            return True
        except ImportError:
            return False


def available() -> bool:
    """Whether this source can be used at all."""
    if not ENABLED:
        return False
    try:
        import spotdl  # noqa: F401
        import syncedlyrics  # noqa: F401
    except ImportError:
        return False
    return True


def _spotify_search(title, artist=None, album=None, limit=8):
    """Raw Spotify track search. Returns a list of result dicts."""
    if not _ensure_spotify():
        return []

    from spotdl.utils.spotify import SpotifyClient

    # Field filters make Spotify far pickier than a bare keyword search.
    parts = [f'track:"{title}"']
    if artist and normalise(artist) not in PLACEHOLDER_ARTISTS:
        parts.append(f'artist:"{artist}"')
    if album:
        parts.append(f'album:"{album}"')
    query = " ".join(parts)

    try:
        response = SpotifyClient().search(query, type="track", limit=limit)
    except Exception:
        return []
    if not response:
        return []
    return (response.get("tracks") or {}).get("items") or []


def find_track(title: str, artist: str = None, album: str = None,
               duration: float = None):
    """
    Find the Spotify track this file is, refusing anything unconvincing.

    Spotify answers every query with its best guess, however poor, so a
    made-up song comes back as a real but unrelated track. The result is only
    returned when the title and artist genuinely resemble what was asked for,
    and - when a duration is known - when the lengths agree too.

    Parameters:
    title (str): Track title, as tagged.
    artist (str, optional): Artist. "Various Artists" is ignored as a match
        key, since compilations tag it on everything.
    album (str, optional): Album, used only to narrow the search.
    duration (float, optional): Track length in seconds.

    Returns:
    dict: name, artists, album, duration, spotify_id, title_score,
          artist_score - or None if nothing credible matched.
    """
    if not title:
        return None

    key = (normalise(title), normalise(artist or ""), round(duration or 0))
    if key in _search_cache:
        return _search_cache[key]

    results = _spotify_search(title, artist, album)
    if not results:
        # Album names on compilations often do not match Spotify's, so a
        # second pass without it is worth the request.
        if album:
            results = _spotify_search(title, artist)
        if not results:
            _search_cache[key] = None
            return None

    artist_matters = artist and normalise(artist) not in PLACEHOLDER_ARTISTS
    best = None

    for item in results:
        names = [a["name"] for a in item.get("artists", [])]
        title_score = similarity(title, item.get("name", ""))
        artist_score = (
            max((similarity(artist, n) for n in names), default=0.0)
            if artist_matters else 1.0
        )
        if title_score < TITLE_SIMILARITY or artist_score < ARTIST_SIMILARITY:
            continue

        item_duration = item.get("duration_ms", 0) / 1000.0
        gap = (abs(item_duration - duration)
               if duration else 0.0)
        if duration and gap > DURATION_TOLERANCE:
            continue

        # Prefer the closest length, then the best title match.
        score = (gap, -title_score, -artist_score)
        if best is None or score < best[0]:
            best = (score, {
                "name": item.get("name"),
                "artists": names,
                "album": (item.get("album") or {}).get("name"),
                "duration": item_duration,
                "spotify_id": item.get("id"),
                "title_score": round(title_score, 3),
                "artist_score": round(artist_score, 3),
                "duration_gap": round(gap, 2),
            })

    result = best[1] if best else None
    _search_cache[key] = result
    return result


def fetch_lrc(title: str, artist: str = None, synced_only: bool = True):
    """
    Ask syncedlyrics for this track, across all the providers it knows.

    Returns:
    str: the LRC text, or None.
    """
    try:
        import syncedlyrics
    except ImportError:
        return None

    term = f"{title} - {artist}" if artist else title
    try:
        return syncedlyrics.search(term, synced_only=synced_only)
    except Exception:
        # syncedlyrics raises a variety of things when a provider misbehaves;
        # none of them should take the lyrics lookup down with them.
        return None


def _candidate(title, duration, artist=None, album=None, synced=True):
    """The work behind get_candidate(), run inside the timeout."""
    match = find_track(title, artist, album, duration)

    # Search with Spotify's spelling when we have it - that is the point of
    # asking - and fall back to the tags when we do not.
    search_title = match["name"] if match else title
    search_artist = (match["artists"][0] if match and match["artists"]
                     else artist)

    lrc = fetch_lrc(search_title, search_artist, synced_only=synced)
    if not lrc and match:
        # Spotify's title may itself be the problem; try the original tags.
        lrc = fetch_lrc(title, artist, synced_only=synced)
    if not lrc:
        return None

    return {
        "source": "spotdl",
        "lrc": lrc,
        "synced": bool(TIMESTAMP.search(lrc)),
        "span": lrc_span(lrc),
        "match": match,
        # A track Spotify confirmed is worth more than a bare string search.
        "verified": match is not None,
    }


def get_candidate(title: str, duration: float = None, artist: str = None,
                  album: str = None, synced: bool = True,
                  timeout: float = None):
    """
    This source's best offering for a track, or None.

    Runs under a hard timeout: the Spotify search can take the better part of
    ten seconds, and the web UI polls every two.

    Returns:
    dict: source, lrc, synced, span, match, verified - or None.
    """
    if not available():
        return None

    timeout = SEARCH_TIMEOUT if timeout is None else timeout
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_candidate, title, duration, artist, album, synced)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            # Let the thread finish in the background; we just stop waiting.
            return None
        except Exception:
            return None


if __name__ == "__main__":
    import json
    import sys

    title = sys.argv[1] if len(sys.argv) > 1 else "Far From Any Road"
    artist = sys.argv[2] if len(sys.argv) > 2 else "The Handsome Family"
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else None

    print("available:", available())
    match = find_track(title, artist, duration=duration)
    print("spotify match:", json.dumps(match, indent=2) if match else None)
    cand = get_candidate(title, duration, artist)
    if cand:
        print(f"lyrics: synced={cand['synced']} span={cand['span']}s "
              f"verified={cand['verified']}")
    else:
        print("lyrics: none")
