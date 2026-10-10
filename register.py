from base64 import (
	b64encode as _b64encode,
	b64decode as _b64decode
)
from threading import Thread, Lock, active_count, local
from random import random, randint, choice
from string import ascii_letters, digits
from gzip import compress, decompress
from urllib.parse import quote_plus, urlparse, unquote, quote
from datetime import datetime as dt
from json import loads, load, dump
from uuid import uuid4 as _uuid4
from hashlib import md5 as _md5
from os.path import isfile, join, dirname, abspath
from time import sleep, monotonic
from secrets import token_hex
import ctypes


from colorama import init, Fore, Style
from curl_cffi import requests as http_requests
from curl_cffi.requests import Session
from names import (
	get_first_name, get_last_name
)
from pysodium import (
	crypto_aead_chacha20poly1305_NPUBBYTES,
	crypto_aead_chacha20poly1305_ietf_NPUBBYTES,

	randombytes,
	crypto_aead_chacha20poly1305_encrypt,
	crypto_aead_chacha20poly1305_decrypt
)


from imap_manager import get_verify_email_url
from register_argparser import args


uuid4 = lambda: str(_uuid4())
md5 = lambda x: _md5(x.encode()).hexdigest()
b64encode = lambda x: _b64encode(x.encode("iso-8859-1")).decode()


START = dt.now()
SESSIONS = {}
OLD_GTOKENS = []
ERRORS_COUNT = 0
USED_ACCOUNTS = []
LOCK_OBJECT = Lock()
REGISTERED_ACCOUNTS = []
ACCOUNTS_NEED = args.accounts
LOCK_OBJECT_FOR_PRINT = Lock()
GATEWAY_URL = "https://coussa.anghami.com/gateway.php"
EMAIL_EXISTS_URL = "https://coussa.anghami.com/anghami/email-exists"
PROXIES = {
	"EG": "http://mrrocat:v1wwAC7RucFlArPc_country-EG:proxy.packetstream.io:31112",
	"RU": "http://mrrocat:v1wwAC7RucFlArPc_country-RU@proxy.packetstream.io:31112"
}
RECAPTCHA_SITE_KEY = "6LcsnakUAAAAAOi6tEpMkI3IQmHJ03hEEq1zEB9v"
RECAPTCHA_PAGE_URL = "https://play.anghami.com/login"
# Anghami rejects solver-farm reCAPTCHA v3 tokens (error 1010) because Google
# scores those browser environments too low, and rejects synthetic
# POSTfingerprint devices because they lack FingerprintJS telemetry. Both the
# tokens and the device fingerprint are therefore minted by a genuine local
# browser (CloakBrowser when available, otherwise the installed Chrome).

_print = print

def print(*args, **kwargs):
	LOCK_OBJECT_FOR_PRINT.acquire()
	_print(*args, **kwargs)
	LOCK_OBJECT_FOR_PRINT.release()

def thread(my_func):
	def wrapper(*args, **kwargs):
		my_thread = Thread(
			target=my_func, args=args, kwargs=kwargs, daemon=True
		)
		my_thread.start()
	return wrapper

def set_tittle(text):
	ctypes.windll.kernel32.SetConsoleTitleW(text)

def get_emails(file_name="accounts.txt"):
	if not isfile(file_name):
		print(f"{Fore.RED}Can't access {file_name} file...{Style.RESET_ALL}")
		return []
	with open(file_name) as f:
		data = f.read().strip().split("\n")
		return data

def get_registered_emails(file_name="registered.txt"):
	if not isfile(file_name):
		return set()
	with open(file_name) as f:
		return {
			items[1]
			for line in f
			if len(items := line.strip().split("~")) > 1
		}

def get_old_gtokens(file_name="re_token.txt"):
	if not isfile(file_name):
		print(f"{Fore.YELLOW}Can't access {file_name} file...{Style.RESET_ALL}")
		return []
	with open(file_name) as f:
		data = f.read().strip().split("\n")
		return data

def generate_password(length=9):
	return "".join(choice(ascii_letters + digits) for i in range(length))

def urlencode(payload):
	return "&".join("=".join(item) for item in payload.items())

