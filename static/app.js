/* Lyrate - polls the backend for the current track and drives the lyric view. */

const POLL_MS = 2000;   // how often we ask the server what is playing
const TICK_MS = 100;    // how often we re-evaluate the active line locally

// Plex clients refresh their reported position on their own schedule, so a
// reading is often a little STALE - that is, behind the music - but never
// ahead of it. The two directions therefore mean different things:
//
//   reported AHEAD of us  -> we are lagging, or the listener skipped forward.
//   reported BEHIND us    -> usually just a stale reading, occasionally a rewind.
//
// So we trust forward corrections quickly and treat backward ones with
// suspicion, otherwise a lazily-reporting client drags the lyrics backwards
// every poll and the highlight stutters.
const FORWARD_SEEK = 2.0;    // jump if a fresh reading is this far ahead
// After we ask the player to seek, Plex keeps serving the OLD position until
// the player next reports in. We hold the position we jumped to until a
// reading turns up that plausibly reflects the seek, or until this expires.
const SEEK_CONFIRM_TOLERANCE = 3.0;   // seconds of slack when matching
// How long we trust our own idea of things after issuing a command, before
// giving up and deferring to the server. Longer than any sane report interval.
const COMMAND_HOLD_MS = 25000;

// Scrolling the lyrics yourself suspends the auto-follow. It resumes this
// long after you stop, or immediately if you press the jump-back button.
const RESUME_FOLLOW_MS = 7000;
// Breathing room above the first lyric, not a half-panel centring gap.
const LYRICS_TOP_PAD = 24;
// Within this of the track length, with nothing new arriving, the song is
// over. Plex reports a finished track as "paused" - or, when a queue runs
// out, goes on claiming it is playing until the player next reports in.
const ENDED_SLACK = 0.25;
// How long a finished track stays on screen before the display clears. The
// Plex session lingers after playback stops, so nothing else removes it.
const ENDED_CLEAR_MS = 20000;
// A dismissed track reporting a position at least this far from the end is
// genuinely playing again, not a stale session sitting on its last reading.
const RESTART_MARGIN = 5;
const BACKWARD_SEEK = 4.0;   // only this far behind counts as a real rewind
// Fraction of a small disagreement corrected per fresh reading.
const SMOOTHING = 0.35;

const el = {
  stage: document.getElementById('stage'),
  backdropArt: document.getElementById('backdrop-art'),
  art: document.getElementById('art'),
  artFallback: document.getElementById('art-fallback'),
  title: document.getElementById('title'),
  artist: document.getElementById('artist'),
  album: document.getElementById('album'),
  source: document.getElementById('source'),
  elapsed: document.getElementById('elapsed'),
  total: document.getElementById('total'),
  progressFill: document.getElementById('progress-fill'),
  lyrics: document.getElementById('lyrics'),
  lyricsScroll: document.getElementById('lyrics-scroll'),
  jumpBack: document.getElementById('jump-back'),
  controls: document.getElementById('controls'),
  btnPrev: document.getElementById('btn-prev'),
  btnPlay: document.getElementById('btn-play'),
  btnNext: document.getElementById('btn-next'),
  progressTrack: document.getElementById('progress-track'),
  progressKnob: document.getElementById('progress-knob'),
  ctlError: document.getElementById('ctl-error'),
  idle: document.getElementById('idle'),
  idleText: document.getElementById('idle-text'),
  idleSub: document.getElementById('idle-sub'),
};

// Everything we know about the track currently on screen.
const state = {
  key: null,          // identifies the track, so we only re-render on change
  duration: 0,
  position: 0,        // seconds, at the moment `fetchedAt` was stamped
  fetchedAt: 0,       // performance.now() for the anchor above
  playing: false,
  ended: false,        // reached the end of the track, not paused
  endedAt: 0,          // performance.now() when it finished
  sourceText: '',      // "Playing on ..." line, replaced while ended
  dismissedKey: null,  // a finished track we have already cleared away
  controllable: false, // whether this player accepts transport commands
  anchored: false,    // false until the first position has landed
  pendingSeek: null,  // {target, seekAt, expires} while a seek is unconfirmed
  pendingPlay: null,  // {want, expires} while a play/pause is unconfirmed
  lastReported: null, // last raw position Plex gave us, to spot fresh reports
  offset: 0,          // manual LYRICS_OFFSET from the server
  lines: [],          // [{time, text}], time is null for unsynced lyrics
  synced: false,
  nodes: [],          // the rendered .line elements, index-aligned with lines
  activeIndex: -1,
  following: true,    // whether the view auto-follows the current line
};

