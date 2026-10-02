import requests
from send_vote import url_unpack

with open('registered.txt') as f:
    line = f.read().strip().splitlines()[0]
parts = line.split('~')
misc = url_unpack(parts[3])
cookies = url_unpack(parts[4])

s = requests.Session()
s.headers.update({
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Origin': 'https://www.anghami.com',
    'Referer': 'https://www.anghami.com/',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124", ";Not A Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-site': 'same-origin',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-user': '?1',
    'sec-fetch-dest': 'document',
})
for k, v in cookies.items():
    s.cookies.set(k, v)

for url in ['https://www.anghami.com/', 'https://api.anghami.com/gateway.php']:
    try:
        resp = s.get(url, params={'songId':'1280677978','output':'jsonhp','type':'GETsong','language':'en','lang':'en','appsid':misc.get('appsidsave',''),'web2':'true','userlanguageprod':'en','fingerprint':misc.get('session_fingerprint',''),'sid':misc.get('appsidsave',''),'angh_type':'GETsong'} if 'api.anghami.com' in url else None, timeout=20)
        print('URL', url)
        print('status', resp.status_code)
        print('content-type', resp.headers.get('content-type'))
        print(resp.text[:3000])
        print('---')
    except Exception as exc:
        print('URL', url, 'ERR', repr(exc))
