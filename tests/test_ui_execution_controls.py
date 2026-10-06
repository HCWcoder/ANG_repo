"""Exercise real browser handlers offline; submitting is an in-memory stub."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "anghami_session/ui/app.js"
INDEX = APP.with_name("index.html")
NODE = shutil.which("node")
DRIVER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {
  'test-count': {value: '2'},
  'test-workers': {value: input.workers ?? '8'},
  'test-failure-limit': {value: input.failures ?? '20'},
};
let submitted = null, error = null;
const context = vm.createContext({
  $: id => elements[id], songDirty: !!input.dirty,
  state: {test_song_id: '1263607749'},
  limits: () => ({tests: 5, workers: 8, failures: 20}),
  requireProxyReady: () => {}, selectedTestRows: () => [1, 2, 3, 4, 5, 6, 7, 8],
  proxyMode: () => !!input.proxy,
  sessionMode: () => !!input.session,
  submitJob: async payload => {submitted = payload;},
  showError: (_id, message) => {error = message;},
});
function section(start, end) {
  const offset = source.indexOf(start);
  if (offset < 0) throw new Error('Missing real browser handler');
  const stop = source.indexOf(end, offset);
  if (stop < 0) throw new Error('Missing real browser handler boundary');
  return source.slice(offset, stop);
}
vm.runInContext(section('  function integer(', '  function validateRandomCount'), context);
vm.runInContext(section('  async function startTest(', '  async function startPreparation('), context);
(async () => {
  await vm.runInContext(`startTest('${input.action}')`, context);
  process.stdout.write(JSON.stringify({submitted, error}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});
"""

def handler(action, **options):
    if NODE is None:
        pytest.skip("Node.js is needed for the real browser handler")
    result = subprocess.run([NODE, "-e", DRIVER, str(APP)], input=json.dumps({"action": action, **options}),
                            text=True, encoding="utf-8", capture_output=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)

@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("proxy", [False, True])
def test_play_like_submit_eight_workers_and_twenty_failure_budget_with_existing_scope(action, proxy):
    result = handler(action, proxy=proxy)
    assert result["error"] is None
    assert result["submitted"] == {
        "action": action, "rows": list(range(1, 9)), "count": 2, "proxy_egypt": proxy,
        "workers": 8, "max_consecutive_failures": 20, "song_id": "1263607749",
    }

@pytest.mark.parametrize("action", ["play", "like", "check", "song"])
def test_saved_sticky_session_is_explicit_and_never_sends_the_pasted_address_in_job(action):
    result = handler(action, proxy=True, session=True)
    assert result["error"] is None
    assert result["submitted"]["proxy_test_session"] is True
    assert result["submitted"]["proxy_egypt"] is True
    assert set(result["submitted"]).issubset({"action", "rows", "count", "proxy_egypt", "proxy_test_session", "workers", "max_consecutive_failures", "song_id"})

@pytest.mark.parametrize("field,value", [
    ("workers", "0"), ("workers", "9007199254740992"), ("workers", "1.5"), ("workers", "1e1"), ("workers", ""),
    ("failures", "0"), ("failures", "-1"), ("failures", "9007199254740992"),
    ("failures", "1.5"), ("failures", "NaN"), ("failures", "1e3"), ("failures", ""),
])
@pytest.mark.parametrize("action", ["play", "like"])
def test_invalid_execution_controls_do_not_submit_any_request(action, field, value):
    result = handler(action, **{field: value})
    assert result["submitted"] is None
    assert "whole number" in result["error"]


@pytest.mark.parametrize("action", ["play", "like"])
@pytest.mark.parametrize("failures", [21, 1000, 9007199254740991])
def test_failure_threshold_accepts_any_positive_safe_integer_without_fixed_twenty_cap(action, failures):
    result = handler(action, failures=str(failures))
    assert result["error"] is None
    assert result["submitted"]["max_consecutive_failures"] == failures
    assert result["submitted"]["count"] == 2

@pytest.mark.parametrize("action", ["check", "song"])
def test_read_only_checks_ignore_execution_controls_and_keep_their_existing_payload(action):
    result = handler(action, workers="invalid", failures="invalid")
    assert result["error"] is None
    assert "workers" not in result["submitted"]
    assert "max_consecutive_failures" not in result["submitted"]
    assert result["submitted"]["count"] == 1

@pytest.mark.parametrize("action", ["play", "like"])
def test_changed_declared_song_still_blocks_submission(action):
    result = handler(action, dirty=True)
    assert result["submitted"] is None
    assert "Save the changed song ID" in result["error"]

class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes = {}
        self.labels = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            attrs["tag"] = tag
            self.attributes[attrs["id"]] = attrs
        if tag == "label" and "for" in attrs:
            self.labels.add(attrs["for"])

