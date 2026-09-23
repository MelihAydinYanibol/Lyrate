"""Find out why playback controls do or do not work.

Remote control is the fiddliest part of the Plex API: the player has to
advertise itself as controllable, and the command has to reach it by a route
your network allows. This dumps what your player actually reports and then
tries each route in turn, printing exactly what comes back.

Run it with music playing:

    python diagnose_control.py

By default it only sends a harmless `setParameters` (volume) probe against the
CURRENT volume, so nothing audibly changes. Pass --pause to test a real
transport command instead, which will pause your music:

    python diagnose_control.py --pause
"""

import json
import sys

import requests

from plex_engine import (
    CLIENT_IDENTIFIER,
    DEFAULT_TOKEN,
    DEFAULT_URL,
    PLAYER_PORT,
    get_active_sessions,
    get_now_playing,
)


def dump_player():
    """Show the raw Player object Plex gives us for the current session."""
    sessions = get_active_sessions()
    if not sessions:
        print("Nothing is playing. Start a track and run this again.")
        return None, None

    raw = sessions[0]
    player = raw.get("Player") or {}
    track = get_now_playing()[0]

    print("=" * 62)
    print("What Plex reports about your player")
    print("=" * 62)
    print(json.dumps(player, indent=2, sort_keys=True))
    print()

    # Anything here naming a play queue would let us tell whether a next or
    # previous track exists. Plex does not document such a field on sessions.
    queue_keys = sorted(k for k in raw if "ueue" in k or "ndex" in k)
    print("Session keys that might describe the play queue:")
    print(f"  {queue_keys if queue_keys else '(none)'}")
    print(f"  all session keys: {sorted(raw.keys())}")
    print()

    caps = player.get("protocolCapabilities")
    print(f"machineIdentifier    : {player.get('machineIdentifier')}")
    print(f"protocolCapabilities : {caps if caps else '(absent)'}")
    print(f"address / port       : {player.get('address')} / {player.get('port')}")
    print(f"local / relayed      : {player.get('local')} / {player.get('relayed')}")
    print()

    if not player.get("machineIdentifier"):
        print("  No machineIdentifier: there is nothing to address commands to.")
        print("  This player cannot be controlled at all.")
    elif caps and "playback" not in caps:
        print("  This player advertises capabilities but NOT 'playback'.")
        print("  Turn on 'Advertise as player' in its settings.")
    elif not caps:
        print("  No capabilities advertised. That does not necessarily mean it")
        print("  refuses commands - Plex often omits the field - so the routes")
        print("  below are still worth trying.")
    else:
        print("  Advertises 'playback', so it should accept commands.")
    print()
    return player, track


def try_route(label, root, player, command, params):
    """Send one command by one route and report verbatim what happened."""
    url = f"{root}/player/playback/{command}"
    headers = {
        "X-Plex-Token": DEFAULT_TOKEN,
        "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
        "X-Plex-Target-Client-Identifier": player.get("machineIdentifier"),
        "Accept": "application/json",
    }
    print(f"--- {label}")
    print(f"    GET {url}")
    try:
        response = requests.get(url, params=params, headers=headers, timeout=8)
    except requests.RequestException as exc:
        print(f"    UNREACHABLE: {exc.__class__.__name__}: {exc}\n")
        return False

    body = " ".join(response.text.split())[:300]
    print(f"    HTTP {response.status_code}")
    if body:
        print(f"    {body}")
    print(f"    => {'WORKS' if response.ok else 'failed'}\n")
    return response.ok


def main():
    use_pause = "--pause" in sys.argv

    player, track = dump_player()
    if not player or not player.get("machineIdentifier"):
        return

    if use_pause:
        command = "pause"
        params = {"type": "music", "commandID": 1}
        print("Probing with a real 'pause' command - your music will stop.\n")
    else:
        # setParameters with the volume it already has: accepted like any
        # other command, but nothing audibly changes.
        command = "setParameters"
        params = {"type": "music", "commandID": 1, "volume": 100}
        print("Probing with a harmless 'setParameters' command.")
        print("(Use --pause to test a real transport command instead.)\n")

    print("=" * 62)
    print("Trying each route")
    print("=" * 62)

    routes = [("server relay", DEFAULT_URL.rstrip("/"))]
    address = player.get("address")
    if address:
        port = player.get("port") or PLAYER_PORT
        routes.append(("direct to player", f"http://{address}:{port}"))
        if port != PLAYER_PORT:
            routes.append(("direct, port 32500", f"http://{address}:{PLAYER_PORT}"))
    else:
        print("(Plex reported no address for this player, so the direct route")
        print(" cannot be tried.)\n")

    working = [label for label, root in routes
               if try_route(label, root, player, command, params)]

    print("=" * 62)
    print("Verdict")
    print("=" * 62)
    if working:
        print(f"  Working route(s): {', '.join(working)}")
        print("  The web UI tries the server relay first and falls back to the")
        print("  direct route, so the controls should work.")
    else:
        print("  No route worked. The usual causes, in order of likelihood:")
        print()
        print("  1. 'Advertise as player' is off in the player's settings.")
        print("     In Plexamp: Settings > Playback > Advertise as player.")
        print("  2. The player is on another network, or a firewall blocks")
        print("     port 32500 between this machine and it.")
        print("  3. The player genuinely has no remote control support.")
        print()
        print("  Paste the output above and I can narrow it down further.")


if __name__ == "__main__":
    main()
