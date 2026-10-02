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
  const actionNames = {play: 'Play test', like: 'Like test', check: 'Session check', song: 'Song access check', prepare: 'Account preparation', preview: 'Account selection preview', login: 'Session refresh', 'proxy-check': 'Egypt connection check'};
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
  let previousRandomMaximum = null;
  const selectedRows = new Set();
  const refreshRows = new Set();

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
  };
  function proxyMode(scope) { return $(connectionScopes[scope].selector).value === 'egypt'; }
  function isActive(job) { return !!job && ['queued', 'running'].includes(job.status); }
  function readyRows() { return [...new Set((state?.cohort?.ready_rows || []).map(Number).filter(row => Number.isSafeInteger(row) && row > 0))]; }
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
    ['test-count', 'count-minus', 'count-plus', 'prepare-count', 'start-row', 'browser', 'headless', 'prepare-reduce-browser-data', 'login-reduce-browser-data', 'login-rows', 'proxy-username', 'proxy-key'].forEach(id => { $(id).disabled = busy || requestPending; });
    updatePreparationMethod();
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
    $('prepare-method-hint').textContent = noBrowser
      ? "Reuse each account's own registered session and validate its identity and authenticated reads without a password login. Expired sessions or required verification stop preparation; there is no browser fallback."
      : 'Capture a normal login when an account needs a saved session, then validate it.';
  }

  function updateProxyMode() {
    const configured = !!state?.proxy?.configured;
    Object.entries(connectionScopes).forEach(([name, scope]) => {
      const egypt = proxyMode(name);
      $(scope.hint).textContent = egypt ? (configured ? 'Uses the saved PacketStream Egypt proxy.' : 'Save credentials in Proxy settings before using Egypt.') : 'Uses your current internet connection without a proxy.';
      $(scope.hint).classList.toggle('route-unavailable', egypt && !configured);
      if (name === 'tests') $('run-route').textContent = egypt ? 'EGYPT' : 'DIRECT';
      if (name !== 'tests') scope.actions.forEach(id => { $(id).disabled = busy || requestPending || !state || (egypt && !configured); });
      if (name === 'tests' && egypt && !configured) scope.actions.forEach(id => { $(id).disabled = true; });
    });
    if (!busy && !requestPending && state) {
      $('check-proxy').disabled = !configured;
    }
  }

  function updateSelections() {
    const max = limits().testAccounts;
    const randomMaximum = Math.min(readyRows().length, max);
    const randomCount = Number($('random-count').value);
    const randomValid = /^\d+$/.test($('random-count').value.trim()) && Number.isSafeInteger(randomCount) && randomCount >= 1 && randomCount <= randomMaximum;
    const selectionDisabled = busy || requestPending || !state;
    $('random-count').max = String(randomMaximum);
    $('random-count').disabled = selectionDisabled || randomMaximum === 0;
    $('random-count').setAttribute('aria-invalid', String(randomMaximum > 0 && !randomValid));
    $('select-all-accounts').disabled = selectionDisabled || readyRows().length === 0;
    $('clear-accounts').disabled = selectionDisabled || selectedRows.size === 0;
    $('select-random-accounts').disabled = selectionDisabled || !randomValid;
    document.querySelectorAll('#account-picker input').forEach(input => {
      input.checked = selectedRows.has(Number(input.value));
      input.disabled = busy || requestPending || (!input.checked && selectedRows.size >= max);
    });
    $('selection-counter').textContent = `${selectedRows.size} selected`;
    let count = Number($('test-count').value);
    if (!Number.isSafeInteger(count) || count < 1 || count > limits().tests) count = null;
    $('run-summary').textContent = selectedRows.size === 0 ? 'Select an account to get started.' : count === null ? `Choose between 1 and ${limits().tests} tests per account.` : `${selectedRows.size} account${selectedRows.size === 1 ? '' : 's'} × ${count} test${count === 1 ? '' : 's'} = ${selectedRows.size * count} run${selectedRows.size * count === 1 ? '' : 's'}`;
    ['run-play', 'run-like'].forEach(id => { $(id).disabled = busy || requestPending || !state || songDirty || selectedRows.size === 0 || count === null; });
    ['check-sessions', 'check-song'].forEach(id => { $(id).disabled = busy || requestPending || !state || selectedRows.size === 0; });
    if (songDirty) $('check-song').disabled = true;
    document.querySelectorAll('#cohort-table input').forEach(input => { input.disabled = busy || requestPending || (!input.checked && refreshRows.size >= limits().accounts); });
    updateProxyMode();
  }

  function renderAccounts() {
    const ready = readyRows();
    const rows = new Map((state?.cohort?.accounts || []).map(account => [Number(account.source_row), account]));
    ready.forEach(row => { if (!rows.has(row)) rows.set(row, {source_row: row, session_saved: true, state: 'ready'}); });
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
    ready.sort((a, b) => a - b).forEach(row => {
      const account = rows.get(row);
      const label = node('label', 'account-choice');
      const checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.value = String(row);
      checkbox.setAttribute('aria-label', `Select account row ${row}`);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selectedRows.add(row); else selectedRows.delete(row);
        showError('workbench-error', ''); showError('selection-error', ''); updateSelections();
      });
      const information = node('span', 'account-info');
      information.append(node('strong', '', `Account ${row}`), node('small', '', account.email || account.account_email || 'Session ready'));
      label.append(checkbox, information, node('span', 'status-dot'));
      $('account-picker').appendChild(label);
    });
    $('empty-accounts').hidden = ready.length > 0;
    $('cohort-table').replaceChildren();
    [...rows.values()].sort((a, b) => Number(a.source_row) - Number(b.source_row)).forEach(account => {
      const row = Number(account.source_row);
      const tr = node('tr');
      const selectCell = node('td');
      const checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.value = String(row); checkbox.checked = refreshRows.has(row);
      checkbox.setAttribute('aria-label', `Select row ${row} for session refresh`);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) refreshRows.add(row); else refreshRows.delete(row);
        $('login-rows').value = [...refreshRows].sort((a, b) => a - b).join(', ');
        updateSelections();
      });
      selectCell.appendChild(checkbox);
      const sessionCell = node('td');
      sessionCell.appendChild(node('span', 'badge ' + (account.session_saved ? 'success' : 'neutral'), account.session_saved ? 'Saved' : 'No session'));
      const statusCell = node('td');
      statusCell.appendChild(node('span', 'badge ' + (ready.includes(row) ? 'success' : 'warning'), ready.includes(row) ? 'Ready to test' : humanLabel(account.state || 'Needs a check')));
      tr.append(selectCell, node('td', '', `Row ${row}`), sessionCell, statusCell);
      $('cohort-table').appendChild(tr);
    });
    $('cohort-count').textContent = `${rows.size} account${rows.size === 1 ? '' : 's'}`;
    $('cohort-empty').hidden = rows.size > 0;
    updateSelections();
  }

  function humanLabel(value) { return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase()); }
  function formatNumber(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString() : '—'; }
  function formatDate(value) {
    if (!value) return 'Saved locally';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? 'Saved locally' : date.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
  }

  function reportLabel(name) { return String(name || 'Report').replace(/\.json$/i, '').replace(/[._-]+/g, ' '); }
  function readableMessage(value) { return typeof value === 'string' && value.trim() !== '[redacted]' ? value : ''; }

  function reportMessage(report) {
    const name = actionNames[report.action] || 'Saved run';
    const [, outcome] = resultStatus(report);
    if (outcome === 'Unknown') return `${name} has an uncertain result. Review the details before repeating the action.`;
    if (outcome === 'Failed') return `${name} stopped. Review the saved details for the failed check.`;
    if (outcome === 'Already liked') return 'The song was already liked; its saved state was verified.';
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
    if (result.unknown || result.result_unknown || result.error?.result_unknown || /unknown|uncertain/.test(flags)) return ['warning', 'Unknown'];
    if (result.passed === false || result.error || /failed|blocked|error|rejected/.test(flags)) return ['error', 'Failed'];
    if (/already.*liked|skipped_already_liked/.test(flags) || result.skipped_already_liked) return ['neutral', 'Already liked'];
    if (result.event_accepted === true || result.mutation_accepted === true || /accepted/.test(flags)) return ['success', 'Accepted'];
    if (/prepared|enrolled/.test(flags)) return ['success', 'Prepared'];
    if (/selected|preview/.test(flags)) return ['neutral', 'Selected'];
    if (result.passed === true || /succeeded|passed|ready|ok|liked|saved|complete/.test(flags)) return ['success', 'Passed'];
    return ['neutral', humanLabel(values[0] || 'Result')];
  }

  function resultRows(results) {
    const entries = Array.isArray(results) ? results : results && typeof results === 'object' ? [results] : [];
    return entries.flatMap(result => {
      if (!result || typeof result !== 'object') return [];
      if (Array.isArray(result.prepared_rows) && result.prepared_rows.length) {
        return result.prepared_rows.map(sourceRow => ({source_row: sourceRow, status: 'prepared', session_saved: true, passed: true}));
      }
      if (result.dry_run && Array.isArray(result.selected_rows)) {
        return result.selected_rows.map(sourceRow => ({source_row: sourceRow, status: 'selected', message: 'Selected for preparation. No login or test event was sent.'}));
      }
      return [result];
    });
  }

  function renderResultList(container, results) {
    container.replaceChildren();
    resultRows(results).forEach((result, index) => {
      if (!result || typeof result !== 'object') return;
      const row = node('div', 'result-row');
      const details = node('div');
      const testNumber = result.test_number ?? result.run ?? result.attempt;
      const title = result.source_row !== undefined ? `Account ${result.source_row}${testNumber !== undefined ? ` · Run ${testNumber}` : ''}` : result.country ? `${result.country} connection` : `Result ${index + 1}`;
      details.appendChild(node('strong', '', title));
      let message = readableMessage(result.message) || readableMessage(result.error?.message) || readableMessage(result.error) || readableMessage(result.reason);
      if (!message && result.event_accepted) message = 'The server accepted the test record. Check the admin dashboard for its statistics.';
      if (!message && result.skipped_already_liked) message = 'Verified the existing like; no new like request was needed.';
      if (!message && result.session_saved) message = 'The saved session is available for testing.';
      if (!message && result.passed) message = 'The check completed successfully.';
      if (message) details.appendChild(node('p', '', message));
      const [color, label] = resultStatus(result);
      row.append(details, node('span', 'badge ' + color, label)); container.appendChild(row);
    });
  }

  function renderJob(job) {
    currentJob = job || null;
    const hasJob = !!job;
    $('job-empty').hidden = hasJob;
    $('job-content').hidden = !hasJob;
    if (!hasJob) {
      $('job-status').className = 'badge neutral'; $('job-status').textContent = 'Idle';
      setBusy(uncertainSubmission);
      return;
    }
    $('job-title').textContent = actionNames[job.action] || humanLabel(job.action || 'Run');
    const active = isActive(job);
    const statusClass = active ? 'running' : job.status === 'succeeded' ? 'success' : 'error';
    $('job-status').className = 'badge ' + statusClass;
    $('job-status').textContent = active ? humanLabel(job.status) : job.status === 'succeeded' ? 'Completed' : 'Stopped';
    $('job-message').textContent = job.message || (active ? 'Your run is in progress.' : job.status === 'succeeded' ? 'The run completed. Review the results below.' : 'The run stopped. Review the result before starting another test.');
    const total = Number(job.progress?.total) || 0;
    const completed = Math.min(total, Number(job.progress?.completed) || 0);
    $('job-phase').textContent = humanLabel(job.phase || (active ? 'Working' : job.status));
    $('job-progress-label').textContent = total ? `${completed} / ${total}` : active ? 'In progress' : 'Complete';
    $('job-progress-bar').style.width = `${total ? Math.max(0, Math.min(100, completed / total * 100)) : active ? 12 : job.status === 'succeeded' ? 100 : 0}%`;
    const error = typeof job.error === 'string' ? job.error : job.error?.message;
    showError('job-error', error || '');
    renderResultList($('job-results'), job.results);
    $('job-json').textContent = JSON.stringify(job, null, 2);
    $('busy-banner-message').textContent = `${actionNames[job.action] || 'A run'} is in progress${total ? ` · ${completed} of ${total}` : ''}`;
    setBusy(active || uncertainSubmission);
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
    $('prepare-count').max = String(limits().accounts);
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
    if (proxyMode(scope) && !state?.proxy?.configured) throw new Error('Save your PacketStream credentials in Proxy settings before using Egypt routing.');
  }

  function selectedTestRows() {
    const rows = [...selectedRows].sort((a, b) => a - b);
    if (!rows.length) throw new Error('Select at least one ready account.');
    if (rows.length > limits().testAccounts || rows.some(row => !readyRows().includes(row))) throw new Error('Choose only ready accounts within the account limit.');
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
      if (['play', 'like', 'song'].includes(action)) payload.song_id = String(state.test_song_id);
      await submitJob(payload, 'workbench-error');
    } catch (error) { showError('workbench-error', error.message); }
  }

  async function startPreparation(action) {
    try {
      if (action !== 'preview') requireProxyReady('prepare');
      const count = integer($('prepare-count').value, 1, limits().accounts, 'Accounts to add');
      const noBrowser = noBrowserPreparation();
      const payload = {action, count, browser: $('browser').value, headless: !noBrowser && $('headless').checked, proxy_egypt: proxyMode('prepare'), reduce_browser_data: !noBrowser && $('prepare-reduce-browser-data').checked, no_browser: noBrowser};
      if ($('start-row').value.trim()) payload.start_row = integer($('start-row').value, 1, Number.MAX_SAFE_INTEGER, 'Starting row');
      await submitJob(payload, 'accounts-error');
    } catch (error) { showError('accounts-error', error.message); }
  }

  async function startLogin() {
    try {
      requireProxyReady('login');
      const raw = $('login-rows').value.trim();
      if (!raw || !/^\d+(?:\s*,\s*\d+)*$/.test(raw)) throw new Error('Enter account rows as whole numbers separated by commas, such as 7, 8.');
      const rows = [...new Set(raw.split(',').map(value => integer(value, 1, Number.MAX_SAFE_INTEGER, 'Account row')))];
      if (rows.length > limits().accounts) throw new Error(`Select up to ${limits().accounts} rows for a session refresh.`);
      await submitJob({action: 'login', rows, count: 1, browser: $('browser').value, headless: $('headless').checked, proxy_egypt: proxyMode('login'), reduce_browser_data: $('login-reduce-browser-data').checked}, 'login-error');
    } catch (error) { showError('login-error', error.message); }
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
      if (report.results) {
        const results = node('div', 'result-list'); renderResultList(results, report.results); $('report-summary').appendChild(results);
      }
      $('report-detail').hidden = false;
    } catch (error) { if (selectedReport === name) showError('report-error', error.message); }
  }

  document.querySelectorAll('[data-panel]').forEach(button => button.addEventListener('click', () => switchPanel(button.dataset.panel)));
  document.querySelectorAll('[data-go-panel]').forEach(button => button.addEventListener('click', () => switchPanel(button.dataset.goPanel)));
  document.querySelector('.brand').addEventListener('click', event => { event.preventDefault(); switchPanel('workbench'); });
  Object.entries(connectionScopes).forEach(([name, scope]) => {
    $(scope.selector).addEventListener('change', () => {
      updateSelections();
      showError(name === 'tests' ? 'workbench-error' : name === 'prepare' ? 'accounts-error' : 'login-error', '');
    });
  });
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
  $('prepare-method').addEventListener('change', () => {
    updatePreparationMethod(); showError('accounts-error', '');
  });
  function replaceSelection(rows) {
    selectedRows.clear();
    rows.forEach(row => selectedRows.add(row));
    showError('selection-error', ''); showError('workbench-error', ''); updateSelections();
  }
  $('select-all-accounts').addEventListener('click', () => {
    if (busy || requestPending || !state) return;
    try { replaceSelection(ConsoleSelection.selectAllRows(readyRows(), limits().testAccounts)); }
    catch (error) { showError('selection-error', error.message); }
  });
  $('select-random-accounts').addEventListener('click', () => {
    if (busy || requestPending || !state) return;
    try {
      const maximum = Math.min(readyRows().length, limits().testAccounts);
      const count = validateRandomCount(maximum);
      replaceSelection(ConsoleSelection.selectRandomRows(readyRows(), count, limits().testAccounts));
    } catch (error) { showError('selection-error', error.message); }
  });
  $('clear-accounts').addEventListener('click', () => {
    if (!busy && !requestPending && state) replaceSelection([]);
  });
  $('random-count').addEventListener('input', () => {
    const maximum = Math.min(readyRows().length, limits().testAccounts);
    try { validateRandomCount(maximum); showError('selection-error', ''); }
    catch (error) { showError('selection-error', error.message, 'random-count'); }
    updateSelections();
  });
  $('count-minus').addEventListener('click', () => { $('test-count').value = String(Math.max(1, (Number($('test-count').value) || 1) - 1)); updateSelections(); });
  $('count-plus').addEventListener('click', () => { $('test-count').value = String(Math.min(limits().tests, (Number($('test-count').value) || 1) + 1)); updateSelections(); });
  $('run-play').addEventListener('click', () => startTest('play'));
  $('run-like').addEventListener('click', () => startTest('like'));
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