def test_controls_have_labels_bounds_defaults_and_descriptions():
    document = Document()
    document.feed(INDEX.read_text(encoding="utf-8"))
    for name, default in [("test-workers", "8"), ("test-failure-limit", "20")]:
        attrs = document.attributes[name]
        assert name in document.labels
        assert attrs["type"] == "number" and attrs["min"] == "1"
        assert "max" not in attrs and attrs["value"] == default
        assert attrs["aria-describedby"] in document.attributes
    assert document.attributes["job-test-metrics"]["role"] == "status"
    assert document.attributes["test-session-route"]["tag"] == "textarea"
    assert document.attributes["test-session-route"]["autocomplete"] == "off"
    assert document.attributes["test-session-route"]["maxlength"] == "1048576"
    assert "masked" in document.attributes["test-session-route"]["class"]
    assert document.attributes["show-test-session-links"]["type"] == "checkbox"
    assert "checked" not in document.attributes["show-test-session-links"]
    assert "test-session-route" in document.labels
    assert "job-result-filter" in document.labels


def test_failure_threshold_hint_explains_reset_pending_and_immediate_stop_exceptions():
    source = INDEX.read_text(encoding="utf-8")
    assert "A successful test resets the count" in source
    assert "Pending connections or verification checks do not advance it" in source
    assert "An unconfirmed session refresh before any play or like request holds that account for a read-only check; other accounts continue" in source
    assert "An unknown play or like result, an account or song mismatch, or a failure to save a report can stop the run immediately" in source


POOL_SAVE_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let callback, submitted = null, error = null, resets = 0;
const elements = {
  'save-test-session': {addEventListener: (_name, listener) => {callback = listener;}},
  'test-session-route': {value: input.route},
  'show-test-session-links': {checked: true},
  'test-session-saved': {hidden: true, textContent: ''},
};
const context = vm.createContext({
  $: id => elements[id], busy: false, requestPending: false,
  state: {test_proxy: {configured: true}}, testSessionNeedsSave: true,
  currentJob: null, uncertainSubmission: false,
  TextEncoder, formatNumber: value => String(value), setBusy: () => {},
  showError: (_id, message) => {error = message;},
  updateRouteMask: () => {resets++;}, isActive: () => false,
  refreshState: async () => {},
  api: async (path, options) => {
    submitted = {path, payload: JSON.parse(options.body)};
    if (input.fail) throw new Error('Synthetic save failure');
    return {configured: true, pool_size: input.pool_size || 10};
  },
});
function section(start, end) {
  const begin = source.indexOf(start), stop = source.indexOf(end, begin);
  if (begin < 0 || stop < 0) throw new Error('Missing real pool handler');
  return source.slice(begin, stop);
}
vm.runInContext(section('  function pastedRouteCount(', '  function updateRouteMask('), context);
vm.runInContext(section("  $('save-test-session').addEventListener(", "  $('discard-test-session').addEventListener("), context);
(async () => {
  await callback();
  process.stdout.write(JSON.stringify({submitted, error, resets, value: elements['test-session-route'].value,
    showing: elements['show-test-session-links'].checked, needsSave: vm.runInContext('testSessionNeedsSave', context),
    savedMessage: elements['test-session-saved'].textContent}));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});
"""


def pool_save(route, **options):
    if NODE is None:
        pytest.skip("Node.js is needed for the real pool handler")
    result = subprocess.run([NODE, "-e", POOL_SAVE_DRIVER, str(APP)],
                            input=json.dumps({"route": route, **options}), text=True, encoding="utf-8",
                            capture_output=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
def test_multiline_pool_save_preserves_all_ten_routes_and_clears_the_paste(separator):
    lines = [f"fixture:synthetic-key_country-EG_session-route{number}:proxy.packetstream.io:31112"
             for number in range(10)]
    value = "  " + separator.join(lines) + separator
    result = pool_save(value)
    assert result["submitted"] == {"path": "/api/test-proxy", "payload": {"route": value.strip()}}
    assert result["value"] == "" and result["showing"] is False and result["needsSave"] is False
    assert "10 unique sticky routes saved" in result["savedMessage"]


@pytest.mark.parametrize("route", ["", " \n\r\n", "x\n" * 10001, "x" * (1024 * 1024 + 1), "\u00e9" * (512 * 1024 + 1)],
                         ids=["empty", "blank", "too_many_lines", "ascii_too_large", "utf8_too_large"])
def test_empty_or_overlimit_pool_never_calls_the_save_api(route):
    result = pool_save(route)
    assert result["submitted"] is None and result["needsSave"] is True
    assert "1–10,000" in result["error"]


def test_failed_multiline_save_clears_secrets_and_requires_explicit_resave_or_revert():
    result = pool_save("fixture:synthetic-key_country-EG_session-route1:proxy.packetstream.io:31112\n"
                       "fixture:synthetic-key_country-EG_session-route2:proxy.packetstream.io:31112", fail=True)
    assert result["value"] == "" and result["showing"] is False
    assert result["needsSave"] is True and "Use saved routes" in result["error"]