/* --- helpers ---------------------------------------------------- */

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

/* Position now, interpolated forward from the anchor while playing.
   Clamped to the track length: when a queue ends, the player simply stops
   reporting, and without this the playhead runs on past the end of the song
   for as long as it takes us to notice. */
function currentTime() {
  const raw = state.playing
    ? state.position + (performance.now() - state.fetchedAt) / 1000
    : state.position;
  return state.duration > 0 ? Math.min(raw, state.duration) : raw;
}

/* The time we match lyric timestamps against. A positive LYRICS_OFFSET makes
   lines appear later; a negative one makes them appear earlier. */
function lyricTime() {
  return currentTime() - state.offset;
}

function trackKey(track) {
  return [track.rating_key, track.title, track.artist, track.duration].join('|');
}

/* --- rendering -------------------------------------------------- */

function setArtwork(thumb) {
  if (!thumb) {
    el.art.removeAttribute('src');
    el.art.classList.remove('loaded');
    el.backdropArt.classList.remove('loaded');
    el.artFallback.classList.remove('hidden');
    return;
  }
  const url = `/api/art?path=${encodeURIComponent(thumb)}`;
  // Only fade the image in once it has actually decoded, to avoid a flash.
  const probe = new Image();
  probe.onload = () => {
    el.art.src = url;
    el.backdropArt.src = url;
    el.art.classList.add('loaded');
    el.backdropArt.classList.add('loaded');
    el.artFallback.classList.add('hidden');
  };
  probe.onerror = () => {
    el.art.classList.remove('loaded');
    el.artFallback.classList.remove('hidden');
  };
  probe.src = url;
}

function renderMeta(track) {
  el.title.textContent = track.title || 'Unknown track';
  el.artist.textContent = track.artist || '';
  el.album.textContent = track.album || '';
  const bits = [track.player, track.user].filter(Boolean);
  state.sourceText = bits.length ? `Playing on ${bits.join(' · ')}` : '';
  updateSourceLine();
  el.total.textContent = formatTime(track.duration || 0);
  document.title = track.title
    ? `${track.title} — ${track.artist || 'Lyrate'}`
    : 'Lyrate';
}

function renderLyrics(lyrics) {
  el.lyrics.innerHTML = '';
  state.nodes = [];
  state.activeIndex = -1;

  // A new track always starts followed and scrolled to the top.
  clearTimeout(resumeTimer);
  state.following = true;
  el.lyrics.classList.remove('browsing');
  el.jumpBack.classList.remove('visible', 'up');
  el.jumpBack.hidden = true;
  el.lyricsScroll.scrollTop = 0;

  if (!lyrics || !lyrics.lines || !lyrics.lines.length) {
    const p = document.createElement('p');
    p.className = 'lyrics-notice';
    p.textContent = 'No lyrics found for this track.';
    el.lyrics.appendChild(p);
    state.lines = [];
    state.synced = false;
    el.lyrics.classList.add('unsynced');
    return;
  }

  state.lines = lyrics.lines;
  state.synced = !!lyrics.synced;
  el.lyrics.classList.toggle('unsynced', !state.synced);

  // Say so, rather than leaving plain lyrics looking like broken synced ones.
  if (!state.synced) {
    const note = document.createElement('p');
    note.className = 'lyrics-notice unsynced-note';
    note.textContent = 'No synced lyrics for this track - scroll to read along.';
    el.lyrics.appendChild(note);
  }

  const frag = document.createDocumentFragment();
  for (const line of lyrics.lines) {
    const p = document.createElement('p');
    p.className = 'line';
    // A timestamped line with no words marks an instrumental gap.
    if (state.synced && !line.text) p.classList.add('instrumental');
    p.textContent = line.text || '';
    frag.appendChild(p);
    state.nodes.push(p);
  }
  el.lyrics.appendChild(frag);
  applyCentringPadding();
}