def js_hash(payload):
	key = "34%i7ateMyImcreept24fjf#ang."
	Ce, Pe, De, Qe = 0, 0, 0, ""
	ye = [i for i in range(256)]

	for ke in range(256):
		Pe = (Pe + ye[ke] + ord(key[ke % len(key)])) % 256
		Ce = ye[ke]
		ye[ke] = ye[Pe]
		ye[Pe] = Ce

	Pe = 0

	for ke in range(len(payload)):
		De = (De + 1) % 256
		Pe = (Pe + ye[De]) % 256
		Ce = ye[De]
		ye[De] = ye[Pe]
		ye[Pe] = Ce
		Qe += chr(ord(payload[ke]) ^ ye[(ye[De] + ye[Pe]) % 256])

	return Qe

def build_gateway_params(**overrides):
	params = {
		"language": "en",
		"lang": "en",
		"web2": "true",
		"web_medium": "web",
		"userlanguageprod": "en",
	}
	params.update(overrides)
	return params

def gateway_headers(session=None):
	user_agent = None
	if session is not None:
		user_agent = session.headers.get("User-Agent")
	return {
		"Accept": "application/json, text/plain, */*",
		"Origin": "https://play.anghami.com",
		"Referer": "https://play.anghami.com/",
		"User-Agent": user_agent or (
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
			"AppleWebKit/537.36 (KHTML, like Gecko) "
			"Chrome/124.0.0.0 Safari/537.36"
		)
	}

def assert_response_ok(response, context="request"):
	if getattr(response, "ok", False):
		return response
	status_code = getattr(response, "status_code", None)
	reason = getattr(response, "reason", None) or "unknown"
	text = getattr(response, "text", "")
	message = f"{context} failed with {status_code} {reason}"
	if text:
		message = f"{message}: {text[:200]}"
	raise AssertionError(message)

def get_key(timestamp, to_server, session_fingerprint):
	session_fingerprint = session_fingerprint.lower()
	x = 7 if to_server else 13
	nfe = sum(ord(i) for i in session_fingerprint)
	salt = "-Jlfi6:CFND;bpKs;svX]dj@"

	for _ in range(nfe % x + 1):
		salt = md5(salt + session_fingerprint + str(timestamp))

	return salt

def decrypt_payload(payload, key):
	payload = _b64decode(payload.encode())

	nonce_offset = 2
	ad_offset = nonce_offset + 8
	payload_offset = ad_offset + 12

	nonce = payload[nonce_offset:ad_offset]
	ad = payload[ad_offset:payload_offset]
	encrypted = payload[payload_offset:]

	decrypted = crypto_aead_chacha20poly1305_decrypt(
		encrypted, ad, nonce, key.encode()
	)

	return decompress(decrypted)

def encrypt_payload(payload, key):
	payload = compress(payload.encode())
	nonce = randombytes(crypto_aead_chacha20poly1305_NPUBBYTES)
	ad = randombytes(crypto_aead_chacha20poly1305_ietf_NPUBBYTES)
	
	encrypted = crypto_aead_chacha20poly1305_encrypt(
		payload, ad, nonce, key.encode()
	)

	return bytes([35, 35, *nonce, *ad, *encrypted])

def parse_payload(payload):
	return loads(payload.decode())

def brute_payload(payload, server_timestamp, session_fingerprint):
	for offset in range(-60, 60):
		key = get_key(
			int(server_timestamp)+offset,
			False,
			session_fingerprint
		)
		try:
			return parse_payload(
				decrypt_payload(payload, key)
			)
		except Exception:
			pass
	
	raise Exception("Impossible to decrypt server response")

def send_with_encryption(session, params, payload, session_fingerprint):
	timestamp = int(dt.now().timestamp())

	key = get_key(timestamp, True, session_fingerprint)

	payload = encrypt_payload(
		urlencode(payload), key
	)

	response = session.post(
		GATEWAY_URL,
		params=build_gateway_params(**params),
		data=payload,
		headers={
			**gateway_headers(session),
			"X-ANGH-SESSION": "undefined",
			"X-ANGH-ENCPAYLOAD": "3",
			"X-ANGH-TS": str(timestamp),
			"X-ANGH-UDID": session_fingerprint.lower()
		}
	)

	response = assert_response_ok(response, "send-with-encryption")
	assert response.json().get("reply")

	server_timestamp = response.headers["x-angh-t32"]
	payload = response.json()["reply"]

	payload = brute_payload(
		payload, server_timestamp, session_fingerprint
	)

	return payload

