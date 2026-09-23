"""Lyrate - fetch song lyrics from LRCLIB (https://lrclib.net)."""

import requests

# LRCLIB is free and key-less, but asks clients to identify themselves.
USER_AGENT = "Lyrate v0.1.0 (https://github.com/MelihAydinYanibol/Lyrate)"


def get_lyrics(
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
    str: The lyrics of the song, or None if no match was found.
         Synced lyrics look like "[00:09.65] The club isn't the best...".
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
            lyrics = response.json().get(field)
            if lyrics:
                return lyrics

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

    return min(candidates, key=duration_gap)[field]


if __name__ == "__main__":
    print(get_lyrics(title="Daddy Cool", artist="Boney M.", duration=189))