const prefersReducedMotion =
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Scroll the lyrics so `node` sits in the middle of the panel. Marked as our
   own scrolling, so the resulting scroll events do not look like the user. */
function centreOn(node, smooth = true) {
  const box = el.lyricsScroll;
  const top = node.offsetTop + node.offsetHeight / 2 - box.clientHeight / 2;
  box.scrollTo({
    top: Math.max(0, top),
    behavior: smooth && !prefersReducedMotion ? 'smooth' : 'auto',
  });
}

function centreOnActive(smooth = true) {
  const active = state.nodes[state.activeIndex];
  if (active) centreOn(active, smooth);
}

/* Padding above and below the lyrics.
   Below: enough that the LAST line can still reach the middle of the panel.
   Above: almost none. Centring the first line would mean half a panel of
   empty space above it, which is what you see the moment you scroll to the
   top. Instead the lyrics start near the top and only drift to the centre
   once the song has enough lines behind it - scrollTop simply clamps at 0. */
function applyCentringPadding() {
  if (!state.synced || !state.nodes.length) {
    // Plain lyrics are read top to bottom; the stylesheet handles them.
    el.lyrics.style.paddingTop = '';
    el.lyrics.style.paddingBottom = '';
    return;
  }
  const last = state.nodes[state.nodes.length - 1];
  el.lyrics.style.paddingTop = `${LYRICS_TOP_PAD}px`;
  el.lyrics.style.paddingBottom =
    `${Math.max(0, el.lyricsScroll.clientHeight / 2 - last.offsetHeight / 2)}px`;
}

/* Point the chevron at wherever the current line actually is: up when it is
   above the view, down when it is below. */
function updateJumpDirection() {
  const active = state.nodes[state.activeIndex];
  if (!active) return;
  const box = el.lyricsScroll;
  const activeCentre = active.offsetTop + active.offsetHeight / 2;
  const viewCentre = box.scrollTop + box.clientHeight / 2;
  el.jumpBack.classList.toggle('up', activeCentre < viewCentre);
}

/* Stop auto-following because the user is reading elsewhere. */
let resumeTimer;
function suspendFollow() {
  // Plain lyrics have no line to follow, so there is nothing to suspend.
  if (!state.synced) return;
  state.following = false;
  // Reading ahead should not be through the depth blur.
  el.lyrics.classList.add('browsing');
  el.jumpBack.hidden = false;
  // Force layout so the transition runs from the hidden state. A rAF would
  // be tidier but never fires while the tab is in the background.
  void el.jumpBack.offsetWidth;
  el.jumpBack.classList.add('visible');
  updateJumpDirection();
  clearTimeout(resumeTimer);
  resumeTimer = setTimeout(resumeFollow, RESUME_FOLLOW_MS);
}

function resumeFollow() {
  clearTimeout(resumeTimer);
  state.following = true;
  el.lyrics.classList.remove('browsing');
  el.jumpBack.classList.remove('visible');
  // Wait for the fade before removing it from the layout entirely.
  setTimeout(() => {
    if (state.following) el.jumpBack.hidden = true;
  }, 240);
  centreOnActive();
}

/* Keep the chevron pointing the right way as the user keeps scrolling. This
   listens to scroll events only to read position - suspending the follow is
   driven by the input events below. */
el.lyricsScroll.addEventListener('scroll', () => {
  if (!state.following) updateJumpDirection();
}, { passive: true });

/* Watch for the user's own input rather than scroll events. A programmatic
   scrollTo fires scroll events too, and a long smooth one can still be
   running after any sensible timeout - which made it look like the user. */
const SCROLL_INTENT = ['wheel', 'touchmove', 'pointerdown'];
for (const event of SCROLL_INTENT) {
  el.lyricsScroll.addEventListener(event, suspendFollow, { passive: true });
}

// Keys that scroll the panel when it has focus.
const SCROLL_KEYS = new Set([
  'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End',
]);
el.lyricsScroll.addEventListener('keydown', (event) => {
  if (SCROLL_KEYS.has(event.key)) suspendFollow();
});

el.jumpBack.addEventListener('click', resumeFollow);

/* Highlight the line for `time` and, unless the user is scrolling, keep it
   in the middle of the panel. */
