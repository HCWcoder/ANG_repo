"""The browser's account selector is tested offline with synthetic row numbers."""

import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest


MODULE = Path(__file__).resolve().parents[1] / "anghami_session" / "ui" / "selection.js"
APP = MODULE.with_name("app.js")
INDEX = MODULE.with_name("index.html")
NODE = shutil.which("node")
DRIVER = r"""
const fs = require('node:fs');
let networkCalls = 0;
globalThis.fetch = () => { networkCalls++; throw new Error('Selection attempted network I/O'); };
const selection = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const rows = input.rows;
if (Array.isArray(rows)) Object.freeze(rows);
let randomCalls = 0;
const values = input.random || [0.4];
const random = () => values[randomCalls++ % values.length];
try {
  const selected = input.action === 'all'
    ? selection.selectAllRows(rows, input.max)
    : selection.selectRandomRows(rows, input.count, input.max, random);
  process.stdout.write(JSON.stringify({ok: true, selected, inputAfter: rows, networkCalls, randomCalls}));
} catch (_) {
  process.stdout.write(JSON.stringify({ok: false, inputAfter: rows, networkCalls, randomCalls}));
}
"""


def require_node():
    if NODE is None:
        pytest.skip("Node.js is required to exercise the browser's pure selection helper")
    assert MODULE.is_file(), "The browser selection module must be present"


