"""Persist the explicitly declared test song without changing account sessions."""

import json
import os
from pathlib import Path
import re
import tempfile

from .errors import SessionError

DEFAULT_TEST_SONG_ID = "1263607749"
MAX_SONG_ID = 2**63 - 1


def validate_test_song_id(value):
    if type(value) not in (str, int):
        raise SessionError("Enter a positive numeric song ID.")
    song_id = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", song_id) or int(song_id) > MAX_SONG_ID:
        raise SessionError("Enter a positive numeric song ID up to 9223372036854775807.")
    return song_id


def read_test_song_id(path):
    path = Path(path)
    try:
        if path.stat().st_size > 4096:
            raise SessionError("The saved test song setting is invalid. Save a valid song ID before testing.")
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_TEST_SONG_ID
    except (OSError, ValueError):
        raise SessionError("The saved test song setting could not be read. Save a valid song ID before testing.") from None
    if not isinstance(settings, dict) or set(settings) != {"test_song_id"}:
        raise SessionError("The saved test song setting is invalid. Save a valid song ID before testing.")
    return validate_test_song_id(settings["test_song_id"])


def write_test_song_id(song_id, path):
    song_id = validate_test_song_id(song_id)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix="test-settings-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"test_song_id": song_id}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return song_id
    except OSError:
        raise SessionError("The test song setting could not be saved. Check the local settings folder.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
