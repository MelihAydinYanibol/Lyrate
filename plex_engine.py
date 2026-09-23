"""Plex - read what is currently playing on a Plex Media Server."""

import itertools
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
        # Unrounded seconds: the web UI needs sub-second precision to keep
        # synced lyrics lined up. "position" stays whole for printing.
        "position_exact": (position_ms / 1000) if position_ms else 0.0,
        "state": player.get("state"),  # "playing", "paused" or "buffering"
        "player": player.get("title"),
        "user": user.get("title"),
        "rating_key": session.get("ratingKey"),
        "thumb": session.get("thumb"),  # art path, proxied by the web UI
        # Needed to send this player transport commands.
        "machine_identifier": player.get("machineIdentifier"),
        # Where the player itself listens, for talking to it directly when
        # relaying through the server does not work.
        "address": player.get("address"),
        "port": player.get("port"),
        "protocol_capabilities": player.get("protocolCapabilities"),
        # A player that advertises "playback" definitely accepts commands.
        # Plex often omits this field entirely, though, and absence is not
        # evidence of absence - so we only rule a player out when it states
        # capabilities that exclude playback. Otherwise let the command run
        # and report whatever the player says.
        "controllable": bool(player.get("machineIdentifier")) and (
            "playback" in player["protocolCapabilities"]
            if player.get("protocolCapabilities")
            else True  # field absent: unknown, so let the command try
        ),
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


# --- Playback control -------------------------------------------------
#
# Commands are sent to the server, which relays them to the target player.
# See https://github.com/plexinc/plex-media-player/wiki/Remote-control-API

# Identifies US as the controller. Any stable string will do.
CLIENT_IDENTIFIER = "lyrate-web-ui"

# Plex expects a number that rises with each command from a controller, so it
# can discard ones that arrive out of order.
_command_counter = itertools.count(1)

# Commands we are willing to relay, and the extra parameter each one takes.
COMMANDS = {
    "play": None,
    "pause": None,
    "stop": None,
    "skipNext": None,
    "skipPrevious": None,
    "seekTo": "offset",        # milliseconds from the start of the track
    "setParameters": "volume",  # 0-100
}


class PlexCommandError(RuntimeError):
    """A player refused a command, or could not be reached to send one."""


# Plex players listen on this port for direct control.
PLAYER_PORT = 32500


def send_command(
    command: str,
    machine_identifier: str,
    value=None,
    base_url: str = None,
    token: str = None,
    address: str = None,
    port: int = None,
):
    """
    Sends a transport command to a Plex player.

    Tries the Plex server first, which relays to the player. Many setups
    answer that with a 500, so if an address for the player is known this
    falls back to talking to it directly.

    Parameters:
    command (str): One of the keys of COMMANDS.
    machine_identifier (str): The target player, from get_track_info().
    value (int, optional): The offset in ms for seekTo, or 0-100 for
        setParameters. Ignored by the commands that take no parameter.
    base_url (str, optional): Plex server address. Defaults to $PLEX_URL.
    token (str, optional): Plex auth token. Defaults to $PLEX_TOKEN.
    address (str, optional): The player's own IP, from get_track_info().
    port (int, optional): The player's control port. Defaults to 32500.

    Returns:
    dict: {"ok": True, "via": <which route worked>}

    Raises:
    ValueError: for an unknown command, a missing target, or a missing value.
    RuntimeError: if no Plex token is configured.
    PlexCommandError: if every route failed, listing what each one said.
    """
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    if not machine_identifier:
        raise ValueError("no target player; this session has no machine identifier")

    base_url = (base_url or DEFAULT_URL).rstrip("/")
    token = token or DEFAULT_TOKEN
    if not token:
        raise RuntimeError("No Plex token. Set the PLEX_TOKEN environment variable.")

    params = {"type": "music", "commandID": next(_command_counter)}
    param_name = COMMANDS[command]
    if param_name:
        if value is None:
            raise ValueError(f"{command} needs a {param_name}")
        params[param_name] = int(value)

    headers = {
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
        "X-Plex-Target-Client-Identifier": machine_identifier,
        "Accept": "application/json",
    }

    routes = [("server relay", base_url)]
    if address:
        routes.append(("direct to player", f"http://{address}:{port or PLAYER_PORT}"))

    failures = []
    for label, root in routes:
        try:
            response = requests.get(
                f"{root}/player/playback/{command}",
                params=params,
                headers=headers,
                timeout=8,
            )
        except requests.RequestException as exc:
            failures.append(f"{label}: unreachable ({exc.__class__.__name__})")
            continue
        if response.ok:
            return {"ok": True, "via": label}
        detail = " ".join(response.text.split())[:160]
        failures.append(f"{label}: HTTP {response.status_code} {detail}")

    raise PlexCommandError("; ".join(failures))


