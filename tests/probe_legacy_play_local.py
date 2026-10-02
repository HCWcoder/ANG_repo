"""Run the original play_song function against local mock responses only."""

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
SONG_ID = "1263607749"
FIXTURE_DURATION = 114.99
FUNCTIONS = {"get_song", "play_song", "build_gateway_params", "assert_response_ok"}


class Reply:
    ok = True
    status_code = 200
    reason = "OK"

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class LocalTransport:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params, headers):
        operation = params.get("type")
        if operation not in {"GETsong", "REGISTERwebplay"}:
            raise AssertionError("Unexpected operation in the local probe")
        if operation == "GETsong":
            if str(params.get("songId")) != SONG_ID:
                raise AssertionError("The metadata request targets a different song")
            payload = {"status": 1, "id": SONG_ID, "duration": str(FIXTURE_DURATION)}
        else:
            if str(params.get("songid")) != SONG_ID:
                raise AssertionError("The play-record request targets a different song")
            payload = {"status": "ok"}
        self.calls.append({"operation": operation, "params": dict(params)})
        return Reply(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--song-id", default=SONG_ID, help="The song represented by the local fixture")
    args = parser.parse_args(argv)
    if args.song_id != SONG_ID:
        parser.error(f"This local metadata fixture represents song {SONG_ID}")

    source = ROOT / "send_vote.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    if {node.name for node in functions} != FUNCTIONS:
        raise RuntimeError("The original play-function dependencies were not found")
    # Load the function definitions, avoiding all legacy setup/account globals.
    namespace = {
        "dt": datetime, "random": lambda: 0.0,
        "GATEWAY_URL": "https://coussa.anghami.com/gateway.php",
    }
    module = ast.Module(body=functions, type_ignores=[])
    exec(compile(module, filename=str(source), mode="exec"), namespace)
    transport = LocalTransport()
    started = time.perf_counter()
    namespace["play_song"](transport, SONG_ID, "local-test-fingerprint", "local-test-session")
    elapsed = time.perf_counter() - started
    if [call["operation"] for call in transport.calls] != ["GETsong", "REGISTERwebplay"]:
        raise AssertionError("The original function did not produce the expected request sequence")
    record = transport.calls[1]["params"]
    report = {
        "passed": True, "scope": "original send_vote.play_song with local mock transport",
        "song_id": SONG_ID, "metadata_duration_seconds": FIXTURE_DURATION,
        "operations": [call["operation"] for call in transport.calls],
        "reported_play_seconds": float(record["playsecs"]),
        "reported_play_fraction": float(record["playper"]),
        "elapsed_seconds": elapsed,
        "response_source": "local mock", "external_requests": 0,
        "network_bytes": 0, "audio_bytes": 0, "listening_statistics_submitted": False,
    }
    directory = ROOT / ".anghami"
    directory.mkdir(exist_ok=True)
    (directory / "legacy-play-local-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
