"""The player's normal encrypted media API, authenticated by existing cookies.

Only session bootstrap and media-location requests are implemented here. Issued
keys remain in memory; this module sends no listening or engagement events.
"""

from base64 import b64decode, b64encode
from gzip import compress, decompress
from hashlib import md5
import json
import re
import time
from urllib.parse import parse_qsl, urlsplit

from .client import GATEWAY_URL
from .bandwidth import measured_request
from .errors import RequestFailure, SessionError
from .provider_recovery import observe_provider_failure, retry_after_seconds
from curl_cffi.curl import CurlError

_SALT = "-Jlfi6:CFND;bpKs;svX]dj@"


def _derive_key(fingerprint: str, timestamp: int, *, request: bool) -> bytes:
    fingerprint = fingerprint.lower()
    # The SDK sums JavaScript UTF-16 code units rather than Unicode code points.
    encoded = fingerprint.encode("utf-16-le")
    total = sum(int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2))
    rounds = total % (7 if request else 13) + 1
    salt = _SALT
    for _ in range(rounds):
        salt = md5((salt + fingerprint + str(timestamp)).encode("utf-8")).hexdigest()
    return salt.encode("ascii")


def _encrypt(payload: dict, key: bytes) -> bytes:
    from pysodium import crypto_aead_chacha20poly1305_encrypt, randombytes
    serialized = "&".join(f"{name}={value}" for name, value in payload.items())
    nonce, associated = randombytes(8), randombytes(12)
    ciphertext = crypto_aead_chacha20poly1305_encrypt(compress(serialized.encode("utf-8")), associated, nonce, key)
    return b"##" + nonce + associated + ciphertext


def _decrypt(reply: str, key: bytes) -> dict:
    from pysodium import crypto_aead_chacha20poly1305_decrypt
    envelope = b64decode(reply, validate=True)
    if len(envelope) < 38 or envelope[:2] != b"##":
        raise ValueError("Invalid media API envelope")
    plaintext = crypto_aead_chacha20poly1305_decrypt(envelope[22:], envelope[10:22], envelope[2:10], key)
    data = json.loads(decompress(plaintext))
    if not isinstance(data, dict):
        raise ValueError("Invalid media API response")
    return data