function updateActiveLine(time) {
  if (!state.synced || !state.nodes.length) return;

  let index = -1;
  for (let i = 0; i < state.lines.length; i++) {
    if (state.lines[i].time <= time) index = i;
    else break;
  }
  if (index === state.activeIndex) return;
  state.activeIndex = index;

  state.nodes.forEach((node, i) => {
    node.classList.remove('active', 'past', 'ahead-1', 'ahead-2', 'ahead-3');
    if (i === index) node.classList.add('active');
    else if (i < index) node.classList.add('past');
    else if (i - index <= 3) node.classList.add(`ahead-${i - index}`);
  });

  if (state.following) centreOnActive();
  else updateJumpDirection();
}

function updateProgress(time) {
  el.elapsed.textContent = formatTime(time);
  const pct = state.duration ? Math.min(100, (time / state.duration) * 100) : 0;
  el.progressFill.style.width = `${pct}%`;
  el.progressKnob.style.left = `${pct}%`;
  el.progressTrack.setAttribute('aria-valuenow', Math.round(time));
  el.progressTrack.setAttribute('aria-valuemax', Math.round(state.duration));
}

/* --- controls --------------------------------------------------- */

/* Send a transport command, then poll straight away so the UI reflects it
   without waiting out the normal interval. */
let errorTimer;

function showControlError(message) {
  clearTimeout(errorTimer);
  el.ctlError.textContent = message;
  el.ctlError.classList.add('visible');
  // Long enough to read, short enough not to linger.
  errorTimer = setTimeout(() => el.ctlError.classList.remove('visible'), 9000);
}

function clearControlError() {
  clearTimeout(errorTimer);
  el.ctlError.classList.remove('visible');
}

