from threading import Thread, Lock, active_count
from datetime import datetime as dt
from uuid import uuid4 as _uuid4
from json import load, dump
from os.path import isfile
from random import random
from time import sleep
import platform
if platform.system() == 'Windows':
    # Windows-specific code using ctypes.windll
	import ctypes


from colorama import init, Fore, Style
from requests import Session


from send_vote_argparser import args


uuid4 = lambda: str(_uuid4())


START = dt.now()
VOTES_SEND = 0
ERRORS_COUNT = 0
USED_ACCOUNTS = []
LOCK_OBJECT = Lock()
VOTES_NEED = args.votes
LOCK_OBJECT_FOR_PRINT = Lock()
TIME_BETWEEN_PLAYS = 60 * 60 * 2
TARGET_ID = args.like or args.play or args.follow
TYPE_OF_ID = "MUSIC" if args.follow is None else "ARTIST"
PROXIES = {
	"EG": "http://mrrocat:v1wwAC7RucFlArPc_country-Egypt@proxy.packetstream.io:31112",
	"RU": "http://mrrocat:v1wwAC7RucFlArPc_country-Russia@proxy.packetstream.io:31112"
}
SESSIONS = {
	"like": {},
	"play": {},
	"follow": {},
}

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
	if platform.system() == 'Windows':
		ctypes.windll.kernel32.SetConsoleTitleW(text)
	else:
		print(text, end="")
	

def url_unpack(line):
	return {
		k: v for k, v in map(lambda x: x.split("="), line.split(";"))
	}

def get_accounts(file_name="registered.txt"):
	if not isfile(file_name):
		print(f"{Fore.RED}Can't access {file_name} file...{Style.RESET_ALL}")
		return []

	country_sorted = []
	with open(file_name) as f:
		for line in f:
			line = line.strip()
			if line.startswith(args.country):
				items = line.split("~")
				country_sorted.append(
					[
						*items[1:3],
						url_unpack(items[3]),
						url_unpack(items[4])
					]
				)
		return country_sorted

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

@thread
def separator():
	while True:
		if LOCK_OBJECT.locked():
			sleep(.3)
			LOCK_OBJECT.release()
		else:
			sleep(.1)

@thread
def worker(email, password, misc, cookies):
	global VOTES_SEND, ERRORS_COUNT

	with Session() as session:
		if args.country in PROXIES.keys():
			session.proxies = {
				"http": PROXIES[args.country],
				"https": PROXIES[args.country]
			}

		session.cookies.update(cookies)

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

		timer = dt.now()

		LOCK_OBJECT.acquire()

		try:
			session_sid = misc["appsidsave"]
			if args.play is not None:
				play_song(
					session, args.play, misc["session_fingerprint"], session_sid
				)
			elif args.like is not None:
				like_song(
					session, args.like, misc["session_fingerprint"], session_sid
				)
			elif args.follow is not None:
				follow_artist(
					session, args.follow, misc["session_uuid"], session_sid
				)
			else:
				raise Exception("Unknown method selected, can't invoke")

			VOTES_SEND += 1

			print(
				f"{Fore.GREEN}Vote finished - [{email}] - "
				f"time [{round((dt.now() - timer).total_seconds(), 2)}] "
				f"seconds.{Style.RESET_ALL}"
			)
		except Exception as reason:
			ERRORS_COUNT += 1

			print(
				f"{Fore.RED}Vote finished - [{email}] - "
				f"with error - [{repr(reason)}] - "
				f"time [{round((dt.now() - timer).total_seconds(), 2)}] "
				f"seconds on Thread.{Style.RESET_ALL}"
			)
		finally:
			if args.play is not None:
				SESSIONS["play"][TARGET_ID].update(
					{
						email: int(dt.now().timestamp()) + TIME_BETWEEN_PLAYS
					}
				)
			elif args.like is not None:
				SESSIONS["like"][TARGET_ID].append(email)
			elif args.follow is not None:
				SESSIONS["follow"][TARGET_ID].append(email)

def main():
	global SESSIONS, USED_ACCOUNTS, OLD_GTOKENS

	set_tittle(
		f"AnghamiVote - {TYPE_OF_ID} ID [{TARGET_ID}] "
		f"Votes [{VOTES_SEND}/{VOTES_NEED}] - "
		f"Threads [{active_count() - 2}] - "
		f"Running [{int((dt.now() - START).total_seconds())}s] - "
		f"Errors [{ERRORS_COUNT}]"
	)

	init()
	separator()

	if isfile("vote_sessions.json"):
		with open("vote_sessions.json") as f:
			SESSIONS = load(f)

	mode = ""

	if args.play is not None:
		mode = "play"
	elif args.like is not None:
		mode = "like"
	else:
		mode = "follow"

	if TARGET_ID in SESSIONS[mode].keys():
		print("Restoring progress ...")
		if args.play is not None:
			for email in list(SESSIONS["play"][TARGET_ID].keys()):
				if SESSIONS["play"][TARGET_ID][email] > dt.now().timestamp():
					USED_ACCOUNTS.append(email)
				else:
					SESSIONS["play"][TARGET_ID].pop(email)
		elif args.like is not None:
			USED_ACCOUNTS = SESSIONS["like"][TARGET_ID]
		elif args.follow is not None:
			USED_ACCOUNTS = SESSIONS["follow"][TARGET_ID]
	else:
		SESSIONS[mode][TARGET_ID] = {} if mode == "play" else []

	print("Starting votes...")

	accounts = get_accounts()

	while len(accounts) > 0 and VOTES_SEND < VOTES_NEED:
		if active_count() - 2 < args.threads and \
				(VOTES_NEED - VOTES_SEND - (active_count() - 2)) > 0:
			creds = accounts.pop(0)
			if creds[0] not in USED_ACCOUNTS:
				worker(*creds)
		else:
			set_tittle(
				f"AnghamiVote - {TYPE_OF_ID} ID [{TARGET_ID}] "
				f"Votes [{VOTES_SEND}/{VOTES_NEED}] - "
				f"Threads [{active_count() - 2}] - "
				f"Running [{int((dt.now() - START).total_seconds())}s] - "
				f"Errors [{ERRORS_COUNT}]"
			)

			sleep(.1)

	while active_count() > 2:
		set_tittle(
			f"AnghamiVote - {TYPE_OF_ID} ID [{TARGET_ID}] "
			f"Votes [{VOTES_SEND}/{VOTES_NEED}] - "
			f"Threads [{active_count() - 2}] - "
			f"Running [{int((dt.now() - START).total_seconds())}s] - "
			f"Errors [{ERRORS_COUNT}]"
		)
		sleep(.1)

if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		pass
	except Exception as reason:
		print(f"Got unexpected error: {repr(reason)}")
	finally:
		print("Saving progress to session ...")

		print(
			f"Voting ended, votes sended [{VOTES_SEND}] - "
			f"during - [{int((dt.now() - START).total_seconds())}s] - "
			f"errors count - [{ERRORS_COUNT}]\nHave a nice day =)"
		)
		#need add lock file
		#lib lockfile
		#or we can split files to write another types
		with open("vote_sessions.json", "w") as f:
			dump(SESSIONS, f, separators=(",", ":"))
