"""Lyrate web UI - a live lyrics display for whatever is playing on Plex."""

import hashlib
import json
import os
import threading
import time

import dotenv
import requests
from flask import Flask, Response, jsonify, render_template, request

from lyric_engine import choose, get_best, parse_lrc
import plex_engine
from plex_engine import DEFAULT_TOKEN, DEFAULT_URL, get_now_playing

dotenv.load_dotenv()

OBSERVED_USER = os.getenv("OBSERVED_USER")  # only follow this Plex user, if set

# Nudge the lyric timing if it consistently runs early or late, in seconds.
# Positive values push the lyrics later. Some Plex clients report their
# position lazily, and some LRC files are simply transcribed a beat off.
LYRICS_OFFSET = float(os.getenv("LYRICS_OFFSET", "0"))

# Per-song nudges, on top of LYRICS_OFFSET. Kept in a small file so they
# survive a restart - an offset you dialled in once should stay dialled in.
OFFSETS_FILE = os.getenv("LYRIC_OFFSETS_FILE", "lyric_offsets.json")
# Beyond this the lyrics are not merely out of step, they are the wrong file.
MAX_OFFSET = 30.0

_offsets = {}
_offsets_lock = threading.Lock()


def _offset_key(track):
    """Identify a song across restarts, independent of Plex's rating keys."""
    return "|".join([
        (track.get("title") or "").strip().casefold(),
        (track.get("artist") or "").strip().casefold(),
        str(round(track.get("duration") or 0)),
    ])


def _load_offsets():
    try:
        with open(OFFSETS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items()}
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        # A corrupt file should not stop the app; it is only a convenience.
        pass
    return {}


def _save_offsets():
    """Write via a temporary file so a crash cannot leave a half-written one."""
    temporary = f"{OFFSETS_FILE}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(_offsets, handle, indent=1, sort_keys=True)
        os.replace(temporary, OFFSETS_FILE)
    except OSError:
        pass


_offsets = _load_offsets()

app = Flask(__name__)

# Whether a next/previous track exists needs two extra requests, so it is
# cached: it only changes when the track or the queue does.
_queue_cache = {"key": None, "at": 0.0, "value": None}
QUEUE_TTL = 10.0

# LRCLIB is rate-limited and the browser polls every couple of seconds, so
# lyrics are looked up once per track and kept. None is cached too, so a track
# with no lyrics is not retried on every poll.
_lyrics_cache = {}

def pick_track(tracks):
    """Choose which session to follow: a playing one, from OBSERVED_USER."""
    if OBSERVED_USER:
        tracks = [t for t in tracks if t.get("user") == OBSERVED_USER]
    playing = [t for t in tracks if t.get("state") == "playing"]
    # Fall back to a paused track so the UI keeps showing the last song.
    return (playing or tracks or [None])[0]


# The winning candidate behind each cached entry, so a later, better one can
# be compared against it.
_candidates = {}
_lyrics_lock = threading.Lock()
_upgrading = set()


def _entry(best):
    """Turn a pool candidate into what the browser receives."""
    if not best:
        return None
    lines = (parse_lrc(best["lrc"]) if best["synced"]
             else [{"time": None, "text": t} for t in best["lrc"].splitlines()])
    return {
        "synced": best["synced"],
        "lines": lines,
        "source": best.get("source"),
        "span": best.get("span"),
        # Identifies the lyrics by their CONTENT, so the page re-renders only
        # when the words actually change. Two sources often hold the same LRC
        # file, and re-rendering then would throw away the reader's scroll
        # position for nothing.
        "id": hashlib.sha1(best["lrc"].encode("utf-8")).hexdigest()[:12],
    }


def _find(track, sources, synced=True):
    """One pool lookup, synced first and plain text as a fallback."""
    best = get_best(
        title=track["title"], duration=track["duration"],
        artist=track.get("artist"), album=track.get("album"),
        synced=True, sources=sources,
    )
    if not best:
        best = get_best(
            title=track["title"], duration=track["duration"],
            artist=track.get("artist"), album=track.get("album"),
            synced=False, sources=sources,
        )
    return best


def _upgrade(key, track):
    """Ask the slow sources in the background and keep the better answer."""
    try:
        candidate = _find(track, ("spotdl",))
        if not candidate:
            return
        with _lyrics_lock:
            current = _candidates.get(key)
            winner = choose([current, candidate], track.get("duration"))
            if winner is candidate:
                _candidates[key] = candidate
                _lyrics_cache[key] = _entry(candidate)
    except Exception:
        pass          # a failed upgrade just leaves the first answer in place
    finally:
        _upgrading.discard(key)


def lyrics_for(track):
    """
    Lyrics for a track, cached, with the slow sources consulted in the
    background.

    LRCLIB answers in about a second and spotdl in about ten, so waiting for
    both would leave the first verse of every new track blank. The fast
    answer is shown straight away and quietly replaced if spotdl turns out to
    have a better match.
    """
    key = (track.get("title"), track.get("artist"), track.get("duration"))

    if key not in _lyrics_cache:
        try:
            fast = _find(track, ("lrclib",))
        except requests.RequestException:
            return None      # transient: do not cache, retry on the next poll
        with _lyrics_lock:
            _candidates[key] = fast
            _lyrics_cache[key] = _entry(fast)

    # Consult the slower pool once per track, whatever the fast one found.
    if key not in _upgrading and spotdl_available():
        _upgrading.add(key)
        threading.Thread(target=_upgrade, args=(key, track), daemon=True).start()

    return _lyrics_cache[key]