def select(action, rows, *, count=None, max_accounts=100, random=None):
    require_node()
    payload = {"action": action, "rows": rows, "count": count, "max": max_accounts}
    if random is not None:
        payload["random"] = random
    completed = subprocess.run(
        [NODE, "-e", DRIVER, str(MODULE)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["networkCalls"] == 0, "Selecting accounts must not submit a test or make a request"
    assert result["inputAfter"] == rows, "Selection must not mutate the eligible account pool"
    return result


def test_select_all_returns_every_unique_eligible_row_without_truncation():
    pool = [1, 2, 7, 8, 9, 10, 11, 7]
    result = select("all", pool, max_accounts=7)
    assert result["ok"] is True
    assert set(result["selected"]) == set(pool)
    assert len(result["selected"]) == len(set(pool))


def test_select_all_supports_a_larger_ready_cohort_when_allowed():
    pool = list(range(1, 61))
    result = select("all", pool, max_accounts=60)
    assert result["ok"] is True and result["selected"] == pool


def test_select_all_rejects_over_limit_instead_of_silently_taking_first_rows():
    result = select("all", [1, 2, 7, 8, 9, 10], max_accounts=5)
    assert result["ok"] is False


@pytest.mark.parametrize("count", [1, 2, 3, 5, 7])
def test_random_selection_returns_exact_unique_eligible_count(count):
    pool = [1, 2, 7, 8, 9, 10, 11, 7]
    result = select("random", pool, count=count, max_accounts=7)
    assert result["ok"] is True
    assert len(result["selected"]) == count
    assert len(set(result["selected"])) == count
    assert set(result["selected"]).issubset(set(pool))


def test_random_selection_supports_exact_count_above_old_five_account_limit():
    pool = list(range(1, 61))
    result = select("random", pool, count=57, max_accounts=60)
    assert result["ok"] is True
    assert len(result["selected"]) == len(set(result["selected"])) == 57
    assert set(result["selected"]).issubset(set(pool))


def test_random_selection_replaces_pool_with_exact_new_subset():
    # A caller receives exactly N replacement rows, never a merged selection.
    result = select("random", [1, 2, 7, 8, 9], count=2, max_accounts=5)
    assert result["ok"] is True and len(result["selected"]) == 2
    assert set(result["selected"]) != {1, 2, 7, 8, 9}


def test_random_sampler_responds_to_random_source_and_accepts_both_range_edges():
    pool = [1, 2, 7, 8, 9]
    low = select("random", pool, count=2, max_accounts=5, random=[0])
    high = select("random", pool, count=2, max_accounts=5, random=[0.999999999])
    assert low["ok"] is True and high["ok"] is True
    assert len(set(low["selected"])) == len(set(high["selected"])) == 2
    assert set(low["selected"]) != set(high["selected"])


@pytest.mark.parametrize("count", [None, 0, -1, 1.5, True, "2", 6])
def test_random_selection_rejects_invalid_or_excessive_count(count):
    assert select("random", [1, 2, 7, 8, 9], count=count, max_accounts=5)["ok"] is False


def test_random_selection_does_not_count_duplicates_as_additional_eligible_accounts():
    assert select("random", [1, 1, 7], count=3, max_accounts=5)["ok"] is False


def test_random_selection_rejects_count_above_limit_even_when_enough_rows_exist():
    assert select("random", list(range(1, 11)), count=6, max_accounts=5)["ok"] is False


@pytest.mark.parametrize("action", ["all", "random"])
@pytest.mark.parametrize("pool", [[], None, "1,2", [0, 1], [-1, 1], [True, 1], [1.5, 2], ["1", 2]])
def test_selection_rejects_empty_or_invalid_eligible_pool(action, pool):
    assert select(action, pool, count=1, max_accounts=5)["ok"] is False


@pytest.mark.parametrize("action", ["all", "random"])
@pytest.mark.parametrize("maximum", [0, -1, 1.5, True, "5", None])
def test_selection_rejects_invalid_account_limit(action, maximum):
    assert select(action, [1], count=1, max_accounts=maximum)["ok"] is False


class Inputs(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.inputs = {}
        self.ids = set()
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "input" and "id" in attrs:
            self.inputs[attrs["id"]] = attrs


def test_browser_data_controls_are_independent_accessible_and_unchecked():
    source = INDEX.read_text(encoding="utf-8")
    document = Inputs(source)
    for scope, name in [("prepare", "preparation"), ("login", "session refresh")]:
        control = document.inputs[f"{scope}-reduce-browser-data"]
        assert control["type"] == "checkbox"
        assert "checked" not in control
        assert control["aria-label"] == f"Reduce browser data for {name}"
        assert control["aria-describedby"] in document.ids
    assert "checked" in document.inputs["headless"]
    assert source.count("<strong>Reduce browser data usage</strong>") == 2
    assert source.count("Skip unnecessary artwork, fonts and media, and reuse public app files. Required login and session checks stay enabled.") == 2


HANDLER_DRIVER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
function section(start, end) {
  const offset = source.indexOf(start);
  if (offset < 0) throw new Error('Missing handler');
  return source.slice(offset, source.indexOf(end, offset));
}
const elements = {
  'prepare-count': {value: '2'}, 'start-row': {value: ''},
  'prepare-method': {value: input.no_browser ? 'http' : 'browser'},
  browser: {value: 'chrome'}, headless: {checked: true},
  'prepare-reduce-browser-data': {checked: input.prepare},
  'login-reduce-browser-data': {checked: input.login},
  'login-rows': {value: '7, 8'},
};
let submitted = null;
let failure = null;
const context = vm.createContext({
  $: id => elements[id], limits: () => ({accounts: 5}),
  integer: value => Number(value), requireProxyReady: () => {},
  proxyMode: () => input.proxy,
  submitJob: async payload => {submitted = payload;},
  showError: (_id, message) => {failure = message;},
});
vm.runInContext(section('  function noBrowserPreparation', '  function updatePreparationMethod'), context);
vm.runInContext(section('  async function startPreparation', '  async function loadReport'), context);
(async () => {
  await vm.runInContext(input.action === 'login' ? 'startLogin()' : `startPreparation('${input.action}')`, context);
  process.stdout.write(JSON.stringify({submitted, failure}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});
"""


@pytest.mark.parametrize("action", ["prepare", "preview", "login"])
@pytest.mark.parametrize("proxy", [False, True])
@pytest.mark.parametrize("prepare, login", [(False, False), (True, False), (False, True), (True, True)])
def test_browser_data_payload_uses_own_toggle_independent_of_connection(action, proxy, prepare, login):
    require_node()
    payload = {"action": action, "proxy": proxy, "prepare": prepare, "login": login}
    completed = subprocess.run(
        [NODE, "-e", HANDLER_DRIVER, str(APP)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failure"] is None
    assert result["submitted"]["action"] == action
    assert result["submitted"]["reduce_browser_data"] is (login if action == "login" else prepare)
    assert result["submitted"]["proxy_egypt"] is proxy
    assert result["submitted"]["browser"] == "chrome"
    assert result["submitted"]["headless"] is True
    if action != "login":
        assert result["submitted"]["no_browser"] is False


def test_browser_data_controls_follow_busy_lock_and_do_not_change_test_payloads():
    source = APP.read_text(encoding="utf-8")
    busy = source[source.index("  function setBusy"):source.index("  function updateProxyMode")]
    assert "'prepare-reduce-browser-data'" in busy
    assert "'login-reduce-browser-data'" in busy
    assert "disabled = busy || requestPending" in busy
    tests = source[source.index("  async function startTest"):source.index("  async function startPreparation")]
    assert "reduce_browser_data" not in tests


def test_preparation_method_has_explicit_accessible_choice_and_browser_default():
    source = INDEX.read_text(encoding="utf-8")
    assert '<label for="prepare-method">Preparation method</label>' in source
    assert '<select id="prepare-method" aria-describedby="prepare-method-hint">' in source
    assert '<option value="browser">Browser login</option><option value="http">Reuse registered session · No browser</option>' in source
    assert 'id="prepare-method-hint"' in source


@pytest.mark.parametrize("action", ["prepare", "preview"])
@pytest.mark.parametrize("proxy", [False, True])
@pytest.mark.parametrize("reduce", [False, True])
def test_no_browser_preparation_payload_is_explicit_and_ignores_browser_data_control(action, proxy, reduce):
    require_node()
    completed = subprocess.run(
        [NODE, "-e", HANDLER_DRIVER, str(APP)],
        input=json.dumps({"action": action, "proxy": proxy, "prepare": reduce, "login": True, "no_browser": True}),
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failure"] is None
    payload = result["submitted"]
    assert payload["action"] == action and payload["no_browser"] is True
    assert payload["reduce_browser_data"] is False and payload["headless"] is False
    assert payload["proxy_egypt"] is proxy and payload["count"] == 2


def test_no_browser_preparation_choice_does_not_change_session_refresh_payload():
    require_node()
    completed = subprocess.run(
        [NODE, "-e", HANDLER_DRIVER, str(APP)],
        input=json.dumps({"action": "login", "proxy": True, "prepare": True, "login": True, "no_browser": True}),
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)["submitted"]
    assert "no_browser" not in payload
    assert payload["browser"] == "chrome" and payload["headless"] is True
    assert payload["reduce_browser_data"] is True and payload["proxy_egypt"] is True


METHOD_DRIVER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {
  'prepare-method': {value: 'http'}, 'prepare-method-hint': {textContent: ''},
  browser: {value: 'cloakbrowser'}, headless: {checked: true},
  'prepare-reduce-browser-data': {checked: true},
  'login-reduce-browser-data': {checked: true},
  'prepare-connection-mode': {value: 'egypt'}, 'login-connection-mode': {value: 'direct'},
};
const start = source.indexOf('  function noBrowserPreparation');
const end = source.indexOf('  function updateProxyMode', start);
if (start < 0 || end < 0) throw new Error('Missing preparation controls');
const context = vm.createContext({$: id => elements[id], busy: input.busy, requestPending: input.pending, state: input.state ? {} : null});
vm.runInContext(source.slice(start, end), context);
vm.runInContext('updatePreparationMethod()', context);
const disabled = JSON.parse(JSON.stringify(elements));
elements['prepare-method'].value = 'browser';
vm.runInContext('updatePreparationMethod()', context);
process.stdout.write(JSON.stringify({disabled, restored: elements}));
"""


@pytest.mark.parametrize("busy,pending,state", [(False, False, True), (True, False, True), (False, True, True), (False, False, False)])
def test_preparation_method_disables_browser_controls_and_restores_retained_choices(busy, pending, state):
    require_node()
    completed = subprocess.run(
        [NODE, "-e", METHOD_DRIVER, str(APP)], input=json.dumps({"busy": busy, "pending": pending, "state": state}),
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    locked = busy or pending or not state
    assert result["disabled"]["prepare-method"]["disabled"] is locked
    for control in ("browser", "headless", "prepare-reduce-browser-data"):
        assert result["disabled"][control]["disabled"] is True
        assert result["restored"][control]["disabled"] is locked
    assert result["restored"]["headless"]["checked"] is True
    assert result["restored"]["prepare-reduce-browser-data"]["checked"] is True
    assert result["disabled"]["login-reduce-browser-data"]["checked"] is True
    assert result["restored"]["prepare-connection-mode"]["value"] == "egypt"
    assert result["restored"]["login-connection-mode"]["value"] == "direct"
    hint = result["disabled"]["prepare-method-hint"]["textContent"]
    assert "without a password login" in hint and "no browser fallback" in hint
    assert "updatePreparationMethod();" in APP.read_text(encoding="utf-8").split("  function setBusy", 1)[1].split("  function noBrowserPreparation", 1)[0]
