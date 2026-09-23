# Lyrate

Fetch song lyrics - plain or time-synced - from a pool of sources, and show
them live against whatever is playing on Plex.

## Setup

```bash
python -m venv env
source env/bin/activate  # Windows: env\Scripts\activate
pip install -r requirements.txt
```

## Usage

```python
from lyric_engine import get_lyrics, get_best

# Just the words, as an LRC string:
lyrics = get_lyrics(title="Daddy Cool", artist="Boney M.", duration=206)

# Or the whole candidate, to see where it came from:
best = get_best("Daddy Cool", 206, "Boney M.")
print(best["source"], best["synced"], best["span"])   # e.g. spotdl True 175.15
```

`get_lyrics` returns time-synced LRC as a string, or `None` if no source had
it. Pass `synced=False` for plain text instead. `duration` is in seconds and
is what decides which version of a song you get.

### Where lyrics come from

Two sources are consulted:

- **LRCLIB** directly. It matches on the track's duration, which is the
  strongest signal for picking the right version of a song.
- **spotdl**, which first resolves the track against Spotify and then searches
  `syncedlyrics` - Deezer, Genius, Lrclib, Lyricsify, Megalobiz, Musixmatch
  and NetEase. That is six providers LRCLIB alone does not cover.

Both answers are judged against the same thing: how far the lyrics run
compared with how long the file actually is. A candidate whose last timestamp
runs *past* the end of the file is a different, longer recording and is
rejected. One that fits is preferred from spotdl, because it got there by
confirming the track against Spotify rather than by matching a string.

**Spotify always answers**, even when nothing matches - search for a made-up
song and it will confidently return somebody else's track. So a result is only
believed when the title and artist genuinely resemble what was asked for and,
when a duration is known, the lengths agree within a few seconds. Otherwise
the match is discarded and that source simply contributes nothing.

Compilation tags are handled: an artist of "Various Artists" is ignored as a
match key rather than compared against, since it is tagged on everything.

The two sources answer at very different speeds - LRCLIB in about a second,
spotdl in about ten, because of the Spotify lookup. Waiting for both would
leave the first verse of every new track blank, so the fast answer is shown
immediately and quietly replaced if spotdl turns out to have the better match.
The page only re-renders when the words actually change: the two sources often
hold the identical LRC file, and re-rendering then would throw away wherever
you had scrolled to.

Set `SPOTDL=0` to switch the second source off, or `SPOTDL_TIMEOUT` to change
how long it may take (12 seconds by default).

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
- `lyric_engine.py` asks the lyric sources and picks between their answers.
- `spotdl_engine.py` is the second source: Spotify for metadata, then
  `syncedlyrics` for the LRC itself.
- `web.py` glues the two together and serves the page.

The browser polls `/api/now-playing` every 2 seconds but advances the playhead
locally between polls, so the highlight tracks the music smoothly rather than
stepping every 2 seconds. Lyrics are cached per track, since the sources are
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

### Nudging a song's timing

Bottom left is a small **− / +700 ms / +** control. It shifts the lyrics for
the song currently playing: **+** delays them, **−** pulls them earlier. One
press moves 100 ms, shift-click moves 500 ms, and clicking the number resets
it to zero.

Each song's shift is remembered, so a track you dialled in once stays that way
next time it plays. They live in `lyric_offsets.json`, keyed by title, artist
and duration - not by Plex's rating key, so re-importing your library does not
lose them. The file is ignored by git, since it is yours rather than the
project's. Shifts are capped at thirty seconds either way; past that the
lyrics are not out of step, they are the wrong file.

This is the per-song version of the setting below, and it is applied on top of
it. Reach for this one when a single song is off, and for `LYRICS_OFFSET` when
everything is.

If lyrics are consistently early or late across every song, nudge them:

```
LYRICS_OFFSET=-1.5
```

Negative pulls lyrics earlier, positive pushes them later. If only one song is
off, that song's LRC file is simply transcribed off-beat, and the global offset
will not help.
