"""Lyrate - fetch song lyrics from a pool of sources.

LRCLIB is asked directly, because it matches on the track's duration, which
is the strongest signal there is for picking the right version of a song.
spotdl is asked in parallel: it resolves the track against Spotify first and
then searches six more lyric providers, which covers songs LRCLIB has not.

The two are then judged against the same thing - how far the lyrics run
compared with how long the file actually is.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import requests

# Matches an LRC timestamp: [mm:ss.xx], [mm:ss.xxx] or [mm:ss]
TIMESTAMP = re.compile(r"\[(\d+):(\d{2}(?:[.:]\d+)?)\]")

# A candidate whose lyrics run past the end of the file is a different, longer
# recording - allow only this much overrun.
SPAN_OVERRUN = 3.0
# ...and it has to cover at least this much of the file to be worth believing.
MIN_COVERAGE = 0.45

# LRCLIB is free and key-less, but asks clients to identify themselves.
USER_AGENT = "Lyrate v0.1.0 (https://github.com/MelihAydinYanibol/Lyrate)"


def lrclib_candidate(
    title: str,
    duration: int,
    artist: str = None,
    album: str = None,
    synced: bool = True,
):
    """
    Fetches the lyrics for a given song title, optionally filtered by artist name and album name.

    Parameters:
    title (str): The title of the song.
    duration (int): The track's length in seconds. Used to pick between
        several versions of the same song; the closest match wins.
    artist (str, optional): The name of the artist. Defaults to None.
    album (str, optional): The name of the album. Defaults to None.
    synced (bool, optional): Return time-synced LRC lyrics. Defaults to True.

    Returns:
    dict: a pool candidate (source, lrc, synced, span, ...), or None.
    """
    field = "syncedLyrics" if synced else "plainLyrics"
    headers = {"User-Agent": USER_AGENT}

    # With an artist we can ask for an exact match; requests drops None params.
    if artist:
        response = requests.get(
            "https://lrclib.net/api/get",
            params={
                "track_name": title,
                "artist_name": artist,
                "album_name": album,
                # LRCLIB only accepts this match within +/-2s of its record,
                # so a near miss 404s and falls through to the search below.
                "duration": duration,
            },
            headers=headers,
            timeout=10,
        )
        # Plenty of records are plain-text only, so an exact match that has no
        # synced lyrics still falls through to the search below.
        if response.ok:
            record = response.json()
            lyrics = record.get(field)
            if lyrics:
                return _candidate(record, lyrics, synced)

    # No artist, no exact match, or no synced lyrics on it: search instead.
    # The album is deliberately left out of the query: LRCLIB's free-text
    # search matches it poorly and including it usually returns nothing.
    query = " ".join(filter(None, [title, artist]))
    response = requests.get(
        "https://lrclib.net/api/search",
        params={"q": query},
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()

    # Only records carrying the format we asked for are eligible; among those,
    # the one whose length is closest to `duration` wins. Records with no
    # duration at all sort last rather than being discarded outright.
    candidates = [result for result in response.json() if result.get(field)]
    if not candidates:
        return None

    def duration_gap(result):
        if result.get("duration") is None:
            return float("inf")
        return abs(result["duration"] - duration)

    best = min(candidates, key=duration_gap)
    return _candidate(best, best[field], synced)


def _candidate(record, lyrics, synced):
    """Wrap an LRCLIB record as a pool candidate."""
    return {
        "source": "lrclib",
        "lrc": lyrics,
        "synced": bool(synced and TIMESTAMP.search(lyrics or "")),
        "span": lrc_span(lyrics),
        # LRCLIB stores a duration per record, which is a stronger signal
        # than the span, so keep it for the comparison.
        "record_duration": record.get("duration"),
        "verified": record.get("duration") is not None,
    }





# --- the pool ---------------------------------------------------------


def lrc_span(lrc: str):
    """How far into the track the lyrics run, from the last timestamp."""
    if not lrc:
        return None
    latest = None
    for minutes, seconds in TIMESTAMP.findall(lrc):
        at = int(minutes) * 60 + float(seconds.replace(":", "."))
        if latest is None or at > latest:
            latest = at
    return latest


def parse_lrc(lrc: str):
    """
    Turn an LRC string into [{"time": seconds, "text": str}].

    Lines with no timestamp (metadata like "[ar:Boney M.]") are dropped.
    Timestamped lines with no words are kept: they mark instrumental gaps.
    """
    lines = []
    for raw in (lrc or "").splitlines():
        stamps = TIMESTAMP.findall(raw)
        if not stamps:
            continue
        text = TIMESTAMP.sub("", raw).strip()
        for minutes, seconds in stamps:
            lines.append({"time": int(minutes) * 60 + float(seconds.replace(":", ".")),
                          "text": text})
    lines.sort(key=lambda line: line["time"])
    return lines


def fits_duration(span, duration):
    """
    Whether lyrics of this length plausibly belong to a file of that length.

    Lyrics finish before the music does - an outro can run a long way past the
    last word - so this is deliberately loose at the bottom end and strict at
    the top: running PAST the end means it is a different, longer recording.
    """
    if not span or not duration:
        return False
    if span > duration + SPAN_OVERRUN:
        return False
    return span >= duration * MIN_COVERAGE


def choose(candidates, duration):
    """
    Pick the best candidate from the pool.

    spotdl wins when its lyrics fit the file's length, because it got there by
    confirming the track against Spotify first - a stronger provenance than a
    keyword match. When they do not fit, LRCLIB's duration-matched record is
    the safer answer.
    """
    usable = [c for c in candidates if c and c.get("lrc")]
    if not usable:
        return None

    synced = [c for c in usable if c.get("synced")]
    pool = synced or usable

    spotdl = next((c for c in pool if c["source"] == "spotdl"), None)
    lrclib = next((c for c in pool if c["source"] == "lrclib"), None)

    if spotdl and spotdl.get("verified") and fits_duration(spotdl.get("span"), duration):
        return spotdl
    if lrclib:
        return lrclib
    return spotdl or pool[0]


def get_best(title: str, duration: float = None, artist: str = None,
             album: str = None, synced: bool = True, sources=None):
    """
    Ask the sources and return the winning candidate.

    Parameters:
    sources (iterable, optional): which sources to consult - "lrclib",
        "spotdl", or both by default. LRCLIB answers in about a second;
        spotdl takes closer to ten, because it resolves the track against
        Spotify first. Callers that cannot wait can ask for one, then the
        other, and keep whichever wins.

    Returns:
    dict: source, lrc, synced, span - or None if nothing was found.
    """
    import spotdl_engine   # local: keeps this module importable without it

    wanted = set(sources) if sources else {"lrclib", "spotdl"}
    jobs = []
    if "lrclib" in wanted:
        jobs.append(lambda: lrclib_candidate(title, duration, artist, album, synced))
    if "spotdl" in wanted and spotdl_engine.available():
        jobs.append(lambda: spotdl_engine.get_candidate(
            title, duration, artist, album, synced))
    if not jobs:
        return None

    candidates = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for future in [pool.submit(job) for job in jobs]:
            try:
                candidates.append(future.result())
            except Exception:
                candidates.append(None)   # one bad source must not sink the rest

    return choose(candidates, duration)


def get_lyrics(title: str, duration: int, artist: str = None,
               album: str = None, synced: bool = True):
    """
    Fetches the lyrics for a given song title, optionally filtered by artist
    name and album name.

    Parameters:
    title (str): The title of the song.
    duration (int): The track's length in seconds, used to pick between
        versions and to judge whether a candidate really fits.
    artist (str, optional): The name of the artist.
    album (str, optional): The name of the album.
    synced (bool, optional): Prefer time-synced LRC. Defaults to True.

    Returns:
    str: The lyrics, or None if no source had them.
    """
    best = get_best(title, duration, artist, album, synced)
    return best["lrc"] if best else None


if __name__ == "__main__":
    import sys

    title = sys.argv[1] if len(sys.argv) > 1 else "Daddy Cool"
    artist = sys.argv[2] if len(sys.argv) > 2 else "Boney M."
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 189

    best = get_best(title, duration, artist)
    if best:
        print(f"source={best['source']} synced={best['synced']} "
              f"span={best['span']}s of {duration}s "
              f"fits={fits_duration(best['span'], duration)}")
    else:
        print("no lyrics found")