def _read_cookie_value(cookie_store, key):
	if cookie_store is None:
		return None
	if hasattr(cookie_store, "get"):
		return cookie_store.get(key)
	if isinstance(cookie_store, dict):
		return cookie_store.get(key)
	return None

def _store_fingerprint_cookies(session, fingerprint, fingerprint_id):
	if fingerprint:
		session.cookies.set("fingerprint", fingerprint)
	if fingerprint_id:
		session.cookies.set("xxlfingerprint", fingerprint_id)

def initialize_browser_fingerprint(session, session_uuid=None, fallback_cookies=None):
	fingerprint_id = session_uuid or uuid4()
	session_hash = b64encode(js_hash(fingerprint_id))
	encoded_hash = quote_plus(session_hash)
	params = build_gateway_params(
		fp=fingerprint_id,
		hash=encoded_hash,
		type="POSTfingerprint",
		fingerprint="",
		angh_type="POSTfingerprint"
	)
	try:
		response = session.get(
			GATEWAY_URL,
			params=params,
			headers=gateway_headers(session),
			timeout=30,
		)
		response.raise_for_status()
		payload = response.json()
		if payload.get("status") != "ok":
			raise RuntimeError(payload)
		fingerprint = payload.get("fingerprint")
		if fingerprint:
			_store_fingerprint_cookies(session, fingerprint, fingerprint_id)
			return fingerprint, fingerprint_id
		return None, fingerprint_id
	except Exception:
		cookie_sources = []
		if fallback_cookies is not None:
			cookie_sources.append(fallback_cookies)
		cookie_sources.append(session.cookies)
		for cookie_source in cookie_sources:
			fallback_fingerprint = _read_cookie_value(cookie_source, "fingerprint")
			fallback_uuid = _read_cookie_value(cookie_source, "xxlfingerprint") or session_uuid
			if fallback_fingerprint:
				_store_fingerprint_cookies(session, fallback_fingerprint, fallback_uuid)
				return fallback_fingerprint, fallback_uuid
		raise

_RECAPTCHA_EXECUTE_JS = """
async (args) => {
  const [sitekey, action] = args;
  if (!window.grecaptcha || !window.grecaptcha.execute) {
    await new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "https://www.google.com/recaptcha/api.js?render=" + sitekey;
      s.onload = res;
      s.onerror = rej;
      document.head.appendChild(s);
    });
  }
  await new Promise((res) => window.grecaptcha.ready(res));
  return await window.grecaptcha.execute(sitekey, {action});
}
"""

class _BrowserIdentity:
	"""One account's genuine browser identity: a fresh browser context whose
	own FingerprintJS telemetry produced the fingerprint cookies."""

	def __init__(self, context):
		self.context = context
		self.cookies = context.cookies()
		values = {cookie["name"]: cookie["value"] for cookie in self.cookies}
		self.fingerprint = values.get("fingerprint")
		self.fingerprint_id = values.get("xxlfingerprint")

	def close(self):
		try:
			self.context.close()
		except Exception:
			pass

def _proxy_route_for_country(country):
	"""(curl_url, playwright_proxy) sharing one sticky exit IP, or None.

	The working PacketStream credentials live in the local encrypted store;
	the PROXIES literals are kept only as a fallback for other machines.
	"""
	if not country:
		return None
	dpapi_path = join(dirname(abspath(__file__)), ".anghami", "packetstream.dpapi")
	if isfile(dpapi_path):
		try:
			from anghami_session.proxy import (
				PacketStreamProxy, load_packetstream_proxy
			)
			saved = load_packetstream_proxy(dpapi_path)
			route = PacketStreamProxy.from_route(
				saved.username, saved.auth_key, token_hex(8),
				"http://proxy.packetstream.io:31112", country=country
			)
			options = route.transport_options()
			username, password = options["proxy_auth"]
			# Chrome's CONNECT tunnel is unreliable over PacketStream's HTTPS
			# endpoint, so the browser uses HTTP:31112; curl uses HTTPS:31111.
			# Stickiness keys on the session label, so both exit the same IP.
			curl_server = "https://proxy.packetstream.io:31111"
			curl_url = (
				f"{curl_server.split('://')[0]}://{quote(username)}:"
				f"{quote(password)}@proxy.packetstream.io:31111"
			)
			return curl_url, {"server": options["proxy"], "username": username,
							  "password": password}
		except Exception:
			pass
	url = PROXIES.get(country)
	if not url or "*" in url:
		return None
	try:
		# Legacy literals miss the "@" and read user:password:host:port.
		parts = url.split("://", 1)[1].split(":")
		username, password, host, port = parts[0], parts[1], parts[2], parts[3]
	except (IndexError, ValueError):
		return None
	curl_url = f"http://{quote(username)}:{quote(password)}@{host}:{port}"
	return curl_url, {"server": f"http://{host}:{port}",
					  "username": username, "password": password}

