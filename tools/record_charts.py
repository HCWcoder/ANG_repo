"""Record ranked song lists from public Anghami playlist/chart pages.

Snapshots the ordered songs of each configured playlist into
.anghami/chart-monitor/ so rank and membership changes can be compared
across runs. Public pages only: no accounts, sessions, or proxies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".anghami" / "chart-monitor"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PLAYLISTS = {
    "36571799": "Top Lebanese",
    "6471781": "Top Arabic Egypt (candidate)",
    "5890887": "Top Arabic (candidate)",
}

_SONG_PATTERN = re.compile(
    r'\{"nofollow":false,"id":"(\d+)","title":"(.*?)","album".*?"artist":"(.*?)".*?"rankchange":"(\w+)"'
)
_TITLE_PATTERN = re.compile(r"<title>(.*?)</title>")


def fetch_playlist(playlist_id: str) -> dict:
    result = subprocess.run(
        ["curl.exe", "-s", "-L", f"https://play.anghami.com/playlist/{playlist_id}", "-H", f"User-Agent: {USER_AGENT}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    html = unescape(result.stdout).replace("&q;", '"')
    title = _TITLE_PATTERN.search(html)
    songs = [
        {"rank": rank, "song_id": song_id, "title": title_text, "artist": artist, "rank_change_label": change}
        for rank, (song_id, title_text, artist, change) in enumerate(_SONG_PATTERN.findall(html), 1)
    ]
    if not songs:
        raise ValueError(f"No songs parsed from playlist {playlist_id}")
    return {
        "playlist_id": playlist_id,
        "label": PLAYLISTS.get(playlist_id, ""),
        "page_title": unescape(title.group(1)).strip() if title else None,
        "song_count": len(songs),
        "songs": songs,
    }


def snapshot_name(observed: datetime, playlist_id: str) -> str:
    return f"playlist-{playlist_id}-{observed.strftime('%Y-%m-%dT%H%M%S')}.json"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    observed = datetime.now(timezone.utc)
    for playlist_id in PLAYLISTS:
        snapshot = {"observed_at": observed.isoformat(), **fetch_playlist(playlist_id)}
        path = OUT_DIR / snapshot_name(observed, playlist_id)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps({
            "playlist_id": playlist_id, "label": snapshot["label"],
            "page_title": snapshot["page_title"], "song_count": snapshot["song_count"],
            "file": path.name,
        }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
