'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="app-token"]').content;
  const titles = {
    workbench: ['Ready when you are.', 'Choose your accounts, pick a connection, and run a test.', 'Workbench'],
    accounts: ['Your accounts. Ready to test.', 'Prepare a test group and manage its saved sessions.', 'Accounts'],
    reports: ['Every run, in view.', 'Review the results saved in your local workspace.', 'Reports'],
    proxy: ['Choose your connection.', 'Use PacketStream Egypt when your test needs a different route.', 'Proxy settings'],
  };
  const actionNames = {play: 'Play test', like: 'Like test', check: 'Session check', song: 'Song access check', prepare: 'Account preparation', preview: 'Account selection preview', login: 'Session refresh', 'review-sessions': 'Saved session review', 'proxy-check': 'Egypt connection check'};
  let state = null;
  let currentJob = null;
  let busy = false;
  let uncertainSubmission = false;
  let requestPending = false;
  let pollTimer = null;
  let refreshing = false;
  let selectedReport = null;
  let firstSelection = true;
  let songDirty = false;
  let songSavedMessage = '';
  let testSessionNeedsSave = false;
  let prepareStickyNeedsSave = false;
  const routeMaskSupported = typeof CSS !== 'undefined' && CSS.supports('-webkit-text-security', 'disc');
  let previousRandomMaximum = null;
  const selectedRows = new Set();
  const refreshRows = new Set();
  const reviewRows = new Set();
  const sessionReviewRows = new Set();
  let likeHistoryCache = null;
  let stopRequestPending = false;
  let preparationAvailabilityLookup = null;

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined && text !== null) result.textContent = String(text);
    return result;
  }

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function showError(id, message, kind = '') {
    $(id).textContent = message || '';
    $(id).hidden = !message;
    if (id === 'selection-error') $(id).dataset.kind = message ? kind : '';
  }

  function integer(value, min, max, label) {
    const raw = String(value).trim();
    const number = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(number) || number < min || number > max) {
      throw new Error(`${label} must be a whole number between ${min} and ${max}.`);
    }
    return number;
  }

  function validateRandomCount(maximum) {
    if (maximum < 1) throw new Error('No ready accounts are available. Prepare verified accounts before selecting a random group.');
    return integer($('random-count').value, 1, maximum, 'Random accounts');
  }

  function limits() {
    const accounts = Math.max(1, Number(state?.limits?.accounts) || 5);
    const testAccounts = Number(state?.limits?.test_accounts);
    return {
      accounts,
      testAccounts: Number.isSafeInteger(testAccounts) && testAccounts >= 0 ? testAccounts : accounts,
      tests: Math.max(1, Number(state?.limits?.tests_per_account) || 5),
    };
  }

  const connectionScopes = {
    tests: {selector: 'connection-mode', hint: 'proxy-mode-hint', actions: ['run-play', 'run-like', 'check-sessions', 'check-song']},
    prepare: {selector: 'prepare-connection-mode', hint: 'prepare-proxy-mode-hint', actions: ['prepare-accounts']},
    login: {selector: 'login-connection-mode', hint: 'login-proxy-mode-hint', actions: ['refresh-sessions']},
    sessionReview: {selector: 'session-review-connection-mode', hint: 'session-review-proxy-mode-hint', actions: ['check-review-sessions']},
  };
  function stickyMode(scope) { return scope === 'prepare' && $(connectionScopes[scope].selector).value === 'sticky'; }
  function sessionMode(scope) { return scope === 'tests' && $(connectionScopes[scope].selector).value === 'session'; }
  function proxyMode(scope) { return ['egypt', 'sticky', 'session'].includes($(connectionScopes[scope].selector).value); }
  function pastedRouteCount() { return $('test-session-route').value.split(/\r\n|\r|\n/).filter(line => line.trim()).length; }
  function updateRouteMask() {
    const show = $('show-test-session-links').checked;
    $('test-session-route').classList.toggle('masked', routeMaskSupported && !show);
    $('test-session-route').hidden = !routeMaskSupported && !show;
    $('test-session-mask-hint').hidden = routeMaskSupported || show;
    $('test-session-line-count').textContent = `${formatNumber(pastedRouteCount())} line${pastedRouteCount() === 1 ? '' : 's'} pasted`;
  }
  function preparationStickyDraft() { return prepareStickyNeedsSave || !!$('prepare-sticky-routes')?.value.trim(); }
  function preparationStickyLineCount() { return ($('prepare-sticky-routes')?.value || '').split(/\r\n|\r|\n/).filter(line => line.trim()).length; }
  function updatePreparationStickyMask() {
    const input = $('prepare-sticky-routes');
    if (!input) return;
    const show = $('prepare-sticky-show-links').checked;
    input.classList.toggle('masked', routeMaskSupported && !show);
    input.hidden = !routeMaskSupported && !show;
    $('prepare-sticky-mask-hint').hidden = routeMaskSupported || show;
    const count = preparationStickyLineCount();
    $('prepare-sticky-line-count').textContent = `${formatNumber(count)} line${count === 1 ? '' : 's'} pasted`;
  }
  function isActive(job) { return !!job && ['queued', 'running'].includes(job.status); }
  function heldSessionRows() {
    const review = state?.session_review;
    const rows = [...(Array.isArray(review?.held_rows) ? review.held_rows : []), ...(Array.isArray(review?.session_review_rows) ? review.session_review_rows : []), ...(Array.isArray(review?.accounts) ? review.accounts.map(account => account?.source_row) : [])];
    return new Set(rows.filter(row => Number.isSafeInteger(row) && row >= 1 && row <= 2147483647));
  }
  function readyRows() { const held = heldSessionRows(); return [...new Set((state?.cohort?.ready_rows || []).map(Number).filter(row => Number.isSafeInteger(row) && row > 0 && !held.has(row)))]; }
  function filteredReadyRows() {
    const country = $('test-account-country')?.value || '';
    if (!country) return readyRows();
    if (!['EG', 'LB'].includes(country)) return [];
    const countries = new Map((state?.cohort?.accounts || []).map(account => [Number(account.source_row), account.registered_country]));
    return readyRows().filter(row => countries.get(row) === country);
  }
  function currentLikeHistory() {
    const source = state?.like_history, song = String(state?.test_song_id || '');
    if (likeHistoryCache && likeHistoryCache.source === source && likeHistoryCache.song === song) return likeHistoryCache;
    const known = !!source && typeof source === 'object' && !Array.isArray(source) && typeof source.song_id === 'string' && validSongId(source.song_id) && source.song_id === song;
    const rows = name => new Set(known && Array.isArray(source[name]) ? source[name].filter(row => Number.isSafeInteger(row) && row >= 1 && row <= 2147483647) : []);
    likeHistoryCache = {source, song, known, confirmed: rows('confirmed_rows'), verification: rows('verification_pending_rows'), unknown: rows('unknown_rows'), eligible: known && Array.isArray(source.eligible_rows) ? rows('eligible_rows') : null};
    return likeHistoryCache;
  }
  function likeHistoryStatus(row, history = currentLikeHistory()) {
    if (!history.known) return 'eligible';
    if (history.unknown.has(row)) return 'unknown';
    if (history.verification.has(row)) return 'verification_pending';
    if (history.confirmed.has(row)) return 'confirmed';
    return history.eligible && !history.eligible.has(row) ? 'held' : 'eligible';
  }
  function notYetLikedRows() { const history = currentLikeHistory(); return filteredReadyRows().filter(row => likeHistoryStatus(row, history) === 'eligible'); }
  function validSongId(value) {
    return /^[1-9][0-9]{0,18}$/.test(value) && (value.length < 19 || value <= '9223372036854775807');
  }

  function updateSongState() {
    const value = $('song-id').value.trim();
    const configured = String(state?.test_song_id || '');
    songDirty = value !== configured;
    const valid = validSongId(value);
    $('save-song').disabled = busy || requestPending || !state || !songDirty || !valid;
    $('song-id').disabled = busy || requestPending || !state;
    $('song-id').setAttribute('aria-invalid', String(songDirty && !valid));
    $('song-hint').textContent = configured ? `Configured: ${configured}${songDirty ? (valid ? ' · Unsaved changes' : ' · Unsaved changes (invalid ID)') : ''}` : 'Loading the configured test song…';
    $('song-hint').classList.toggle('unsaved', songDirty);
    $('song-status').textContent = songSavedMessage;
    $('song-status').hidden = songDirty || !songSavedMessage;
  }

  async function api(path, options = {}) {
    const headers = {'X-App-Token': token, ...options.headers};
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options, headers});
    } catch (_) {
      const error = new Error('Cannot reach the local console. Check that its terminal is still running, then refresh the workspace.');
      error.network = true;
      throw error;
    }
    let result;
    try { result = await response.json(); }
    catch (_) { throw new Error('The local console returned an unreadable response. Refresh the workspace to check the run status.'); }
    if (!response.ok) {
      const message = typeof result.error === 'string' ? result.error : result.error?.message || result.message || `The console could not complete this request (HTTP ${response.status}).`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return result;
  }

  function switchPanel(name) {
    if (!titles[name]) return;
    document.querySelectorAll('.panel').forEach(panel => { panel.hidden = panel.id !== 'panel-' + name; });
    document.querySelectorAll('.nav-button').forEach(button => {
      const active = button.dataset.panel === name;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    $('page-title').textContent = titles[name][0];
    $('page-description').textContent = titles[name][1];
    $('breadcrumb-current').textContent = titles[name][2];
    history.replaceState(null, '', '#' + name);
  }

  function setBusy(value) {
    busy = value;
    document.querySelectorAll('[data-mutation]').forEach(button => { button.disabled = busy || !state || requestPending; });
    Object.values(connectionScopes).forEach(scope => { $(scope.selector).disabled = busy || requestPending || !state; });
    ['test-count', 'test-workers', 'test-failure-limit', 'test-session-route', 'show-test-session-links', 'count-minus', 'count-plus', 'prepare-count', 'start-row', 'browser', 'headless', 'prepare-reduce-browser-data', 'login-reduce-browser-data', 'login-rows', 'proxy-username', 'proxy-key'].forEach(id => { $(id).disabled = busy || requestPending || !state; });
    updatePreparationMethod();
    updatePreparationCountry();
    ['prepare-sticky-routes', 'prepare-sticky-show-links'].forEach(id => { if ($(id)) $(id).disabled = busy || requestPending || !state; });
    $('busy-banner').hidden = !busy;
    updateSongState();
    updateSelections();
    updateProxyMode();
  }

  function noBrowserPreparation() { return $('prepare-method').value === 'http'; }

  function updatePreparationMethod() {
    const noBrowser = noBrowserPreparation();
    const locked = busy || requestPending || !state;
    $('prepare-method').disabled = locked;
    ['browser', 'headless', 'prepare-reduce-browser-data'].forEach(id => { $(id).disabled = locked || noBrowser; });
    const workers = $('prepare-workers');
    if (workers) {
      const countrySelected = ['EG', 'LB'].includes($('prepare-country')?.value);
      workers.disabled = locked || !noBrowser || !countrySelected;
      if (!noBrowser || !countrySelected) workers.value = '1';
      workers.removeAttribute?.('max');
      $('prepare-workers-hint').textContent = !noBrowser
        ? 'Browser login prepares one account at a time. Choose Reuse registered session for more workers.'
        : !countrySelected ? 'Choose Egypt or Lebanon to prepare multiple accounts at once.'
        : `Choose any positive whole number. Active workers are limited by available accounts. Each account keeps its own session and route.${Number(workers.value) > 20 ? ' After 20 consecutive failures, no new accounts start; accounts already active finish.' : ''}`;
    }
    $('prepare-method-hint').textContent = noBrowser
      ? "Reuse each account's own registered session and validate its identity and authenticated reads without a password login. Rejected sessions enter account review; required verification or uncertain renewal needs review. There is no browser fallback."
      : 'Capture a normal login when an account needs a saved session, then validate it.';
  }

  function preparationStartRow() {
    const raw = $('start-row').value.trim();
    if (!raw) return 1;
    const value = Number(raw);
    return /^\d+$/.test(raw) && Number.isSafeInteger(value) && value >= 1 ? value : null;
  }

  function validPreparationAvailability(value, startRow) {
    return !!value && value.available === true && value.start_row === startRow
      && ['EG', 'LB', 'all'].every(country => Number.isSafeInteger(value.counts?.[country]) && value.counts[country] >= 0);
  }

  function preparationAvailability() {
    const country = $('prepare-country')?.value || '';
    if (country && !['EG', 'LB'].includes(country)) return {known: false};
    const baseline = state?.preparation_availability;
    if (!validPreparationAvailability(baseline, 1)) return {known: false};
    const startRow = country ? 1 : preparationStartRow();
    if (startRow === null) return {known: false, invalidRow: true};
    if (startRow === 1) return {known: true, remaining: baseline.counts[country || 'all']};
    const lookup = preparationAvailabilityLookup;
    if (!lookup || lookup.state !== state || lookup.startRow !== startRow) return {known: false, loading: true};
    if (!validPreparationAvailability(lookup.value, startRow)) return {known: false, loading: lookup.loading};
    return {known: true, remaining: lookup.value.counts.all};
  }

  async function refreshPreparationAvailability(startRow, boundState) {
    const lookup = {state: boundState, startRow, loading: true, value: null};
    preparationAvailabilityLookup = lookup;
    try {
      const value = await api(`/api/preparation-availability?start_row=${startRow}`);
      if (preparationAvailabilityLookup !== lookup || state !== boundState || $('prepare-country').value || preparationStartRow() !== startRow) return;
      lookup.value = value;
    } catch (_) {
      // Keep unavailable counts separate from a confirmed empty pool.
    } finally {
      lookup.loading = false;
      if (preparationAvailabilityLookup === lookup && state === boundState && !$('prepare-country').value && preparationStartRow() === startRow) updatePreparationCountry();
    }
  }

  function updatePreparationCountry(clamp = true) {
    const control = $('prepare-country');
    if (!control) return;
    const country = control.value;
    const locked = busy || requestPending || !state;
    control.disabled = locked;
    $('start-row').disabled = locked || !!country;
    $('prepare-country-hint').textContent = country
      ? `Each preview or preparation randomly selects eligible ${country} accounts from registered.txt. Prepared accounts and duplicate identities are skipped. Your connection choice is independent.`
      : 'Selects the next eligible registered accounts in row order, starting at the optional row below. Your connection choice is independent.';
    $('start-row-hint').textContent = country
      ? 'Not used when choosing random accounts by country.'
      : 'Skip prepared accounts and duplicates.';
    const startRow = country ? 1 : preparationStartRow();
    if (startRow !== null && startRow > 1 && validPreparationAvailability(state?.preparation_availability, 1)
        && (!preparationAvailabilityLookup || preparationAvailabilityLookup.state !== state || preparationAvailabilityLookup.startRow !== startRow)) {
      void refreshPreparationAvailability(startRow, state);
    }
    const availability = preparationAvailability();
    const count = $('prepare-count');
    count.disabled = locked || !availability.known || availability.remaining < 1;
    if (availability.known && availability.remaining > 0) {
      count.max = String(availability.remaining);
      const entered = Number(count.value);
      if (clamp && /^\d+$/.test(count.value.trim()) && Number.isSafeInteger(entered) && entered > availability.remaining) count.value = String(availability.remaining);
    } else count.removeAttribute?.('max');
    const scope = country ? ` ${country}` : '';
    $('prepare-count-hint').textContent = availability.known
      ? availability.remaining > 0
        ? `${formatNumber(availability.remaining)} unique${scope} accounts left to prepare${!country && startRow > 1 ? ` from row ${formatNumber(startRow)}` : ''}. Minimum 1; maximum ${formatNumber(availability.remaining)}.`
        : `No eligible${scope} accounts are left to prepare${!country && startRow > 1 ? ` from row ${formatNumber(startRow)}` : ''}.`
      : availability.invalidRow ? 'Enter a valid starting row to check how many accounts are left.'
        : availability.loading ? 'Checking how many accounts are left to prepare…'
          : 'Remaining account count is unavailable. Refresh the workspace before preparing accounts.';
    $('preview-accounts').disabled = locked || !availability.known || availability.remaining < 1;
    const routeReady = !proxyMode('prepare') || (stickyMode('prepare')
      ? !!state?.sticky_pool?.configured && !preparationStickyDraft()
      : !!state?.proxy?.configured);
    $('prepare-accounts').disabled = locked || !availability.known || availability.remaining < 1 || !routeReady;
  }

  function updateProxyMode() {
    const configured = !!state?.proxy?.configured;
    Object.entries(connectionScopes).forEach(([name, scope]) => {
      const egypt = proxyMode(name);
      const sticky = stickyMode(name);
      const session = sessionMode(name);
      const unsaved = sticky ? preparationStickyDraft() : session && (testSessionNeedsSave || !!$('test-session-route').value.trim());
      const routeConfigured = sticky ? !!state?.sticky_pool?.configured && !unsaved : session ? !!state?.test_proxy?.configured && !unsaved : configured;
      const poolSize = Math.max(1, Number(state?.test_proxy?.pool_size) || 1);
      $(scope.hint).textContent = sticky ? (unsaved ? 'Save the pasted links before preparing accounts, or choose Use saved routes to keep the previous pool.' : routeConfigured ? `Cycles ${formatNumber(state.sticky_pool.pool_size)} saved PacketStream sticky routes, one per account. Each route and session is verified during preparation.` : 'Paste and save PacketStream Egypt or US sticky links below, one per line.') : session ? (unsaved ? ($('test-session-route').value.trim() ? 'Save the pasted links before running a test.' : 'Paste the links again and save them before testing.') : routeConfigured ? `Cycles ${formatNumber(poolSize)} saved sticky route${poolSize === 1 ? '' : 's'} across accounts. Safe connection retries can replace a route before a write; later tests use that replacement. Rate limits wait on the same route.` : 'Paste and save PacketStream Egypt or US sticky links below, one per line.') : egypt ? (configured ? 'Uses the saved PacketStream Egypt proxy.' : 'Save credentials in Proxy settings before using Egypt.') : 'Uses your current internet connection without a proxy.';
      $(scope.hint).classList.toggle('route-unavailable', egypt && !routeConfigured);
      if (name === 'tests') {
        $('run-route').textContent = session ? 'PACKETSTREAM · STICKY POOL' : egypt ? 'EGYPT' : 'DIRECT';
        $('test-session-settings').hidden = !session;
        $('test-session-badge').textContent = state?.test_proxy?.configured ? `${formatNumber(poolSize)} route${poolSize === 1 ? '' : 's'} saved` : 'No saved routes';
        $('test-session-badge').className = 'badge ' + (state?.test_proxy?.configured ? 'success' : 'neutral');
        $('save-test-session').disabled = busy || requestPending || !state || !$('test-session-route').value.trim();
        $('discard-test-session').hidden = !state?.test_proxy?.configured || !unsaved;
        $('discard-test-session').disabled = busy || requestPending || !state;
        updateRouteMask();
      }
      if (name === 'prepare' && $('prepare-sticky-settings')) {
        $('prepare-sticky-settings').hidden = !sticky;
        const savedCount = Math.max(1, Number(state?.sticky_pool?.pool_size) || 1);
        $('prepare-sticky-badge').textContent = state?.sticky_pool?.configured ? `${formatNumber(savedCount)} route${savedCount === 1 ? '' : 's'} saved` : 'No saved routes';
        $('prepare-sticky-badge').className = 'badge ' + (state?.sticky_pool?.configured ? 'success' : 'neutral');
        $('prepare-sticky-save').disabled = busy || requestPending || !state || !$('prepare-sticky-routes').value.trim();
        $('prepare-sticky-discard').hidden = !state?.sticky_pool?.configured || !unsaved;
        $('prepare-sticky-discard').disabled = busy || requestPending || !state;
        $('prepare-sticky-remove').hidden = !state?.sticky_pool?.configured;
        $('prepare-sticky-remove').disabled = busy || requestPending || !state;
        updatePreparationStickyMask();
      }
      if (name !== 'tests') scope.actions.forEach(id => {
        const availability = name === 'prepare' ? preparationAvailability() : null;
        $(id).disabled = busy || requestPending || !state || (egypt && !routeConfigured) || (availability && (!availability.known || availability.remaining < 1));
      });
      if (name === 'tests' && egypt && !routeConfigured) scope.actions.forEach(id => { $(id).disabled = true; });
    });
    if (!busy && !requestPending && state) {
      $('check-proxy').disabled = !configured;
    }
    updateReviewSelection();
    updateSessionReviewSelection();
  }

  function sessionReviewAccounts() {
    const accounts = Array.isArray(state?.session_review?.accounts) ? state.session_review.accounts : [];
    const eligible = Array.isArray(state?.session_review?.session_review_rows) ? new Set(state.session_review.session_review_rows) : null;
    const seen = new Set();
    return accounts.filter(account => {
      if (!account || !Number.isSafeInteger(account.source_row) || account.source_row < 1 || account.source_row > 2147483647 || seen.has(account.source_row) || eligible && !eligible.has(account.source_row)) return false;
      seen.add(account.source_row); return true;
    });
  }

  function updateSessionReviewSelection() {
    const available = new Set(sessionReviewAccounts().map(account => account.source_row));
    for (const row of sessionReviewRows) if (!available.has(row)) sessionReviewRows.delete(row);
    const locked = busy || requestPending || !state;
    const max = limits().accounts;
    $('session-review-selection-count').textContent = `${sessionReviewRows.size} selected · up to ${max} per check`;
    $('session-review-select-all').textContent = `Select all (up to ${max})`;
    $('session-review-select-all').disabled = locked || !available.size;
    $('session-review-clear').disabled = locked || !sessionReviewRows.size;
    const routeReady = !proxyMode('sessionReview') || !!state?.proxy?.configured;
    $('check-review-sessions').disabled = locked || !sessionReviewRows.size || sessionReviewRows.size > max || !routeReady;
    document.querySelectorAll('#session-review-accounts-table input').forEach(input => {
      input.checked = sessionReviewRows.has(Number(input.value));
      input.disabled = locked || (!input.checked && sessionReviewRows.size >= max);
    });
  }

  function renderSessionReview() {
    const accounts = sessionReviewAccounts();
    const codes = new Set(['request_transport_failed', 'request_http_failed', 'request_rate_limited', 'request_proxy_unverified', 'session_authentication_rejected', 'session_response_invalid', 'session_control_failed', 'session_identity_mismatch', 'session_renewal_unknown', 'session_review_pending']);
    const stages = new Set(['relations', 'playlists', 'negative_control', 'identity', 'metadata', 'likes_read', 'song_metadata', 'preflight', 'country_check', 'session_recovery_preflight', 'session_recovery_renewal', 'session_recovery_validation']);
    $('session-review-accounts-table').replaceChildren();
    accounts.forEach(account => {
      const row = account.source_row, tr = node('tr'), select = node('td'), checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.value = String(row); checkbox.checked = sessionReviewRows.has(row);
      checkbox.setAttribute('aria-label', `Select held account row ${row} for a read-only saved-session check`);
      checkbox.addEventListener('change', () => {
        if (busy || requestPending || !state) { checkbox.checked = sessionReviewRows.has(row); return; }
        if (checkbox.checked && !sessionReviewRows.has(row) && sessionReviewRows.size >= limits().accounts) {
          checkbox.checked = false; showError('session-review-error', `Choose up to ${limits().accounts} held accounts per check.`);
        } else { if (checkbox.checked) sessionReviewRows.add(row); else sessionReviewRows.delete(row); showError('session-review-error', ''); }
        updateSessionReviewSelection();
      });
      select.appendChild(checkbox);
      const detail = node('td'); detail.appendChild(node('span', 'badge warning', 'Session review pending'));
      const code = codes.has(account.failure_code) ? humanLabel(account.failure_code) : 'Saved session needs checking';
      const stage = stages.has(account.failed_stage) ? humanLabel(account.failed_stage) : '';
      const diagnostic = account.session_failure || {};
      const facts = [code, stage, Number.isSafeInteger(diagnostic.http_status) && diagnostic.http_status >= 100 && diagnostic.http_status <= 599 ? `HTTP ${diagnostic.http_status}` : '', Number.isSafeInteger(diagnostic.curl_code) && diagnostic.curl_code >= 1 && diagnostic.curl_code <= 999 ? `Curl ${diagnostic.curl_code}` : ''].filter(Boolean);
      detail.appendChild(node('small', 'review-stage', facts.join(' · ')));
      tr.append(select, node('td', '', `Row ${row}`), detail, node('td', '', formatDate(account.held_at || account.reviewed_at || account.failed_at)));
      $('session-review-accounts-table').appendChild(tr);
    });
    const total = Number.isSafeInteger(state?.session_review?.total) && state.session_review.total >= accounts.length ? state.session_review.total : accounts.length;
    $('session-review-account-count').textContent = `${formatNumber(total)} account${total === 1 ? '' : 's'}`;
    $('session-review-empty').hidden = accounts.length > 0;
    $('session-review-list-scope').hidden = total <= accounts.length;
    $('session-review-list-scope').textContent = `Showing ${formatNumber(accounts.length)} of ${formatNumber(total)} held accounts.`;
    updateSessionReviewSelection();
  }

  function failureReviewAccounts() {
    const accounts = Array.isArray(state?.failure_review?.accounts) ? state.failure_review.accounts : [];
    const held = heldSessionRows();
    const seen = new Set();
    return accounts.filter(account => {
      if (!account || !Number.isSafeInteger(account.source_row) || account.source_row < 1 || seen.has(account.source_row) || held.has(account.source_row)
        || !['session_authentication_rejected', 'session_identity_mismatch'].includes(account.failure_code)) return false;
      seen.add(account.source_row); return true;
    });
  }

  function updateReviewSelection() {
    const available = new Set(failureReviewAccounts().map(account => account.source_row));
    for (const row of reviewRows) if (!available.has(row)) reviewRows.delete(row);
    const locked = busy || requestPending || !state;
    const max = limits().accounts;
    $('review-selection-count').textContent = `${reviewRows.size} selected · up to ${max} per preparation`;
    $('review-select-all').textContent = `Select all (up to ${max})`;
    $('review-select-all').disabled = locked || !available.size;
    $('review-clear').disabled = locked || !reviewRows.size;
    const routeReady = stickyMode('prepare') ? !!state?.sticky_pool?.configured && !preparationStickyDraft() : !proxyMode('prepare') || !!state?.proxy?.configured;
    $('prepare-review-accounts').disabled = locked || !reviewRows.size || reviewRows.size > max || !routeReady;
    document.querySelectorAll('#review-accounts-table input').forEach(input => {
      input.checked = reviewRows.has(Number(input.value));
      input.disabled = locked || (!input.checked && reviewRows.size >= max);
    });
  }

  function renderFailureReview() {
    const accounts = failureReviewAccounts();
    $('review-accounts-table').replaceChildren();
    accounts.forEach(account => {
      const row = account.source_row, tr = node('tr'), select = node('td'), checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.value = String(row); checkbox.checked = reviewRows.has(row);
      checkbox.setAttribute('aria-label', `Select failed account row ${row} for preparation`);
      checkbox.addEventListener('change', () => {
        if (busy || requestPending) { checkbox.checked = reviewRows.has(row); return; }
        if (checkbox.checked && reviewRows.size >= limits().accounts) {
          checkbox.checked = false;
          showError('review-error', `Choose up to ${limits().accounts} failed accounts per preparation.`);
        } else {
          if (checkbox.checked) reviewRows.add(row); else reviewRows.delete(row);
          showError('review-error', '');
        }
        updateReviewSelection();
      });
      select.appendChild(checkbox);
      const reason = account.failure_code === 'session_identity_mismatch' ? 'Identity check failed' : 'Saved session rejected';
      const detail = node('td'); detail.append(node('span', 'badge error', reason), node('small', 'review-stage', humanLabel(account.failed_stage)));
      tr.append(select, node('td', '', `Row ${row}`), detail, node('td', '', formatDate(account.failed_at)));
      $('review-accounts-table').appendChild(tr);
    });
    const total = Math.max(accounts.length, Number(state?.failure_review?.total) || 0);
    $('review-account-count').textContent = `${formatNumber(total)} account${total === 1 ? '' : 's'}`;
    $('review-empty').hidden = accounts.length > 0;
    $('review-list-scope').hidden = total <= accounts.length;
    $('review-list-scope').textContent = `Showing ${formatNumber(accounts.length)} of ${formatNumber(total)} accounts needing review.`;
    updateReviewSelection();
  }

  function updateSelections() {
    const max = limits().testAccounts;
    const held = heldSessionRows();
    const eligible = filteredReadyRows();
    const randomMaximum = Math.min(eligible.length, max);
    const randomCount = Number($('random-count').value);
    const randomValid = /^\d+$/.test($('random-count').value.trim()) && Number.isSafeInteger(randomCount) && randomCount >= 1 && randomCount <= randomMaximum;
    const selectionDisabled = busy || requestPending || !state;
    $('random-count').max = String(randomMaximum);
    $('random-count').disabled = selectionDisabled || randomMaximum === 0;
    $('random-count').setAttribute('aria-invalid', String(randomMaximum > 0 && !randomValid));
    $('test-account-country').disabled = selectionDisabled;
    $('test-account-country-hint').textContent = `${formatNumber(eligible.length)} ready account${eligible.length === 1 ? '' : 's'} match this registered country. Your connection choice is independent.`;
    $('select-all-accounts').disabled = selectionDisabled || eligible.length === 0;
    $('clear-accounts').disabled = selectionDisabled || selectedRows.size === 0;
    $('select-random-accounts').disabled = selectionDisabled || !randomValid;
    const history = currentLikeHistory();
    $('select-not-liked-accounts').disabled = selectionDisabled || songDirty || !notYetLikedRows().length;
    const likeCounts = {eligible: 0, confirmed: 0, verification_pending: 0, unknown: 0, held: 0};
    selectedRows.forEach(row => { likeCounts[likeHistoryStatus(row, history)] += 1; });
    const likeParts = [`${formatNumber(likeCounts.eligible)} eligible selected`, `${formatNumber(likeCounts.confirmed)} already liked in saved history (will skip)`];
    if (likeCounts.verification_pending) likeParts.push(`${formatNumber(likeCounts.verification_pending)} awaiting like verification`);
    if (likeCounts.unknown || likeCounts.held) likeParts.push(`${formatNumber(likeCounts.unknown + likeCounts.held)} held from another like request`);
    $('like-history-summary').textContent = songDirty ? 'Save the song ID to load its like history.' : `Likes for song ${history.song || '—'}: ${likeParts.join(' · ')}. Like tests run once per account/song; extra repeats are skipped.`;
    document.querySelectorAll('#account-picker input').forEach(input => {
      input.checked = selectedRows.has(Number(input.value));
      input.disabled = busy || requestPending || (!input.checked && selectedRows.size >= max);
    });
    $('selection-counter').textContent = `${selectedRows.size} selected`;
    let count = Number($('test-count').value);
    if (!Number.isSafeInteger(count) || count < 1 || count > limits().tests) count = null;
    $('run-summary').textContent = selectedRows.size === 0 ? 'Select an account to get started.' : count === null ? `Choose between 1 and ${limits().tests} tests per account.` : `Play: ${selectedRows.size} account${selectedRows.size === 1 ? '' : 's'} × ${count} test${count === 1 ? '' : 's'} = ${selectedRows.size * count} run${selectedRows.size * count === 1 ? '' : 's'}`;
    const workers = Number($('test-workers').value);
    const failures = Number($('test-failure-limit').value);
    const workersValid = /^\d+$/.test($('test-workers').value.trim()) && Number.isSafeInteger(workers) && workers >= 1;
    const failuresValid = /^\d+$/.test($('test-failure-limit').value.trim()) && Number.isSafeInteger(failures) && failures >= 1;
    $('test-workers').setAttribute('aria-invalid', String(!workersValid));
    $('test-workers-hint').textContent = `Choose any positive whole number. Active workers are limited by selected accounts. Each account's tests stay in order.${workersValid && failuresValid && workers > failures ? ` After ${failures} consecutive failures, no new tests start; tests already active finish.` : ''}`;
    $('test-failure-limit').setAttribute('aria-invalid', String(!failuresValid));
    if (selectedRows.size && count !== null && workersValid && failuresValid) $('run-summary').textContent += ` · up to ${Math.min(workers, selectedRows.size)} worker${Math.min(workers, selectedRows.size) === 1 ? '' : 's'} · stop at ${failures} consecutive failure${failures === 1 ? '' : 's'}`;
    ['run-play', 'run-like'].forEach(id => { $(id).disabled = busy || requestPending || !state || songDirty || selectedRows.size === 0 || count === null || !workersValid || !failuresValid; });
    ['check-sessions', 'check-song'].forEach(id => { $(id).disabled = busy || requestPending || !state || selectedRows.size === 0; });
    if (songDirty) $('check-song').disabled = true;
    document.querySelectorAll('#cohort-table input').forEach(input => { input.disabled = busy || requestPending || held.has(Number(input.value)) || (!input.checked && refreshRows.size >= limits().accounts); });
    updateProxyMode();
  }

  function renderAccounts() {
    const held = heldSessionRows();
    const previousRefreshCount = refreshRows.size;
    for (const row of refreshRows) if (held.has(row)) refreshRows.delete(row);
    if (refreshRows.size !== previousRefreshCount) $('login-rows').value = [...refreshRows].sort((a, b) => a - b).join(', ');
    const allReady = readyRows();
    const ready = filteredReadyRows();
    const rows = new Map((state?.cohort?.accounts || []).map(account => [Number(account.source_row), account]));
    allReady.forEach(row => { if (!rows.has(row)) rows.set(row, {source_row: row, session_saved: true, state: 'ready'}); });
    for (const row of selectedRows) { if (!ready.includes(row)) selectedRows.delete(row); }
    const maximum = Math.min(ready.length, limits().testAccounts);
    const currentRandomCount = Number($('random-count').value);
    if (previousRandomMaximum !== null && previousRandomMaximum !== maximum && maximum > 0 && Number.isSafeInteger(currentRandomCount) && currentRandomCount > maximum) {
      $('random-count').value = String(maximum);
      showError('selection-error', '');
    }
    if (previousRandomMaximum !== null && previousRandomMaximum !== maximum && $('selection-error').dataset.kind === 'random-count') {
      try { validateRandomCount(maximum); showError('selection-error', ''); }
      catch (error) { showError('selection-error', error.message, 'random-count'); }
    }
    previousRandomMaximum = maximum;
    if (firstSelection && ready.length) {
      selectedRows.add(ready.includes(7) ? 7 : ready[0]);
      firstSelection = false;
    }
    $('account-picker').replaceChildren();
    const likeHistory = currentLikeHistory();
    ready.sort((a, b) => a - b).forEach(row => {
      const account = rows.get(row);
      const label = node('label', 'account-choice');
      const checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.value = String(row);
      checkbox.setAttribute('aria-label', `Select account row ${row}`);
      checkbox.addEventListener('change', () => {
        if (busy || requestPending || !state || !filteredReadyRows().includes(row) || checkbox.checked && !selectedRows.has(row) && selectedRows.size >= limits().testAccounts) { updateSelections(); return; }
        if (checkbox.checked) selectedRows.add(row); else selectedRows.delete(row);
        showError('workbench-error', ''); showError('selection-error', ''); updateSelections();
      });
      const information = node('span', 'account-info');
      const country = ['EG', 'LB'].includes(account.registered_country) ? account.registered_country : 'Other';
      information.append(node('strong', '', `Account ${row} · ${country}`), node('small', '', account.email || account.account_email || 'Session ready'));
      const likeStatus = likeHistoryStatus(row, likeHistory);
      if (likeStatus === 'confirmed') information.appendChild(node('small', 'like-history-hint', 'Already liked (saved history)'));
      else if (likeStatus === 'verification_pending') information.appendChild(node('small', 'like-history-hint pending', 'Like verification pending · Like skipped'));
      else if (likeStatus === 'unknown' || likeStatus === 'held') information.appendChild(node('small', 'like-history-hint pending', 'Unknown like result · Like held'));
      label.append(checkbox, information, node('span', 'status-dot'));
      $('account-picker').appendChild(label);
    });
    $('empty-accounts').hidden = ready.length > 0;
    if ($('empty-accounts-title')) $('empty-accounts-title').textContent = allReady.length && !ready.length ? 'No ready accounts match this country.' : 'No test accounts are ready yet.';
    $('cohort-table').replaceChildren();
    [...rows.values()].sort((a, b) => Number(a.source_row) - Number(b.source_row)).forEach(account => {
      const row = Number(account.source_row);
      const tr = node('tr');
      const selectCell = node('td');
      const checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.value = String(row); checkbox.checked = refreshRows.has(row);
      checkbox.setAttribute('aria-label', `Select row ${row} for session refresh`);
      checkbox.addEventListener('change', () => {
        if (heldSessionRows().has(row)) { checkbox.checked = false; return; }
        if (checkbox.checked) refreshRows.add(row); else refreshRows.delete(row);
        $('login-rows').value = [...refreshRows].sort((a, b) => a - b).join(', ');
        updateSelections();
      });
      selectCell.appendChild(checkbox);
      const sessionCell = node('td');
      sessionCell.appendChild(node('span', 'badge ' + (account.session_saved ? 'success' : 'neutral'), account.session_saved ? 'Saved' : 'No session'));
      const statusCell = node('td');
      statusCell.appendChild(node('span', 'badge ' + (allReady.includes(row) ? 'success' : 'warning'), held.has(row) ? 'Session review pending' : allReady.includes(row) ? 'Ready to test' : humanLabel(account.state || 'Needs a check')));
      const country = ['EG', 'LB'].includes(account.registered_country) ? account.registered_country : 'Other';
      tr.append(selectCell, node('td', '', `Row ${row} · ${country}`), sessionCell, statusCell);
      $('cohort-table').appendChild(tr);
    });
    $('cohort-count').textContent = `${rows.size} account${rows.size === 1 ? '' : 's'}`;
    $('cohort-empty').hidden = rows.size > 0;
    renderFailureReview();
    renderSessionReview();
    updateSelections();
  }

  function humanLabel(value) { return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase()); }
  function formatNumber(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString() : '—'; }
  function validTransferNumber(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0; }
  function formatBytes(value) {
    if (!validTransferNumber(value)) return 'Not recorded';
    if (value < 1000) return `${Math.round(value)} B`;
    const units = ['KB', 'MB', 'GB', 'TB'], power = Math.min(units.length, Math.floor(Math.log10(value) / 3));
    return `${(value / (1000 ** power)).toFixed(2)} ${units[power - 1]}`;
  }
  function formatTransferCost(value) {
    if (!validTransferNumber(value)) return 'Not recorded';
    if (value === 0) return '$0';
    if (value < 0.000001) return '<$0.000001';
    return `$${value.toFixed(value < 0.01 ? 6 : 4)}`;
  }
  function transferUsage(report) {
    const usage = report?.network_usage;
    if (usage && typeof usage === 'object' && !Array.isArray(usage) && ['measured', 'partial', 'unavailable'].includes(usage.measurement)) return usage;
    return legacyTransferUsage(report);
  }
  function legacyTransferUsage(report) {
    const parts = report?.bandwidth;
    if (!parts || typeof parts !== 'object' || Array.isArray(parts)) return null;
    const counterNames = ['request_bytes', 'upload_body_bytes', 'download_body_bytes', 'response_header_bytes'];
    const validCounter = value => Number.isSafeInteger(value) && value >= 0;
    const parsePart = value => {
      if (!value || typeof value !== 'object' || Array.isArray(value) || !validCounter(value.request_count)) return null;
      const counters = Object.fromEntries(counterNames.map(name => [name, validCounter(value[name]) ? value[name] : null]));
      return {request_count: value.request_count, ...counters};
    };
    const total = parsePart(parts.total);
    const completeTotal = total && counterNames.every(name => total[name] !== null);
    const groups = completeTotal ? [total] : [parsePart(parts.get_song), parsePart(parts.play_song)];
    const counters = {request_count: 0, sent_bytes: 0, received_bytes: 0};
    let known = false;
    for (const group of groups) {
      if (!group || group.request_count === 0) continue;
      const available = counterNames.some(name => group[name] !== null);
      counters.request_count += group.request_count;
      if (!available) continue;
      known = true;
      // Curl REQUEST_SIZE already includes the body. When it is absent or
      // partial, a known upload-body count still provides a lower bound.
      counters.sent_bytes += Math.max(group.request_bytes ?? 0, group.upload_body_bytes ?? 0);
      counters.received_bytes += (group.download_body_bytes ?? 0) + (group.response_header_bytes ?? 0);
    }
    if (!counters.request_count || !known || !Object.values(counters).every(validCounter)) return null;
    const bytes = counters.sent_bytes + counters.received_bytes;
    if (!validCounter(bytes)) return null;
    const proxy = report.proxy && typeof report.proxy === 'object' && !Array.isArray(report.proxy) && report.proxy.provider === 'PacketStream' && ['EG', 'US'].includes(report.proxy.country) ? report.proxy : null;
    const declared = typeof report.proxy_egypt === 'boolean' ? report.proxy_egypt : null;
    const metadata = proxy ? proxy.proxy_used === false ? false : true : null;
    const route = declared !== null && metadata !== null && declared !== metadata ? null : declared ?? metadata;
    return {...counters, total_bytes: bytes, measurement: 'partial', scope: 'legacy_play_requests',
      proxy_bytes: route === true ? bytes : route === false ? 0 : null,
      direct_bytes: route === false ? bytes : route === true ? 0 : null,
      unknown_route_bytes: route === null ? bytes : 0,
      ...(route === null ? {} : {cost_usd: route ? bytes / 1e9 : 0})};
  }
  function transferCost(usage) {
    if (!usage || usage.measurement === 'unavailable') return null;
    if (validTransferNumber(usage.cost_usd)) return usage.cost_usd;
    return validTransferNumber(usage.proxy_bytes) ? usage.proxy_bytes / 1e9 : null;
  }
  function partialTransferCost(usage) { return usage?.measurement === 'partial' || validTransferNumber(usage?.unknown_route_bytes) && usage.unknown_route_bytes > 0; }
  function bandwidthResultText(report) {
    const usage = transferUsage(report);
    if (!usage || usage.measurement === 'unavailable') return 'This check · HTTP usage: Not recorded · Est. proxy cost: Not recorded';
    const partial = usage.measurement === 'partial';
    const bytes = formatBytes(usage.total_bytes), cost = formatTransferCost(transferCost(usage));
    return `This check · HTTP usage: ${bytes}${partial ? ' recorded (partial)' : ''} · Est. proxy cost: ${cost}${partialTransferCost(usage) && cost !== 'Not recorded' ? ' (minimum)' : ''}`;
  }
  function bandwidthAccountText(report) {
    if (!Number.isSafeInteger(report.account_tests_completed) || report.account_tests_completed <= 1) return '';
    const usage = transferUsage({network_usage: report.account_network_usage});
    if (!usage || usage.measurement === 'unavailable') return `Account total: Not recorded across ${formatNumber(report.account_tests_completed)} finished checks`;
    const cost = formatTransferCost(transferCost(usage));
    return `Account total: ${formatBytes(usage.total_bytes)}${usage.measurement === 'partial' ? ' recorded (partial)' : ''} across ${formatNumber(report.account_tests_completed)} finished checks · Est. proxy cost: ${cost}${partialTransferCost(usage) && cost !== 'Not recorded' ? ' (minimum)' : ''}`;
  }
  function bandwidthPanel(report) {
    const panel = node('div', 'bandwidth-panel');
    panel.appendChild(node('h3', '', 'Bandwidth and estimated cost'));
    const usage = transferUsage(report), available = !!usage && usage.measurement !== 'unavailable';
    const partial = usage?.measurement === 'partial';
    const costPartial = partialTransferCost(usage);
    const byteValue = value => available ? formatBytes(value) : 'Not recorded';
    const costValue = value => available ? formatTransferCost(value) : 'Not recorded';
    const metrics = [
      [partial ? 'Recorded HTTP transfer (minimum)' : 'Observed HTTP transfer', byteValue(usage?.total_bytes)],
      [costPartial ? 'Est. proxy cost (minimum)' : 'Estimated proxy cost', costValue(transferCost(usage))],
      ['Upload', byteValue(usage?.sent_bytes)],
      ['Download', byteValue(usage?.received_bytes)],
    ];
    const run = Number.isSafeInteger(report?.completed_tests) || Number.isSafeInteger(report?.attempted);
    if (run) metrics.push(
      ['Average / check', byteValue(usage?.avg_bytes_per_test)],
      ['Average / account', byteValue(usage?.avg_bytes_per_account)],
      [partial ? 'Projected full run (minimum)' : 'Projected full run', byteValue(usage?.estimated_total_bytes)],
      [costPartial ? 'Projected proxy cost (minimum)' : 'Projected proxy cost', costValue(usage?.estimated_total_cost_usd)],
    );
    const grid = node('div', 'bandwidth-grid');
    metrics.forEach(([label, value]) => {
      const item = node('div', 'bandwidth-stat'); item.append(node('span', '', label), node('strong', '', value)); grid.appendChild(item);
    });
    panel.appendChild(grid);
    const notes = [];
    if (!available) notes.push('Bandwidth was not recorded. A full-run estimate is unavailable.');
    else {
      if (Number.isSafeInteger(usage.sampled_tests) && usage.sampled_tests >= 0 && Number.isSafeInteger(usage.completed_tests) && usage.completed_tests >= 0) notes.push(`Usage recorded for ${formatNumber(usage.sampled_tests)} of ${formatNumber(usage.completed_tests)} finished checks.`);
      if (usage.scope === 'legacy_play_requests') notes.push('Only metadata and play requests were recorded; session checks, login and connection retries are excluded.');
      else if (partial) notes.push('Some requests or failed transfers were not fully measured. Totals and projections are minimum estimates.');
      if (validTransferNumber(usage.unknown_route_bytes) && usage.unknown_route_bytes > 0) notes.push('Some measured traffic could not be classified as direct or proxy. Proxy costs are minimum estimates.');
      if (isActive(report)) notes.push('Active checks are added when they finish.');
      if (run && validTransferNumber(usage.estimated_total_bytes)) notes.push('Full-run projections use the observed average and may change.');
    }
    panel.appendChild(node('p', 'activity-hint bandwidth-coverage', notes.join(' ')));
    panel.appendChild(node('p', 'activity-hint bandwidth-note', 'HTTP transfer estimate; PacketStream billing may differ. Proxy cost uses $1 per GB (1,000,000,000 bytes). Direct traffic has no proxy charge.'));
    return panel;
  }
  function renderRunBandwidth(job) {
    const target = $('job-bandwidth');
    if (!target) return;
    const visible = !!job && (['play', 'like'].includes(job.action) || !!transferUsage(job));
    target.hidden = !visible;
    target.replaceChildren(...(visible ? [bandwidthPanel(job)] : []));
  }
  function formatDate(value) {
    if (!value) return 'Saved locally';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? 'Saved locally' : date.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
  }

  function reportLabel(name) { return String(name || 'Report').replace(/\.json$/i, '').replace(/[._-]+/g, ' '); }
  function readableMessage(value) { return typeof value === 'string' && value.trim() !== '[redacted]' ? value : ''; }
  function pendingSummary(job) {
    const parts = [];
    if (Number(job.connection_pending) > 0) parts.push(`${formatNumber(job.connection_pending)} connections pending`);
    if (Number.isSafeInteger(job.preparation_held) && job.preparation_held > 0) parts.push(`${formatNumber(job.preparation_held)} preparation results held for review`);
    if (Number(job.verification_pending) > 0) parts.push(`${formatNumber(job.verification_pending)} accepted likes awaiting verification`);
    if (Number.isSafeInteger(job.session_review_pending) && job.session_review_pending > 0) parts.push(`${formatNumber(job.session_review_pending)} session${job.session_review_pending === 1 ? '' : 's'} held for review`);
    return parts.join('; ');
  }
  function completedPendingLabel(job) {
    return Number(job.verification_pending) > 0 || Number.isSafeInteger(job.preparation_held) && job.preparation_held > 0 || Number.isSafeInteger(job.session_review_pending) && job.session_review_pending > 0 || Number.isSafeInteger(job.like_verification_held) && job.like_verification_held > 0 || Number.isSafeInteger(job.unknown_like_held) && job.unknown_like_held > 0 ? 'Completed with pending checks' : 'Completed with connections pending';
  }

  function reportMessage(report) {
    const name = actionNames[report.action] || 'Saved run';
    const [, outcome] = resultStatus(report);
    if (outcome === 'Unknown') return `${name} has an uncertain result. Review the details before repeating the action.`;
    if (report.status === 'completed_with_failures') return `${name} finished with ${formatNumber(report.failed)} failed tests. Review the saved results.`;
    if (report.status === 'completed_with_pending') return `${name} finished with ${pendingSummary(report) || 'pending checks'}. These accounts were not marked as failed.`;
    if (outcome === 'Verification pending') return 'The server accepted the like. Checking its saved state is still pending; the like was not sent again.';
    if (outcome === 'Session review pending') return 'The session refresh result was not confirmed. The account is held for a read-only saved-session check; no play or like request was sent.';
    if (outcome === 'Failed') return `${name} stopped. Review the saved details for the failed check.`;
    if (outcome === 'Already liked') return 'The song was already liked; its saved state was verified.';
    if (outcome === 'Already liked (saved history)') return 'Saved history already confirms this account liked this song. No account or like request was sent.';
    if (outcome === 'Like history pending') return 'A previous like needs verification. No new like request was sent.';
    if (outcome === 'Accepted') return `${name} was accepted. The saved details show the verified state.`;
    if (report.passed === true || report.status === 'succeeded') return `${name} completed successfully.`;
    if (report.dry_run) return 'Account selection preview completed. No login or test event was sent.';
    if (['queued', 'running'].includes(report.status)) return `${name} was in progress when this report was saved.`;
    return 'Review the saved results and details below.';
  }

  function renderReportLists() {
    const reports = state?.reports || [];
    $('recent-reports').replaceChildren();
    $('report-list').replaceChildren();
    if (!reports.length) {
      $('recent-reports').appendChild(node('div', 'empty-inline', 'Your completed runs will appear here.'));
      $('report-list').appendChild(node('div', 'empty-inline', 'No saved reports yet.'));
      return;
    }
    reports.slice(0, 3).forEach(report => {
      const button = node('button', 'recent-report'); button.type = 'button';
      const file = node('span', 'report-file-icon'); file.appendChild(icon('file'));
      const text = node('span'); text.append(node('strong', '', reportLabel(report.name)), node('small', '', formatDate(report.modified_at)));
      button.append(file, text, icon('arrow'));
      button.addEventListener('click', () => { switchPanel('reports'); loadReport(report.name); });
      $('recent-reports').appendChild(button);
    });
    reports.forEach(report => {
      const button = node('button', 'report-list-item' + (selectedReport === report.name ? ' active' : '')); button.type = 'button';
      const file = node('span', 'report-file-icon'); file.appendChild(icon('file'));
      const text = node('div'); text.append(node('strong', '', reportLabel(report.name)), node('small', '', formatDate(report.modified_at)));
      button.append(file, text, icon('arrow'));
      button.addEventListener('click', () => loadReport(report.name));
      $('report-list').appendChild(button);
    });
  }

  function resultStatus(result) {
    const values = [result.status, result.outcome, result.mutation_result, result.event_result, result.result].filter(value => typeof value === 'string' && value !== '[redacted]');
    const flags = values.join(' ').toLowerCase();
    if (result.outcome === 'history_skipped') return ['neutral', 'Already liked (saved history)'];
    if (result.outcome === 'like_history_pending') return ['warning', 'Like history pending'];
    if (result.outcome === 'session_review_pending') return ['warning', 'Session review pending'];
    if (result.unknown || result.result_unknown || result.error?.result_unknown || /unknown|uncertain/.test(flags)) return ['warning', 'Unknown'];
    if (result.outcome === 'connection_pending') return ['warning', 'Connection pending'];
    if (result.outcome === 'verification_pending') return ['warning', 'Verification pending'];
    if (result.outcome === 'account_failed') return ['error', 'Account failed'];
    if (result.status === 'completed_with_pending') return ['warning', completedPendingLabel(result)];
    if (result.status === 'completed_with_failures') return ['warning', 'Completed with failures'];
    if (result.passed === false || result.error || /failed|blocked|error|rejected/.test(flags)) return ['error', 'Failed'];
    if (/already.*liked|skipped_already_liked/.test(flags) || result.skipped_already_liked) return ['neutral', 'Already liked'];
    if (result.event_accepted === true || result.mutation_accepted === true || /accepted/.test(flags)) return ['success', 'Accepted'];
    if (/prepared|enrolled/.test(flags)) return ['success', 'Prepared'];
    if (/selected|preview/.test(flags)) return ['neutral', 'Selected'];
    if (result.passed === true || /succeeded|passed|ready|ok|liked|saved|complete/.test(flags)) return ['success', 'Passed'];
    return ['neutral', humanLabel(values[0] || 'Result')];
  }

  function actionWriteCounts(job) {
    const names = ['writes_attempted', 'writes_accepted', 'new_likes_verified', 'already_liked_verified'];
    if (names.every(name => Number.isSafeInteger(job[name]) && job[name] >= 0)) return Object.fromEntries(names.map(name => [name, job[name]]));
    // Older jobs do not store these counters. Derive them only when every
    // finished result is retained; a partial result list cannot prove totals.
    const results = Array.isArray(job.results) ? job.results : [];
    if (Number(job.results_total ?? job.completed_tests ?? 0) > results.length) return Object.fromEntries(names.map(name => [name, null]));
    const counts = Object.fromEntries(names.map(name => [name, 0]));
    const prefix = job.action === 'play' ? 'event' : 'mutation';
    results.forEach(result => {
      const attempted = result[`${prefix}_attempted`] === true;
      const accepted = attempted && result[`${prefix}_accepted`] === true && result[`${prefix}_result`] === 'accepted';
      counts.writes_attempted += Number(attempted); counts.writes_accepted += Number(accepted);
      const verified = job.action === 'like' && result.passed === true && result.persisted_state_verified === true && result.liked_after === true;
      counts.new_likes_verified += Number(verified && result.liked_before === false && accepted);
      counts.already_liked_verified += Number(verified && result.liked_before === true && result.mutation_attempted === false && result.mutation_result === 'skipped_already_liked');
    });
    return counts;
  }
  function completedCheckCount(job) {
    if (Number.isSafeInteger(job?.completed_tests) && job.completed_tests >= 0) return job.completed_tests;
    return Number.isSafeInteger(job?.progress?.completed) && job.progress.completed >= 0 ? job.progress.completed : 0;
  }
  function resolvedCheckCount(job) {
    const finished = completedCheckCount(job);
    if (job?.action !== 'like') return finished;
    const cached = ['cached_like_skips', 'cached_like_holds'].reduce((count, name) => count + (Number.isSafeInteger(job[name]) && job[name] >= 0 ? job[name] : 0), 0);
    return Number.isSafeInteger(finished + cached) ? finished + cached : finished;
  }

  function actionWriteSummary(job) {
    const counts = actionWriteCounts(job);
    if (counts.writes_attempted === null) return 'Write totals were not recorded for this older run.';
    const label = job.action === 'like' ? 'likes' : 'play records';
    const parts = [counts.writes_attempted ? `${formatNumber(counts.writes_attempted)} ${job.action} request(s) attempted` : `0 ${label} sent`];
    if (job.action === 'like' && (counts.writes_attempted || counts.already_liked_verified)) parts.push(`${formatNumber(counts.new_likes_verified)} new likes verified`, `${formatNumber(counts.already_liked_verified)} already liked`);
    if (job.action === 'play' && counts.writes_attempted) parts.push(`${formatNumber(counts.writes_accepted)} play records accepted`);
    if (job.action === 'like') {
      if (Number.isSafeInteger(job.already_liked_skipped) && job.already_liked_skipped > 0) parts.push(`${formatNumber(job.already_liked_skipped)} accounts skipped from saved like history`);
      if (Number.isSafeInteger(job.like_verification_held) && job.like_verification_held > 0) parts.push(`${formatNumber(job.like_verification_held)} accounts held for like verification`);
      if (Number.isSafeInteger(job.unknown_like_held) && job.unknown_like_held > 0) parts.push(`${formatNumber(job.unknown_like_held)} accounts held for an unknown like result`);
      if (Number.isSafeInteger(job.duplicate_like_skipped_tests) && job.duplicate_like_skipped_tests > 0) parts.push(`${formatNumber(job.duplicate_like_skipped_tests)} repeated like checks skipped`);
    }
    const pending = pendingSummary(job);
    if (pending) parts.push(pending);
    return parts.join('; ') + '.';
  }

  function durationLabel(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60), rest = value % 60;
    return hours ? `${hours}h ${minutes}m ${rest}s` : minutes ? `${minutes}m ${rest}s` : `${rest}s`;
  }

  function elapsedBetween(start, finish) {
    const began = Date.parse(start || ''), ended = finish ? Date.parse(finish) : Date.now();
    return Number.isFinite(began) && Number.isFinite(ended) ? Math.max(0, (ended - began) / 1000) : 0;
  }

  function stopMessage(reason, action) {
    const messages = {
      consecutive_failure_limit: 'Stopped because the configured consecutive failure limit was reached.',
      result_unknown: 'Paused because a write result is uncertain. Review the account report before another test.',
      journal_failed: 'Paused because a local report could not be saved. Review existing account reports.',
      immediate_failure: 'Paused for a scope, configuration or unconfirmed result check.',
      completed_with_failures: 'All planned tests finished. Some confirmed failures need review.',
      completed_with_pending: 'All planned tests finished. Pending connections, saved-state checks or held sessions need review; these accounts were not marked as failed.',
      provider_unavailable: 'The available connection attempts were exhausted. No write was retried; the account remains pending.',
      user_stop: action === 'prepare'
        ? 'Preparation stopped by your request. Active accounts were allowed to finish and save their sessions; no new accounts started.'
        : 'Stopped by your request. Checks already running were allowed to finish; no new checks started.',
    };
    return messages[reason] || '';
  }

  function resultRows(results) {
    const entries = Array.isArray(results) ? results : results && typeof results === 'object' ? [results] : [];
    return entries.flatMap(result => {
      if (!result || typeof result !== 'object') return [];
      if ((Array.isArray(result.prepared_rows) && result.prepared_rows.length) || result.connection_pending_rows?.length || result.account_failed_rows?.length || result.attention_required_rows?.length) {
        return [
          ...(result.prepared_rows || []).map(sourceRow => ({source_row: sourceRow, status: 'prepared', session_saved: true, passed: true})),
          ...(result.account_failed_rows || []).map(sourceRow => ({source_row: sourceRow, outcome: 'account_failed', passed: false})),
          ...(result.connection_pending_rows || []).map(sourceRow => ({source_row: sourceRow, outcome: 'connection_pending', passed: false})),
          ...(result.attention_required_rows || []).map(sourceRow => ({source_row: sourceRow, outcome: 'session_review_pending', passed: false, message: 'Preparation result held for review. This account was not retried; other accounts continued.', session_storage_failure: result.recent_session_storage_failures?.find(item => item.source_row === sourceRow)})),
        ];
      }
      if (result.dry_run && Array.isArray(result.selected_rows)) {
        return result.selected_rows.map(sourceRow => ({source_row: sourceRow, status: 'selected', message: 'Selected for preparation. No login or test event was sent.'}));
      }
      return [result];
    });
  }

  function renderResultList(container, results, {showBandwidth = false} = {}) {
    container.replaceChildren();
    resultRows(results).forEach((result, index) => {
      if (!result || typeof result !== 'object') return;
      const row = node('div', 'result-row');
      const details = node('div');
      const testNumber = result.test_number ?? result.run ?? result.attempt;
      const title = result.source_row !== undefined ? `Account ${result.source_row}${testNumber !== undefined ? ` · Run ${testNumber}` : ''}` : result.country ? `${result.country} connection` : `Result ${index + 1}`;
      details.appendChild(node('strong', '', title));
      let message = result.outcome === 'session_review_pending' ? readableMessage(result.message) || 'The session refresh result was not confirmed. This account is held for a read-only saved-session check; no play or like request was sent. Other accounts continued.' : result.outcome === 'verification_pending' ? 'The server accepted the like, but its saved state could not be verified. The like was not sent again; other accounts continued.' : readableMessage(result.message) || readableMessage(result.error?.message) || readableMessage(result.error) || readableMessage(result.reason);
      if (result.outcome === 'history_skipped') message = 'Saved history already confirms this account liked this song. No account or like request was sent.';
      if (result.outcome === 'like_history_pending') message = 'A previous like needs verification. This account was held without sending another like request.';
      if (result.session_review_cleared === true) message = 'The saved session passed read-only checks. Its hold was cleared; no login, play or like request was sent.';
      if (!message && result.event_accepted) message = 'The server accepted the test record. Check the admin dashboard for its statistics.';
      if (!message && result.skipped_already_liked) message = 'Verified the existing like; no new like request was needed.';
      if (!message && result.mutation_result === 'skipped_already_liked') message = 'Verified the existing like; no new like request was needed.';
      if (!message && result.session_saved) message = 'The saved session is available for testing.';
      if (!message && result.mutation_attempted === true && result.mutation_accepted === true && result.mutation_result === 'accepted' && result.passed === true && result.liked_before === false && result.liked_after === true && result.persisted_state_verified === true) message = 'A new like was saved and verified.';
      if (!message && result.passed) message = 'The check completed successfully.';
      if (!message && result.outcome === 'connection_pending') message = 'Connection attempts were exhausted before a write. The account remains pending and is not in the failed-account review list.';
      if (!message && result.outcome === 'account_failed') message = 'The account session or identity was rejected. Select this row in Accounts to prepare it for review.';
      if (message) details.appendChild(node('p', '', message));
      const facts = [];
      if (Number.isFinite(result.elapsed_seconds)) facts.push(`${Number(result.elapsed_seconds).toFixed(1)}s`);
      if (result.failed_phase) facts.push(`${result.outcome === 'verification_pending' ? 'Verification pending at' : 'Failed at'} ${humanLabel(result.failed_phase)}`);
      if (result.error_code) facts.push(humanLabel(result.error_code));
      const http = result.event_http_status ?? result.mutation_http_status ?? result.http_status;
      if (Number.isInteger(http)) facts.push(`HTTP ${http}`);
      if (result.finished_at) facts.push(formatDate(result.finished_at));
      if (result.proxy_failure?.failure_kind) facts.push(`Proxy: ${humanLabel(result.proxy_failure.failure_kind)}${Number.isInteger(result.proxy_failure.curl_code) ? ` (Curl ${result.proxy_failure.curl_code})` : ''}`);
      if (Number.isSafeInteger(result.proxy_route_number)) facts.push(`Route ${result.proxy_route_number}${Number.isSafeInteger(result.proxy_pool_size) ? ` of ${result.proxy_pool_size}` : ''}`);
      if (result.session_failure?.code) facts.push(humanLabel(result.session_failure.code));
      if (result.session_storage_failure?.code) facts.push(`Storage: ${humanLabel(result.session_storage_failure.code)}${Number.isInteger(result.session_storage_failure.sqlite_code) ? ` (SQLite ${result.session_storage_failure.sqlite_code})` : ''}`);
      if (result.ui_read_failure?.code) facts.push(`Local status: ${humanLabel(result.ui_read_failure.code)} · ${humanLabel(result.ui_read_failure.stage)} · ${result.ui_read_failure.attempts} read attempt${result.ui_read_failure.attempts === 1 ? '' : 's'}`);
      if (Number.isInteger(result.provider_attempts)) facts.push(`${result.provider_attempts} connection attempt${result.provider_attempts === 1 ? '' : 's'}`);
      if (Number.isInteger(result.retry_count) && result.retry_count > 0) facts.push(`${result.retry_count} retr${result.retry_count === 1 ? 'y' : 'ies'} before the write`);
      if (facts.length) details.appendChild(node('small', 'result-facts', facts.join(' · ')));
      if (showBandwidth || transferUsage(result)) details.appendChild(node('small', 'result-bandwidth', bandwidthResultText(result)));
      const accountBandwidth = bandwidthAccountText(result);
      if (accountBandwidth) details.appendChild(node('small', 'result-bandwidth', accountBandwidth));
      if (Array.isArray(result.attempt_history) && result.attempt_history.length > 1) {
        const history = node('details', 'attempt-history'); history.appendChild(node('summary', '', 'Connection attempts'));
        result.attempt_history.slice(0, 3).forEach(attempt => {
          const failure = attempt.session_failure || attempt.proxy_failure || {};
          const info = [`Attempt ${attempt.attempt}`, Number.isInteger(attempt.proxy_route_number) ? `Route ${attempt.proxy_route_number}` : '', humanLabel(attempt.outcome), Number.isInteger(failure.http_status) ? `HTTP ${failure.http_status}` : '', Number.isInteger(failure.curl_code) ? `Curl ${failure.curl_code}` : '', failure.code ? humanLabel(failure.code) : failure.failure_kind ? humanLabel(failure.failure_kind) : ''].filter(Boolean);
          history.appendChild(node('p', '', info.join(' · ')));
        });
        details.appendChild(history);
      }
      const [color, label] = resultStatus(result);
      row.append(details, node('span', 'badge ' + color, label)); container.appendChild(row);
    });
  }

  function renderRunVisibility(job) {
    renderRunBandwidth(job);
    const tests = !!job && ['play', 'like'].includes(job.action) && Number.isSafeInteger(job.attempted);
    $('job-overview').hidden = !tests;
    $('job-active-section').hidden = !tests;
    $('job-result-toolbar').hidden = !tests;
    $('job-stop-reason').hidden = !job || !stopMessage(job?.stop_reason, job?.action);
    $('job-stop-reason').textContent = stopMessage(job?.stop_reason, job?.action);
    if (!tests) { $('job-results-empty').hidden = true; $('job-results-retention').hidden = true; return; }
    const total = Math.max(0, Number(job.progress?.total) || 0);
    const finished = completedCheckCount(job);
    const resolved = Math.min(total, resolvedCheckCount(job));
    const active = Math.max(0, Number(job.active_workers) || 0);
    const elapsed = Number.isFinite(job.elapsed_seconds) && job.elapsed_seconds >= 0 ? job.elapsed_seconds : elapsedBetween(job.started_at, job.finished_at);
    const pending = Math.max(0, total - resolved - active);
    const providerWaiting = (Array.isArray(job.active_tests) ? job.active_tests : []).filter(test => test.connection_status === 'waiting').length;
    const requestedWorkers = Number.isSafeInteger(job.requested_workers) && job.requested_workers >= 1 ? job.requested_workers : Number.isSafeInteger(job.workers) && job.workers >= 1 ? job.workers : null;
    const effectiveWorkers = Number.isSafeInteger(job.effective_workers) && job.effective_workers >= 0 && (requestedWorkers === null || job.effective_workers <= requestedWorkers) ? job.effective_workers : null;
    const writeCounts = actionWriteCounts(job), writeValue = name => writeCounts[name] === null ? 'Not recorded' : formatNumber(writeCounts[name]);
    const metrics = [
      ['Checks finished', `${formatNumber(resolved)} / ${formatNumber(total)}`],
      ...(resolved > finished ? [['Checks run', formatNumber(finished)]] : []),
      ['Checks passed', formatNumber(job.succeeded)],
      [job.action === 'like' ? 'Like requests attempted' : 'Play requests attempted', writeValue('writes_attempted')],
      ...(job.action === 'like' ? [['New likes verified', writeValue('new_likes_verified')], ['Already liked', writeValue('already_liked_verified')]] : [['Play records accepted', writeValue('writes_accepted')]]),
      ...(job.action === 'like' ? [['selected_accounts', 'Accounts selected'], ['eligible_accounts', 'Like eligible accounts'], ['already_liked_skipped', 'Saved likes skipped'], ['like_verification_held', 'Like verification held'], ['unknown_like_held', 'Unknown likes held'], ['duplicate_like_skipped_tests', 'Repeated likes skipped']].flatMap(([key, label]) => Number.isSafeInteger(job[key]) && job[key] >= 0 ? [[label, formatNumber(job[key])]] : []) : []),
      ['Account failures', formatNumber(job.account_failed || 0)],
      ['Other failures', formatNumber(Math.max(0, (Number(job.failed) || 0) - (Number(job.account_failed) || 0)))],
      ['Connection pending', formatNumber(job.connection_pending || 0)],
      ['Session review pending', formatNumber(Number.isSafeInteger(job.session_review_pending) && job.session_review_pending >= 0 ? job.session_review_pending : 0)],
      ...(job.action === 'like' ? [['Verification pending', formatNumber(job.verification_pending || 0)]] : []),
      [isActive(job) ? 'Waiting' : 'Unattempted', formatNumber(isActive(job) ? pending : job.skipped)],
      ...(requestedWorkers === null ? [] : [['Workers requested', formatNumber(requestedWorkers)]]),
      ['Workers active', effectiveWorkers === null ? formatNumber(active) : `${formatNumber(active)} / ${formatNumber(effectiveWorkers)}`],
      ['Failure streak', `${formatNumber(job.consecutive_failures)} / ${formatNumber(job.max_consecutive_failures)}`],
      ['Provider retries', formatNumber(job.provider_retries || 0)],
      ['Provider waiting', formatNumber(providerWaiting)],
    ];
    $('job-overview').replaceChildren(...metrics.map(([label, value]) => {
      const item = node('div', 'run-stat'); item.append(node('span', '', label), node('strong', '', value)); return item;
    }));
    const route = job.proxy_test_session ? `PacketStream sticky pool${Number.isSafeInteger(job.proxy_pool_size) ? ` · ${formatNumber(job.proxy_pool_size)} routes` : ''}` : job.proxy_egypt ? 'Egypt proxy' : 'Direct';
    $('job-test-metrics').textContent = `${durationLabel(elapsed)} elapsed · ${route}${elapsed >= 1 && finished ? ` · ${(finished / elapsed * 60).toFixed(1)} tests/min average` : ''}${Number(job.retried) > 0 ? ` · ${formatNumber(job.retried)} tests needed a connection retry` : ''}${isActive(job) ? ' · Updates every second' : ''}`;
    $('job-active-count').textContent = `${active} active`;
    const activeTests = Array.isArray(job.active_tests) ? job.active_tests : (job.active_rows || []).map(source_row => ({source_row}));
    $('job-active-tests').replaceChildren();
    activeTests.forEach(test => {
      const item = node('div', 'active-test');
      const status = test.connection_status === 'waiting' ? 'Waiting for provider' : test.connection_status === 'retrying' ? 'Retrying connection' : 'Running';
      item.append(node('span', 'spinner'), node('strong', '', `Account ${test.source_row}${test.test_number ? ` · Run ${test.test_number} of ${job.count}` : ''}${Number.isSafeInteger(test.proxy_route_number) ? ` · Route ${test.proxy_route_number}` : ''}${Number.isInteger(test.provider_attempts) ? ` · Attempt ${test.provider_attempts} / 3` : ''}`), node('span', '', `${status}${test.started_at ? ` · ${durationLabel(elapsedBetween(test.started_at))}` : ''}`));
      $('job-active-tests').appendChild(item);
    });
    if (!activeTests.length) $('job-active-tests').appendChild(node('p', 'activity-hint', isActive(job) ? 'Waiting for the next test to start…' : 'No tests are running.'));
    const filter = $('job-result-filter').value;
    const results = (Array.isArray(job.results) ? job.results : []).slice().reverse().filter(result => {
      const [, outcome] = resultStatus(result);
      return filter === 'all' || filter === 'pending' && ['Connection pending', 'Verification pending', 'Session review pending', 'Like history pending'].includes(outcome) || filter === 'history_skipped' && outcome === 'Already liked (saved history)' || filter === 'session_review_pending' && outcome === 'Session review pending' || filter === 'verification_pending' && outcome === 'Verification pending' || filter === 'unknown' && outcome === 'Unknown' || filter === 'failed' && ['Failed', 'Account failed', 'Unknown'].includes(outcome) || filter === 'passed' && ['Passed', 'Accepted', 'Already liked', 'Already liked (saved history)'].includes(outcome);
    });
    const shown = results.slice(0, 50);
    $('job-result-count').textContent = `${shown.length} shown${results.length > 50 ? ` of ${results.length} matching` : ''} · newest first`;
    renderResultList($('job-results'), shown, {showBandwidth: true});
    $('job-results-empty').hidden = shown.length > 0;
    $('job-results-empty').textContent = filter !== 'all' ? 'No completed results match this filter.' : isActive(job) ? 'Results appear as each test finishes.' : 'No completed test results.';
    $('job-results-retention').hidden = !(Number(job.results_total) > shown.length);
    $('job-results-retention').textContent = `This run has ${formatNumber(job.results_total)} saved test results. The live view keeps the latest 500 and displays up to 50 matching results. Full per-test reports are saved locally.`;
  }

  function renderJob(job) {
    if (job && currentJob?.id !== job.id) {
      $('job-result-filter').value = 'all';
      showError('job-control-error', ''); showError('prepare-control-error', '');
    }
    currentJob = job || null;
    updateStopControl();
    const hasJob = !!job;
    $('job-empty').hidden = hasJob;
    $('job-content').hidden = !hasJob;
    if (!hasJob) {
      $('job-status').className = 'badge neutral'; $('job-status').textContent = 'Idle';
      renderRunVisibility(null);
      setBusy(uncertainSubmission);
      return;
    }
    $('job-title').textContent = actionNames[job.action] || humanLabel(job.action || 'Run');
    const active = isActive(job);
    const statusClass = active ? 'running' : job.status === 'succeeded' ? 'success' : ['completed_with_failures', 'completed_with_pending', 'stopped'].includes(job.status) ? 'warning' : 'error';
    $('job-status').className = 'badge ' + statusClass;
    $('job-status').textContent = active ? humanLabel(job.status) : job.status === 'succeeded' ? 'Completed' : job.status === 'completed_with_failures' ? 'Completed with failures' : job.status === 'completed_with_pending' ? completedPendingLabel(job) : 'Stopped';
    const message = job.message || (active ? 'Your run is in progress.' : job.status === 'succeeded' ? 'The run completed. Review the results below.' : job.status === 'completed_with_pending' ? 'All planned checks finished. Review the pending results below.' : 'The run stopped. Review the result before starting another test.');
    const writeSummary = !active && ['play', 'like'].includes(job.action) ? actionWriteSummary(job) : '';
    $('job-message').textContent = writeSummary && !message.startsWith(writeSummary) ? `${writeSummary} ${message}` : message;
    const total = Number(job.progress?.total) || 0;
    const finished = resolvedCheckCount(job);
    const completed = Math.min(total, finished);
    $('job-phase').textContent = humanLabel(job.phase || (active ? 'Working' : job.status));
    $('job-progress-label').textContent = total ? `${completed} / ${total}${['play', 'like'].includes(job.action) ? ' checks finished' : ''}` : active ? 'In progress' : 'Complete';
    $('job-progress-bar').style.width = `${total ? Math.max(0, Math.min(100, completed / total * 100)) : active ? 12 : job.status === 'succeeded' ? 100 : 0}%`;
    const showMetrics = ['play', 'like'].includes(job.action) && Number.isSafeInteger(job.attempted);
    $('job-test-metrics').hidden = !showMetrics;
    const error = typeof job.error === 'string' ? job.error : job.error?.message;
    showError('job-error', error || '');
    $('job-error').classList.toggle('warning', job.status === 'completed_with_pending');
    $('job-error').classList.toggle('error', job.status !== 'completed_with_pending');
    if (!showMetrics) renderResultList($('job-results'), job.results);
    if (job.action === 'prepare' && Number.isSafeInteger(job.connection_pending)) {
      $('job-test-metrics').hidden = false;
      const workerCount = Number.isSafeInteger(job.requested_workers) && job.requested_workers >= 1 ? job.requested_workers : Number.isSafeInteger(job.workers) && job.workers >= 1 ? job.workers : null;
      const effectiveCount = Number.isSafeInteger(job.effective_workers) && job.effective_workers >= 0 && (workerCount === null || job.effective_workers <= workerCount) ? job.effective_workers : null;
      const activeLimit = effectiveCount === null ? workerCount : effectiveCount;
      const activeCount = activeLimit !== null && Number.isSafeInteger(job.active_workers) && job.active_workers >= 0 && job.active_workers <= activeLimit ? job.active_workers : null;
      const heldCount = Number.isSafeInteger(job.preparation_held) && job.preparation_held >= 0 ? job.preparation_held : null;
      const infrastructureCount = Number.isSafeInteger(job.preparation_infrastructure_failed) && job.preparation_infrastructure_failed >= 0 ? job.preparation_infrastructure_failed : null;
      const failureLabel = infrastructureCount === null && Number.isSafeInteger(job.account_failed) && job.account_failed > 0 ? 'preparation errors' : 'account failures';
      $('job-test-metrics').textContent = `${formatNumber(job.progress?.completed || 0)} prepared · ${formatNumber(job.account_failed || 0)} ${failureLabel}${infrastructureCount === null ? '' : ` · ${formatNumber(infrastructureCount)} infrastructure errors`} · ${formatNumber(job.connection_pending)} connection pending${heldCount === null ? '' : ` · ${formatNumber(heldCount)} held for review`}${workerCount === null ? '' : ` · ${formatNumber(workerCount)} worker${workerCount === 1 ? '' : 's'} requested`}${effectiveCount === null ? '' : ` · ${formatNumber(effectiveCount)} effective`}${activeCount === null ? '' : ` · ${formatNumber(activeCount)} active`}`;
    }
    renderRunVisibility(job);
    $('job-json').textContent = JSON.stringify(job, null, 2);
    $('busy-banner-message').textContent = `${actionNames[job.action] || 'A run'} is in progress${total ? ` · ${completed} of ${total}` : ''}`;
    setBusy(active || uncertainSubmission);
  }
  function updateStopControl() {
    const button = $('stop-job');
    const prepareButton = $('stop-prepare');
    const preparation = currentJob?.action === 'prepare';
    const available = isActive(currentJob) && ['play', 'like', 'prepare'].includes(currentJob.action);
    const preparationSupported = state?.job_controls?.stop_preparation === true;
    const stopping = currentJob?.stop_requested === true || stopRequestPending;
    for (const control of [button, prepareButton]) {
      if (!control) continue;
      control.hidden = !available || (control === prepareButton && !preparation);
      control.disabled = !available || stopping || (preparation && !preparationSupported);
      control.textContent = stopping ? 'Stopping…' : preparation ? 'Stop preparation' : 'Stop run';
    }
    for (const hint of [$('prepare-stop-hint'), $('job-stop-hint')]) {
      if (!hint) continue;
      hint.hidden = !preparation || !available;
      hint.textContent = !preparationSupported
        ? 'The running console needs a restart to enable Stop preparation. Restart it after this run finishes.'
        : stopping
          ? 'Stop requested. Waiting for active accounts to finish and save their sessions.'
          : 'Stop starts no more accounts. Active accounts finish safely and keep their saved sessions.';
    }
    const status = $('prepare-run-status');
    if (status) {
      status.hidden = !preparation;
      const completed = Math.max(0, Number(currentJob?.progress?.completed) || 0);
      const total = Math.max(0, Number(currentJob?.progress?.total) || 0);
      const active = Number.isSafeInteger(currentJob?.active_workers) && currentJob.active_workers >= 0 ? currentJob.active_workers : null;
      status.textContent = !preparation ? '' : stopping && available
        ? `Stopping preparation · ${formatNumber(completed)} prepared${active === null ? '' : ` · ${formatNumber(active)} accounts finishing`}`
        : `${available ? 'Preparing accounts' : currentJob.status === 'succeeded' ? 'Preparation complete' : 'Preparation stopped'} · ${formatNumber(completed)}${total ? ` / ${formatNumber(total)}` : ''} prepared`;
    }
  }
  async function stopCurrentJob() {
    if (!isActive(currentJob) || !['play', 'like', 'prepare'].includes(currentJob.action) || currentJob.stop_requested === true || stopRequestPending) return;
    if (currentJob.action === 'prepare' && state?.job_controls?.stop_preparation !== true) return;
    const jobId = currentJob.id;
    const preparation = currentJob.action === 'prepare';
    if (!/^[0-9a-f]{32}$/.test(jobId || '')) return;
    stopRequestPending = true; updateStopControl();
    showError('job-control-error', ''); showError('prepare-control-error', '');
    try {
      const response = await api('/api/jobs/stop', {method: 'POST', body: JSON.stringify({job_id: jobId})});
      const job = response?.job || response;
      if (currentJob?.id !== jobId) return;
      if (!job || job.id !== jobId) throw new Error('The stop result could not be confirmed. Refresh the run status.');
      renderJob(job);
      if (isActive(job)) schedulePoll(); else await refreshState();
    } catch (error) {
      if (currentJob?.id === jobId) {
        showError('job-control-error', error.message);
        if (preparation) showError('prepare-control-error', error.message);
      }
    } finally { stopRequestPending = false; updateStopControl(); }
  }

  function renderState() {
    $('stat-imported').textContent = formatNumber(state.vault?.records ?? state.vault?.record_count ?? state.vault?.total_records);
    const unique = state.vault?.unique_accounts ?? state.vault?.unique_emails;
    $('stat-unique').textContent = unique !== undefined ? `${formatNumber(unique)} unique accounts` : 'From registered.txt';
    $('stat-ready').textContent = formatNumber(readyRows().length);
    $('nav-account-count').textContent = formatNumber(readyRows().length);
    $('stat-reports').textContent = formatNumber(state.reports?.length || 0);
    if (!songDirty) $('song-id').value = state.test_song_id || '';
    updateSongState();
    $('test-count').max = String(limits().tests);
    $('test-workers').removeAttribute?.('max');
    $('test-failure-limit').removeAttribute?.('max');
    document.querySelectorAll('[data-account-limit]').forEach(element => { element.textContent = String(limits().accounts); });
    document.querySelectorAll('[data-test-account-limit]').forEach(element => { element.textContent = String(limits().testAccounts); });
    $('proxy-configured-badge').textContent = state.proxy?.configured ? 'Credentials saved' : 'Not configured';
    $('proxy-configured-badge').className = 'badge ' + (state.proxy?.configured ? 'success' : 'neutral');
    $('connection-status').classList.remove('offline');
    $('connection-status').replaceChildren(node('span', 'status-dot'), document.createTextNode('Local console connected'));
    renderAccounts(); renderReportLists();
    if (state.job) renderJob(state.job);
    else if (!currentJob || isActive(currentJob)) renderJob(null);
    setBusy(isActive(currentJob) || uncertainSubmission);
  }

  async function refreshState() {
    if (refreshing) return;
    refreshing = true; $('refresh-state').disabled = true;
    try {
      state = await api('/api/state');
      uncertainSubmission = false;
      renderState(); showError('global-error', '');
      if (isActive(currentJob)) schedulePoll();
    } catch (error) {
      showError('global-error', error.message);
      $('connection-status').classList.add('offline');
      $('connection-status').replaceChildren(node('span', 'status-dot'), document.createTextNode('Console unavailable'));
      if (state) state.preparation_availability = {available: false};
      preparationAvailabilityLookup = null;
      updatePreparationCountry();
    } finally { refreshing = false; $('refresh-state').disabled = false; }
  }

  function schedulePoll() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = setTimeout(pollJob, 1000);
  }

  async function pollJob() {
    pollTimer = null;
    try {
      const response = await api('/api/job');
      const job = response && Object.prototype.hasOwnProperty.call(response, 'job') ? response.job : response;
      uncertainSubmission = false;
      renderJob(job); showError('global-error', '');
      if (isActive(job)) schedulePoll();
      else await refreshState();
    } catch (error) {
      showError('global-error', `${error.message} The last run status is retained; do not submit the same test again until it is confirmed.`);
      uncertainSubmission = true;
      setBusy(true);
      $('busy-banner-message').textContent = 'Run status could not be confirmed. Refresh the workspace.';
    }
  }

  function requireProxyReady(scope) {
    if (sessionMode(scope)) {
      if (testSessionNeedsSave || $('test-session-route').value.trim()) throw new Error('Save the pasted PacketStream links before running a test.');
      if (!state?.test_proxy?.configured) throw new Error('Paste and save PacketStream Egypt or US sticky links before testing.');
      return;
    }
    if (stickyMode(scope)) {
      if (preparationStickyDraft()) throw new Error('Save the pasted preparation links or choose Use saved routes before preparing accounts.');
      if (!state?.sticky_pool?.configured) throw new Error('Paste and save PacketStream Egypt or US sticky links before preparing accounts.');
      return;
    }
    if (proxyMode(scope) && !stickyMode(scope) && !state?.proxy?.configured) throw new Error('Save your PacketStream credentials in Proxy settings before using Egypt routing.');
  }

  function selectedTestRows() {
    const rows = [...selectedRows].sort((a, b) => a - b);
    if (!rows.length) throw new Error('Select at least one ready account.');
    if (rows.length > limits().testAccounts || rows.some(row => !filteredReadyRows().includes(row))) throw new Error('Choose only ready accounts matching the registered country within the account limit.');
    return rows;
  }

  async function submitJob(payload, errorId) {
    if (busy || requestPending || !state) return;
    showError(errorId, ''); showError('global-error', '');
    requestPending = true; setBusy(false);
    try {
      const response = await api('/api/jobs', {method: 'POST', body: JSON.stringify(payload)});
      uncertainSubmission = false;
      renderJob(response.job || response);
      switchPanel('workbench');
      if (isActive(currentJob)) schedulePoll(); else await refreshState();
    } catch (error) {
      if (error.network || !error.status || error.status >= 500) {
        uncertainSubmission = true;
        showError('global-error', 'The request was sent, but its result could not be confirmed. Checking the run status before allowing another test.');
        schedulePoll();
      } else {
        showError(errorId, error.message);
        if (error.status === 409) await refreshState();
      }
    } finally { requestPending = false; setBusy(isActive(currentJob) || uncertainSubmission); }
  }

  async function startTest(action) {
    try {
      if (['play', 'like', 'song'].includes(action) && songDirty) throw new Error('Save the changed song ID before running a song test.');
      requireProxyReady('tests');
      const rows = selectedTestRows();
      const count = ['play', 'like'].includes(action) ? integer($('test-count').value, 1, limits().tests, 'Tests per account') : 1;
      const payload = {action, rows, count, proxy_egypt: proxyMode('tests')};
      if (sessionMode('tests')) payload.proxy_test_session = true;
      if (['play', 'like'].includes(action)) {
        payload.workers = integer($('test-workers').value, 1, Number.MAX_SAFE_INTEGER, 'Concurrent workers');
        payload.max_consecutive_failures = integer($('test-failure-limit').value, 1, Number.MAX_SAFE_INTEGER, 'Consecutive failure limit');
      }
      if (action === 'play' && $('test-with-audio')?.checked) payload.with_audio = true;
      if (['play', 'like', 'song'].includes(action)) payload.song_id = String(state.test_song_id);
      await submitJob(payload, 'workbench-error');
    } catch (error) { showError('workbench-error', error.message); }
  }

  async function startPreparation(action) {
    try {
      const country = $('prepare-country')?.value || '';
      if (country && !['EG', 'LB'].includes(country)) throw new Error('Choose Egypt, Lebanon or any country for registered accounts.');
      const availability = preparationAvailability();
      if (!availability.known) throw new Error(availability.invalidRow ? 'Starting row must be a positive whole number.' : 'Remaining account count is unavailable. Refresh the workspace before preparing accounts.');
      if (availability.remaining < 1) throw new Error('No eligible accounts are left to prepare for this selection.');
      const count = integer($('prepare-count').value, 1, availability.remaining, 'Accounts to add');
      if (action !== 'preview') requireProxyReady('prepare');
      const noBrowser = noBrowserPreparation();
      const payload = {action, count, browser: $('browser').value, headless: !noBrowser && $('headless').checked, proxy_egypt: proxyMode('prepare'), proxy_sticky_pool: stickyMode('prepare'), reduce_browser_data: !noBrowser && $('prepare-reduce-browser-data').checked, no_browser: noBrowser};
      if (action === 'prepare' && noBrowser) payload.workers = country ? integer($('prepare-workers')?.value ?? '1', 1, Number.MAX_SAFE_INTEGER, 'Preparation workers') : 1;
      if (country) payload.account_country = country;
      else if ($('start-row').value.trim()) payload.start_row = integer($('start-row').value, 1, Number.MAX_SAFE_INTEGER, 'Starting row');
      await submitJob(payload, 'accounts-error');
    } catch (error) { showError('accounts-error', error.message); }
  }

  async function startLogin() {
    try {
      requireProxyReady('login');
      const raw = $('login-rows').value.trim();
      if (!raw || !/^\d+(?:\s*,\s*\d+)*$/.test(raw)) throw new Error('Enter account rows as whole numbers separated by commas, such as 7, 8.');
      const rows = [...new Set(raw.split(',').map(value => integer(value, 1, Number.MAX_SAFE_INTEGER, 'Account row')))];
      if (rows.some(row => heldSessionRows().has(row))) throw new Error('Use Check saved sessions in Session review for held accounts. A new login is not part of that check.');
      if (rows.length > limits().accounts) throw new Error(`Select up to ${limits().accounts} rows for a session refresh.`);
      await submitJob({action: 'login', rows, count: 1, browser: $('browser').value, headless: $('headless').checked, proxy_egypt: proxyMode('login'), reduce_browser_data: $('login-reduce-browser-data').checked}, 'login-error');
    } catch (error) { showError('login-error', error.message); }
  }

  async function startReviewPreparation() {
    try {
      if (busy || requestPending || !state) return;
      const available = new Set(failureReviewAccounts().map(account => account.source_row));
      const rows = [...reviewRows].sort((a, b) => a - b);
      if (!rows.length || rows.length > limits().accounts || rows.some(row => !available.has(row))) {
        throw new Error(`Choose 1–${limits().accounts} accounts from the failed-account review list.`);
      }
      requireProxyReady('prepare');
      const noBrowser = noBrowserPreparation();
      await submitJob({
        action: 'prepare', review_rows: rows, count: rows.length, browser: $('browser').value,
        headless: !noBrowser && $('headless').checked, proxy_egypt: proxyMode('prepare'),
        proxy_sticky_pool: stickyMode('prepare'), reduce_browser_data: !noBrowser && $('prepare-reduce-browser-data').checked,
        no_browser: noBrowser,
      }, 'review-error');
    } catch (error) { showError('review-error', error.message); }
  }

  async function startSessionReview() {
    try {
      if (busy || requestPending || !state) return;
      const available = new Set(sessionReviewAccounts().map(account => account.source_row));
      const rows = [...sessionReviewRows].sort((a, b) => a - b);
      if (!rows.length || rows.length > limits().accounts || rows.some(row => !available.has(row))) throw new Error(`Choose 1–${limits().accounts} held accounts from Session review.`);
      if (!['direct', 'egypt'].includes($('session-review-connection-mode').value)) throw new Error('Choose Direct or Egypt for read-only session review.');
      requireProxyReady('sessionReview');
      await submitJob({action: 'review-sessions', rows, proxy_egypt: proxyMode('sessionReview')}, 'session-review-error');
    } catch (error) { showError('session-review-error', error.message); }
  }

  async function loadReport(name) {
    selectedReport = name; renderReportLists(); showError('report-error', '');
    $('report-empty').hidden = true; $('report-detail').hidden = true;
    try {
      const response = await api('/api/reports?name=' + encodeURIComponent(name));
      if (selectedReport !== name) return;
      const report = response.report || {};
      $('report-name').textContent = response.name || name;
      $('report-json').textContent = JSON.stringify(report, null, 2);
      const [color, label] = resultStatus(report);
      $('report-badge').className = 'badge ' + color; $('report-badge').textContent = label;
      $('report-summary').replaceChildren();
      if (report.source_row !== undefined) $('report-summary').appendChild(node('p', '', `Account row ${report.source_row}${report.song_id ? ` · Song ${report.song_id}` : ''}`));
      $('report-summary').appendChild(node('p', '', readableMessage(report.message) || readableMessage(report.reason) || reportMessage(report)));
      if (['play', 'like'].includes(report.action) || transferUsage(report)) $('report-summary').appendChild(bandwidthPanel(report));
      if (report.results) {
        const results = node('div', 'result-list'); renderResultList(results, report.results, {showBandwidth: ['play', 'like'].includes(report.action)}); $('report-summary').appendChild(results);
      }
      $('report-detail').hidden = false;
    } catch (error) { if (selectedReport === name) showError('report-error', error.message); }
  }

  document.querySelectorAll('[data-panel]').forEach(button => button.addEventListener('click', () => switchPanel(button.dataset.panel)));
  document.querySelectorAll('[data-go-panel]').forEach(button => button.addEventListener('click', () => switchPanel(button.dataset.goPanel)));
  document.querySelector('.brand').addEventListener('click', event => { event.preventDefault(); switchPanel('workbench'); });
  Object.entries(connectionScopes).forEach(([name, scope]) => {
    $(scope.selector).addEventListener('change', () => {
      if (stickyMode(name)) { $('prepare-method').value = 'http'; updatePreparationMethod(); }
      updateSelections();
      showError(name === 'tests' ? 'workbench-error' : name === 'prepare' ? 'accounts-error' : 'login-error', '');
    });
  });
  $('test-session-route').addEventListener('input', () => {
    testSessionNeedsSave = !!$('test-session-route').value.trim();
    $('test-session-saved').hidden = true;
    showError('test-session-error', '');
    updateSelections();
  });
  $('show-test-session-links').addEventListener('change', updateRouteMask);
  $('save-test-session').addEventListener('click', async () => {
    if (busy || requestPending || !state) return;
    const route = $('test-session-route').value.trim();
    const lines = pastedRouteCount();
    if (!route || lines > 10000 || new TextEncoder().encode(route).length > 1024 * 1024) {
      showError('test-session-error', 'Paste 1–10,000 PacketStream Egypt or US sticky links, one per line, up to 1 MiB.');
      return;
    }
    requestPending = true; setBusy(false); showError('test-session-error', '');
    $('test-session-saved').hidden = true;
    try {
      const response = await api('/api/test-proxy', {method: 'POST', body: JSON.stringify({route})});
      if (response.configured !== true) throw new Error('The session was not confirmed.');
      testSessionNeedsSave = false;
      $('test-session-route').value = '';
      const savedCount = Math.max(1, Number(response.pool_size) || 1);
      $('test-session-saved').textContent = `${formatNumber(savedCount)} unique sticky route${savedCount === 1 ? '' : 's'} saved securely for tests. Routes cycle across accounts; each account keeps its route for the run.`;
      $('test-session-saved').hidden = false;
      await refreshState();
    } catch (_) {
      testSessionNeedsSave = true;
      showError('test-session-error', 'The links could not be saved. Check that every nonempty line is a valid PacketStream Egypt or US sticky address and paste the list again.' + (state?.test_proxy?.configured ? ' Choose Use saved routes to keep the previous list.' : ''));
    } finally { $('test-session-route').value = ''; $('show-test-session-links').checked = false; updateRouteMask(); requestPending = false; setBusy(isActive(currentJob) || uncertainSubmission); }
  });
  $('discard-test-session').addEventListener('click', () => {
    if (busy || requestPending || !state?.test_proxy?.configured) return;
    $('test-session-route').value = '';
    $('show-test-session-links').checked = false;
    updateRouteMask();
    testSessionNeedsSave = false;
    showError('test-session-error', '');
    $('test-session-saved').hidden = true;
    updateSelections();
  });
  $('prepare-sticky-routes').addEventListener('input', () => {
    if ($('prepare-sticky-routes').value.trim()) prepareStickyNeedsSave = true;
    $('prepare-sticky-saved').hidden = true;
    showError('prepare-sticky-error', '');
    updateSelections();
  });
  $('prepare-sticky-show-links').addEventListener('change', updatePreparationStickyMask);
  $('prepare-sticky-save').addEventListener('click', async () => {
    if (busy || requestPending || !state) return;
    const routes = $('prepare-sticky-routes').value.trim();
    const lines = preparationStickyLineCount();
    prepareStickyNeedsSave = true;
    $('prepare-sticky-saved').hidden = true;
    if (!routes || lines > 10000 || new TextEncoder().encode(routes).length > 1024 * 1024) {
      $('prepare-sticky-routes').value = '';
      $('prepare-sticky-show-links').checked = false;
      showError('prepare-sticky-error', 'Paste 1–10,000 PacketStream Egypt or US sticky links, one per line, up to 1 MiB.');
      updateSelections();
      return;
    }
    requestPending = true; setBusy(false); showError('prepare-sticky-error', '');
    let poolSaved = false;
    try {
      const response = await api('/api/preparation-proxy', {method: 'POST', body: JSON.stringify({routes})});
      if (response.configured !== true || !Number.isSafeInteger(response.pool_size) || response.pool_size < 1 || response.pool_size > 10000 || !['EG', 'US'].includes(response.country)) throw new Error('The pool was not confirmed.');
      state.sticky_pool = {configured: true, provider: 'PacketStream', country: response.country, sticky: true, pool_size: response.pool_size};
      prepareStickyNeedsSave = false;
      $('prepare-sticky-routes').value = '';
      $('prepare-sticky-saved').textContent = `${formatNumber(response.pool_size)} unique ${response.country} sticky route${response.pool_size === 1 ? '' : 's'} saved securely for preparation. Each route is checked during preparation. Test routes are saved separately.`;
      $('prepare-sticky-saved').hidden = false;
      poolSaved = true;
    } catch (_) {
      prepareStickyNeedsSave = true;
      showError('prepare-sticky-error', 'The preparation links could not be saved. Check that every nonempty line is a valid PacketStream Egypt or US sticky address and paste the list again.' + (state?.sticky_pool?.configured ? ' Choose Use saved routes to keep the previous list.' : ''));
    } finally {
      $('prepare-sticky-routes').value = '';
      $('prepare-sticky-show-links').checked = false;
      updatePreparationStickyMask();
      if (poolSaved) {
        try { await refreshState(); }
        catch (_) { showError('global-error', 'Preparation routes are saved. Refresh the workspace to update its status.'); }
      }
      requestPending = false; setBusy(isActive(currentJob) || uncertainSubmission);
    }
  });
  $('prepare-sticky-discard').addEventListener('click', () => {
    if (busy || requestPending || !state?.sticky_pool?.configured) return;
    $('prepare-sticky-routes').value = '';
    $('prepare-sticky-show-links').checked = false;
    $('prepare-sticky-saved').hidden = true;
    prepareStickyNeedsSave = false;
    showError('prepare-sticky-error', '');
    updateSelections();
  });
  $('prepare-sticky-remove').addEventListener('click', async () => {
    if (busy || requestPending || !state?.sticky_pool?.configured) return;
    requestPending = true; setBusy(false); showError('prepare-sticky-error', '');
    let poolRemoved = false;
    try {
      const response = await api('/api/preparation-proxy/remove', {method: 'POST', body: JSON.stringify({})});
      if (response.configured !== false || response.pool_size !== 0) throw new Error('Removal was not confirmed.');
      state.sticky_pool = {configured: false, pool_size: 0};
      prepareStickyNeedsSave = false;
      $('prepare-sticky-routes').value = '';
      $('prepare-sticky-show-links').checked = false;
      $('prepare-sticky-saved').textContent = 'Preparation route pool removed.';
      $('prepare-sticky-saved').hidden = false;
      poolRemoved = true;
    } catch (_) {
      showError('prepare-sticky-error', 'The preparation route pool could not be removed. Check the local console and try again.');
    } finally {
      if (poolRemoved) {
        updatePreparationStickyMask();
        try { await refreshState(); }
        catch (_) { showError('global-error', 'Preparation routes are removed. Refresh the workspace to update its status.'); }
      }
      requestPending = false; setBusy(isActive(currentJob) || uncertainSubmission);
    }
  });
  $('job-result-filter').addEventListener('change', () => renderRunVisibility(currentJob));
  $('review-select-all').addEventListener('click', () => {
    if (busy || requestPending || !state) return;
    reviewRows.clear();
    failureReviewAccounts().slice(0, limits().accounts).forEach(account => reviewRows.add(account.source_row));
    showError('review-error', ''); updateReviewSelection();
  });
  $('review-clear').addEventListener('click', () => {
    if (busy || requestPending) return;
    reviewRows.clear(); showError('review-error', ''); updateReviewSelection();
  });
  $('prepare-review-accounts').addEventListener('click', startReviewPreparation);
  $('session-review-select-all').addEventListener('click', () => {
    if (busy || requestPending || !state) return;
    sessionReviewRows.clear(); sessionReviewAccounts().slice(0, limits().accounts).forEach(account => sessionReviewRows.add(account.source_row));
    showError('session-review-error', ''); updateSessionReviewSelection();
  });
  $('session-review-clear').addEventListener('click', () => {
    if (busy || requestPending || !state) return;
    sessionReviewRows.clear(); showError('session-review-error', ''); updateSessionReviewSelection();
  });
  $('check-review-sessions').addEventListener('click', startSessionReview);
  $('song-id').addEventListener('input', () => {
    songSavedMessage = '';
    updateSongState(); updateSelections(); showError('workbench-error', '');
  });
  $('save-song').addEventListener('click', async () => {
    if (busy || requestPending || !state) return;
    const value = $('song-id').value.trim();
    if (!validSongId(value)) {
      showError('workbench-error', 'Enter a positive song ID using digits only, without a leading zero, up to 9223372036854775807.');
      return;
    }
    requestPending = true; setBusy(false); showError('workbench-error', '');
    try {
      const response = await api('/api/test-song', {method: 'POST', body: JSON.stringify({song_id: value})});
      const canonical = String(response.test_song_id || '');
      if (!validSongId(canonical)) throw new Error('The console did not confirm a valid configured song ID. Refresh the workspace before testing.');
      state.test_song_id = canonical;
      $('song-id').value = canonical;
      songDirty = false;
      songSavedMessage = 'Test song saved. Future tests use this ID; no test event was sent.';
      await refreshState();
    } catch (error) { showError('workbench-error', error.message); }
    finally { requestPending = false; setBusy(isActive(currentJob) || uncertainSubmission); }
  });
  $('test-count').addEventListener('input', updateSelections);
  $('test-workers').addEventListener('input', updateSelections);
  $('test-failure-limit').addEventListener('input', updateSelections);
  $('prepare-method').addEventListener('change', () => {
    if (!noBrowserPreparation() && stickyMode('prepare')) $('prepare-connection-mode').value = 'direct';
    updatePreparationMethod(); updateProxyMode(); showError('accounts-error', '');
  });
  $('prepare-country').addEventListener('change', () => {
    preparationAvailabilityLookup = null;
    updatePreparationCountry(); updatePreparationMethod(); showError('accounts-error', '');
  });
  $('start-row').addEventListener('input', () => { preparationAvailabilityLookup = null; updatePreparationCountry(); showError('accounts-error', ''); });
  $('prepare-count').addEventListener('input', () => { updatePreparationCountry(false); showError('accounts-error', ''); });
  $('prepare-workers').addEventListener('input', () => {
    updatePreparationMethod(); showError('accounts-error', '');
  });
  $('test-account-country').addEventListener('change', () => {
    if (busy || requestPending || !state) return;
    firstSelection = false;
    renderAccounts(); showError('selection-error', ''); showError('workbench-error', '');
  });
  function replaceSelection(rows) {
    selectedRows.clear();
    rows.forEach(row => selectedRows.add(row));
    showError('selection-error', ''); showError('workbench-error', ''); updateSelections();
  }
  $('select-all-accounts').addEventListener('click', () => {
    if (busy || requestPending || !state) return;
    try { replaceSelection(ConsoleSelection.selectAllRows(filteredReadyRows(), limits().testAccounts)); }
    catch (error) { showError('selection-error', error.message); }
  });
  $('select-not-liked-accounts').addEventListener('click', () => {
    if (busy || requestPending || !state || songDirty) return;
    try { replaceSelection(ConsoleSelection.selectAllRows(notYetLikedRows(), limits().testAccounts)); }
    catch (error) { showError('selection-error', error.message); }
  });
  $('select-random-accounts').addEventListener('click', () => {
    if (busy || requestPending || !state) return;
    try {
      const maximum = Math.min(filteredReadyRows().length, limits().testAccounts);
      const count = validateRandomCount(maximum);
      replaceSelection(ConsoleSelection.selectRandomRows(filteredReadyRows(), count, limits().testAccounts));
    } catch (error) { showError('selection-error', error.message); }
  });
  $('clear-accounts').addEventListener('click', () => {
    if (!busy && !requestPending && state) replaceSelection([]);
  });
  $('random-count').addEventListener('input', () => {
    const maximum = Math.min(filteredReadyRows().length, limits().testAccounts);
    try { validateRandomCount(maximum); showError('selection-error', ''); }
    catch (error) { showError('selection-error', error.message, 'random-count'); }
    updateSelections();
  });
  $('count-minus').addEventListener('click', () => { $('test-count').value = String(Math.max(1, (Number($('test-count').value) || 1) - 1)); updateSelections(); });
  $('count-plus').addEventListener('click', () => { $('test-count').value = String(Math.min(limits().tests, (Number($('test-count').value) || 1) + 1)); updateSelections(); });
  $('run-play').addEventListener('click', () => startTest('play'));
  $('run-like').addEventListener('click', () => startTest('like'));
  $('stop-job').addEventListener('click', stopCurrentJob);
  $('stop-prepare').addEventListener('click', stopCurrentJob);
  $('check-sessions').addEventListener('click', () => startTest('check'));
  $('check-song').addEventListener('click', () => startTest('song'));
  $('preview-accounts').addEventListener('click', () => startPreparation('preview'));
  $('prepare-accounts').addEventListener('click', () => startPreparation('prepare'));
  $('refresh-sessions').addEventListener('click', startLogin);
  $('refresh-state').addEventListener('click', refreshState);
  $('refresh-reports').addEventListener('click', refreshState);
  $('login-rows').addEventListener('input', () => {
    refreshRows.clear();
    $('login-rows').value.split(',').forEach(value => { const number = Number(value.trim()); if (Number.isSafeInteger(number) && number > 0) refreshRows.add(number); });
    document.querySelectorAll('#cohort-table input').forEach(input => { input.checked = refreshRows.has(Number(input.value)); });
    updateSelections();
  });
  $('find-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button');
    if (button.disabled) return;
    button.disabled = true; $('find-result').hidden = false; $('find-result').textContent = 'Looking up this account…';
    try {
      const response = await api('/api/find', {method: 'POST', body: JSON.stringify({email: $('find-email').value.trim()})});
      const rows = response.source_rows || [];
      $('find-result').textContent = rows.length ? `Found in source row${rows.length === 1 ? '' : 's'} ${rows.join(', ')}.` : 'This email was not found in the imported accounts.';
    } catch (error) { $('find-result').textContent = error.message; }
    finally { button.disabled = false; }
  });
  $('proxy-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || requestPending) return;
    showError('proxy-error', ''); $('proxy-saved').hidden = true;
    const username = $('proxy-username').value.trim();
    const authKey = $('proxy-key').value.trim();
    if (!username || !authKey) { showError('proxy-error', 'Enter both the proxy username and auth key.'); return; }
    requestPending = true; setBusy(false);
    try {
      await api('/api/proxy', {method: 'POST', body: JSON.stringify({username, auth_key: authKey})});
      $('proxy-username').value = ''; $('proxy-key').value = '';
      $('proxy-saved').textContent = 'Credentials saved locally. Choose Egypt beside the test, preparation, or session refresh you want to run.';
      $('proxy-saved').hidden = false;
      await refreshState();
    } catch (_) { showError('proxy-error', 'The credentials could not be saved. Check the local console, then try again.'); }
    finally { $('proxy-key').value = ''; requestPending = false; setBusy(isActive(currentJob) || uncertainSubmission); }
  });
  $('check-proxy').addEventListener('click', () => submitJob({action: 'proxy-check', proxy_egypt: true, count: 1}, 'proxy-error'));

  switchPanel(location.hash.slice(1) || 'workbench');
  setBusy(false);
  refreshState();
})();
