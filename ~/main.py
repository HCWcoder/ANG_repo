from base64 import (
	b64encode as _b64encode,
	b64decode as _b64decode
)
from threading import Thread, active_count
from random import random, randint, choice
from string import ascii_letters, digits
from gzip import compress, decompress
from urllib.parse import quote_plus
from datetime import datetime as dt
from json import loads, load, dump
from uuid import uuid4 as _uuid4
from hashlib import md5 as _md5


from twocaptcha import TwoCaptcha
from requests import Session
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


uuid4 = lambda: str(_uuid4())
md5 = lambda x: _md5(x.encode()).hexdigest()
b64encode = lambda x: _b64encode(x.encode("iso-8859-1")).decode()

def thread(my_func):
	def wrapper(*args, **kwargs):
		my_thread = Thread(
			target=my_func, args=args, kwargs=kwargs, daemon=True
		)
		my_thread.start()
	return wrapper

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
	for offset in range(-30, 30):
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
		"https://api.anghami.com/gateway.php",
		params=params,
		data=payload,
		headers={
			"Accept": "application/json, text/plain, */*",
			"X-ANGH-SESSION": "undefined",
			"X-ANGH-ENCPAYLOAD": "3",
			"X-ANGH-TS": str(timestamp),
			"X-ANGH-UDID": session_fingerprint.lower()
		}
	)

	assert response.ok, response.reason
	assert response.json().get("reply")

	server_timestamp = response.headers["x-angh-t32"]
	payload = response.json()["reply"]

	payload = brute_payload(
		payload, server_timestamp, session_fingerprint
	)

	return payload