class _BrowserService:
	"""Per-thread genuine browser that mints device fingerprints and tokens.

	The Playwright sync API is bound to the thread that created it, and each
	worker thread handles one account, so one service per thread keeps both
	rules satisfied without cross-thread plumbing. CloakBrowser is preferred
	for its fresh antidetect fingerprint per launch; the system Chrome is the
	fallback.
	"""

	def __init__(self, proxy=None):
		self._playwright = None
		self._anonymous = None
		self._proxy = proxy
		self._browser = self._launch()

	def _launch(self):
		proxy = self._proxy
		try:
			import cloakbrowser

			kwargs = {"headless": True, "chromium_sandbox": True,
					  "stealth_args": False}
			if proxy:
				kwargs["proxy"] = proxy
			# No --fingerprint pin: every launch must mint a fresh device.
			return cloakbrowser.launch(**kwargs)
		except Exception:
			pass
		from playwright.sync_api import sync_playwright

		self._playwright = sync_playwright().start()
		kwargs = {"headless": True}
		if proxy:
			kwargs["proxy"] = proxy
		try:
			return self._playwright.chromium.launch(channel="chrome", **kwargs)
		except Exception:
			return self._playwright.chromium.launch(**kwargs)

	def new_identity(self):
		last_error = None
		for attempt in range(3):
			if attempt == 2 and self._proxy is not None:
				# A dead sticky exit IP should not sink the registration:
				# mint the identity over a direct connection instead.
				self._relaunch_without_proxy()
			try:
				return self._mint_identity_once()
			except Exception as error:
				last_error = error
		raise RuntimeError(
			f"Browser did not produce a device fingerprint: {last_error}"
		)

	def _relaunch_without_proxy(self):
		self._proxy = None
		try:
			self._browser.close()
		except Exception:
			pass
		if self._playwright is not None:
			try:
				self._playwright.stop()
			except Exception:
				pass
			self._playwright = None
		self._anonymous = None
		self._browser = self._launch()

	def _mint_identity_once(self):
		context = self._browser.new_context()
		page = context.new_page()
		try:
			page.goto(RECAPTCHA_PAGE_URL, timeout=90000,
					  wait_until="domcontentloaded")
		except Exception:
			pass
		deadline = monotonic() + 60
		while monotonic() < deadline:
			identity = _BrowserIdentity(context)
			if identity.fingerprint:
				return identity
			sleep(1)
		context.close()
		raise RuntimeError("no fingerprint cookie after page load")

	def solve(self, action, identity=None):
		# A fresh page per token matches how the site itself executes
		# reCAPTCHA (once per user action); the identity context is kept so
		# the token shares the account's cookies and fingerprint.
		context = identity.context if identity is not None else self._anon_context()
		last_error = None
		for _ in range(2):
			page = context.new_page()
			try:
				page.goto(RECAPTCHA_PAGE_URL, timeout=90000,
						  wait_until="domcontentloaded")
				token = page.evaluate(
					_RECAPTCHA_EXECUTE_JS, [RECAPTCHA_SITE_KEY, action]
				)
				if token:
					return token
				raise RuntimeError("browser returned an empty token")
			except Exception as error:
				last_error = error
			finally:
				page.close()
		raise RuntimeError(
			f"Browser failed to produce a reCAPTCHA token: {last_error}"
		)

	def _anon_context(self):
		if self._anonymous is None:
			self._anonymous = self._browser.new_context()
		return self._anonymous

	def close(self):
		try:
			self._browser.close()
		except Exception:
			pass
		if self._playwright is not None:
			try:
				self._playwright.stop()
			except Exception:
				pass

_browser_tls = local()