def spotdl_available():
    try:
        import spotdl_engine
        return spotdl_engine.available()
    except Exception:
        return False


def queue_state_for(track):
    """Cached "is there a next/previous track", or None if unknown."""
    key = (track.get("rating_key"), track.get("machine_identifier"))
    now = time.monotonic()
    if _queue_cache["key"] != key or now - _queue_cache["at"] > QUEUE_TTL:
        _queue_cache.update(
            key=key, at=now, value=plex_engine.get_queue_state(track)
        )
    return _queue_cache["value"]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/now-playing")
def now_playing():
    """Current track plus its lyrics, for the browser to poll."""
    # Time the Plex call. The position it reports is accurate as of somewhere
    # inside that call, so treat its midpoint as the moment of the reading;
    # by the time the browser sees this response the reading has aged.
    plex_started = time.monotonic()
    try:
        tracks = get_now_playing()
    except requests.RequestException as exc:
        return jsonify({"error": f"Cannot reach Plex: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    sampled_at = (plex_started + time.monotonic()) / 2

    track = pick_track(tracks)
    if not track:
        return jsonify({"playing": False})

    lyrics = lyrics_for(track) if track.get("duration") else None

    return jsonify(
        {
            "playing": True,
            "track": track,
            "lyrics": lyrics,
            # None means "could not tell", which the UI treats as "maybe",
            # leaving the skip buttons enabled rather than wrongly greying
            # them out.
            "queue": queue_state_for(track),
            # How stale the position reading already is. The browser adds this
            # (plus its own round-trip) before interpolating, so the lyrics do
            # not run behind by the length of the request.
            "sample_age": time.monotonic() - sampled_at,
            "offset": LYRICS_OFFSET,
            # This song's own nudge, set from the buttons in the UI.
            "track_offset": _offsets.get(_offset_key(track), 0.0),
        }
    )


@app.route("/api/offset", methods=["POST"])
def set_offset():
    """Store a per-song lyric shift, in seconds. Positive delays the lyrics."""
    body = request.get_json(silent=True) or {}
    try:
        value = float(body.get("value", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "value must be a number"}), 400
    value = max(-MAX_OFFSET, min(MAX_OFFSET, round(value, 3)))

    try:
        tracks = get_now_playing()
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 502

    track = pick_track(tracks)
    if not track:
        return jsonify({"error": "Nothing is playing"}), 409

    key = _offset_key(track)
    with _offsets_lock:
        if value:
            _offsets[key] = value
        else:
            _offsets.pop(key, None)   # zero is the default; do not store it
        _save_offsets()

    return jsonify({"ok": True, "value": value})


@app.route("/api/control/<command>", methods=["POST"])
def control(command):
    """Relay a transport command to whichever player we are following."""
    body = request.get_json(silent=True) or {}

    try:
        tracks = get_now_playing()
    except requests.RequestException as exc:
        return jsonify({"error": f"Cannot reach Plex: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    track = pick_track(tracks)
    if not track:
        return jsonify({"error": "Nothing is playing"}), 409
    if not track.get("machine_identifier"):
        return jsonify({"error": "This player cannot be controlled"}), 409

    # "playPause" is ours, not Plex's: turn it into the right command for
    # whatever the player is doing right now.
    if command == "playPause":
        command = "pause" if track.get("state") == "playing" else "play"

    # seekTo is given in seconds by the browser; Plex wants milliseconds.
    value = body.get("value")
    if command == "seekTo" and value is not None:
        value = round(float(value) * 1000)

    try:
        result = plex_engine.command_for_track(track, command, value)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except plex_engine.PlexCommandError as exc:
        # Pass the player's own words through, so the UI can show why.
        return jsonify({"error": str(exc)}), 502
    except requests.RequestException as exc:
        return jsonify({"error": f"Cannot reach Plex: {exc}"}), 502

    return jsonify({"ok": True, "command": command, "via": result.get("via")})


@app.route("/api/art")
def art():
    """Proxy Plex artwork, which needs the token the browser must not have."""
    path = request.args.get("path", "")
    # Only ever proxy server-relative Plex paths.
    if not path.startswith("/") or ".." in path:
        return Response("bad path", status=400)

    upstream = requests.get(
        f"{DEFAULT_URL.rstrip('/')}{path}",
        headers={"X-Plex-Token": DEFAULT_TOKEN},
        timeout=10,
    )
    if not upstream.ok:
        return Response("not found", status=upstream.status_code)

    return Response(
        upstream.content,
        content_type=upstream.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


if __name__ == "__main__":
    # Localhost only by default. Set HOST=0.0.0.0 to reach it from a phone on
    # the same network - but note that also exposes the artwork proxy and the
    # playback controls to everyone on that network, with no authentication.
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    if host not in ("127.0.0.1", "localhost"):
        print(f" * Reachable on the local network at http://{host}:{port}")
        print(" * Anyone on this network can control playback. Do not do this")
        print("   on a network you do not trust.")
    app.run(host=host, port=port, debug=True)