def like_song(session, song_id, session_fingerprint, session_sid):
	payload = {
		"type": "PUTplaylist",
		"name": "$1234567890LIKED#",
		"songid": song_id,
		"action": "append",
		"extras": "",
		"x-socket-id": uuid4()
	}

	params = {
		"lang": "en",
		"language": "en",
		"output": "jsonhp",
		"fingerprint": session_fingerprint,
		"web2": "true",
		"sid": session_sid,
		"angh_type": "PUTplaylist"
	}

	response = session.post(
		"https://api.anghami.com/gateway.php",
		params=params,
		data=payload,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	assert response.json().get("status") == "ok"

def get_song(session, song_id, session_fingerprint, session_sid):
	params = {
		"songId": song_id,
		"output": "jsonhp",
		"type": "GETsong",
		"language": "en",
		"lang": "en",
		"appsid": session_sid,
		"web2": "true",
		"userlanguageprod": "en",
		"fingerprint": session_fingerprint,
		"sid": session_sid,
		"angh_type": "GETsong"
	}

	response = session.get(
		"https://api.anghami.com/gateway.php",
		params=params,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	assert response.json().get("status")

	return response.json()

def play_song(session, song_id, session_fingerprint, session_sid):
	song_data = get_song(session, song_id,
		session_fingerprint, session_sid)

	timestamp = int(dt.now().timestamp() * 1e3)
	playsecs = round(float(song_data["duration"]) + random() / 1e2, 6)

	params = {
		"output": "jsonhp",
		"RetrievalMode": "Streamed",
		"localtimestamp": str(timestamp),
		"playsecs": str(playsecs),
		"songid": song_id,
		"playper": "1",
		"is_SPQ_broadcaster": "false",
		"type": "REGISTERwebplay",
		"language": "en",
		"lang": "en",
		"appsid": session_sid,
		"web2": "true",
		"userlanguageprod": "en",
		"fingerprint": session_fingerprint,
		"sid": session_sid,
		"angh_type": "REGISTERwebplay"
	}

	response = session.get(
		"https://api.anghami.com/gateway.php",
		params=params,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	assert response.json().get("status") == "ok"

def follow_artist(session, artist_id, session_uuid, session_sid):
	payload = {
		"type": "followARTIST",
		"action": "follow",
		"artistid": artist_id,
		"extras": "",
		"x-socket-id": uuid4()
	}

	params = {
		"lang": "en",
		"language": "en",
		"output": "jsonhp",
		"fingerprint": session_uuid,
		"web2": "true",
		"sid": session_sid,
		"angh_type": "followARTIST"
	}

	response = session.post(
		"https://api.anghami.com/gateway.php",
		params=params,
		data=payload,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	assert response.json().get("status") == "ok"

def get_session_fingerprint(session, session_uuid, session_hash):
	params = {
		"fp": session_uuid,
		"hash": quote_plus(session_hash),
		"type": "POSTfingerprint",
		"language": "en",
		"lang": "en",
		"web2": "true",
		"fingerprint": "",
		"angh_type": "POSTfingerprint"
	}

	response = session.get(
		"https://api.anghami.com/gateway.php",
		params=params,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	assert response.json()["status"] == "ok", response.json()["status"]

	session_fingerprint = response.json()["fingerprint"]

	return session_fingerprint

def get_email_exists(session, email, session_uuid):
	params = {
		"lang": "en",
		"language": "en",
		"output": "jsonhp",
		"fingerprint": session_uuid,
		"web2": "true",
		"angh_type": "GETEmailExists",
		"cahceBust": str(round(random(), 7))
	}

	data = {
		"type": "GETEmailExists",
		"email": email
	}

	response = session.post(
		"https://api.anghami.com/gateway.php",
		params=params,
		data=data,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	return response.json().get("exists")

def login_account(session, email, password):
	session_uuid = uuid4()
	session_hash = b64encode(js_hash(session_uuid))
	session_fingerprint = get_session_fingerprint(session, session_uuid, session_hash)

	if not get_email_exists(session, email, session_uuid):
		raise Exception("Account doesn't exist")

	re_token = solver.recaptcha(
		sitekey="6LcsnakUAAAAAOi6tEpMkI3IQmHJ03hEEq1zEB9v",
		url="https://www.anghami.com/",
		action="authenticate"
	).get("code")

	session.cookies.set("fingerprint", session_fingerprint)
	session.cookies.set("reCaptchaInterval", str(int(dt.now().timestamp())))

	payload = {
		"m": "an",# type of login [const]
		"u": email,# email
		"p": quote_plus(password),# password
		"output": "jsonhp",# const
		"devicename": "Chrome 103",
		"re_token": re_token
	}

	params = {
		"m": "an",
		"u": email,
		"p": quote_plus(password),
		"output": "jsonhp",
		"devicename": "Chrome 103",
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

	re_token = solver.recaptcha(
		sitekey="6LcsnakUAAAAAOi6tEpMkI3IQmHJ03hEEq1zEB9v",
		url="https://www.anghami.com/",
		action="authenticate"
	).get("code")

	session.cookies.set("reCaptchaInterval", str(int(dt.now().timestamp())))

	payload = {
		"devicename": "Chrome 103",
		"disableCaptcha": "false",
		"output": "jsonhp",# const
		"re_token": re_token,
		"reauthenticate": "true",
		"sid": session_sid,
		"supports_atmos": "false",
		"udid": session_fingerprint
	}

	params = {
		"devicename": "Chrome 103",
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

def register_account(session, email):
	session_uuid = uuid4()
	new_password = generate_password()

	if get_email_exists(session, email, session_uuid):
		raise Exception("Account already exists")

	params = {
		"lang": "en",
		"language": "en",
		"output": "jsonhp",
		"fingerprint": session_uuid,
		"web2": "true",
		"angh_type": "REGISTERuser"
	}

	data = {
		"type": "REGISTERuser",
		"email": email,
		"password": new_password,
		"firstname": get_first_name(),
		"lastname": get_last_name(),
		"age": randint(18, 36),
		"gender": choice(["male", "female"]),
		"m": "an"
	}

	response = session.post(
		"https://api.anghami.com/gateway.php",
		params=params,
		data=data,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	assert response.json().get("status") == "ok"

	return new_password

def confirm_email(session, url):
	session_uuid = uuid4()
	session_hash = b64encode(js_hash(session_uuid))
	session_fingerprint = get_session_fingerprint(session, session_uuid, session_hash)

	response = session.get(url)

	assert response.ok, response.reason

	token = response.url.split("?token=")[1]

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
		"https://api.anghami.com/gateway.php",
		params=params,
		data=data,
		headers={
			"Accept": "application/json, text/plain, */*"
		}
	)

	assert response.ok, response.reason
	assert response.json().get("title") == "Success!", \
			"Email already confirmed"

def main():
	email = "billy.pippa_520@outlook.com"
	password = ",?t2f%91,d$Ud)"

	solver = TwoCaptcha("")

	with Session() as session:
		session.headers = {
			"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
					"image/avif,image/webp,image/apng,*/*;q=0.8,"
					"application/signed-exchange;v=b3;q=0.9",
			"Content-Type": "application/x-www-form-urlencoded",
			"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
						"AppleWebKit/537.36 (KHTML, like Gecko) "
						"Chrome/103.0.0.0 Safari/537.36",
			"Origin": "https://www.anghami.com",
			"Referer": "https://www.anghami.com/",
			"Accept-Language": "en-US,en;q=0.9",
			"Accept-Encoding": "gzip, deflate, br"
		}

		session.get("https://www.anghami.com/")

		# play_song(session, "72939623", session_fingerprint, session_sid)
		# like_song(session, "72939623", session_fingerprint, session_sid)

		# follow_artist(session, "2632850", session_uuid, session_sid)

if __name__ == "__main__":
	main()
