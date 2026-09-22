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
