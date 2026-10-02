"""Explicit live QA: normal login, five seconds of audio, and restored likes.

Run manually; pytest does not collect this probe. At most five distinct imported
accounts are selected, and all results omit account credentials and request URLs.
"""
import argparse
from datetime import datetime, timezone
from http.cookies import SimpleCookie
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anghami_session.browser import launch_browser
from anghami_session.capture import capture_login
from anghami_session.vault import AccountVault

ROOT = Path(__file__).resolve().parents[1] / ".anghami"
SONG_ID = "1263607749"
SONG_URL = f"https://play.anghami.com/song/{SONG_ID}"
REPORT = ROOT / "five-account-functional-qa-report.json"


class QAError(RuntimeError):
    pass


def emit(result):
    print(json.dumps(result, ensure_ascii=True), flush=True)


def operation(response):
    parts = urlsplit(response.url)
    if (parts.hostname, parts.path) != ("coussa.anghami.com", "/gateway.php"):
        return ""
    query = parse_qs(parts.query)
    return query.get("type", query.get("angh_type", [""]))[0]


def is_like_mutation(response):
    name = operation(response).upper()
    return "PLAYLIST" in name and name.startswith(("PUT", "POST", "DELETE"))


def like_state(page):
    liked = page.get_by_role("button", name="Liked", exact=True)
    unliked = page.get_by_role("button", name="Like", exact=True)
    if liked.count() == 1 and liked.is_visible():
        return True
    if unliked.count() == 1 and unliked.is_visible():
        return False
    raise QAError("Could not establish the requested song's like state.")


def toggle_like(page, state):
    with page.expect_response(is_like_mutation, timeout=15000) as pending:
        page.get_by_role("button", name="Liked" if state else "Like", exact=True).click()
    response = pending.value
    if response.status != 200:
        raise QAError("The like mutation returned a non-success HTTP status.")
    payload = response.json()
    status = payload.get("status") if isinstance(payload, dict) else None
    if status is not None and status != "ok":
        raise QAError("The like mutation returned a non-success API status.")
    page.get_by_role("button", name="Like" if state else "Liked", exact=True).wait_for(state="visible", timeout=10000)
    return {
        "operation": operation(response), "method": response.request.method,
        "http_status": response.status,
        "api_status": status,
        "encrypted_reply_present": isinstance(payload, dict) and "reply" in payload,
    }


def sample_media(page):
    return page.evaluate("""() => [...window.__observedPlaybackMedia].map(x => ({
        current_time: x.currentTime, duration: Number.isFinite(x.duration) ? x.duration : null,
        paused: x.paused, ready_state: x.readyState, error_code: x.error?.code ?? null,
        decoded_audio_bytes: x.webkitAudioDecodedByteCount ?? null,
        played: Array.from({length: x.played.length}, (_, i) => [x.played.start(i), x.played.end(i)]),
    }))""")


def pause_media(page):
    page.evaluate("() => [...window.__observedPlaybackMedia].forEach(x => x.pause())")


def settled_song(page, *, reload=False):
    if reload:
        page.reload(wait_until="domcontentloaded")
    page.get_by_role("button", name="Play", exact=True).wait_for(state="visible", timeout=20000)
    page.wait_for_timeout(4000)
    if urlsplit(page.url).path != f"/song/{SONG_ID}":
        raise QAError("The player navigated away from the requested song.")