async function command(name, value, button) {
  if (!state.controllable) return;
  try {
    const response = await fetch(`/api/control/${name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    clearControlError();
  } catch (err) {
    // Say why, rather than leaving the button looking simply dead.
    if (button) {
      button.classList.remove('rejected');
      void button.offsetWidth;            // restart the animation
      button.classList.add('rejected');
    }
    showControlError(err.message);
    console.warn(`${name} failed:`, err.message);
    return;
  }
  // Poll straight away to pick up the new play/pause state. We deliberately
  // do NOT force a re-anchor: Plex will still be serving the pre-command
  // position, and trusting it would drag the playhead backwards.
  poll();
}

/* Turn a click on the progress bar into a position in seconds. */
function seekPositionFromEvent(event) {
  const rect = el.progressTrack.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  return ratio * state.duration;
}

/* Flip play/pause straight away rather than waiting for Plex to notice. The
   player reports its state on its own schedule - up to 15s on some clients -
   and an icon that lags that far behind the button press feels broken. */
function togglePlayPause() {
  if (!state.controllable) return;
  const at = currentTime();          // read before changing state.playing
  const want = !state.playing;

  state.position = at;               // freeze here, or resume from here
  state.fetchedAt = performance.now();
  state.playing = want;
  state.pendingPlay = { want, expires: performance.now() + COMMAND_HOLD_MS };
  el.controls.classList.toggle('playing', want);

  command('playPause', null, el.btnPlay);
}

el.btnPlay.addEventListener('click', togglePlayPause);
el.btnNext.addEventListener('click', () => command('skipNext', null, el.btnNext));
el.btnPrev.addEventListener('click', () => command('skipPrevious', null, el.btnPrev));

/* Jump to `target` locally and tell the player to follow. The playhead stays
   where the user put it until Plex confirms, rather than snapping back. */
function seekTo(target) {
  state.position = target;
  state.fetchedAt = performance.now();
  state.pendingSeek = {
    target,
    seekAt: performance.now(),
    expires: performance.now() + COMMAND_HOLD_MS,
  };
  updateProgress(target);
  updateActiveLine(target - state.offset);
  command('seekTo', target, null);
}

el.progressTrack.addEventListener('click', (event) => {
  if (!state.duration) return;
  seekTo(seekPositionFromEvent(event));
});

// Arrow keys nudge by 5s when the bar has focus.
el.progressTrack.addEventListener('keydown', (event) => {
  const step = event.key === 'ArrowRight' ? 5
             : event.key === 'ArrowLeft' ? -5 : 0;
  if (!step || !state.duration) return;
  event.preventDefault();
  seekTo(Math.max(0, Math.min(state.duration, currentTime() + step)));
});

// Space toggles playback, as long as focus is not already on a button.
document.addEventListener('keydown', (event) => {
  if (event.code !== 'Space' || event.target.closest('button')) return;
  event.preventDefault();
  togglePlayPause();
});

/* The metadata line: what is playing and where, unless it has finished. */
function updateSourceLine() {
  el.source.textContent = state.ended ? 'Finished' : state.sourceText;
}

/* Decide whether the track has run out, from the live playhead rather than a
   reading that may be seconds old and still insist it is playing. */
function updateEnded() {
  const finished = state.duration > 0
    && !state.pendingSeek
    && !state.pendingPlay
    && currentTime() >= state.duration - ENDED_SLACK;

  if (finished === state.ended) return;
  state.ended = finished;
  state.endedAt = finished ? performance.now() : 0;

  el.controls.classList.toggle('ended', finished);
  // A finished track is not playing, whatever Plex is still reporting.
  if (finished) el.controls.classList.remove('playing');
  else el.controls.classList.toggle('playing', state.playing);
  updateSourceLine();
}

function showIdle(text, sub) {
  el.idleText.textContent = text;
  el.idleSub.textContent = sub || '';
  el.idle.classList.add('visible');
  el.stage.classList.remove('ready');
  document.title = 'Lyrate';
}

function hideIdle() {
  el.idle.classList.remove('visible');
  el.stage.classList.add('ready');
}

/* --- polling ---------------------------------------------------- */

async function poll() {
  let data;
  const sentAt = performance.now();
  try {
    const response = await fetch('/api/now-playing');
    data = await response.json();
    if (data.error) {
      showIdle('Cannot reach Plex', data.error);
      return;
    }
  } catch (err) {
    showIdle('Lost the server', 'Is web.py still running?');
    return;
  }
  const roundTrip = (performance.now() - sentAt) / 1000;

  if (!data.playing) {
    state.key = null;
    state.dismissedKey = null;
    state.playing = false;
    state.ended = false;
    state.anchored = false;
    showIdle('Waiting for music…', 'Start a track on Plex and it will appear here.');
    return;
  }

  const track = data.track;
  const key = trackKey(track);

  // Plex keeps serving a session for a while after the queue runs out, so a
  // finished track would otherwise sit on screen indefinitely. Once it has
  // been cleared, ignore that same track until something else plays.
  if (key === state.dismissedKey) {
    // The same track can legitimately come back - you can replay it. A dead
    // session keeps reporting the end position; a replay reports the start.
    const pos = track.position_exact != null
      ? track.position_exact
      : (track.position || 0);
    const restarted = track.state === 'playing'
      && track.duration
      && pos < track.duration - RESTART_MARGIN;
    if (!restarted) {
      showIdle('Nothing playing', 'The queue finished.');
      return;
    }
    state.dismissedKey = null;   // it really is playing again
  }
  if (state.ended && performance.now() - state.endedAt > ENDED_CLEAR_MS) {
    state.dismissedKey = key;
    state.key = null;
    state.ended = false;
    state.playing = false;
    showIdle('Nothing playing', 'The queue finished.');
    return;
  }

  const trackChanged = key !== state.key;

  if (trackChanged) {
    state.key = key;
    state.dismissedKey = null;   // something new is playing
    state.ended = false;
    state.endedAt = 0;
    state.lastReported = null;
    state.pendingSeek = null;
    state.pendingPlay = null;
    state.duration = track.duration || 0;
    renderMeta(track);
    setArtwork(track.thumb);
    renderLyrics(data.lyrics);
  }

  state.offset = data.offset || 0;
  const wasPlaying = state.playing;
  const reportedPlaying = track.state === 'playing';

  // Hold our own play/pause state until the player catches up with the
  // command we sent, or until the hold expires.
  if (state.pendingPlay && !trackChanged) {
    if (reportedPlaying === state.pendingPlay.want
        || performance.now() > state.pendingPlay.expires) {
      state.pendingPlay = null;
      state.playing = reportedPlaying;
    }
    // else: keep the state we optimistically set on the button press
  } else {
    state.pendingPlay = null;
    state.playing = reportedPlaying;
  }

  state.controllable = !!track.controllable && !!track.machine_identifier;
  // A finished track shows the play glyph, whatever Plex still reports.
  el.controls.classList.toggle('playing', state.playing && !state.ended);
  el.controls.classList.toggle('disabled', !state.controllable);

  // queue is null when we could not read it, which means "unknown" - leave
  // the buttons alone rather than greying out a skip that would have worked.
  const queue = data.queue;
  el.btnPrev.disabled = !state.controllable
    || (queue ? !queue.has_previous : false);
  el.btnNext.disabled = !state.controllable
    || (queue ? !queue.has_next : false);
  el.btnPrev.title = el.btnPrev.disabled && queue
    ? 'No previous track' : 'Previous';
  el.btnNext.title = el.btnNext.disabled && queue
    ? 'No next track' : 'Next';
  el.controls.title = state.controllable
    ? ''
    : 'This player does not accept remote control';

  // The reading Plex gave us is already stale: it aged `sample_age` seconds
  // on the server, plus roughly half the round trip getting here.
  const staleness = (data.sample_age || 0) + roundTrip / 2;
  const reported = track.position_exact != null
    ? track.position_exact
    : (track.position || 0);

  // A player tells Plex where it is only every few seconds. In between, the
  // value Plex serves is frozen and growing staler, so believing it whenever
  // we happen to poll leaves us locked however late that first reading was.
  // The one moment it is trustworthy is when it CHANGES - that is the player
  // reporting in. So we re-anchor on changes and extrapolate in between.
  const fresh = reported !== state.lastReported;
  state.lastReported = reported;

  // We spot a change up to one poll after Plex recorded it, so on average
  // it is already half a poll old.
  const detectionLag = POLL_MS / 2000;
  const target = state.playing
    ? reported + staleness + detectionLag
    : reported;

  const shown = currentTime();

  // While a play/pause is unconfirmed the server still describes the old
  // state, and its position keeps advancing through a pause we have already
  // applied. Leave our clock alone until it agrees.
  if (state.pendingPlay && !trackChanged) {
    state.position = shown;
    state.fetchedAt = performance.now();
    state.anchored = true;
    hideIdle();
    return;
  }

  // While a seek is unconfirmed, ignore readings that cannot be a result of
  // it. A post-seek reading sits between the target and the target plus the
  // time since we asked; anything else is the old position still being
  // served, and believing it would yank the playhead back.
  if (state.pendingSeek && !trackChanged) {
    const { target, seekAt, expires } = state.pendingSeek;
    const elapsed = (performance.now() - seekAt) / 1000;
    const confirmed = fresh
      && reported >= target - SEEK_CONFIRM_TOLERANCE
      && reported <= target + elapsed + SEEK_CONFIRM_TOLERANCE;

    if (confirmed || performance.now() > expires) {
      state.pendingSeek = null;   // the player caught up, or we gave up
    } else {
      // Hold where the user put us, with our own clock still running.
      state.position = shown;
      state.fetchedAt = performance.now();
      state.anchored = true;
      hideIdle();
      return;
    }
  }

  const restart = !state.anchored
    || trackChanged
    || wasPlaying !== state.playing
    || (fresh && target > shown + FORWARD_SEEK)    // skipped forward
    || (fresh && target < shown - BACKWARD_SEEK);  // genuinely rewound

  if (restart) {
    // First reading, a new track, a play/pause, or a real seek: jump.
    state.position = target;
  } else if (fresh) {
    // The player just reported in, so this reading is worth believing -
    // in both directions. Ease onto it rather than snapping.
    state.position = shown + (target - shown) * SMOOTHING;
  } else {
    // Same frozen value as last time: it tells us nothing new. Keep our own
    // clock running instead of being dragged backwards by a stale reading.
    state.position = shown;
  }
  state.fetchedAt = performance.now();
  state.anchored = true;

  hideIdle();
}

/* Re-evaluate locally between polls so the highlight tracks the music
   smoothly instead of stepping every two seconds. */
function tick() {
  if (!state.key) return;
  updateEnded();
  updateProgress(currentTime());
  updateActiveLine(lyricTime());
}

poll();
setInterval(poll, POLL_MS);
setInterval(tick, TICK_MS);

// Re-centre the active line if the window is resized.
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    applyCentringPadding();
    if (state.following) centreOnActive(false);
  }, 150);
});