def command_for_track(track, command, value=None):
    """Send a command to the player a get_track_info() dict came from."""
    return send_command(
        command,
        track.get("machine_identifier"),
        value,
        address=track.get("address"),
        port=track.get("port"),
    )


def play(machine_identifier, **kwargs):
    """Resume playback on a player."""
    return send_command("play", machine_identifier, **kwargs)


def pause(machine_identifier, **kwargs):
    """Pause playback on a player."""
    return send_command("pause", machine_identifier, **kwargs)


def skip_next(machine_identifier, **kwargs):
    """Skip to the next track."""
    return send_command("skipNext", machine_identifier, **kwargs)


def skip_previous(machine_identifier, **kwargs):
    """Skip to the previous track."""
    return send_command("skipPrevious", machine_identifier, **kwargs)


def seek_to(machine_identifier, seconds, **kwargs):
    """Seek to a position, given in seconds (Plex wants milliseconds)."""
    return send_command("seekTo", machine_identifier, round(seconds * 1000), **kwargs)


def set_volume(machine_identifier, volume, **kwargs):
    """Set player volume, 0-100."""
    return send_command(
        "setParameters", machine_identifier, max(0, min(100, int(volume))), **kwargs
    )


# --- Play queue -------------------------------------------------------
#
# /status/sessions says nothing about the play queue, so "is there a next
# track?" cannot be answered from the server alone. The player itself knows:
# its timeline carries a playQueueID, and the server will then describe that
# queue. Both steps are best effort - anything missing means "unknown", and
# the caller should leave the skip buttons enabled rather than guess.


def get_timeline(address: str, machine_identifier: str, port: int = None,
                 media_type: str = "music"):
    """
    Asks a player directly what it is doing.

    Returns:
    dict: the matching Timeline entry, or None if the player cannot be
          reached or reports nothing for this media type.
    """
    if not address or not machine_identifier:
        return None

    try:
        response = requests.get(
            f"http://{address}:{port or PLAYER_PORT}/player/timeline/poll",
            params={"wait": 0, "commandID": next(_command_counter)},
            headers={
                "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                "X-Plex-Device-Name": "Lyrate",
                "X-Plex-Target-Client-Identifier": machine_identifier,
                "Accept": "application/json",
            },
            timeout=3,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return None

    try:
        timelines = response.json().get("MediaContainer", {}).get("Timeline", [])
    except ValueError:
        return None

    for timeline in timelines:
        if timeline.get("type") == media_type:
            return timeline
    return None


def get_queue_position(play_queue_id, base_url: str = None, token: str = None):
    """
    Asks the server where we are in a play queue.

    Returns:
    dict: {"offset": int, "total": int, "has_previous": bool,
           "has_next": bool}, or None if the queue cannot be read.
    """
    if not play_queue_id:
        return None

    base_url = (base_url or DEFAULT_URL).rstrip("/")
    token = token or DEFAULT_TOKEN
    try:
        response = requests.get(
            f"{base_url}/playQueues/{play_queue_id}",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=8,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return None

    try:
        container = response.json().get("MediaContainer", {})
    except ValueError:
        return None

    offset = container.get("playQueueSelectedItemOffset")
    total = container.get("playQueueTotalCount", container.get("size"))
    if offset is None or total is None:
        return None

    return {
        "offset": offset,
        "total": total,
        "has_previous": offset > 0,
        "has_next": offset < total - 1,
    }


def get_queue_state(track: dict):
    """
    Works out whether the current track has anything before or after it.

    Parameters:
    track (dict): one from get_now_playing().

    Returns:
    dict: as get_queue_position(), or None when it cannot be determined -
          which the UI should treat as "unknown", not as "no".
    """
    timeline = get_timeline(track.get("address"),
                            track.get("machine_identifier"),
                            track.get("port"))
    if not timeline:
        return None
    return get_queue_position(timeline.get("playQueueID"))
