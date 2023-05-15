from email import message_from_bytes, message, utils, policy
from imaplib import IMAP4, IMAP4_SSL
from datetime import datetime as dt
from xml.etree import ElementTree
from re import findall, search
import imaplib


from requests import get


imaplib._MAXLINE = 9999999999


presettings = {
	"myrambler.ru": {
		"type": "imap",
		"imap" : "mail.rambler.ru",
		"port" : 993,
		"ssl" : True
	}
}

def get_email_settings(email):
	domain = email.split('@')[1]

	if domain in presettings.keys():
		return presettings[domain]

	response = get(f"https://autoconfig.thunderbird.net/v1.1/{domain}")

	if response.status_code == 200:

		root = ElementTree.fromstring(response.content)

		for child in root.iter("incomingServer"):

			if child.attrib["type"] == "imap":

				return {
					"type": "imap",
					"imap" : child.find("hostname").text,
					"port" : int(child.find("port").text),
					"ssl" : child.find("socketType").text == "SSL"
				}

			raise Exception(f"Can't get imap settings for {email}.")

	return {"type": "none"}

def is_match_in_mail(regex, mail):
	if type(regex) == list:
		for r in regex:
			if search(r, mail) != None:
				return True
	else:
		if search(regex, mail) != None:
			return True
	return False

def find_msg_data(keyword, resp):
	for item in resp:
		if type(item) == tuple:
			for msg in item:
				if keyword.lower().encode() in msg.lower():
					return msg
	return ""

def get_mail_by_imap_from(email_settings, email, password, mail_from, regex = None):
	now = dt.now()

	while (dt.now() - now).total_seconds() <= 100:
		if email_settings["ssl"]:
			imap = IMAP4_SSL(email_settings["imap"],
				email_settings["port"])
		else:
			imap = IMAP4(email_settings["imap"],
				email_settings["port"])

		imap.socket().settimeout(180)

		imap.login(email, password)

		try:
			status, imap_dirs = imap.list()
			assert status == "OK", "Can't load list of dirs"
			for imap_dir in imap_dirs:
				if type(imap_dir) is bytes and len(imap_dir) > 0:
					imap_dir = imap_dir.decode("utf-8", "ignore")
					imap_dir = "\"" + imap_dir.split(" \"")[-1] if imap_dir.count("\"") > 2 else imap_dir.split()[-1]
				else:
					continue
				status, data = imap.select(imap_dir)
				assert status == "OK", "Can't select dir"
				status, data = imap.search(None, "ALL")
				assert status == "OK" and data[0] != None, "Can't search mails"
				msg_ids = data[0].decode("utf-8", "ignore").split()[::-1]
				for msg_id in msg_ids:
					status, msg_data = imap.fetch(msg_id, "(RFC822)")
					assert status == "OK", "Can't fetch mail"
					msg_data = find_msg_data("From:", msg_data)
					assert msg_data != "", "Can't find msg_data"
					msg = message_from_bytes(msg_data, _class = message.EmailMessage, 
							policy = policy.default)
					date_tz = utils.parsedate_tz(str(msg["Date"]))
					if date_tz != None:
						if (now - dt.fromtimestamp(
							utils.mktime_tz(date_tz))).total_seconds() <= 100:
							if mail_from in str(msg["From"]).lower():
								msg_text = msg_data \
									.decode("utf-8", "ignore")\
									.replace("=\r\n", "") \
									.replace("=3D", "=") \
									.replace("&amp;", "&")
								if regex != None:
									if is_match_in_mail(regex, msg_text):
										imap.logout()
										return msg_text
								else:
									imap.logout()
									return msg_text
		except Exception as reason:
			print(reason)
			continue
	imap.logout()
	raise Exception("Can't find letter in mailbox")


def get_verify_email_url(email, password):
	email_settings = get_email_settings(email)

	assert email_settings.get("type") != "none", "Unknown email settings"

	regex = r"(http://email.anghami.com/c/.*?)\""

	mail_text = get_mail_by_imap_from(email_settings, email, password,
		"hello@anghami.com",
		regex=regex)

	if mail_text != "":
		return findall(regex, mail_text)[1]
	
	raise Exception(f"Can't find mail with link {email}")