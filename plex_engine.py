"""Plex - read what is currently playing on a Plex Media Server."""

import os
import dotenv
import requests

dotenv.load_dotenv()  # read .env file if present
# Keep credentials out of the source: set these in your environment.
#   PLEX_URL    e.g. http://192.168.1.10:32400
#   PLEX_TOKEN  see https://support.plex.tv/articles/204059436
DEFAULT_URL = os.environ.get("PLEX_URL", "http://localhost:32400")
DEFAULT_TOKEN = os.environ.get("PLEX_TOKEN")


def get_active_sessions(base_url: str = None, token: str = None):
    """
    Returns every music track currently being played on the Plex server.

    Parameters:
    base_url (str, optional): Plex server address. Defaults to $PLEX_URL.
    token (str, optional): Plex auth token. Defaults to $PLEX_TOKEN.

    Returns:
    list: The raw Plex session dicts whose type is "track". Empty when no
          music is playing. Use get_track_info() to tidy one up.
    """
    base_url = (base_url or DEFAULT_URL).rstrip("/")
    token = token or DEFAULT_TOKEN
    if not token:
        raise RuntimeError(
            "No Plex token. Set the PLEX_TOKEN environment variable, or pass "
            "token= explicitly."
        )

    response = requests.get(
        f"{base_url}/status/sessions",
        headers={"X-Plex-Token": token, "Accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()

    container = response.json().get("MediaContainer", {})
    # Plex omits Metadata entirely when nothing is playing, rather than
    # sending an empty list.
    sessions = container.get("Metadata", [])
    return [s for s in sessions if s.get("type") == "track"]


def get_track_info(session: dict):
    """
    Pulls the useful fields out of one Plex session.

    Plex reports times in milliseconds; both duration and position are
    converted to seconds here, which is what LRCLIB expects.

    Parameters:
    session (dict): One entry from get_active_sessions().

    Returns:
    dict: title, artist, album, duration, position, state, player, user
          and rating_key. Missing values come back as None.
    """
    player = session.get("Player") or {}
    user = session.get("User") or {}

    duration_ms = session.get("duration")
    position_ms = session.get("viewOffset")

    return {
        "title": session.get("title"),
        "artist": session.get("grandparentTitle"),  # Plex nests track>album>artist
        "album": session.get("parentTitle"),
        "duration": round(duration_ms / 1000) if duration_ms else None,
        "position": round(position_ms / 1000) if position_ms else 0,
        "state": player.get("state"),  # "playing", "paused" or "buffering"
        "player": player.get("title"),
        "user": user.get("title"),
        "rating_key": session.get("ratingKey")
    }


def get_now_playing(base_url: str = None, token: str = None):
    """
    Returns tidied-up info for every music track playing right now.

    Shorthand for calling get_track_info() on each active session.

    Returns:
    list: One dict per music session, as described in get_track_info().
    """
    return [get_track_info(s) for s in get_active_sessions(base_url, token)]


if __name__ == "__main__":
    tracks = get_now_playing()
    if not tracks:
        print("Nothing playing.")
    for track in tracks:
        print(
            f"{track['title']} - {track['artist']} ({track['album']}) "
            f"[{track['position']}s / {track['duration']}s] "
            f"{track['state']} on {track['player']} / {track['user']}" 
        )
