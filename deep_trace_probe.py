"""One-off deep browser playback trace using a saved account session.

Injects the account's cookies into CloakBrowser, opens the song page, plays,
and logs every request/response with params, headers and JSON bodies.
Local diagnostics only; output contains credentials — never commit.
"""

import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from anghami_session.browser import launch_browser
from anghami_session.vault import AccountVault

ROW = 3
SONG_ID = "1302875825"
OUT = ROOT / ".anghami" / f"deep-trace-row-{ROW}-{SONG_ID}.json"


def cookiejar_from_header(cookie_header: str) -> list:
    cookies = []
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies.append({"name": name, "value": value, "domain": ".anghami.com", "path": "/"})
    return cookies


def main() -> int:
    events = []

    with AccountVault() as vault:
        saved = vault.session(ROW)
    cookie_header = saved["requests"]["relations"]["headers"].get("cookie", "")
    cookies = cookiejar_from_header(cookie_header)
    print(f"row {ROW}: injecting {len(cookies)} cookies", flush=True)

    browser = launch_browser(headless=True, backend="cloakbrowser", direct=True)
    try:
        context = browser.new_context(no_viewport=True)
        context.add_cookies(cookies)
        page = context.new_page()

        t0 = time.monotonic()

        def on_request(request):
            try:
                parts = urlsplit(request.url)
                entry = {
                    "t": round(time.monotonic() - t0, 2), "kind": "request",
                    "method": request.method, "url": request.url[:600],
                    "host": parts.hostname, "resource_type": request.resource_type,
                    "headers": dict(request.headers),
                }
                if request.method in ("POST", "PUT"):
                    try:
                        body = request.post_data
                        entry["post_data"] = body[:3000] if body else None
                    except Exception:
                        entry["post_data"] = "<unavailable>"
                events.append(entry)
            except Exception:
                pass

        def on_response(response):
            try:
                parts = urlsplit(response.url)
                entry = {
                    "t": round(time.monotonic() - t0, 2), "kind": "response",
                    "status": response.status, "url": response.url[:600],
                    "host": parts.hostname,
                }
                content_type = response.headers.get("content-type", "")
                if "json" in content_type and parts.hostname and "anghami" in parts.hostname:
                    try:
                        body = response.json()
                        serialized = json.dumps(body)
                        entry["json"] = body if len(serialized) < 5000 else {"_truncated_keys": list(body)[:50] if isinstance(body, dict) else "list"}
                    except Exception:
                        entry["json"] = "<parse failed>"
                events.append(entry)
            except Exception:
                pass

        context.on("request", on_request)
        context.on("response", on_response)

        print("opening song page...", flush=True)
        page.goto(f"https://play.anghami.com/song/{SONG_ID}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        state = page.evaluate("""() => {
          const login = document.querySelector('a[href*="login"], button[aria-label*="Log"]');
          const m = document.querySelector('audio, video');
          return {has_login_link: !!login, media: !!m};
        }""")
        print("page state:", state, flush=True)

        play = page.get_by_role("button", name="Play", exact=True)
        play.wait_for(state="visible", timeout=30000)
        print("clicking play...", flush=True)
        play.click()

        for elapsed in range(0, 200, 20):
            page.wait_for_timeout(20000)
            media = page.evaluate("""() => {
              const m = document.querySelector('audio, video');
              return m ? {current: m.currentTime, duration: m.duration, paused: m.paused} : null;
            }""")
            print(f"t+{elapsed + 20}s media: {media}", flush=True)
    finally:
        result = {"row": ROW, "song_id": SONG_ID, "events": events,
                  "event_count": len(events),
                  "hosts": sorted({e.get("host") for e in events if e.get("host")})}
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {len(events)} events from {len(result['hosts'])} hosts -> {OUT}", flush=True)
        try:
            browser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
