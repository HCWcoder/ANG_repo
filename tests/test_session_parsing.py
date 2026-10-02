from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("send_vote", ROOT / "send_vote.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_url_unpack_parses_cookie_string():
    parsed = module.url_unpack(
        "appsidsave=i7%3Aabc%3Adef;fingerprint=abc123;reCaptchaInterval=123"
    )

    assert parsed["appsidsave"] == "i7%3Aabc%3Adef"
    assert parsed["fingerprint"] == "abc123"
    assert parsed["reCaptchaInterval"] == "123"


def test_get_accounts_reads_registered_file():
    accounts = module.get_accounts(file_name=ROOT / "registered.txt")

    assert accounts
    assert all(len(account) == 4 for account in accounts)
    assert all(account[0] for account in accounts)


def test_gateway_settings_match_current_anghami_flow():
    params = module.build_gateway_params()

    assert module.GATEWAY_URL == "https://coussa.anghami.com/gateway.php"
    assert params["web_medium"] == "web"
    assert params["web2"] == "true"
    assert params["language"] == "en"
    assert params["lang"] == "en"
