import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DummyCookies(dict):
    def set(self, key, value):
        self[key] = value

    def get(self, key, default=None):
        return super().get(key, default)


class DummySession:
    def __init__(self):
        self.cookies = DummyCookies()
        self.cookies["fingerprint"] = "stored-fingerprint"
        self.cookies["xxlfingerprint"] = "stored-uuid"

    def get(self, *args, **kwargs):
        raise RuntimeError("forbidden")


colorama = types.ModuleType("colorama")
colorama.init = lambda *args, **kwargs: None
colorama.Fore = types.SimpleNamespace(RED="", YELLOW="", GREEN="", RESET_ALL="")
colorama.Style = types.SimpleNamespace(RESET_ALL="")
sys.modules.setdefault("colorama", colorama)

twocaptcha = types.ModuleType("twocaptcha")


class TwoCaptcha:
    def __init__(self, *args, **kwargs):
        pass


twocaptcha.TwoCaptcha = TwoCaptcha
sys.modules.setdefault("twocaptcha", twocaptcha)

names = types.ModuleType("names")
names.get_first_name = lambda: "First"
names.get_last_name = lambda: "Last"
sys.modules.setdefault("names", names)

pysodium = types.ModuleType("pysodium")
pysodium.crypto_aead_chacha20poly1305_NPUBBYTES = 12
pysodium.crypto_aead_chacha20poly1305_ietf_NPUBBYTES = 12
pysodium.randombytes = lambda size: b"x" * size
pysodium.crypto_aead_chacha20poly1305_encrypt = lambda *args, **kwargs: b""
pysodium.crypto_aead_chacha20poly1305_decrypt = lambda *args, **kwargs: b""
sys.modules.setdefault("pysodium", pysodium)

imap_manager = types.ModuleType("imap_manager")
imap_manager.get_verify_email_url = lambda *args, **kwargs: "http://example.invalid"
sys.modules.setdefault("imap_manager", imap_manager)

register_argparser = types.ModuleType("register_argparser")
register_argparser.args = types.SimpleNamespace(accounts=1, old_tokens=False, country="EG", threads=1)
sys.modules.setdefault("register_argparser", register_argparser)


spec = importlib.util.spec_from_file_location("register", ROOT / "register.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_initialize_browser_fingerprint_falls_back_to_saved_cookie_values():
    session = DummySession()

    fingerprint, fingerprint_id = module.initialize_browser_fingerprint(
        session,
        "session-uuid",
        fallback_cookies={"fingerprint": "cookie-fingerprint", "xxlfingerprint": "cookie-uuid"},
    )

    assert fingerprint == "cookie-fingerprint"
    assert fingerprint_id == "cookie-uuid"
