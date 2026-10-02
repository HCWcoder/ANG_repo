# ANG_repo

The `anghami_session` package provides a saved-login workflow for an existing
Anghami account. After capturing a login once, it reuses the session through
direct HTTP requests. Checking the session and reading relations or playlists
does not launch CloakBrowser or require the account password.

## Local test console

Double-click **Test Console.cmd** to open the local browser interface. Keep its
terminal window open while using the console; closing that window stops the
server. Existing imported accounts, saved sessions, proxy settings, and reports
are reused from this workspace.

In **Workbench**, select ready accounts and choose **Tests per account** before
running a play or like test. **Select all** chooses every currently verified
account with a saved session; **Select random** replaces the selection with
exactly the number of unique ready accounts you enter. **Clear** removes the
selection. These controls only choose accounts and do not start a test. Newly
prepared accounts join the selection pool after their sessions are verified;
failed preparations are excluded. Enter a test song ID and click **Save song**
when you want to change the declared track. Saving sends no play or like event; the saved
ID applies to future play, like, and song-access tests. Unsaved changes must be
saved or reverted before those tests can run. For example, three selected
accounts with two tests
each produces six sequential runs. A song that is already liked is verified and
skipped rather than receiving another like request. **Check sessions** and
**Check song access** only read account or song state.

In **Accounts**, **Accounts to add** controls how many additional imported
accounts to prepare, currently up to five per preparation run. Preview shows the
selected source rows without logging in. Prepare captures a normal login when
needed and saves the session for future browser-free tests; it does not send play
or like events. You can also find an email's source rows or refresh selected
sessions there.

Choose **Reuse registered session · No browser** to prepare through Python HTTP
requests. This reuses the selected account's own saved session, or recovers its
existing session from the imported registered record. Recovery verifies the
server account email, authenticated reads, an anonymous negative control and
normal existing-session renewal before saving. It performs no password login
and opens no browser. An expired session or required verification stops the run
without falling back to a browser. The browser data option applies only to
**Browser login** preparation.

Choose the connection beside the action you want to run. **Connection for tests**
in Workbench applies to play, like, session checks, and song access checks.
**Connection for preparation** in Accounts applies to new account preparation;
its preview uses the same choice but only selects rows without logging in.
**Connection for session refresh** applies only to refreshing saved sessions.
These three choices are independent and each starts with **Direct connection**,
which uses your internet connection without a proxy. **Egypt · PacketStream**
uses the saved proxy. Choosing Egypt for preparation does not change how later
play or like tests connect.

Account preparation and session refresh each have an independent **Reduce browser
data usage** checkbox. It starts off and works with either Direct or Egypt. When
enabled, it skips nonessential artwork, fonts and media, and reuses explicitly
public, versioned application files after their first download. Each account
keeps its own login context, cookies and captured session; authentication and
session verification remain enabled. The first run fills the public asset cache,
so later runs can save more data. Normal saved-session Python tests do not use
this browser option.

Public app files are downloaded without account credentials through the action's
selected connection, then reused from disk. Login and account requests continue
through that selected connection with the account's own session.

The cache also covers those same public files when background workers fetch
them, including the two verified cosmetic login backgrounds and the exact
Euclid/Tajawal font files. Ordinary unnecessary image and font requests remain
blocked; background preloads can reuse their verified public bytes. These
unversioned cosmetic and font files are cached for the server's allowed duration,
capped at 24 hours. reCAPTCHA libraries, challenge, token and verification
requests always use the browser's normal network requests. Existing cached
reCAPTCHA libraries are ignored. Expired or invalid public cache files are
downloaded again through the selected connection. Background workers remain enabled because disabling them
did not complete the measured login flow.

Save updated proxy credentials or check routing in **Proxy settings**. A section
using Egypt requires saved credentials before its network action can start;
direct actions in the other sections remain available. Connection choices are
locked while a run or submission is in progress. The console never displays a
stored auth key. View run progress and per-account results in Workbench, and open
saved results in **Reports**. A run that fails or has an uncertain result stops
instead of automatically repeating its request.