class PlaybackGateway:
    def __init__(self, session):
        self.session = session
        template = session._template("relations")
        self.headers = dict(template["headers"])
        self.common = {
            name: value for name, value in parse_qsl(urlsplit(template["url"]).query)
            if name in {"lang", "language", "userlanguageprod", "web2"}
        }
        # Preserve the raw cookie value exactly as the browser SDK does.
        self.fingerprint = next((
            item.strip().split("=", 1)[1]
            for item in self.headers.get("cookie", "").split(";")
            if item.strip().startswith("fingerprint=")
        ), "")
        if not self.fingerprint or self.fingerprint == "undefined":
            raise SessionError("The saved session has no media fingerprint cookie. Refresh the selected account.")
        self.common.update({"fingerprint": self.fingerprint, "web2": "true"})
        self.saved_sid = None
        if session._saved.get("renewal_method") == "saved_sid":
            query = parse_qsl(urlsplit(template["url"]).query, keep_blank_values=True)
            identifiers = [[value for name, value in query if name == field] for field in ("sid", "appsid")]
            if (
                len(identifiers[0]) != 1 or identifiers[0] != identifiers[1]
                or not identifiers[0][0] or len(identifiers[0][0]) > 4096
                or identifiers[0][0] in {"undefined", "null"}
                or any(ord(char) < 33 or ord(char) > 126 or char == "&" for char in identifiers[0][0])
            ):
                raise SessionError("The saved renewal session identifier is invalid. Refresh the selected account.")
            self.saved_sid = identifiers[0][0]
        self.tokens = None

    def _post(self, operation: str, payload: dict, *, authenticate: bool = False) -> dict:
        if operation not in {"authenticate", "GETdownload"}:
            raise SessionError("Unsupported media API operation.")
        timestamp = int(time.time())
        if authenticate:
            request_key = _derive_key(self.fingerprint, timestamp, request=True)
            response_key = _derive_key(self.fingerprint, timestamp, request=False)
            socket_id = self.saved_sid or "undefined"
        else:
            if self.tokens is None:
                raise SessionError("The media session has not been validated.")
            request_key = self.tokens["reqkey"].encode("utf-8")
            response_key = self.tokens["reskey"].encode("utf-8")
            socket_id = self.tokens["socketsessionid"]
        params = {**self.common, **payload, "type": operation, "ngsw-bypass": "true"}
        if authenticate:
            params.pop("re_token", None)  # The normal SDK omits undefined query values.
            if self.saved_sid is not None:
                params.update({"sid": self.saved_sid, "appsid": self.saved_sid})
        else:
            params.update({"sid": socket_id, "appsid": socket_id})
        headers = {
            **self.headers, "x-angh-udid": self.fingerprint.lower(),
            "x-angh-session": socket_id, "x-angh-encpayload": "3",
            "x-angh-ts": str(timestamp), "content-type": "application/octet-stream",
        }
        try:
            body = _encrypt(payload, request_key)
            response = measured_request(self.session._http, "post",
                GATEWAY_URL, params=params, data=body, headers=headers,
                timeout=25, allow_redirects=False,
            )
            self.session._require_proxy_route(response)
            if response.status_code != 200:
                failure = RequestFailure("request_rate_limited" if response.status_code == 429 else "request_http_failed",
                                     stage="identity" if authenticate else "metadata",
                                     http_status=response.status_code,
                                     retry_after_seconds=retry_after_seconds(getattr(response, "headers", None)),
                                     retry_safe=False)
                observe_provider_failure(failure)
                raise failure
            data = response.json()
            if isinstance(data, dict) and isinstance(data.get("reply"), str):
                data = _decrypt(data["reply"], response_key)
            if not isinstance(data, dict) or data.get("status") != "ok" or data.get("error"):
                raise RequestFailure("session_authentication_rejected" if authenticate and isinstance(data, dict) and data.get("status") == "failed" else "session_response_invalid",
                                     stage="identity" if authenticate else "metadata", retry_safe=False)
            return data
        except SessionError:
            raise
        except Exception as exc:
            raise RequestFailure("request_transport_failed" if isinstance(exc, (CurlError, OSError)) else "session_response_invalid",
                                 stage="identity" if authenticate else "metadata",
                                 curl_code=getattr(exc, "code", None), retry_safe=False) from None

    def bootstrap(self) -> None:
        agent = self.headers.get("user-agent", "")
        major = re.search(r"Chrome/(\d+)", agent)
        payload = {"reauthenticate": "true"}
        if self.saved_sid is not None:
            payload["sid"] = self.saved_sid
        payload.update({
            "output": "jsonhp",
            "devicename": "Chrome " + (major.group(1) if major else ""),
            "re_token": "undefined",
        })
        data = self._post("authenticate", payload, authenticate=True)
        authentication = data.get("authenticate")
        identity = self.session._saved.get("account_email")
        if (
            not isinstance(authentication, dict) or not identity
            or not isinstance(authentication.get("email"), str)
            or authentication["email"].strip().casefold() != identity
        ):
            mismatch = isinstance(authentication, dict) and isinstance(authentication.get("email"), str) and bool(authentication["email"].strip()) and bool(identity)
            raise RequestFailure("session_identity_mismatch" if mismatch else "session_response_invalid", stage="identity", retry_safe=False)
        required = ("reqkey", "reskey", "socketsessionid", "signingkey")
        if any(
            not isinstance(authentication.get(name), str) or not authentication[name]
            or len(authentication[name]) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in authentication[name])
            for name in required
        ):
            raise SessionError("The media bootstrap did not provide the required playback keys.")
        if any(len(authentication[name].encode("utf-8")) != 32 for name in ("reqkey", "reskey")):
            raise SessionError("The media bootstrap returned unsupported playback keys.")
        self.tokens = {name: authentication[name] for name in required}

    def media_source(self, song_id: str | int) -> dict:
        song_id = str(song_id)
        if not song_id.isascii() or not song_id.isdecimal() or not 1 <= len(song_id) <= 20:
            raise SessionError("Song ID must contain one to twenty decimal digits.")
        if self.tokens is None:
            self.bootstrap()
        timestamp = str(int(time.time() * 1000))
        agent = b64encode(self.headers.get("user-agent", "").encode("latin-1")).decode("ascii")
        signature = md5((timestamp + agent + self.tokens["signingkey"]).encode("utf-8")).hexdigest()
        data = self._post("GETdownload", {
            "fileid": song_id, "HQ": "64", "output": "jsonhp", "retry": "0",
            "ts": timestamp, "ts_hashed": signature,
        })
        returned_id = data["song"].get("id") if isinstance(data.get("song"), dict) else data.get("requestedfileid")
        if str(returned_id) != song_id:
            raise SessionError("The media response did not match the requested song.")
        location = data.get("location")
        if not location:
            try:
                location = data["sections"][0]["data"][0]["location"]
            except (KeyError, IndexError, TypeError):
                location = None
        if not isinstance(location, str) or not location:
            raise SessionError("Anghami did not provide a playable media location.")
        return {"location": location, "song_id": song_id}
