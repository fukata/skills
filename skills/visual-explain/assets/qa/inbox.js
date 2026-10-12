/* エージェントからの質問（Claude Code の AskUserQuestion・Codex の request_user_input）の受け箱。serve.py の /__qa/inbox に出す。
 * 配信サーバーとつないだままにし（/__qa/events）、質問が増えたら一覧を取り直す。答えを待っている質問があれば、
 * その資料を開く（開いた時点で待っている質問も。「自動で開く」を外せば知らせるだけ）。 */
(function () {
  'use strict';
  var API = '/__qa/';
  var KEY = 've-qa:inbox-auto';
  var baseTitle = document.title, OPENED = 've-qa:inbox-opened';

  function opened() {
    try { return JSON.parse(sessionStorage.getItem(OPENED) || '{}') || {}; } catch (e) { return {}; }
  }

  if (/[?&]k=/.test(location.search)) {
    var clean = new URL(location.href);
    clean.searchParams.delete('k');
    history.replaceState(history.state, '', clean.pathname + clean.search + clean.hash);
  }

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') e.textContent = attrs[k]; else e.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  }

  var auto = true;
  try { auto = localStorage.getItem(KEY) !== 'off'; } catch (e) { /* 保存できない環境 */ }
  var autoBox = el('input', { type: 'checkbox', id: 've-qa-inbox-auto' });
  autoBox.checked = auto;
  autoBox.addEventListener('change', function () {
    auto = autoBox.checked;
    try { localStorage.setItem(KEY, auto ? 'on' : 'off'); } catch (e) { /* 保存できない環境 */ }
  });
  var status = el('p', { class: 've-qa-note', role: 'status' });
  var alerts = el('div', { class: 've-qa-alerts' });
  var list = el('ol', { class: 've-qa-inbox-list', 'aria-live': 'polite' });
  var main = el('main', { class: 've-qa ve-qa-inbox' }, [
    el('h1', { text: 'エージェントからの質問' }),
    el('p', { text: 'Claude Code（AskUserQuestion）や Codex（request_user_input）が質問すると、その資料をここから開きます（開いた時点で答えを待っている質問も開きます）。資料の右の欄で選んで送ると、答えがそのセッションの作業に戻ります。このページを一度開くと、それ以降の質問は、ターミナルではなくページで答えを待ちます（ターミナルには「running PreToolUse hooks…」とだけ出ます）。' }),
    el('p', {}, [autoBox, el('label', { for: 've-qa-inbox-auto', text: ' 質問が来たら自動で開く' })]),
    alerts, status, list
  ]);
  document.body.appendChild(main);

  var LABEL = { waiting: '答えを待っています', answered: '答えた', terminal: 'ターミナルで答える', expired: '締め切った' };

  function clock(iso) {
    var d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function open(a) { return a.status === 'waiting' && a.waiting; }

  function render(asks) {
    list.textContent = '';
    if (!asks.length) list.appendChild(el('li', { text: 'まだ質問はありません。' }));
    asks.forEach(function (a) {
      var state = a.status === 'waiting' && !a.waiting ? 'ターミナルで答える' : LABEL[a.status] || a.status;
      list.appendChild(el('li', { 'data-status': open(a) ? 'waiting' : a.status }, [
        el('a', { href: '/' + a.doc.split('/').map(encodeURIComponent).join('/'), text: a.question }),
        el('p', { class: 've-qa-meta', text: [state, a.where && a.session && a.session.branch ? a.where + '（' + a.session.branch + '）' : a.where,
          clock(a.created_at)].filter(Boolean).join(' · ') }),
        a.session && a.session.prompt ? el('p', { class: 've-qa-meta', text: '最初の依頼: 「' + a.session.prompt + '」' }) : document.createTextNode('')
      ]));
    });
    var waiting = asks.filter(open).length;
    document.title = (waiting ? '(' + waiting + ') ' : '') + baseTitle;
  }

  var timer = null;

  // ---- 知らせ方。デスクトップ通知は HTTPS（または localhost）のページで許可したときだけ使える。音は一度押して有効にする

  var soundOn = false, audioCtx = null, known = null;
  try { soundOn = localStorage.getItem('ve-qa:sound') === 'on'; } catch (e) { /* 保存できない環境 */ }

  function chime() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') audioCtx.resume();
      var t = audioCtx.currentTime, o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.frequency.value = 880;
      o.connect(g);
      g.connect(audioCtx.destination);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.6);
      o.start(t);
      o.stop(t + 0.6);
    } catch (e) { /* 音を出せない環境 */ }
  }

  function label(a) {
    var s = a.session || {};
    return [a.where && s.branch ? a.where + '（' + s.branch + '）' : a.where, s.prompt ? '「' + s.prompt + '」' : '']
      .filter(Boolean).join(' · ');
  }

  function notify(a) {
    if (soundOn) chime();
    if (!window.isSecureContext || typeof Notification !== 'function' || Notification.permission !== 'granted') return;
    try {
      var n = new Notification((a.agent_name || 'エージェント') + ' からの質問', { body: a.question + (label(a) ? '\n' + label(a) : ''), tag: 've-qa-ask:' + a.doc });
      n.onclick = function () { window.focus(); location.href = '/' + a.doc.split('/').map(encodeURIComponent).join('/'); n.close(); };
    } catch (e) { /* 通知を出せない環境 */ }
  }

  function renderAlerts() {
    alerts.textContent = '';
    if (window.isSecureContext && typeof Notification === 'function') {
      if (Notification.permission === 'default') {
        var allow = el('button', { type: 'button', class: 've-qa-link', text: 'デスクトップ通知を有効にする' });
        allow.addEventListener('click', function () { Notification.requestPermission().then(renderAlerts); });
        alerts.appendChild(allow);
      } else {
        alerts.appendChild(el('span', { text: Notification.permission === 'granted'
          ? 'デスクトップ通知: 有効（このタブが裏にあっても、質問が来たら通知します）' : 'デスクトップ通知: ブラウザで止められています' }));
      }
    } else {
      alerts.appendChild(el('span', { text: 'デスクトップ通知は HTTPS の受け箱で使えます（serve.py ask-config --https on）' }));
    }
    var sound = el('input', { type: 'checkbox', id: 've-qa-sound' });
    sound.checked = soundOn;
    sound.addEventListener('change', function () {
      soundOn = sound.checked;
      try { localStorage.setItem('ve-qa:sound', soundOn ? 'on' : 'off'); } catch (e) { /* 保存できない環境 */ }
      if (soundOn) chime();  // 押したときに鳴らしておくと、ブラウザがこのページで音を出せるようになる
    });
    alerts.append(' ', sound, el('label', { for: 've-qa-sound', text: ' 音で知らせる' }));
  }

  renderAlerts();

  function poll() {
    clearTimeout(timer);
    fetch(API + 'asks', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        if (r.status === 401) { status.textContent = '受け箱は、案内された URL（?k= 付き）で開き直してください。'; return null; }
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (st) {
        if (!st) return;
        status.textContent = '';
        var asks = st.asks || [];
        render(asks);
        // 開いた後に新しく来た質問を知らせる（開いた時点で既にあった質問は一覧で見える）
        var waiting = asks.filter(open);
        if (known) waiting.filter(function (a) { return !known[a.doc]; }).forEach(notify);
        known = known || {};
        waiting.forEach(function (a) { known[a.doc] = true; });
        // 答えを待っている質問を開く。開いた時点で既に待っている質問も開く（質問が来てから受け箱を開けば済むように）。
        // ただし、このタブで一度開いた質問はもう開かない（質問の資料から受け箱に戻ったときに、また飛ばないように）
        var fresh = asks.filter(function (a) { return open(a) && !opened()[a.doc]; });
        if (fresh.length && auto) {
          var seen = opened();
          seen[fresh[0].doc] = true;
          try { sessionStorage.setItem(OPENED, JSON.stringify(seen)); } catch (e) { /* 保存できない環境 */ }
          location.href = '/' + fresh[0].doc.split('/').map(encodeURIComponent).join('/');
        }
      })
      .catch(function () { status.textContent = '配信サーバーに接続できません。Claude Code か Codex のセッションを始めると、配信サーバーはまた起動します。'; })
      .then(function () { timer = setTimeout(poll, 30000); });  // 締め切りなどの表示を直すため。質問が来たら下の知らせで取り直す
  }

  // つないでいる間は、エージェントの質問をページに出す。質問が増えた・締め切られたら知らせが来る（切れたら自動でつなぎ直す）
  if (typeof EventSource === 'function') {
    var events = new EventSource(API + 'events');
    events.addEventListener('asks', poll);
    events.addEventListener('open', poll);
  }
  poll();
})();
