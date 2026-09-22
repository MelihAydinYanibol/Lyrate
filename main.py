"""
Main Loop
"""

import os
import dotenv
dotenv.load_dotenv()
from plex_engine import get_now_playing
from lyric_engine import get_lyrics

OBSERVED_USER = os.getenv("OBSERVED_USER")  # Plex username to watch for
OBSERVE_INTERVAL = int(os.getenv("OBSERVE_INTERVAL", 5))  # seconds between checks

def observer():
    while True:
        tracks = get_now_playing()
        print(f"Found {len(tracks)} tracks playing.")
        if not tracks:
            print("Nothing playing.")
        filter = []
        for track in tracks:
            if track['state'] == 'playing' and track['user'] == OBSERVED_USER:
                filter.append(track)
            """ print(
                f"{track['title']} - {track['artist']} ({track['album']}) "
                f"[{track['position']}s / {track['duration']}s] "
                f"{track['state']} on {track['player']} / {track['user']}" 
            ) """
        if len(filter) > 0:
            track = filter[0]
            print("Getting lyrics for: " + track['title'] + " - " + track['artist'] + " (" + track['album'] + ")")
            lyric_data = get_lyrics(track['title'], track['duration'], track['artist'], track['album'], True)
            if lyric_data:
                print(lyric_data); break


observer()