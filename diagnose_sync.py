"""Measure how well Plex reports playback position, for lyric timing.

Synced lyrics are only as good as the playhead they are matched against, and
that playhead comes from Plex's `viewOffset`. Plex clients refresh it on their
own schedule: some every second, some only every five or ten. This script
watches a playing track and reports what yours actually does.

Run it with music playing:

    python diagnose_sync.py

It makes no changes; it only reads.
"""

import statistics
import sys
import time

import requests

from plex_engine import get_now_playing

SAMPLE_INTERVAL = 0.5   # seconds between reads
DEFAULT_DURATION = 40   # seconds to watch


def sample(duration_s):
    """Poll Plex, recording (wall clock, reported position, request latency)."""
    samples = []
    started = time.monotonic()
    print(f"Watching for {duration_s}s - leave the track playing...\n")

    while time.monotonic() - started < duration_s:
        t0 = time.monotonic()
        try:
            tracks = get_now_playing()
        except requests.RequestException as exc:
            print(f"  Plex unreachable: {exc}")
            time.sleep(SAMPLE_INTERVAL)
            continue
        latency = time.monotonic() - t0

        playing = [t for t in tracks if t.get("state") == "playing"]
        if not playing:
            print("  nothing playing - start a track")
            time.sleep(SAMPLE_INTERVAL)
            continue

        track = playing[0]
        pos = track.get("position_exact", track.get("position"))
        samples.append((time.monotonic(), pos, latency, track))
        time.sleep(SAMPLE_INTERVAL)

    return samples


def report(samples):
    if len(samples) < 4:
        print("Not enough samples. Is something actually playing?")
        return

    track = samples[-1][3]
    print(f"Track    : {track.get('title')} - {track.get('artist')}")
    print(f"Player   : {track.get('player')} ({track.get('user')})")
    print(f"Samples  : {len(samples)}\n")

    latencies = [s[2] for s in samples]
    print("Request latency")
    print(f"  median {statistics.median(latencies)*1000:6.0f} ms")
    print(f"  worst  {max(latencies)*1000:6.0f} ms")
    print("  (the web UI compensates for this, but high values mean a")
    print("   coarser correction)\n")

    # How often does the reported position actually change, and by how much?
    steps, step_gaps, last_change_at, last_pos = [], [], samples[0][0], samples[0][1]
    for clock, pos, _, _ in samples[1:]:
        if pos != last_pos:
            steps.append(pos - last_pos)
            step_gaps.append(clock - last_change_at)
            last_change_at, last_pos = clock, pos

    print("Position reporting")
    if not steps:
        print("  The reported position NEVER changed during the run.")
        print("  This client reports very lazily; expect loose lyric timing.")
        return

    print(f"  changed {len(steps)} times in {samples[-1][0]-samples[0][0]:.0f}s")
    print(f"  median gap between updates : {statistics.median(step_gaps):.2f}s")
    print(f"  median jump per update     : {statistics.median(steps):.2f}s")
    print(f"  largest jump               : {max(steps):.2f}s\n")

    # Does it advance at real time? A rate far from 1.0 means trouble.
    span = samples[-1][0] - samples[0][0]
    advanced = samples[-1][1] - samples[0][1]
    print("Rate check")
    print(f"  wall clock advanced {span:.1f}s, Plex position advanced {advanced:.1f}s")
    if len(steps) >= 5:
        print(f"  rate = {advanced/span:.3f} (1.000 is perfect)\n")
    else:
        print("  too few updates to judge the rate - a number below 1.0 here")
        print("  just reflects the coarse reporting above, not real drift\n")

    worst_lag = max(steps)
    print("Verdict")
    print(f"  Your player tells Plex where it is about every "
          f"{statistics.median(step_gaps):.0f}s, so the value Plex")
    print(f"  serves can be up to {worst_lag:.0f}s out of date.")
    print()
    if worst_lag <= 2:
        print("  That is frequent enough that lyric timing should be tight.")
    else:
        print("  The web UI handles this: rather than believing every poll, it")
        print("  re-anchors only when the reported value CHANGES - the moment")
        print("  your player actually reported in - and runs its own clock in")
        print("  between. Expect around +/-1s regardless of the gap above.")
        print()
        print("  One thing it cannot fix: after you seek, the display stays")
        print(f"  wrong until your player next reports, up to {worst_lag:.0f}s later.")
    print()
    print("  Only set LYRICS_OFFSET if lyrics are consistently early or late")
    print("  once playing steadily. It is a global nudge for LRC files that")
    print("  are transcribed off-beat, NOT a fix for the staleness above.")
    print("  Negative pulls lyrics earlier, positive pushes them later.")


if __name__ == "__main__":
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DURATION
    report(sample(seconds))
