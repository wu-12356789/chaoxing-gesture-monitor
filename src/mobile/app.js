const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('panel-key') || '';
let config = null;
let busy = false;
let timer;

function notice(message) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}

async function api(path, data) {
  const response = await fetch('/api/' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
    signal: AbortSignal.timeout(45000)
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) disconnect();
    throw new Error(result.error || '请求失败，请稍后重试');
  }
  return result;
}

function drawPresets(gestures) {
  $('presets').replaceChildren();
  for (const code of gestures) {
    const item = document.createElement('div');
    item.className = 'preset';
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 200;
    canvas.setAttribute('aria-label', '手势轨迹 ' + code);
    canvas.setAttribute('role', 'img');
    const context = canvas.getContext('2d');
    const point = n => [30 + ((n - 1) % 3) * 70, 30 + Math.floor((n - 1) / 3) * 70];
    context.strokeStyle = '#428366';
    context.lineWidth = 6;
    context.lineCap = context.lineJoin = 'round';
    context.beginPath();
    [...code].forEach((n, i) => { const [x, y] = point(Number(n)); if (i === 0) context.moveTo(x, y); else context.lineTo(x, y); });
    context.stroke();
    for (let n = 1; n <= 9; n++) {
      const [x, y] = point(n);
      context.beginPath();
      context.arc(x, y, 8, 0, Math.PI * 2);
      context.fillStyle = code.includes(String(n)) ? '#254e3e' : '#cbd6c8';
      context.fill();
    }
    const label = document.createElement('code');
    label.textContent = code;
    item.append(canvas, label);
    $('presets').append(item);
  }
}

function render(state) {
  $('pairing').hidden = true;
  $('dashboard').hidden = false;
  $('status').textContent = state.read_detail ? state.status + '。学习通提示：' + state.read_detail : state.status;
  $('dot').classList.toggle('running', state.running);
  $('start').disabled = state.running || !state.logged_in;
  $('stop').disabled = !state.running;
  $('read-check').disabled = state.running || !state.logged_in;
  $('login-card').hidden = state.logged_in;
  $('last-scan').textContent = state.last_scan ? '最近检测 ' + new Date(state.last_scan).toLocaleTimeString('zh-CN') : '尚未完成检测';
  $('connection').textContent = '已连接服务器';
  $('mail-status').textContent = state.mail_configured ? '已配置，提醒将发往 ' + state.mail_recipient : '尚未启用，请先设置发信邮箱并发送测试邮件。';
  const changed = !config || JSON.stringify(config.gestures) !== JSON.stringify(state.config.gestures);
  if (changed) drawPresets(state.config.gestures);
  if (!config) {
    $('mail-to').value = state.mail_recipient || '';
    if (/@qq\.com$/i.test(state.mail_recipient || '')) $('mail-from').value = state.mail_recipient;
    $('gestures').value = state.config.gestures.join('\n');
    $('interval').value = state.config.poll_seconds;
  }
  config = state.config;
  $('events').replaceChildren();
  for (const text of [...state.events].reverse()) {
    const item = document.createElement('li');
    item.textContent = text;
    $('events').append(item);
  }
  if (!state.events.length) $('events').textContent = '开始监测后，记录会显示在这里。';
}

function disconnect() {
  clearTimeout(timer);
  token = '';
  config = null;
  sessionStorage.removeItem('panel-key');
  $('dashboard').hidden = true;
  $('pairing').hidden = false;
  $('key').value = '';
}

async function refresh() {
  clearTimeout(timer);
  if (!token || busy) return;
  try {
    render(await api('status'));
  } catch (error) {
    $('connection').textContent = '连接中断，正在重试';
    notice(error.message);
  }
  if (token) timer = setTimeout(refresh, 5000);
}

async function action(path, data) {
  if (busy) return;
  busy = true;
  clearTimeout(timer);
  notice('正在处理…');
  try {
    render(await api(path, data));
    notice('');
  } catch (error) {
    notice(error.message);
  } finally {
    busy = false;
    if (token) timer = setTimeout(refresh, 5000);
  }
}

$('pair-form').addEventListener('submit', async event => {
  event.preventDefault();
  token = $('key').value.trim();
  try {
    render(await api('status'));
    sessionStorage.setItem('panel-key', token);
    $('key').value = '';
    notice('');
    refresh();
  } catch (error) { notice(error.message); }
});
$('login-form').addEventListener('submit', event => {
  event.preventDefault();
  const password = $('password').value;
  $('password').value = '';
  action('login', { account: $('account').value, password });
});
$('config-form').addEventListener('submit', event => {
  event.preventDefault();
  action('config', { ...config, gestures: $('gestures').value.split(/[\s,，]+/).filter(Boolean), poll_seconds: Number($('interval').value) });
});
$('start').addEventListener('click', () => action('start', {}));
$('read-check').addEventListener('click', () => action('read-check', {}));
$('mail-form').addEventListener('submit', event => {
  event.preventDefault();
  const password = $('mail-password').value;
  $('mail-password').value = '';
  action('mail', { sender: $('mail-from').value.trim(), recipient: $('mail-to').value.trim(), password });
});
$('stop').addEventListener('click', () => action('stop', {}));
$('disconnect').addEventListener('click', () => { disconnect(); notice('已断开面板连接，服务器上的监测状态保持。'); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
if (token) refresh();