def functional_checks(saved, result, checkpoint, backend, headless):
    browser = launch_browser(headless=headless, backend=backend, enable_audio=True)
    page = None
    original_like = None
    try:
        context = browser.new_context(viewport={"width": 1365, "height": 900}, locale="en-US")
        cookies = SimpleCookie()
        cookies.load(saved["requests"]["relations"]["headers"].get("cookie", ""))
        context.add_cookies([
            {"name": name, "value": item.value, "domain": ".anghami.com", "path": "/", "secure": True}
            for name, item in cookies.items()
        ])
        context.add_init_script("""(() => {
            const observed = new Set();
            const play = HTMLMediaElement.prototype.play;
            HTMLMediaElement.prototype.play = function(...args) { observed.add(this); return play.apply(this, args); };
            Object.defineProperty(window, '__observedPlaybackMedia', {value: observed});
        })();""")
        page = context.new_page()
        audio_responses = []
        api_events = []
        decoder = {}

        def record(response):
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("audio/") or response.request.resource_type == "media":
                audio_responses.append({"http_status": response.status, "content_type": content_type})
            name = operation(response)
            if name:
                api_events.append({"operation": name, "method": response.request.method, "http_status": response.status})

        page.on("response", record)
        cdp = context.new_cdp_session(page)

        def properties(event):
            for prop in event.get("properties", []):
                if "audiodecoder" in prop.get("name", "").lower():
                    decoder[prop["name"]] = prop.get("value")

        cdp.on("Media.playerPropertiesChanged", properties)
        cdp.send("Media.enable")
        page.goto(SONG_URL, wait_until="domcontentloaded")
        settled_song(page)
        original_like = like_state(page)
        result["original_like_state"] = original_like
        result["like_restore_verified"] = True
        checkpoint()

        page.get_by_role("button", name="Play", exact=True).click()
        page.wait_for_function("() => [...window.__observedPlaybackMedia].some(x => !x.paused && x.currentTime > 0.2 && x.readyState >= 3)", timeout=20000)
        before = sample_media(page)
        page.wait_for_timeout(5000)
        after = sample_media(page)
        pause_media(page)
        first = next(item for item in before if not item["paused"] and item["ready_state"] >= 3)
        last = next(item for item in after if not item["paused"] and item["ready_state"] >= 3)
        elapsed = last["current_time"] - first["current_time"]
        playback_ok = elapsed >= 4 and last["error_code"] is None and (last["decoded_audio_bytes"] or 0) > 0
        result["playback"] = {
            "passed": playback_ok, "observed_progress_seconds": round(elapsed, 3),
            "samples": [first, last], "audio_responses": audio_responses,
            "decoder": decoder,
        }
        if not playback_ok:
            raise QAError("Actual audio playback did not advance as expected.")

        result["like_restore_verified"] = False
        checkpoint()
        result["like_mutation"] = toggle_like(page, original_like)
        result["changed_like_state"] = like_state(page)
        if result["changed_like_state"] == original_like:
            raise QAError("The like state did not change.")
        result["like_roundtrip_passed"] = False
        checkpoint()
    except Exception as exc:
        result["functional_error_type"] = type(exc).__name__
        if isinstance(exc, QAError):
            result["functional_error"] = str(exc)
    finally:
        if page is not None:
            try:
                pause_media(page)
            except Exception:
                pass
        if original_like is not None:
            try:
                settled_song(page, reload=True)
                current = like_state(page)
                if current != original_like:
                    result["restore_mutation"] = toggle_like(page, current)
                settled_song(page, reload=True)
                restored = like_state(page) == original_like
                result["like_restore_verified"] = restored
                result["like_roundtrip_passed"] = restored and "like_mutation" in result and result.get("changed_like_state") != original_like
            except Exception as exc:
                result["like_restore_verified"] = False
                result["restoration_error_type"] = type(exc).__name__
        result["api_events"] = api_events if page is not None else []
        checkpoint()
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", nargs="+", type=int)
    parser.add_argument("--browser", choices=("cloakbrowser", "chrome"), default="cloakbrowser")
    parser.add_argument("--headed", action="store_true", help="Open a temporary visible browser window")
    args = parser.parse_args()
    with AccountVault() as vault:
        if args.rows:
            rows = list(dict.fromkeys(args.rows))
            if not 1 <= len(rows) <= 5 or any(row < 1 for row in rows):
                parser.error("Choose one to five positive source rows")
            keys = []
            for row in rows:
                key = vault._db.execute("SELECT email_key FROM accounts WHERE source_row=?", (row,)).fetchone()
                if key is None:
                    parser.error("A selected source row is missing")
                keys.append(key[0])
            if len(set(keys)) != len(keys):
                parser.error("The selected rows must identify distinct accounts")
        else:
            rows, seen = [], set()
            for row, key in vault._db.execute("SELECT source_row, email_key FROM accounts ORDER BY source_row"):
                if key not in seen:
                    rows.append(row)
                    seen.add(key)
                if len(rows) == 5:
                    break
    started = datetime.now(timezone.utc)
    ROOT.mkdir(exist_ok=True)
    run_report = ROOT / f"functional-qa-{started.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    report = {
        "started_at_utc": started.isoformat(),
        "browser_backend": args.browser,
        "song_id": SONG_ID, "headless": not args.headed, "selected_rows": rows,
        "scope": "normal login, short real playback, reversible like state change",
        "synthetic_full_duration_events_sent": 0,
        "accounts": [],
    }

    def checkpoint():
        serialized = json.dumps(report, indent=2) + "\n"
        for destination in (run_report, REPORT):
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(destination)

    checkpoint()
    for row in rows:
        result = {"source_row": row}
        report["accounts"].append(result)
        checkpoint()
        emit({"source_row": row, "stage": "refreshing_login"})
        try:
            with AccountVault() as vault:
                record = vault.record(row)
            saved, metadata = capture_login(
                email=record["email"], password=record["password"], headless=not args.headed,
                browser_backend=args.browser,
            )
            record = None
            with AccountVault() as vault:
                result["login"] = vault.attach(row, saved)
            (ROOT / f"account-{row}.login-request.redacted.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            checkpoint()
            emit({"source_row": row, "stage": "login_verified_testing_player"})
            functional_checks(saved, result, checkpoint, args.browser, not args.headed)
        except Exception as exc:
            result["error_type"] = type(exc).__name__
        checkpoint()
        emit({
            "source_row": row,
            "login_passed": result.get("login", {}).get("authenticated", False),
            "playback_passed": result.get("playback", {}).get("passed", False),
            "like_roundtrip_passed": result.get("like_roundtrip_passed", False),
            "like_restore_verified": result.get("like_restore_verified"),
            "error_type": result.get("error_type", result.get("functional_error_type")),
        })
        if result.get("like_restore_verified") is False:
            report["stopped_reason"] = "An original like state could not be confirmed restored. Further mutations stopped."
            break
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = {
        "accounts_attempted": len(report["accounts"]),
        "logins_passed": sum(bool(item.get("login", {}).get("authenticated")) for item in report["accounts"]),
        "playback_checks_passed": sum(bool(item.get("playback", {}).get("passed")) for item in report["accounts"]),
        "like_roundtrips_passed": sum(bool(item.get("like_roundtrip_passed")) for item in report["accounts"]),
        "unrestored_like_states": sum(item.get("like_restore_verified") is False for item in report["accounts"]),
    }
    checkpoint()
    emit(report["summary"])
    return 0 if all(
        item.get("login", {}).get("authenticated")
        and item.get("playback", {}).get("passed")
        and item.get("like_roundtrip_passed")
        and item.get("like_restore_verified")
        and not item.get("functional_error_type")
        and not item.get("error_type")
        for item in report["accounts"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
