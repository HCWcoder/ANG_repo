import json
import sys
import requests
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from send_vote import url_unpack

with open(ROOT / 'registered.txt', encoding='utf-8') as handle:
    line = handle.read().strip().splitlines()[0]

parts = line.split('~')
misc = url_unpack(parts[3])
cookies = url_unpack(parts[4])

session = requests.Session()
session.headers.update({
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Origin': 'https://www.anghami.com',
    'Referer': 'https://www.anghami.com/',
    'sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124", ";Not A Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-site': 'same-origin',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-user': '?1',
    'sec-fetch-dest': 'document',
})

for key, value in cookies.items():
    session.cookies.set(key, value)

for url in [
    'https://www.anghami.com/',
    'https://api.anghami.com/gateway.php',
]:
    params = None
    if 'api.anghami.com' in url:
        params = {
            'songId': '1263607749',
            'output': 'jsonhp',
            'type': 'GETsong',
            'language': 'en',
            'lang': 'en',
            'appsid': misc.get('appsidsave', ''),
            'web2': 'true',
            'userlanguageprod': 'en',
            'fingerprint': misc.get('session_fingerprint', ''),
            'sid': misc.get('appsidsave', ''),
            'angh_type': 'GETsong',
        }

    response = session.get(url, params=params, timeout=30)
    print('URL', url)
    print('status', response.status_code)
    print('content-type', response.headers.get('content-type'))
    print('set-cookie', response.headers.get('set-cookie'))
    print('request headers', json.dumps(dict(session.headers), indent=2))
    print('cookies sent', json.dumps({k: v for k, v in session.cookies.items()}, indent=2))
    print('body', response.text[:4000])
    print('---')