## Your registered account list

The account manager imports `registered.txt` into `.anghami/accounts.sqlite3`.
Account contents (including passwords, legacy session fields, cookies, and any
newly captured sessions) are encrypted with Windows DPAPI. Account lookup uses a
keyed index whose key is also DPAPI-encrypted. Counts, source row numbers, and
check timestamps remain ordinary database metadata.

```powershell
.\.venv\Scripts\python.exe main.py accounts import
.\.venv\Scripts\python.exe main.py accounts status
```

Import runs entirely offline, leaves the source file byte-for-byte intact, and
creates an exact encrypted backup under `.anghami/backups/`. Duplicate emails
remain separate source rows. Reimporting the same file preserves saved sessions;
importing a different file into an existing vault is rejected. Use `--vault PATH`
after the account command to create or select a different vault.

**Imported does not mean authenticated.** Legacy session data is preserved for
recovery, but is not automatically treated as a working browser session. Each
account needs its own normal login or an account-bound session captured earlier.

Find an account's row, then refresh and check that selected account:

```powershell
.\.venv\Scripts\python.exe main.py accounts find --email "you@example.com"
.\.venv\Scripts\python.exe main.py accounts login --row 1
.\.venv\Scripts\python.exe main.py accounts check --row 1
.\.venv\Scripts\python.exe main.py accounts playlists --row 1
```

Replace `1` with the row reported by `find`. Unique email addresses can also be
selected with `--email` instead of `--row`. Duplicate emails require an explicit
row. Double-click **Accounts Status.cmd** to see counts, or **Refresh Account.cmd**
to enter an account email locally and sign in using that row's stored credentials.
No password is placed in process arguments or printed by the account manager.

If an account's password has changed, enter the new password in a hidden local
prompt. The vault updates it only after a successful login and HTTP validation:

```powershell
.\.venv\Scripts\python.exe main.py accounts login --row 1 --prompt-password
```

This changes the locally saved credential; it does not reset the password on
Anghami or rewrite the original source file.

For a headless sign-in check, add `--headless` to the selected account's login
command. If Anghami requires interactive verification, rerun without that flag.

To sign in with your installed Google Chrome instead of CloakBrowser:

```powershell
.\.venv\Scripts\python.exe main.py accounts login --row 5 --browser chrome
```

This opens a temporary, separate Chrome profile and needs no CloakBrowser key.
Chrome must already be installed. Both browser options use the normal Anghami
login page; the saved HTTP session works independently of the browser afterward.
Installed Chrome loaded Anghami in a visible window during verification; its
headless navigation was rejected before credentials were entered on this machine.

Account login closes its browser before validating direct HTTP access and the
unauthenticated control. It saves a replacement only after both checks succeed.
The capture's email must match the selected row, preventing a session from being
assigned to a different account. Failed renewals preserve the previous session.
`check_failed` means the most recent check failed; it does not distinguish an
expired session from a network error. The saved session is retained for retry.

To attach a separately captured session that includes the account identity:

```powershell
.\.venv\Scripts\python.exe main.py accounts attach --row 1 --session ".anghami\session.dpapi"
```

Old captures without an account identity require a fresh login through the
account manager. Account checks and reads need no browser after a successful
login. This manager exposes individually selected account operations. The
explicit test commands below are limited to the configured test track and
selected test accounts.

The original source still contains plaintext credentials and remains private.
To recover an exact copy from the encrypted backup, choose a new output filename:

```powershell
.\.venv\Scripts\python.exe main.py accounts restore-source --output ".anghami\registered-recovered.txt"
```

Restore refuses to overwrite an existing file. It intentionally produces the
original plaintext account list, so keep the restored file private too.

## Use the saved session

