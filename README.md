# Lyrate

Fetch song lyrics from [LRCLIB](https://lrclib.net), a free lyrics API that
needs no key and no registration.

## Setup

```bash
python -m venv env
source env/bin/activate  # Windows: env\Scripts\activate
pip install -r requirements.txt
```

## Usage

```python
from main import get_lyrics

print(get_lyrics(title="Daddy Cool", artist="Boney M.", duration=189))
# [00:05.79] She's crazy like a fool
```

Returns time-synced LRC lyrics as a string, or `None` if nothing matched.
Pass `synced=False` for plain text instead.

### Why duration matters

Popular songs have dozens of records in LRCLIB — live versions, remasters,
edits — all under the same title. `duration` (in seconds) is what picks between
them: **the record closest to it wins**, considering only records that actually
carry synced lyrics.

It works in two stages. An exact lookup runs first, which LRCLIB answers only
when a record is within ±2s of the duration given — and that searches the whole
database. If nothing is that close, a keyword search runs and the closest of
those results wins, however far off it is.

So `duration` is a preference, not a filter: you always get lyrics if the song
exists at all, but an accurate duration gets you the right *version*.

| duration given | record used | off by |
| --- | --- | --- |
| 210 | 209.0s | 1.0s |
| 240 | 239.0s | 1.0s |
| 189 | 205.0s | 16.0s (nothing closer exists) |

## Web UI

A live lyrics display for whatever is playing on Plex, in the style of
Spotify or Apple Music: the current line is highlighted and glides to the
centre of the screen as the song plays.

```bash
python web.py
```

Then open <http://127.0.0.1:5000>.

Configure it with a `.env` file in the project root:

```
PLEX_URL=http://192.168.1.10:32400
PLEX_TOKEN=your-plex-token
OBSERVED_USER=your-plex-username
```

`OBSERVED_USER` is optional; without it the UI follows whichever track is
playing. Getting a Plex token is described at
<https://support.plex.tv/articles/204059436>.

### How it works

- `plex_engine.py` reads `/status/sessions` from Plex and keeps the music ones.
- `lyric_engine.py` looks the track up on LRCLIB, matching on duration.
- `web.py` glues the two together and serves the page.

The browser polls `/api/now-playing` every 2 seconds but advances the playhead
locally between polls, so the highlight tracks the music smoothly rather than
stepping every 2 seconds. Lyrics are cached per track, since LRCLIB is
rate-limited and the page polls continuously.

Album art is proxied through `/api/art` because fetching it from Plex needs the
token, which stays on the server and never reaches the browser.

Tracks with no synced lyrics fall back to plain text, shown without
highlighting. Tracks with no lyrics at all say so.

### Controls

Under the progress bar are previous / play-pause / next, and the bar itself is
a scrubber: click anywhere on it to seek, or focus it and use the arrow keys to
nudge by five seconds. Space toggles playback.

Play, pause and seek take effect on screen immediately rather than waiting for
Plex to notice, because a player may not report its state for another fifteen
seconds. The display holds what you asked for until the player confirms it.

These are relayed to your player through the Plex server, using the remote
control API. A player only accepts them if it advertises the `playback`
capability, which in practice means "Advertise as player" is switched on in its
settings. When it does not, the controls grey out and say so rather than
failing silently.

`plex_engine.py` exposes them directly too:

```python
from plex_engine import get_now_playing, pause, seek_to

track = get_now_playing()[0]
pause(track["machine_identifier"])
seek_to(track["machine_identifier"], 90)   # seconds
```

Available: `play`, `pause`, `skip_next`, `skip_previous`, `seek_to`,
`set_volume`, and `send_command` underneath them all.

### Knowing whether a next track exists

`/status/sessions` says nothing about the play queue, so the server alone
cannot answer this. The player can: its own timeline carries a `playQueueID`,
and the server will then describe that queue and our position in it. That is
what `get_queue_state()` does, and it is how the skip buttons know when to
grey out.

It needs to reach the player directly on port 32500. When it cannot - a remote
player, a firewall, a client that does not answer - the answer is *unknown*,
and the buttons stay enabled rather than wrongly greying out.

### When the queue runs out

This is messier than it sounds. A player that finishes its last track simply
stops reporting, so Plex goes on serving the last thing it heard - which still
says `playing`, at whatever position it had then. Left alone, the playhead
happily counts past the end of the song and the session sits there forever.

Lyrate handles it in three parts:

- The playhead is clamped to the track length, so it stops at the end.
- "Finished" is judged from that clamped clock rather than from Plex's state,
  because Plex may still be claiming the track is playing.
- A finished track clears to an idle screen after twenty seconds, since the
  Plex session itself lingers and nothing else would remove it.

Replaying the same track brings it straight back: a dead session keeps
reporting the end position, whereas a replay reports the start.

### Scrolling and plain lyrics

The lyrics follow the song on their own, but you can scroll them freely with
the wheel, a touchscreen, or the arrow and page keys once the panel has focus.
Scrolling away pauses the auto-follow and raises a **Jump to current line**
button, whose arrow points whichever way the line being sung actually is;
press it, or just stop scrolling for seven seconds, and it resumes.

While you are scrolling, the depth blur on upcoming lines is switched off -
those are the lines you are trying to read.

Not every track on LRCLIB has synced lyrics. When only plain text exists it is
shown as a normal scrollable page, labelled as unsynced, with no highlighting -
there are no timings to highlight against. Tracks with no lyrics at all say so.

### Layout

The page is a single flexible layout rather than a desktop one with a phone
version bolted on:

- **Wide screens** put the track beside the lyrics in two columns.
- **Under 900px** the track becomes a header strip - artwork, title, progress
  bar and transport controls - with the lyrics filling everything below.
- **Landscape on a short screen** returns to two columns, since stacking
  wastes the little height there is.
- **Touch devices** get finger-sized controls and a permanently visible
  scrubber handle, because there is no hover to reveal it.

It uses `100dvh` so the layout does not jump when a phone's address bar
appears, and keeps clear of notches and home indicators via the safe-area
insets.

To open it on a phone, bind to the network rather than just localhost:

```bash
HOST=0.0.0.0 python web.py
```

Then browse to `http://<this machine's IP>:5000` from the phone. Be aware that
this puts the playback controls and the artwork proxy on your local network
with no authentication - anyone on that network can pause your music. It stays
on localhost unless you ask for otherwise.

### Lyric timing

Synced lyrics are only as good as the playhead they are matched against, and
that comes from Plex's `viewOffset`. Three things make it imprecise, and the
UI corrects for each:

- **The reading is already stale when it arrives.** `/api/now-playing` reports
  how long ago it sampled Plex, and the browser adds that plus half its own
  round trip before interpolating.
- **Plex reports coarsely, and this dominates everything else.** Players tell
  Plex where they are on their own schedule - measured at roughly every 15
  seconds on a real desktop client. Between those reports Plex serves a frozen
  value that grows staler by the second, so a UI that believes every poll ends
  up locked as far behind as its first reading happened to be.

  The fix is to notice that the value is only trustworthy at the moment it
  *changes* - that is the player reporting in. So the UI re-anchors on changes
  and runs its own clock in between, which holds it within about a second even
  when Plex only updates every 15. It still treats a backward disagreement
  with more suspicion than a forward one, since staleness only ever lags.
- **Whole-second rounding.** `position_exact` keeps the unrounded value for
  the UI; `position` stays whole for printing.

To see what your own client does:

```bash
python diagnose_sync.py
```

It watches a playing track for 40 seconds and reports how often the position
updates, how far it can lag, and whether it advances at real time.

The one thing this cannot fix is seeking: after you skip, the display stays
wrong until your player next reports in, which can take as long as the gap
above.

If lyrics are consistently early or late across every song, nudge them:

```
LYRICS_OFFSET=-1.5
```

Negative pulls lyrics earlier, positive pushes them later. If only one song is
off, that song's LRC file is simply transcribed off-beat, and the global offset
will not help.