def _get_browser_service():
	service = getattr(_browser_tls, "service", None)
	if service is None:
		proxy_route = getattr(_browser_tls, "proxy_route", None)
		proxy = proxy_route[1] if proxy_route else None
		try:
			service = _BrowserService(proxy=proxy)
		except Exception as error:
			raise RuntimeError(
				"Could not start a browser for reCAPTCHA/fingerprint minting. "
				"Install requirements-login.txt (CloakBrowser) or Google Chrome. "
				f"Underlying error: {error}"
			) from error
		_browser_tls.service = service
	return service

def _close_browser_service():
	service = getattr(_browser_tls, "service", None)
	if service is not None:
		service.close()
		_browser_tls.service = None
	_browser_tls.identity = None

def get_recaptcha_token(action, session=None):
	if args.old_tokens:
		return choice(OLD_GTOKENS)
	service = _get_browser_service()
	identity = getattr(_browser_tls, "identity", None)
	return service.solve(action, identity)

def _session_identity(session):
	"""Fingerprint for API calls: the browser-minted device when the worker
	already applied its cookies, otherwise the legacy POSTfingerprint flow."""
	fingerprint = _read_cookie_value(session.cookies, "fingerprint")
	fingerprint_id = _read_cookie_value(session.cookies, "xxlfingerprint")
	if fingerprint:
		return fingerprint, fingerprint_id
	return initialize_browser_fingerprint(session, fingerprint_id)

def get_email_exists(session, email, session_fingerprint, re_token=None):
	params = {
		"email": email,
		"output": "jsonhp",
		"fingerprint": session_fingerprint
	}

	if re_token:
		params["re_token"] = re_token

	response = session.get(
		EMAIL_EXISTS_URL,
		params=build_gateway_params(**params),
		headers=gateway_headers(session)
	)

	assert_response_ok(response, "get-email-exists")
	payload = response.json()
	if payload.get("status") == "failed":
		raise Exception(payload.get("error"))
	return payload.get("exists")

def login_account(session, email, password):
	session_fingerprint, session_uuid = _session_identity(session)

	if not session_fingerprint:
		raise Exception("Couldn't initialize session fingerprint")

	if not get_email_exists(session, email, session_fingerprint):
		raise Exception("Account doesn't exist")

	re_token = get_recaptcha_token("authenticate", session)

	session.cookies.set("fingerprint", session_fingerprint)
	session.cookies.set("reCaptchaInterval", str(int(dt.now().timestamp())))

	payload = {
		"m": "an",# type of login [const]
		"u": email,# email
		"p": quote_plus(password),# password
		"output": "jsonhp",# const
		"devicename": "Chrome 124",
		"re_token": re_token
	}

	params = {
		"m": "an",
		"u": email,
		"p": quote_plus(password),
		"output": "jsonhp",
		"devicename": "Chrome 124",
		"re_token": re_token,
		"ngsw-bypass": "true",
		"type": "authenticate",
		"language": "en",
		"lang": "en",
		"web2": "true",
		"fingerprint": session_fingerprint,
		"angh_type": "authenticate"
	}

	payload = send_with_encryption(
		session, params, payload, session_fingerprint
	)

	session_sid = payload["authenticate"]["socketsessionid"]

	re_token = get_recaptcha_token("authenticate", session)

	session.cookies.set("reCaptchaInterval", str(int(dt.now().timestamp())))

	payload = {
		"devicename": "Chrome 124",
		"disableCaptcha": "false",
		"output": "jsonhp",# const
		"re_token": re_token,
		"reauthenticate": "true",
		"sid": session_sid,
		"supports_atmos": "false",
		"udid": session_fingerprint
	}

	params = {
		"devicename": "Chrome 124",
		"reauthenticate": "true",
		"disableCaptcha": "false",
		"supports_atmos": "false",
		"udid": session_fingerprint,
		"output": "jsonhp",
		"re_token": re_token,
		"ngsw-bypass": "true",
		"type": "authenticate",
		"language": "en",
		"lang": "en",
		"web2": "true",
		"fingerprint": session_fingerprint,
		"angh_type": "authenticate"
	}

	payload = send_with_encryption(
		session, params, payload, session_fingerprint
	)

	session_sid = payload["authenticate"]["socketsessionid"]

	session.cookies.set("appsidsave", quote_plus(session_sid))
	session.cookies.set("oats", str(int(dt.now().timestamp())))

	return {
		"appsidsave": session_sid,
		"session_fingerprint": session_fingerprint,
		"session_uuid": session_uuid
	}