From this repository in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m anghami_session check --negative-control
.\.venv\Scripts\python.exe -m anghami_session relations
.\.venv\Scripts\python.exe -m anghami_session playlists
```

`main.py` forwards to the same CLI. For example,
`.\.venv\Scripts\python.exe main.py check --negative-control` checks the saved
session; running `main.py` without arguments also performs a read-only check.

Double-click **Check Session.cmd** to run the first command. A successful check
requires HTTP 200 **and** API `status: ok` for every captured operation. The
negative control uses a separate HTTP client with authentication removed and
requires API `status: failed`. An HTTP 200 alone is not evidence of a valid login.

The default session is `.anghami/session.dpapi`; `--session PATH` selects a
different encrypted file. `.anghami/last-check.json` contains a timestamped
check report without cookies or session tokens. The data-reading commands print
account data, so avoid sharing their raw output unnecessarily.

Use the client from Python:

```python
from anghami_session import AnghamiSession, SessionError

with AnghamiSession() as session:
    report = session.check()
    playlists = session.request("playlists")
```

The client preserves the captured session ID, fingerprint, cookies, and request
headers. It uses `curl_cffi` with Chrome impersonation: ordinary HTTP clients
returned 403 during verification even with the same authenticated session.
Captured reads and parameterized song metadata requests use the observed Anghami
gateway, with redirects disabled and a 25-second timeout. The playback probe
additionally implements the player's session bootstrap and media-location API.

## Probe playback without a browser

Install the optional audio decoder once:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-playback.txt
```

An account with a saved session needs no browser, password prompt or fresh login
to run this command:

```powershell
.\.venv\Scripts\python.exe main.py accounts probe-playback --row 1 --song-id 1280677978 --seconds 5
```

The command validates authenticated reads and an anonymous negative control,
fetches the requested song, and completes the player's normal HTTP cookie-based
session bootstrap. It checks that the returned account email matches the selected
account, then obtains the audio location using the returned playback keys. Those
keys remain in memory. Verification or expired-session failures stop the probe;
it does not open a browser or perform a password login automatically.

It requests at most 1 MiB of media over HTTPS using a separate HTTP client that
receives no gateway cookies or session headers. PyAV/FFmpeg decodes the actual
audio to PCM, and the probe consumes the requested duration silently at real-time
pace. It records HTTP status, media bytes, codec, decoded duration and nonzero
sample evidence. This checks authentication, media delivery and decoding. It does
not submit listening statistics or claim a completed song listen.

The duration is limited to 1-30 seconds, defaulting to five. Reports omit account
credentials, signing keys and signed media URLs. The selected account's report is
saved to `.anghami/account-1.http-playback-report.json` (with its source row number).

To check the same five saved accounts sequentially without launching a browser:

```powershell
.\.venv\Scripts\python.exe tests\probe_http_playback.py --rows 1 2 3 4 5 --song-id 1280677978 --seconds 5
```

This manual probe accepts at most five distinct accounts and requires all their
sessions to exist before it starts. It writes a combined report to
`.anghami/http-five-account-playback-report.json` plus a timestamped report, and
returns a nonzero exit status if any account fails. It does not change likes or
follow artists. Pytest does not collect this live probe.

From Python:

```python
from anghami_session.vault import AccountVault

with AccountVault() as accounts:
    report = accounts.probe_playback(1, "1280677978", seconds=5)
```

For a separately saved encrypted session, use
`python main.py probe-playback --song-id 1280677978 --seconds 5 --session PATH`.
That session must include its captured account identity. To read only song
metadata, use `accounts song --row 1 --song-id 1280677978`.

## Refresh an expired session

Double-click **Refresh Session.cmd**, or run:

```powershell
.\.venv\Scripts\python.exe -m anghami_session login
```

Enter your email and password in the local terminal. The password input is hidden
and is not saved. This command opens CloakBrowser, signs in through the normal
Anghami page, captures authenticated read requests, and closes the browser. It
then validates the captured session through direct HTTP before atomically
replacing the saved file. A failed login or validation preserves the previous
session. Close any other CloakBrowser windows first if using its one-session
free license.

Login request metadata is recorded in `.anghami/login-request.redacted.json`
with parameter/header **names only**. Authentication bodies, passwords, cookie
values, and raw login URLs are omitted. The new client does not reproduce the
legacy encrypted-password/CAPTCHA-solver flow.

Anghami controls session lifetime. An expired or revoked session needs another
login; this implementation does not claim permanent sessions or browser-free
password authentication.

