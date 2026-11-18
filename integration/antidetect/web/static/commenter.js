/* Antidetect workspace: read-only refresh, explicit operator actions. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {pages: [], tasks: [], overview: {}, profileSafety: null, preflight: null, preflightBusy: false, preflightAt: null, preflightStale: false, preflightRequest: 0, online: false, loading: false, busy: false, pauseBusy: false, resuming: false, revision: 0, selectedPage: null, previewRequest: 0, taskRequest: 0};
  let refreshPromise = null;
  const taskLabels = {PENDING: 'Chờ lịch', SENDING: 'Đang gửi', SUCCESS: 'Thành công', FAILED: 'Lỗi', NEED_CHECK: 'Cần đối chiếu', CANCELLED: 'Đã hủy', PAUSED: 'Tạm dừng'};
  const taskColors = {SUCCESS: 'green', SENDING: 'blue', FAILED: 'red', NEED_CHECK: 'amber'};
  const mode = page => page.send_mode === 'BROWSER' ? 'BROWSER' : 'GRAPH_API';
  const modeName = page => mode(page) === 'BROWSER' ? 'Browser' : 'Graph API';
  const profile = page => page.antidetect_profile_name || page.antidetect_profile_id || 'Chưa ghép profile';
  const profileKey = page => String(page.antidetect_profile_id || page.antidetect_profile_name || '__none');
  const ready = page => page.send_ready === true || page.send_ready === 1;
  const enabled = page => page.rule_enabled === true || page.rule_enabled === 1;
  const active = page => page.is_active === true || page.is_active === 1;
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const knownPause = () => typeof state.overview.metrics?.paused_all === 'boolean';
  const paused = () => state.overview.metrics?.paused_all === true;
  const mayOperate = () => state.online && !state.busy && !state.pauseBusy;
  const counterText = value => value == null ? '—' : String(number(value));
  const dateText = value => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('vi-VN', {timeZone: 'Asia/Bangkok', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) + ' UTC+7';
  };
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const badge = (text, color = '') => node('span', `badge ${color}`, text);
  const button = (text, callback, className = 'secondary', allowed = true) => {
    const element = node('button', `button ${className}`, text);
    element.type = 'button';
    element.disabled = !allowed;
    element.addEventListener('click', callback);
    return element;
  };
  const pageById = id => state.pages.find(page => String(page.page_id) === String(id));
  const safetyProfiles = () => Array.isArray(state.profileSafety) ? state.profileSafety : (Array.isArray(state.profileSafety?.profiles) ? state.profileSafety.profiles : []);
  const pageProfileSafety = page => mode(page) === 'BROWSER' ? safetyProfiles().find(item => String(item.profile_id) === String(page.antidetect_profile_id)) : null;
  const profileBlocked = item => item && String(item.status).toUpperCase() !== 'READY';
  const pageProfileBlocked = page => mode(page) === 'BROWSER' && (!pageProfileSafety(page) || profileBlocked(pageProfileSafety(page)));
  const issues = page => [...new Set([pageProfileBlocked(page) ? (pageProfileSafety(page)?.wait_reason || 'Chưa xác minh trạng thái an toàn chung của profile Browser.') : null, ...['connection_error', 'browser_error', 'scan_error', 'source_error', 'send_error', 'profile_wait_reason', 'send_wait_reason'].map(key => page[key])].filter(Boolean))];
  const automationReady = page => ready(page) && !pageProfileBlocked(page) && active(page) && !page.source_error && Boolean(page.source_synced_at);
  const canCheck = page => mode(page) === 'BROWSER' ? Boolean(page.antidetect_profile_id) : Boolean(page.has_page_token);

  function notice(message, error = false) {
    $('notice-text').textContent = String(message);
    $('notice').classList.toggle('error', error);
    $('notice').hidden = false;
  }
  async function api(path, body) {
    const options = {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch('/api/commenter/' + path, options);
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error(response.status === 401 ? 'Phiên Antidetect đã hết hạn. Mở Profiles để đăng nhập lại.' : 'Commenter trả về dữ liệu không hợp lệ.'); }
    if (!response.ok || data.success === false) {
      throw new Error(typeof data.detail === 'string' ? data.detail : (data.error || 'Không thực hiện được thao tác.'));
    }
    return data;
  }
  function refresh(options = {}) {
    if (refreshPromise) return options.force ? refreshPromise.then(() => refresh(options)) : refreshPromise;
    if ((state.busy || state.pauseBusy) && !options.force) return Promise.resolve(false);
    refreshPromise = refreshOnce(options).finally(() => {refreshPromise = null;});
    return refreshPromise;
  }
  async function refreshOnce({report = false} = {}) {
    const revision = state.revision;
    state.loading = true;
    $('refresh').disabled = true;
    try {
      const data = await api('state');
      if (revision !== state.revision) return false;
      if (!data.overview || !Array.isArray(data.pages) || !Array.isArray(data.tasks)) throw new Error('Dữ liệu trạng thái chưa đầy đủ.');
      state.overview = data.overview;
      state.pages = data.pages;
      state.tasks = data.tasks;
      state.profileSafety = data.profile_safety || null;
      if (data.preflight?.read_only === true) {
        state.preflight = data.preflight;
        state.preflightStale = false;
      }
      state.online = true;
      if ($('task-filter').value || $('task-page-filter').value) await loadTasks(false);
      else state.taskRequest++;
      state.preflightStale = Boolean(state.preflight);
      $('updated-at').textContent = 'Cập nhật lúc ' + new Date().toLocaleTimeString('vi-VN', {timeZone: 'Asia/Bangkok', hour: '2-digit', minute: '2-digit', second: '2-digit'}) + ' UTC+7';
      render();
      if (report) notice('Đã làm mới trạng thái.');
      return true;
    } catch (error) {
      if (revision !== state.revision) return false;
      state.online = false;
      $('updated-at').textContent = 'Chưa kết nối được Commenter. Dữ liệu đang hiển thị có thể đã cũ.';
      render();
      if (report) notice(error.message, true);
      return false;
    } finally {
      state.loading = false;
      $('refresh').disabled = state.busy || state.pauseBusy;
    }
  }
  async function mutate(path, body, fallback) {
    if (!mayOperate()) return;
    state.busy = true;
    state.revision++;
    state.preflightStale = true;
    state.preflightRequest++;
    render();
    try {
      const result = await api(path, body);
      notice([result.message || fallback || 'Đã cập nhật.', ...(Array.isArray(result.errors) ? result.errors.slice(0, 6) : [])].join('\n'));
      const updated = await refresh({force: true});
      if (!updated) notice('Thao tác đã được tiếp nhận nhưng chưa tải được trạng thái mới. Bấm Làm mới để đối chiếu.', true);
    } catch (error) {
      notice(error.message, true);
      await refresh({force: true});
    } finally {
      state.busy = false;
      render();
    }
  }
  async function requestPause() {
    if (state.pauseBusy) return;
    state.pauseBusy = true;
    state.revision++;
    state.preflightStale = true;
    state.preflightRequest++;
    render();
    try {
      const result = await api('system/pause', {paused: true});
      if (typeof result.paused !== 'boolean') throw new Error('Chưa xác nhận được trạng thái Pause.');
      state.overview.metrics = {...state.overview.metrics, paused_all: result.paused};
      notice(result.paused ? 'Đã tạm dừng nhận lượt gửi mới. Lượt đã được chốt có thể đang hoàn tất.' : 'Hệ thống chưa xác nhận Pause. Hãy đối chiếu lại.', !result.paused);
      await refresh({force: true});
    } catch (error) {
      state.online = false;
      notice(error.message + ' Chưa xác nhận được Pause; hãy kiểm tra dịch vụ.', true);
    } finally {
      state.pauseBusy = false;
      render();
    }
  }
  function confirmAction({title, description, submit = 'Xác nhận', check, input = false}) {
    if ($('confirm-dialog').open) return Promise.resolve(null);
    $('confirm-title').textContent = title;
    $('confirm-description').textContent = description;
    $('confirm-submit').textContent = submit;
    $('confirm-check-label').hidden = !check;
    $('confirm-check-text').textContent = check || '';
    $('confirm-check').checked = false;
    $('confirm-check').required = Boolean(check);
    $('confirm-input-label').hidden = !input;
    $('confirm-input').value = '';
    $('confirm-submit').disabled = Boolean(check);
    $('confirm-dialog').showModal();
    return new Promise(resolve => {
      let resolved = false;
      const finish = result => {
        if (resolved) return;
        resolved = true;
        $('confirm-form').removeEventListener('submit', onSubmit);
        $('confirm-dialog').removeEventListener('cancel', onCancel);
        $('confirm-cancel').removeEventListener('click', onCancel);
        $('confirm-check').removeEventListener('change', onCheck);
        $('confirm-dialog').close();
        resolve(result);
      };
      const onSubmit = event => { event.preventDefault(); if (!check || $('confirm-check').checked) finish({comment_id: $('confirm-input').value.trim() || null}); };
      const onCancel = event => { event.preventDefault(); finish(null); };
      const onCheck = () => { $('confirm-submit').disabled = Boolean(check && !$('confirm-check').checked); };
      $('confirm-form').addEventListener('submit', onSubmit);
      $('confirm-dialog').addEventListener('cancel', onCancel);
      $('confirm-cancel').addEventListener('click', onCancel);
      $('confirm-check').addEventListener('change', onCheck);
    });
  }
  function renderRuntime() {
    const online = state.online && knownPause();
    const status = !online ? 'offline' : paused() ? 'paused' : 'running';
    $('runtime-dot').className = 'status-dot ' + status;
    $('runtime-state').textContent = !online ? 'Chưa có kết nối vận hành' : paused() ? 'Hệ thống đang tạm dừng' : 'Hệ thống đang chạy';
    $('runtime-description').textContent = !online ? 'Kiểm tra dịch vụ cục bộ, sau đó làm mới trạng thái.' : paused() ? 'Chưa nhận lượt gửi mới. Bạn có thể kiểm tra kết nối và chuẩn bị cấu hình.' : 'Các Page đã bật tự động sẽ xử lý theo lịch và hạn mức.';
    $('pause-toggle').textContent = online && paused() && !state.resuming ? 'Tiếp tục hệ thống' : 'Tạm dừng toàn bộ';
    $('pause-toggle').className = 'button ' + (online && paused() && !state.resuming ? 'primary' : 'warning');
    // Stop remains available while a connection check or Resume is pending.
    $('pause-toggle').disabled = state.pauseBusy || (online && paused() && state.busy && !state.resuming);
    $('footer-connection').textContent = online ? '● Commenter đã kết nối' : '● Commenter chưa kết nối';
    const counts = state.overview.task_counts || {};
    $('stat-pages').textContent = state.online ? state.pages.length : '—';
    const graph = state.pages.filter(page => mode(page) === 'GRAPH_API').length;
    $('stat-transports').textContent = `${graph} Graph API · ${state.pages.length - graph} Browser`;
    $('stat-auto').textContent = state.online ? state.pages.filter(enabled).length : '—';
    $('stat-ready').textContent = state.pages.filter(page => ready(page) && !pageProfileBlocked(page)).length + ' Page qua kiểm tra kết nối/profile';
    $('stat-pending').textContent = state.online ? number(counts.PENDING) : '—';
    $('stat-sending').textContent = number(counts.SENDING) + ' đang gửi · ' + number(counts.PAUSED) + ' tạm dừng · 500 gần nhất';
    $('stat-review').textContent = state.online ? number(counts.NEED_CHECK) : '—';
    $('tab-page-count').textContent = state.pages.length || '';
    $('tab-task-count').textContent = state.tasks.length || '';
    $('sheet-submit').disabled = !mayOperate();
    const sheet = state.overview.metrics?.sheet_sync || {};
    if (document.activeElement !== $('sheet-url') && !$('sheet-url').dataset.edited) $('sheet-url').value = sheet.sheet_url || '';
    $('sheet-status').textContent = [sheet.sheet_url ? 'Nguồn đã cấu hình' : 'Chưa cấu hình nguồn bình luận.', sheet.last_run ? 'Đồng bộ gần nhất: ' + dateText(sheet.last_run) : '', sheet.last_error].filter(Boolean).join('\n');
  }
  function renderProfiles() {
    const value = $('profile-filter').value;
    const options = new Map();
    state.pages.forEach(page => options.set(profileKey(page), profile(page)));
    $('profile-filter').replaceChildren(new Option('Tất cả profile', ''));
    [...options.entries()].sort((a, b) => a[1].localeCompare(b[1], 'vi')).forEach(([key, title]) => $('profile-filter').add(new Option(title, key)));
    $('profile-filter').value = value;
    const taskPage = $('task-page-filter').value;
    $('task-page-filter').replaceChildren(new Option('Tất cả Fanpage', ''));
    state.pages.forEach(page => $('task-page-filter').add(new Option(page.page_name || page.page_id, String(page.page_id))));
    $('task-page-filter').value = taskPage;
  }
  function renderSafety() {
    const profiles = safetyProfiles();
    const blocked = profiles.filter(profileBlocked);
    $('tab-blocked-count').textContent = blocked.length ? blocked.length + ' chờ' : '';
    $('run-preflight').disabled = !mayOperate() || state.preflightBusy;
    $('run-preflight').textContent = state.preflightBusy ? 'Đang kiểm tra…' : 'Chạy preflight';
    $('profile-safety-list').replaceChildren();
    $('profile-safety-empty').hidden = profiles.length > 0;
    $('profile-safety-empty').textContent = state.profileSafety ? 'Chưa có Page nào sử dụng Browser.' : 'Chưa tải được trạng thái an toàn profile.';
    profiles.forEach(item => {
      const blocked = profileBlocked(item);
      const card = node('article', 'profile-safety-card ' + String(item.status || 'UNKNOWN').toLowerCase());
      const heading = node('div', 'profile-safety-heading');
      const title = node('div'); title.append(node('h3', '', item.profile_name || item.profile_id || 'Chưa ghép profile'), node('span', 'subline', item.profile_id || ''));
      const status = String(item.status || 'UNKNOWN').toUpperCase();
      heading.append(title, badge(status === 'READY' ? 'Chốt profile sẵn sàng' : status === 'BLOCKED' ? 'Bị chặn' : status === 'WAITING' ? 'Đang chờ' : 'Cần kiểm tra', status === 'READY' ? 'green' : status === 'BLOCKED' ? 'red' : 'amber'));
      const facts = node('dl', 'detail-facts');
      fact(facts, 'Page dùng Browser', String(number(item.page_count)));
      fact(facts, 'Thao tác hôm nay', `${counterText(item.operations_today)} / ${counterText(item.operations_limit)}`);
      if (item.reserved_send_operations != null && number(item.reserved_send_operations) > 0) {
        fact(facts, 'Giữ riêng cho gửi', `${number(item.reserved_send_operations)} lượt (quét tối đa ${counterText(item.scan_operations_limit)})`);
      }
      fact(facts, 'Page đã xác minh riêng', String(Array.isArray(item.verified_page_ids) ? item.verified_page_ids.length : 0));
      fact(facts, 'Có thể thử lại', dateText(item.retry_at));
      card.append(heading, facts);
      if (blocked) card.append(node('p', 'profile-block-summary', `Trạng thái này áp dụng cho cả ${number(item.page_count)} Page Browser trong profile.`));
      if (item.wait_reason) card.append(node('p', 'block-reason', item.wait_reason));
      const names = Array.isArray(item.page_names) ? item.page_names : [];
      if (names.length) {const pages = node('details', 'profile-page-list'); pages.append(node('summary', '', 'Danh sách Page')); const list = node('ul'); names.forEach(name => list.append(node('li', '', name))); pages.append(list); card.append(pages);}
      if (item.profile_id) card.append(button('Xem các Page →', () => {$('profile-filter').value = String(item.profile_id); $('mode-filter').value = 'BROWSER'; $('page-filter').value = ''; $('page-search').value = ''; renderPages(); selectTab('pages', true);}, 'secondary'));
      $('profile-safety-list').append(card);
    });
    renderPreflight();
  }
  function renderPreflight() {
    const result = state.preflight;
    $('preflight-summary').replaceChildren();
    $('preflight-findings').replaceChildren();
    if (!result) {$('preflight-status').textContent = state.preflightBusy ? 'Đang kiểm tra cấu hình và trạng thái đã lưu…' : 'Chưa chạy kiểm tra.'; return;}
    $('preflight-status').textContent = !state.online ? 'Mất kết nối · Kết quả có thể đã cũ.' : state.preflightStale ? 'Trạng thái có thể đã thay đổi · Chạy lại preflight để đối chiếu.' : (state.preflightAt ? 'Kiểm tra lúc ' + dateText(state.preflightAt) : 'Trạng thái chỉ đọc mới nhất');
    const size = value => Array.isArray(value) ? value.length : counterText(value);
    const summary = [['Page AUTO đã chuẩn bị', size(result.eligible_pages)], ['Page cần xử lý / chờ', size(result.blocked_pages)], ['Profile bị chặn', size(result.blocked_profiles)], ['Nguồn cần xử lý', size(result.source_errors)], ['Tác vụ cần đối chiếu · toàn bộ', size(result.needs_check_count)]];
    summary.forEach(([title, value]) => {const item = node('div', 'preflight-stat'); item.append(node('span', '', title), node('strong', '', value)); $('preflight-summary').append(item);});
    const findings = Array.isArray(result.findings) ? result.findings : [];
    if (!findings.length) {$('preflight-findings').append(node('p', 'muted', 'Không có vấn đề cấu hình được ghi nhận.')); return;}
    findings.forEach(finding => {
      const severity = String(finding.severity || '').toUpperCase();
      const item = node('article', 'preflight-finding');
      const heading = node('div', 'task-heading');
      heading.append(badge(severity === 'INFO' ? 'Thông tin' : severity === 'ERROR' ? 'Cần xử lý' : 'Đang chờ / lưu ý', severity === 'ERROR' ? 'red' : severity === 'INFO' ? 'blue' : 'amber'), node('strong', '', finding.page_name || finding.page_id || finding.profile_id || 'Hệ thống'));
      item.append(heading, node('p', '', finding.reason || 'Cần đối chiếu trạng thái.'));
      if (finding.page_id) item.append(button('Chi tiết Page', () => openPage(String(finding.page_id)), 'secondary', Boolean(pageById(finding.page_id))));
      $('preflight-findings').append(item);
    });
  }
  async function runPreflight() {
    if (!mayOperate() || state.preflightBusy) return;
    const request = ++state.preflightRequest;
    state.preflightBusy = true;
    renderSafety();
    try {
      const result = await api('preflight');
      if (request !== state.preflightRequest) return;
      if (result.read_only !== true || !Array.isArray(result.findings) || typeof result.paused_all !== 'boolean') throw new Error('Kết quả preflight không hợp lệ.');
      state.preflight = result;
      state.preflightAt = result.generated_at || new Date().toISOString();
      state.preflightStale = false;
      notice('Đã hoàn tất preflight chỉ đọc.');
    } catch (error) {if (request === state.preflightRequest) {state.preflightStale = true; notice(error.message, true);}}
    finally {state.preflightBusy = false; renderSafety();}
  }
  function renderPages() {
    const query = $('page-search').value.trim().toLocaleLowerCase('vi');
    const filter = $('page-filter').value;
    const pages = state.pages.filter(page => {
      const match = [page.page_name, page.page_id, page.fb_profile_id, profile(page)].join(' ').toLocaleLowerCase('vi').includes(query);
      return match && (!$('profile-filter').value || profileKey(page) === $('profile-filter').value) && (!$('mode-filter').value || mode(page) === $('mode-filter').value) && (!filter || (filter === 'enabled' ? enabled(page) : filter === 'disabled' ? !enabled(page) : filter === 'ready' ? automationReady(page) : !automationReady(page) || issues(page).length));
    });
    $('page-result-count').textContent = `${pages.length} / ${state.pages.length} Fanpage`;
    $('page-rows').replaceChildren();
    if (!pages.length) {
      const row = node('tr'); const cell = node('td', 'empty'); cell.colSpan = 5;
      cell.append(node('strong', '', state.online ? 'Không có Fanpage phù hợp' : 'Chưa tải được danh sách Fanpage'), node('span', '', state.online ? 'Thử đổi từ khóa hoặc bộ lọc.' : 'Bấm Làm mới sau khi Commenter được kết nối.'));
      row.append(cell); $('page-rows').append(row); return;
    }
    pages.forEach(page => {
      const row = node('tr');
      const name = node('td');
      const open = node('button', 'page-name', page.page_name || page.page_id); open.type = 'button'; open.addEventListener('click', () => openPage(page.page_id));
      name.append(open, node('span', 'subline', page.page_id), node('div', 'profile-line', profile(page)));
      const connection = node('td');
      connection.append(node('span', 'mode-label', modeName(page)), badge(pageProfileBlocked(page) ? 'Profile chưa sẵn sàng' : ready(page) ? 'Đã xác minh' : 'Cần kiểm tra', pageProfileBlocked(page) ? 'red' : ready(page) ? 'green' : 'amber'));
      if (!active(page)) connection.append(node('div', 'subline', 'Kết nối đang tạm dừng'));
      const errors = issues(page); if (errors.length) {const issue = node('div', 'issue-line', errors[0]); issue.title = errors.join('\n'); connection.append(issue);}
      const automation = node('td'); automation.append(badge(enabled(page) ? 'Đã bật' : 'Đã tắt', enabled(page) ? 'blue' : ''));
      if (enabled(page) && paused()) automation.append(node('span', 'subline', 'Theo Pause toàn hệ thống'));
      if (!page.source_synced_at || page.source_error) automation.append(node('span', 'subline', 'Nguồn cần kiểm tra'));
      const quota = node('td'); const used = number(page.comments_used_today); const limit = number(page.max_comments_per_day) || 20;
      quota.append(node('div', 'quota', `${used} / ${limit} bình luận`));
      const bar = node('div', 'quota-bar'); const fill = node('div', 'quota-fill'); fill.style.width = `${limit > 0 ? Math.min(100, used / limit * 100) : 0}%`; bar.append(fill); quota.append(bar);
      if (mode(page) === 'BROWSER') {const safety = pageProfileSafety(page); quota.append(node('span', 'subline', `Profile: ${counterText(safety?.operations_today)} / ${counterText(safety?.operations_limit)} thao tác`));}
      const actions = node('td'); actions.append(button('Chi tiết →', () => openPage(page.page_id)));
      row.append(name, connection, automation, quota, actions); $('page-rows').append(row);
    });
  }
  function facebookLink(task) {
    if (!task.post_url) return null;
    try {const url = new URL(task.post_url); if (url.protocol === 'https:' && (url.hostname === 'facebook.com' || url.hostname.endsWith('.facebook.com'))) return url.href;} catch (_) { /* Untrusted stored metadata stays plain text. */ }
    return null;
  }
  function renderTasks() {
    const query = $('task-search').value.trim().toLocaleLowerCase('vi');
    const tasks = state.tasks.filter(task => (!$('task-filter').value || task.status === $('task-filter').value) && (!$('task-page-filter').value || String(task.page_id) === $('task-page-filter').value) && [task.page_name, task.page_id, task.post_id, task.comment_content, task.id].join(' ').toLocaleLowerCase('vi').includes(query));
    $('task-result-count').textContent = `${tasks.length} / ${state.tasks.length} tác vụ gần nhất`;
    $('task-list').replaceChildren();
    if (!tasks.length) {const empty = node('div', 'panel empty'); empty.append(node('strong', '', 'Chưa có tác vụ phù hợp'), node('span', '', state.online ? 'Đổi bộ lọc để xem các trạng thái khác.' : 'Đang chờ kết nối Commenter.')); $('task-list').append(empty); return;}
    tasks.forEach(task => {
      const card = node('article', 'task-card'); const body = node('div'); const heading = node('div', 'task-heading');
      heading.append(node('strong', '', task.page_name || pageById(task.page_id)?.page_name || task.page_id), badge(taskLabels[task.status] || task.status, taskColors[task.status]));
      const meta = node('div', 'task-meta'); meta.append(node('span', '', `#${task.id} · ${task.source === 'AUTO' ? 'Tự động' : 'Thủ công'}`), node('span', '', 'Lịch: ' + dateText(task.scheduled_at)));
      if (task.sequence_step) meta.append(node('span', '', 'Bình luận #' + task.sequence_step));
      const href = facebookLink(task); if (href) {const link = node('a', '', 'Mở bài viết ↗'); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; meta.append(link);} else meta.append(node('span', 'post-id', task.post_id || ''));
      body.append(heading, meta);
      const details = node('details'); details.append(node('summary', '', 'Nội dung bình luận'), node('p', 'task-text', task.comment_content || '')); body.append(details);
      if (task.error_message) body.append(node('p', 'task-error', task.error_message));
      const actions = node('div', 'task-actions');
      if (['PENDING', 'PAUSED'].includes(task.status)) actions.append(button('Hủy tác vụ', () => taskAction(task, 'cancel'), 'secondary', mayOperate()));
      if (['FAILED', 'PAUSED', 'NEED_CHECK'].includes(task.status)) {
        actions.append(button(task.status === 'NEED_CHECK' ? 'Đối chiếu & gửi lại' : 'Khôi phục', () => taskAction(task, 'retry'), 'secondary', mayOperate()));
        actions.append(button('Xác nhận đã đăng', () => taskAction(task, 'resolve'), 'secondary', mayOperate()));
      }
      card.append(body, actions); $('task-list').append(card);
    });
  }
  async function loadTasks(report = true) {
    const request = ++state.taskRequest;
    const params = new URLSearchParams();
    if ($('task-filter').value) params.set('status', $('task-filter').value);
    if ($('task-page-filter').value) params.set('page_id', $('task-page-filter').value);
    $('task-result-count').textContent = 'Đang tải tác vụ theo bộ lọc…';
    try {
      const data = await api('tasks' + (params.size ? '?' + params.toString() : ''));
      if (request !== state.taskRequest) return;
      if (!Array.isArray(data.tasks)) throw new Error('Danh sách tác vụ không hợp lệ.');
      state.tasks = data.tasks;
      renderTasks();
    } catch (error) {if (request === state.taskRequest) {renderTasks(); if (report) notice(error.message, true);}}
  }
  async function taskAction(task, action) {
    let confirmed;
    let body = {};
    if (action === 'cancel') confirmed = await confirmAction({title: 'Hủy tác vụ #' + task.id, description: task.source === 'AUTO' ? 'Tác vụ này và các bước phía sau trong cùng chuỗi sẽ bị hủy.' : 'Tác vụ sẽ được đưa ra khỏi hàng đợi.', submit: 'Hủy tác vụ'});
    if (action === 'retry') {
      const uncertain = task.status === 'NEED_CHECK';
      confirmed = await confirmAction({title: uncertain ? 'Đối chiếu trước khi gửi lại' : 'Khôi phục tác vụ #' + task.id, description: uncertain ? 'Mở bài viết và kiểm tra bình luận thực tế trước khi đưa tác vụ về hàng đợi.' : 'Tác vụ trở về hàng đợi. Lịch gửi, Pause và các điều kiện chờ vẫn được áp dụng.', check: uncertain ? 'Tôi đã đối chiếu trên Facebook và xác nhận bình luận này chưa được đăng.' : undefined, submit: 'Đưa về hàng đợi'});
      body = {confirmed_not_posted: uncertain};
    }
    if (action === 'resolve') {
      confirmed = await confirmAction({title: 'Xác nhận bình luận đã đăng', description: 'Chỉ xác nhận sau khi đã thấy đúng bình luận trên bài viết. Tác vụ sẽ được tính thành công và chuỗi có thể chuyển sang bước kế tiếp.', check: 'Tôi đã đối chiếu đúng nội dung bình luận trên Facebook.', input: true, submit: 'Đánh dấu thành công'});
      if (confirmed) body = {comment_id: confirmed.comment_id};
    }
    if (confirmed) await mutate(`tasks/${encodeURIComponent(task.id)}/${action}`, body);
  }
  function render() {renderRuntime(); renderProfiles(); renderSafety(); renderPages(); renderTasks(); renderDetail();}
  function section(title) {const element = node('section', 'detail-section'); element.append(node('h3', '', title)); return element;}
  function fact(list, title, value) {const item = node('div'); item.append(node('dt', '', title), node('dd', '', value || '—')); list.append(item);}
  function renderDetail() {
    if (!state.selectedPage || !$('page-dialog').open) return;
    const page = pageById(state.selectedPage);
    $('page-detail').replaceChildren();
    if (!page) {$('page-detail').append(node('p', 'muted', 'Page không còn trong danh sách.')); return;}
    $('detail-name').textContent = page.page_name || page.page_id;
    $('detail-id').textContent = page.page_id;
    const connection = section('1. Kết nối');
    const modeRow = node('div', 'detail-mode'); const select = node('select'); select.setAttribute('aria-label', 'Cách gửi của Page');
    select.add(new Option('Meta Graph API', 'GRAPH_API')); select.add(new Option('Browser · Profile đã ghép', 'BROWSER')); select.value = mode(page); select.disabled = !mayOperate();
    const saveMode = button('Lưu cách gửi', async () => {
      const selected = select.value;
      if (selected === mode(page)) return;
      const confirmed = await confirmAction({title: 'Đổi cách gửi của Page', description: `Chuyển ${page.page_name || page.page_id} sang ${selected === 'BROWSER' ? 'Browser' : 'Graph API'}. Tác vụ chờ sẽ tạm dừng; cần kiểm tra kết nối trước khi khôi phục.`, submit: 'Lưu cách gửi'});
      if (confirmed) await mutate(`pages/${encodeURIComponent(page.page_id)}/send_mode`, {send_mode: selected});
    }, 'secondary', mayOperate());
    modeRow.append(select, saveMode); connection.append(modeRow);
    const facts = node('dl', 'detail-facts'); fact(facts, 'Kết nối', ready(page) ? 'Đã xác minh' : 'Cần kiểm tra'); fact(facts, 'Trạng thái Page', active(page) ? 'Hoạt động' : 'Tạm dừng'); fact(facts, 'Profile', profile(page));
    fact(facts, mode(page) === 'BROWSER' ? 'Trạng thái Browser' : 'Trạng thái Meta', mode(page) === 'BROWSER' ? page.browser_status : page.token_status);
    if (page.fb_profile_id) fact(facts, 'Facebook profile ID', page.fb_profile_id);
    connection.append(facts);
    const checks = node('div', 'detail-actions'); checks.append(button('Kiểm tra ' + modeName(page), () => mutate(`pages/${encodeURIComponent(page.page_id)}/check`, {}), 'secondary', mayOperate() && canCheck(page)));
    if (!active(page)) checks.append(button('Kích hoạt lại kết nối', () => mutate(`pages/${encodeURIComponent(page.page_id)}/reactivate`, {}), 'secondary', mayOperate() && canCheck(page)));
    connection.append(checks);
    connection.append(button('Xem tác vụ của Page →', () => {closePage(); $('task-page-filter').value = String(page.page_id); $('task-filter').value = ''; selectTab('tasks'); loadTasks();}, 'secondary'));
    if (!canCheck(page)) connection.append(node('p', 'block-reason', mode(page) === 'BROWSER' ? 'Page chưa ghép profile Browser. Ghép profile trong Fanpage Manager trước khi kiểm tra.' : 'Page chưa có kết nối Graph API. Cấu hình kết nối trong Fanpage Manager.'));
    const problems = issues(page); if (problems.length) connection.append(node('p', 'block-reason', problems.join('\n')));
    if (!state.online) connection.append(node('p', 'block-reason', 'Mất kết nối Commenter. Làm mới trước khi thao tác.'));
    const source = section('2. Chuỗi bình luận'); source.append(node('div', 'preview-loading', 'Đang tải mẫu…'));
    source.id = 'detail-sequence';
    const auto = section('3. Tự động & hạn mức'); const autoTitle = auto.querySelector('h3'); autoTitle.append(badge(enabled(page) ? 'Đã bật' : 'Đã tắt', enabled(page) ? 'blue' : ''));
    const quotas = node('dl', 'detail-facts'); fact(quotas, 'Bình luận của Page hôm nay', `${number(page.comments_used_today)} / ${number(page.max_comments_per_day) || 20}`); fact(quotas, 'Còn lại', page.comments_remaining_today == null ? String(Math.max(0, (number(page.max_comments_per_day) || 20) - number(page.comments_used_today))) : number(page.comments_remaining_today).toString());
    if (mode(page) === 'BROWSER') {const safety = pageProfileSafety(page); fact(quotas, 'Thao tác chung của profile', `${counterText(safety?.operations_today)} / ${counterText(safety?.operations_limit)}`); fact(quotas, 'Profile có thể thử lại', dateText(safety?.retry_at));}
    fact(quotas, 'Lượt gửi tiếp theo', dateText(page.next_send_at)); auto.append(quotas);
    const autoActions = node('div', 'detail-actions'); autoActions.append(button(enabled(page) ? 'Tắt tự động Page' : 'Bật tự động Page', async () => {
      const next = !enabled(page);
      if (next) {
        const confirmed = await confirmAction({title: 'Bật tự động cho Page', description: `Bật ${page.page_name || page.page_id} theo chuỗi trong Sheet. ${paused() ? 'Hệ thống vẫn Pause cho tới khi bạn chủ động tiếp tục.' : 'Hệ thống đang chạy; Page có thể tạo và gửi tác vụ theo lịch.'}`, submit: 'Bật tự động'});
        if (!confirmed) return;
      }
      await mutate(`pages/${encodeURIComponent(page.page_id)}/automation`, {enabled: next});
    }, enabled(page) ? 'secondary' : 'primary', mayOperate() && knownPause() && (enabled(page) || automationReady(page))));
    autoActions.append(button('Khôi phục tác vụ tạm dừng', async () => {
      const confirmed = await confirmAction({title: 'Khôi phục tác vụ của Page', description: 'Chỉ tác vụ tạm dừng được đưa về hàng đợi. Tác vụ cần đối chiếu vẫn giữ nguyên; lịch và hạn mức tiếp tục áp dụng.', submit: 'Khôi phục tác vụ'});
      if (confirmed) await mutate(`pages/${encodeURIComponent(page.page_id)}/resume_tasks`, {});
    }, 'secondary', mayOperate() && ready(page) && !pageProfileBlocked(page)));
    auto.append(autoActions);
    if (!enabled(page) && !automationReady(page)) auto.append(node('p', 'block-reason', 'Cần kết nối đã xác minh, Page hoạt động và nguồn Sheet hợp lệ trước khi bật tự động.'));
    $('page-detail').append(connection, source, auto);
    loadPreview(page.page_id);
  }
  async function loadPreview(id) {
    const request = ++state.previewRequest;
    try {
      const data = await api(`pages/${encodeURIComponent(id)}/preview`);
      if (request !== state.previewRequest || state.selectedPage !== id || !$('detail-sequence')) return;
      const target = $('detail-sequence'); target.replaceChildren(node('h3', '', '2. Chuỗi bình luận'));
      if (data.source_error) target.append(node('p', 'block-reason', data.source_error));
      const ordered = Array.isArray(data.comment_sequence) && data.comment_sequence.length > 0;
      const sequence = ordered ? data.comment_sequence : (Array.isArray(data.templates) ? data.templates.map(template => ({template, delay_minutes: data.delay_minutes})) : []);
      if (!sequence.length) {target.append(node('p', 'muted', 'Chưa có mẫu. Đồng bộ tab Comments - Multi-Page để cấu hình.')); return;}
      if (!ordered) target.append(node('p', 'muted', 'Một mẫu được chọn cho mỗi bài mới.'));
      const list = node('ol', 'sequence');
      sequence.forEach((step, index) => {const item = node('li'); item.dataset.step = String(index + 1); item.append(node('strong', '', `${ordered ? 'Bình luận' : 'Mẫu'} #${index + 1} · Sau bài đăng ${number(step.delay_minutes)} phút`), node('p', '', step.template || '')); list.append(item);});
      target.append(list);
    } catch (error) {if (request === state.previewRequest && $('detail-sequence')) {$('detail-sequence').replaceChildren(node('h3', '', '2. Chuỗi bình luận'), node('p', 'block-reason', error.message));}}
  }
  function openPage(id) {state.selectedPage = id; $('page-dialog').showModal(); document.body.classList.add('body-dialog-open'); renderDetail();}
  function closePage() {$('page-dialog').close(); state.selectedPage = null; state.previewRequest++; document.body.classList.remove('body-dialog-open');}
  function selectTab(name, focus = false) {
    document.querySelectorAll('[data-tab]').forEach(tab => {const selected = tab.dataset.tab === name; tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1; if (selected && focus) tab.focus(); $('panel-' + tab.dataset.tab).hidden = !selected;});
  }
  $('refresh').addEventListener('click', () => refresh({report: true}));
  $('run-preflight').addEventListener('click', runPreflight);
  $('notice-close').addEventListener('click', () => {$('notice').hidden = true;});
  ['page-search', 'profile-filter', 'mode-filter', 'page-filter'].forEach(id => $(id).addEventListener(id === 'page-search' ? 'input' : 'change', renderPages));
  $('task-search').addEventListener('input', renderTasks);
  ['task-filter', 'task-page-filter'].forEach(id => $(id).addEventListener('change', () => {renderTasks(); loadTasks();}));
  $('clear-filters').addEventListener('click', () => {['page-search', 'profile-filter', 'mode-filter', 'page-filter'].forEach(id => {$(id).value = '';}); renderPages();});
  document.querySelectorAll('[data-tab]').forEach(tab => {tab.addEventListener('click', () => selectTab(tab.dataset.tab)); tab.addEventListener('keydown', event => {const tabs = [...document.querySelectorAll('[data-tab]')]; const index = tabs.indexOf(tab); if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; selectTab(tabs[next].dataset.tab, true);}});});
  $('open-review').addEventListener('click', () => {$('task-filter').value = 'NEED_CHECK'; renderTasks(); selectTab('tasks'); loadTasks();});
  $('open-pending').addEventListener('click', () => {$('task-filter').value = 'PENDING'; renderTasks(); selectTab('tasks'); loadTasks();});
  $('close-page').addEventListener('click', closePage);
  $('page-dialog').addEventListener('cancel', event => {event.preventDefault(); closePage();});
  $('copy-id').addEventListener('click', async () => {try {await navigator.clipboard.writeText(String(state.selectedPage)); notice('Đã sao chép Page ID.');} catch (_) {notice('Không sao chép được. Page ID hiển thị bên dưới tên Page.', true);}});
  $('pause-toggle').addEventListener('click', async () => {
    if (!state.online || !knownPause() || !paused() || state.resuming) {await requestPause(); return;}
    if (!mayOperate()) return;
    const next = !paused();
    if (!next) {
      const confirmed = await confirmAction({title: 'Tiếp tục hệ thống', description: `${state.pages.filter(enabled).length} Page đã bật tự động. Commenter sẽ kiểm tra nguồn trước khi nhận lượt gửi mới.`, check: 'Tôi đã kiểm tra các Page và chuỗi bình luận được bật.', submit: 'Tiếp tục hệ thống'});
      if (!confirmed) return;
    }
    state.resuming = true;
    try {await mutate('system/pause', {paused: next}, 'Đã xử lý yêu cầu tiếp tục.');}
    finally {state.resuming = false; renderRuntime();}
  });
  $('sheet-url').addEventListener('input', () => {$('sheet-url').dataset.edited = '1';});
  $('sheet-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!mayOperate()) return;
    const url = $('sheet-url').value.trim();
    try {const parsed = new URL(url); if (parsed.protocol !== 'https:' || parsed.hostname !== 'docs.google.com' || !parsed.pathname.startsWith('/spreadsheets/d/')) throw new Error();} catch (_) {notice('Nhập đường dẫn Google Sheet hợp lệ.', true); return;}
    await mutate('sheets/sync', {sheet_url: url}, 'Đã đồng bộ nguồn bình luận.');
  });
  refresh();
  window.setInterval(() => {if (!document.hidden && !$('confirm-dialog').open && !$('page-dialog').open) refresh();}, 20000);
})();
