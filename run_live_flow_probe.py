"""One-off wrapper: run the browser request-flow probe against the currently
declared test song (the probe module pins the historical default song ID).
Not part of the test suite."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("live_flow_probe", ROOT / "tests" / "probe_browser_request_flow.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

probe.SONG_ID = "1295327530"
probe.SONG_URL = "https://play.anghami.com/song/" + probe.SONG_ID

raise SystemExit(probe.main(["--row", "17", "--run"]))