## Setup on a new checkout

Python 3.10+ on Windows is required for the local DPAPI store. This workspace uses
Python 3.14.5.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Only session capture needs the additional browser dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-login.txt
.\.venv\Scripts\python.exe -m cloakbrowser login
```

CloakBrowser uses its existing per-user license and browser cache on this
machine. An encrypted session created by the earlier local setup can be imported
without opening a browser:

```powershell
.\.venv\Scripts\python.exe -m anghami_session import --source "..\CloakBrowser\anghami\session.dpapi"
```

Import also validates the session and the unauthenticated control before saving.
The encrypted file is bound to the Windows user who saved it. It contains session
credentials, so it is excluded from Git along with temporary files and reports.

## Scope and validation

To exercise the original `send_vote.play_song` function locally for the test song:

```powershell
.\.venv\Scripts\python.exe tests\probe_legacy_play_local.py --song-id 1280677978
```

This loads only the original function definitions and supplies a mock transport,
synthetic session fields, and a fixed 114.99-second song fixture. It checks the
metadata/play-record request sequence and saves
`.anghami/legacy-play-local-report.json`. All responses are local mocks: no account
credentials are read, no network requests are made, and no listening statistics
are submitted. It verifies local request construction rather than live endpoint
acceptance. The separate `accounts probe-playback` command above verifies real
saved-session authentication, media delivery and decoding over HTTP.

The saved-session command can exercise the original `send_vote.play_song`
function for the configured test track:

```powershell
.\.venv\Scripts\python.exe main.py accounts test-play-record --row 7 --song-id 1263607749
```

This command is limited to the ID in `anghami_session/play_record.py`'s
`TEST_SONG_ID` (currently `1263607749`) and the selected test accounts in
`TEST_ACCOUNT_ROWS` (initial source rows 1-5 and 7), plus additional rows explicitly
prepared and enrolled with `accounts prepare-tests`. With the default `--count 1`, each invocation checks the saved session and returned account
identity, reads and validates the song metadata, then attempts exactly one
`REGISTERwebplay` request. It opens no browser and downloads no audio. The event
reports the full song duration with `playper=1`, so it is a synthetic completion
test rather than evidence of listening.

On October 1, 2026, a read-only check of `1263607749` using row 1 returned a country
licensing refusal from Anghami and stopped before any event attempt. Recognized
country licensing refusals are reported as `metadata_region_unavailable`.
The separately selected account at row 7 passed login, server account identity
validation and a read-only metadata check for the same track, which returned a
167-second duration. Row 7's encrypted session is saved and ready for the command
above. This access check did not submit a play event.

The redacted result is saved to
`.anghami/account-7.test-play-record-report.json` (using the selected source row).
It distinguishes an API acknowledgment from a verified downstream statistic.
The command journals the attempt before sending and never retries a play-record
request. If the outcome is `unknown`, check the server-side test records before
running the command again; a lost response does not establish that the event was
rejected. It does not run the voting script's bulk worker, like songs, or follow
artists.

The report's byte counters cover the song metadata and event HTTP requests.
Authentication preflight traffic and TLS/TCP/IP overhead are excluded from those
counters; audio traffic is zero for this command.

To test the like action through the saved session for row 7:

```powershell
.\.venv\Scripts\python.exe main.py accounts test-like --row 7 --song-id 1263607749
```

This command checks the selected session and server account identity, validates
the song metadata, and reads its current membership in Liked Songs. If it is not
already liked, it calls the original `send_vote.like_song` function for one
`PUTplaylist` append request, then reads the playlist again to verify the saved
state. Membership comes from the complete stored song ID order, which includes
entries that may be absent from the visible song list. An already liked song is
reported without sending another append request.
The command leaves the song liked. It opens no browser, downloads no audio, and
does not send play or artist-follow events.

Results are saved to `.anghami/account-7.test-like-report.json` (using the selected
source row). The report distinguishes API acknowledgment from verified playlist
membership. A write timeout or ambiguous response is journaled without an
automatic resend; inspect the account's saved like state before another test.

Both test commands accept `--count` from 1 to 5 for this test setup. Tests run
sequentially on the selected row and song, with a new preflight for each run.
Any failure or unknown write result stops the remaining runs. The like command
keeps its normal idempotent behavior: after the song is liked, further tests
verify the stored state and report `skipped_already_liked` without another append.
For counts greater than one, the batch results are saved to
`.anghami/account-7.test-play-record-batch-report.json` or
`.anghami/account-7.test-like-batch-report.json`. The usual single-test report
contains the latest run, including an interrupted or unknown attempt.

### Prepare additional registered accounts

The existing vault already contains the registered.txt import. To choose how many
additional unique accounts to make ready for testing, preview the next five rows:

```powershell
.\.venv\Scripts\python.exe main.py accounts prepare-tests --count 5 --dry-run
```

Prepare those accounts through normal logins and validated saved sessions:

```powershell
# Direct connection for login and validation:
.\.venv\Scripts\python.exe main.py accounts prepare-tests --count 5 --browser chrome --headless