def register_account(session, email):
	new_password = generate_password()

	session_fingerprint, session_uuid = _session_identity(session)

	if not session_fingerprint:
		raise Exception("Couldn't initialize session fingerprint")

	re_token = get_recaptcha_token("email_exists", session)

	if get_email_exists(session, email, session_fingerprint, re_token):
		raise Exception("Account already exists")

	session.cookies.set("reCaptchaInterval", str(int(dt.now().timestamp())))

	profile = {
		"firstname": get_first_name(),
		"lastname": get_last_name(),
		"age": randint(18, 36),
		"gender": choice(["male", "female"])
	}

	last_payload = None
	for _ in range(3):
		re_token = get_recaptcha_token("register", session)
		data = {
			"type": "REGISTERuser",
			"email": email,
			"password": new_password,
			**profile,
			"m": "an",
			"output": "jsonhp",
			"re_token": re_token
		}
		params = {
			**data,
			"fingerprint": session_fingerprint,
			"angh_type": "REGISTERuser"
		}
		response = session.post(
			GATEWAY_URL,
			params=build_gateway_params(**params),
			data=data,
			headers=gateway_headers(session)
		)
		assert_response_ok(response, "register-account")
		payload = response.json()
		if payload.get("status") == "ok":
			return new_password
		error = payload.get("error")
		if isinstance(error, dict) and str(error.get("code")) == "1010":
			# Genuine browser tokens are free, so retry with a fresh one.
			last_payload = payload
			continue
		raise RuntimeError(f"Registration failed: {payload}")
	raise RuntimeError(
		f"Anghami rejected the reCAPTCHA token with error 1010 on 3 "
		f"consecutive genuine browser tokens: {last_payload}"
	)

def confirm_email(session, url):
	session_fingerprint, session_uuid = _session_identity(session)

	response = session.get(url)

	assert response.ok, response.reason

	token = response.url.split("?token=")[1].split("&")[0]

	params = {
		"language": "en",
		"web2": "true",
		"lang": "en",
		"userlanguageprod": "en",
		"token": token,
		"type": "POSTvalidatemailtoken",
		"fingerprint": session_fingerprint
	}

	data = {
		"token": token,
		"validatetype": "undefined",
		"validate": "undefined"
	}

	response = session.post(
		GATEWAY_URL,
		params=build_gateway_params(**params),
		data=data,
		headers=gateway_headers(session)
	)

	assert_response_ok(response, "confirm-email")
	payload = response.json()
	status = payload.get("status")
	if status is None and isinstance(payload.get("_attributes"), dict):
		status = payload["_attributes"].get("status")
	assert payload.get("title") == "Success!" or status == "ok", \
			payload.get("error") or payload

def url_pack(dict_object):
	return ";".join(["=".join(i) for i in dict_object.items()])

@thread
def separator():
	while True:
		if LOCK_OBJECT.locked():
			sleep(.6)
			LOCK_OBJECT.release()
		else:
			sleep(.1)

