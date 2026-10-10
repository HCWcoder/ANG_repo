"""Throwaway probe: threaded identity minting diagnostics."""
import sys
import threading
import time

sys.argv = ["register.py", "-a", "1", "-c", "EG", "-t", "1"]
import register


def run():
    route = register._proxy_route_for_country("EG")
    svc = register._BrowserService(proxy=route[1])
    print("browser class:", type(svc._browser).__module__, flush=True)
    start = time.time()
    context = svc._browser.new_context()
    page = context.new_page()
    try:
        page.goto(register.RECAPTCHA_PAGE_URL, timeout=90000,
                  wait_until="domcontentloaded")
        print("goto done in", round(time.time() - start, 1), "s", flush=True)
    except Exception as error:
        print("goto failed:", type(error).__name__, str(error)[:150], flush=True)
    for i in range(12):
        cookies = {c["name"] for c in context.cookies()}
        print(f"t+{round(time.time() - start, 1)}s cookies: {sorted(cookies)}",
              flush=True)
        if "fingerprint" in cookies:
            break
        time.sleep(5)
    page.close()
    context.close()
    svc.close()


thread = threading.Thread(target=run)
thread.start()
thread.join(240)
print("done")