# Egypt proxy for both browser login and HTTP validation:
.\.venv\Scripts\python.exe main.py accounts prepare-tests --count 5 --browser chrome --headless --proxy-egypt
```

Choose a count from 1 to 5 per preparation run. Selection skips accounts already
in the test group and duplicate email identities, preserving the registered.txt
source row numbers. Existing saved sessions are validated and reused; otherwise
the command captures a normal login in an isolated browser profile. `--headless`
hides that initial browser. Once saved, ordinary play/like tests need no browser.
Rows are added to the persistent test group only after session validation succeeds.
Preparation sends no like or play records. It stops at the first failure and
preserves rows prepared earlier in the run. Its safe report is
`.anghami/accounts-prepare-tests-report.json`.

Add `--reduce-browser-data` to `accounts prepare-tests` or `accounts login` to
enable the same browser option from the terminal. For example, prepare one account
through Egypt with headless CloakBrowser and reduced browser downloads:

```powershell
.\.venv\Scripts\python.exe main.py accounts prepare-tests --count 1 --browser cloakbrowser --headless --proxy-egypt --reduce-browser-data
```

Omit `--proxy-egypt` to use Direct while keeping reduced downloads. Public asset
files are stored under `.anghami/browser-public-assets`; account responses and
authentication data are excluded. Keeping the option off preserves the original
browser loading behavior.

View the prepared test rows and saved readiness state:

```powershell
.\.venv\Scripts\python.exe main.py accounts test-accounts
```

The October 1, 2026 read-only preview selected rows `6 8 9 10 11`. That preview did
not log in or enroll them. After a successful preparation, use the actual row
numbers returned by the command. Both test commands accept `--rows` for up to five
distinct prepared accounts, instead of `--row` or `--email`:

```powershell
.\.venv\Scripts\python.exe main.py accounts test-like --rows 6 8 9 10 11 --song-id 1263607749 --count 1 --proxy-egypt
.\.venv\Scripts\python.exe main.py accounts test-play-record --rows 6 8 9 10 11 --song-id 1263607749 --count 1 --proxy-egypt
```

For these test commands, `--count` is the number of tests per selected account.
Accounts and their tests run sequentially and stop at the first failure or
unknown result. The combined report is `.anghami/test-like.accounts-batch-report.json`
or `.anghami/test-play-record.accounts-batch-report.json`; each row also retains
its individual reports. An already liked song is verified without another append.

The proxy flag is chosen independently for preparation and subsequent tests.
Omit it for a direct connection. Add `--start-row 12` to preparation to select
additional accounts starting from that original source row. Running preparation
again selects the next not-yet-enrolled unique accounts; it does not replace the
registered.txt file or existing working sessions on a failed validation.

### Prepare a country group with a resumable local command

For a longer account-preparation job, the country runner selects the exact country
tag in the first `registered.txt` field and verifies that the file matches the
encrypted vault import. It runs normal logins and saved-session validation one
account at a time, using headless CloakBrowser and reduced browser downloads.
By default it uses Direct: it bypasses configured proxy environment and system
settings without loading PacketStream credentials. Add `--proxy-egypt` to use
the encrypted PacketStream Egypt profile for both browser login and HTTP
validation. The country tag chooses accounts; the proxy flag chooses their
connection independently. Both modes use headless CloakBrowser and reduced
downloads unless `--full-browser-data` is added.

Add `--no-browser` to reuse existing registered sessions through HTTP instead
of opening CloakBrowser. This works with Direct or the Egypt proxy. The browser
download settings have no effect in this mode. If the existing session cannot
be verified and renewed, the account fails without a password login or browser
fallback; the usual pause and review rules remain in place.

Preview the selection offline, then start it from PowerShell:

```powershell
Set-Location 'C:\Users\User\Documents\ANG_repo'
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country LB
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country LB --run
```

For EG-tagged accounts through the Egypt proxy, preview offline, check one
account, then continue the same country checkpoint:

```powershell
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country EG --proxy-egypt --dry-run
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country EG --run --proxy-egypt --limit 1
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country EG --run --proxy-egypt
```

For a bounded browser-free check of the next pending account:

```powershell
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country EG --run --proxy-egypt --no-browser --limit 1
```

After that succeeds, the same command without `--limit 1` continues the country
checkpoint in browser-free mode. Omit `--proxy-egypt` to use Direct.

A bounded browser-free EG preparation on 2026-10-02 recovered source row 25,
saved and enrolled it, and passed a separate saved-session renewal check. The
preparation itself took 21.243 seconds and transferred 100,484 bytes through
PacketStream (0.100484 MB upload plus download). At an assumed $1 per decimal
GB, this sample estimates $0.00010048 per account or $0.10048 per 1,000 similar
preparations. This is one sample, not a billing measurement: the relay includes
destination TLS and tunnel traffic but excludes the outer proxy TLS and TCP/IP
framing. Failed routes, expired sessions and account response sizes change the
result. Details are in `.anghami/http-preparation-bandwidth-report.json`.

Each account uses a fresh sticky Egypt profile shared by its login and session
validation. Credentials are loaded only for an actual run, and a verified Egypt
country check precedes the account attempt. A failed proxy check pauses without
marking an unattempted account as failed or falling back to Direct. Proxy mode
uses paid PacketStream bandwidth; Direct uses your local internet connection.
Preview, status, and stop commands stay offline. The checkpoint and failed-row
details record the selected connection without storing proxy credentials.
A proxy failure also reports `proxy_failure` with the source row, check stage,
and a fixed diagnostic code, such as `authentication_rejected`,
`egypt_unverified`, or `country_check_failed`. A generic transport failure does
not establish whether PacketStream timed out or rejected an upstream route.
Earlier checkpoints remain compatible; an already discarded failure has no
recoverable detailed cause.

The same `--run` command resumes normal stop or limit pauses from the saved checkpoint. Completed accounts
are skipped. Failed or uncertain attempts are retained for review and are not
automatically logged in again. This command prepares accounts only; it sends no
play or like events and does not change the existing small test-run limits.
Three consecutive failures pause the job for review rather than continuing
through the whole list during a connection or login problem.
Repeating `--run` after a failure pause keeps it paused and makes no new login
attempt. Browser cleanup or license failures also require review. After fixing
the cause, explicitly allow one pending account as a diagnostic:

```powershell
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country LB --run --resume-after-review --limit 1
```

This does not retry earlier failed rows or clear failure counters. Another
failure pauses immediately; a successful diagnostic clears the consecutive
failure counters, and the usual `--run` command can then continue.
Add `--full-browser-data` when normal browser downloads are needed during login;
it keeps the selected connection and account validation. For a reviewed
one-account diagnostic with this setting:

```powershell
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country LB --run --resume-after-review --limit 1 --full-browser-data
```

Check progress or request a stop from another PowerShell window:

```powershell
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country LB --status
.\.venv\Scripts\python.exe -m anghami_session.country_preparation --country LB --stop
```

The stop request finishes the active account before pausing. Keep the PC awake
and the preparation terminal open. Avoid starting another browser preparation
or session refresh from the UI while it runs; the runner pauses when it detects
an active UI job. The command uses the Windows account that owns the encrypted
vault and calls no OpenAI API, so the unattended Python run does not require an
active conversation or consume model tokens on its own.

If an interrupted login is marked `unknown`, review its row before continuing.
After that review, adding `--fresh-plan` to `--run` acknowledges the uncertain
rows and continues only the remaining pending rows; it does not retry the
uncertain or failed accounts. Progress is stored privately in
`.anghami/country-LB-preparation-progress.json` for the LB plan.
`--fresh-plan` does not clear or bypass a failure pause.

### Optional PacketStream Egypt proxy

Configure the proxy locally with a hidden key prompt:

```powershell
.\.venv\Scripts\python.exe main.py accounts proxy-configure --username mrrocat
```

Enter the base PacketStream proxy auth key, or the key ending in `_country-EG`.
It is stored using Windows DPAPI in `.anghami/packetstream.dpapi`, bound to the
Windows user who saved it. Credentials are excluded from command output,
reports, and Git. The profile uses PacketStream's HTTPS proxy endpoint at
`https://proxy.packetstream.io:31111` with Egypt targeting and one sticky session
per invocation. An independent cookie-free country check must verify Egypt
before any Anghami request. Failed proxy checks stop the test without a direct
fallback. The authenticated and anonymous session checks use the same selected
proxy; the anonymous check has a fresh cookie jar.