@thread
def worker(email, password):
	global ERRORS_COUNT

	with Session(impersonate="chrome") as session:
		proxy_route = None
		for _ in range(3):
			# Sticky exit IPs die often; probe each route with a cheap
			# request and rotate the session label (new IP) until one works.
			candidate = _proxy_route_for_country(args.country)
			if candidate is None:
				break
			session.proxies = {
				"http": candidate[0],
				"https": candidate[0]
			}
			try:
				session.get("https://api.ipify.org", timeout=15)
				proxy_route = candidate
				break
			except Exception:
				continue
		_browser_tls.proxy_route = proxy_route

		session.headers = {
			"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
					"image/avif,image/webp,image/apng,*/*;q=0.8,"
					"application/signed-exchange;v=b3;q=0.9",
			"Content-Type": "application/x-www-form-urlencoded",
			"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
						"AppleWebKit/537.36 (KHTML, like Gecko) "
						"Chrome/124.0.0.0 Safari/537.36",
			"Origin": "https://www.anghami.com",
			"Referer": "https://www.anghami.com/",
			"Accept-Language": "en-US,en;q=0.9",
			"Accept-Encoding": "gzip, deflate, br"
		}

		timer = dt.now()

		LOCK_OBJECT.acquire()

		identity = None
		try:
			if not args.old_tokens:
				identity = _get_browser_service().new_identity()
				_browser_tls.identity = identity
				for cookie in identity.cookies:
					session.cookies.set(
						cookie["name"], cookie["value"],
						domain=cookie.get("domain"),
						path=cookie.get("path", "/")
					)

			session.get("https://play.anghami.com/")

			anghami_passw = register_account(session, email)
			USED_ACCOUNTS.append(email)
			url = get_verify_email_url(email, password)
			confirm_email(session, url)
			misc_data = login_account(session, email, anghami_passw)

			REGISTERED_ACCOUNTS.append(
				"~".join([args.country, email, anghami_passw, url_pack(misc_data), 
					url_pack(session.cookies.get_dict())])
			)

			print(
				f"{Fore.GREEN}Registration finished - [{email}] - "
				f"time [{round((dt.now() - timer).total_seconds(), 2)}] "
				f"seconds.{Style.RESET_ALL}"
			)
		except Exception as reason:
			ERRORS_COUNT += 1

			print(
				f"{Fore.RED}Registration finish - [{email}] - "
				f"with error - [{repr(reason)}] - "
				f"time [{round((dt.now() - timer).total_seconds(), 2)}] "
				f"seconds on Thread.{Style.RESET_ALL}"
			)
		finally:
			if identity is not None:
				identity.close()
			_close_browser_service()

def main():
	global SESSIONS, USED_ACCOUNTS, OLD_GTOKENS

	set_tittle(
		f"AnghamiRegister - Accounts [{len(REGISTERED_ACCOUNTS)}/{ACCOUNTS_NEED}] - "
		f"Threads [{active_count() - 2}] - "
		f"Running [{int((dt.now() - START).total_seconds())}s] - "
		f"Errors [{ERRORS_COUNT}]"
	)

	init()
	separator()

	if isfile("register_sessions.json"):
		with open("register_sessions.json") as f:
			SESSIONS = load(f)

	if args.old_tokens:
		OLD_GTOKENS = get_old_gtokens()

	if len(SESSIONS.keys()):
		print("Restoring progress ...")
		registered_emails = get_registered_emails()
		USED_ACCOUNTS = [
			email for email in SESSIONS["used_accounts"]
			if email in registered_emails
		]

	print("Starting registration...")

	emails = get_emails()

	while len(emails) > 0 and len(REGISTERED_ACCOUNTS) < ACCOUNTS_NEED:
		if active_count() - 2 < args.threads and \
				(ACCOUNTS_NEED - len(REGISTERED_ACCOUNTS) - (active_count() - 2)) > 0:
			creds = emails.pop(0).split(":")
			if creds[0] not in USED_ACCOUNTS:
				worker(*creds)
		else:
			set_tittle(
				f"AnghamiRegister - Accounts [{len(REGISTERED_ACCOUNTS)}/{ACCOUNTS_NEED}] - "
				f"Threads [{active_count() - 2}] - "
				f"Running [{int((dt.now() - START).total_seconds())}s] - "
				f"Errors [{ERRORS_COUNT}]"
			)

			sleep(.1)

	while active_count() > 2:
		set_tittle(
			f"AnghamiRegister - Accounts [{len(REGISTERED_ACCOUNTS)}/{ACCOUNTS_NEED}] - "
			f"Threads [{active_count() - 2}] - "
			f"Running [{int((dt.now() - START).total_seconds())}s] - "
			f"Errors [{ERRORS_COUNT}]"
		)
		sleep(.1)

if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		SESSIONS.update({
			"used_accounts": USED_ACCOUNTS
		})
	except Exception as reason:
		print(f"Got unexpected error: {repr(reason)}")
		
		SESSIONS.update({
			"used_accounts": USED_ACCOUNTS
		})
	finally:
		if len(REGISTERED_ACCOUNTS):
			with open("registered.txt", "a") as f:
				f.write("\n".join(REGISTERED_ACCOUNTS) + "\n")

		print(
			f"Registration ended, accounts registered [{len(REGISTERED_ACCOUNTS)}] - "
			f"during - [{int((dt.now() - START).total_seconds())}s] - "
			f"errors count - [{ERRORS_COUNT}]\nHave a nice day =)"
		)

		print("Saving progress to session ...")

		with open("register_sessions.json", "w") as f:
			dump(SESSIONS, f, separators=(",", ":"))