Run both functions through the proxy, choosing the number of tests locally:

```powershell
$tests = 3 # Choose 1-5. The default is 1 if --count is omitted.
.\.venv\Scripts\python.exe main.py accounts test-play-record --row 7 --song-id 1263607749 --proxy-egypt --count $tests
.\.venv\Scripts\python.exe main.py accounts test-like --row 7 --song-id 1263607749 --proxy-egypt --count $tests
```

Omit `--proxy-egypt` to use the ordinary direct connection. For a read-only proxy
and saved-session check, run:

```powershell
.\.venv\Scripts\python.exe main.py accounts relations --row 7 --proxy-egypt
```

The test reports include the provider, requested country, and verified country
check. A reused proxy tunnel can report a CONNECT status of zero; the route check
still requires libcurl to confirm that the request used the proxy.

`main.py` now starts the saved-account CLI instead of the old hard-coded login
example. Its legacy helper functions remain available. The registration and
voting entry points (`register.py`, `send_vote.py`) retain their local edits and
existing file formats; they do not automatically consume the new DPAPI session.
Session validation uses relations and playlists reads. A separate, explicitly
invoked probe can verify normal player and like behavior on a small account set.

To run that live functional probe for song `1280677978`:

```powershell
.\.venv\Scripts\python.exe tests\probe_five_accounts.py --rows 1 2 3 4 5 --browser chrome --headed
```

The probe selects at most five distinct imported accounts. For each, it refreshes
and validates the login, observes five seconds of real decoded audio, and changes
the song's like state once before restoring and reloading to confirm the original
state. It stops further account mutations if restoration cannot be confirmed.
It does not send an instant full-duration listening event. Omit `--browser chrome`
to use CloakBrowser, and omit `--headed` to run headless. Use `--rows 5` to retry
only one selected account.

Results are saved without credentials or raw request URLs to
`.anghami/five-account-functional-qa-report.json` and a separate timestamped report
for each run. A failed check returns a nonzero exit status. This probe is manual
and is not collected by pytest.

Run the regression tests with:

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

The new tests cover session validation, transport settings, expired sessions,
redacted errors, negative controls, encrypted storage, account isolation,
duplicate handling, exact source recovery, and preservation of the previous
session when renewal fails. Playback tests use local audio fixtures to verify
decoding, pacing, media URL/redirect validation, bounded downloads and separation
of gateway credentials from media requests. The pytest suite uses local fixtures
and does not submit registrations, plays, likes, or follows.
The synthetic-record tests also check the track/account restrictions, metadata
validation, one-attempt behavior, report journaling, and secret redaction.
