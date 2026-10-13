/* visual-explain の資料に、配信サーバー（serve.py）が差し込む質問欄。
 * 質問を送り、回答と本文の更新を数秒おきに取りに行く。本文の版が変わったら、本文を取り直して差し替える。
 * explanation.html そのものには含めない（ファイルを直接開いたときや印刷では出ない）。
 * data-mode="window" のときは、質問欄だけを別ウィンドウ（/__qa/panel）に出す。資料のウィンドウとは
 * BroadcastChannel でやり取りし、読んでいる節・選んだ文字を受け取り、節へのリンクで資料のウィンドウを動かす。
 * 回答を待つ間は claude の途中経過を出し、待機中・作成中の質問は取り消せる。回答が届いたとき、
 * このタブを見ていなければタイトルの先頭に件数を付ける。回答した質問は、資料の末尾の節に書き出せる。
 * 回答には続けて質問できる（「この回答に質問する」か、回答の中の文字を選ぶ）。続きの質問は、元の質問のすぐ下に並べる。
 * エージェント（Claude Code の AskUserQuestion・Codex の request_user_input）が質問した資料（ask_hook.py が
 * 登録したもの）では、質問欄の上に選択肢を出し、選んだ答えを配信サーバーに送る（フックが受け取り、元のセッションに返す）。 */
(function () {
  'use strict';
  var script = document.currentScript;
  var DOC = script.dataset.doc;
  var API = '/__qa/';
  var FAST = 3000, SLOW = 15000;
  // 質問欄の幅。左端のつまみで変えられ、配信しているマシンに覚える（ポートやアドレスが違っても、そのマシンから配信する
  // 資料で共通）。ブラウザにも覚え、配信サーバーが覚えていないときに使う
  var WIDTH = { def: 440, min: 320, keepDoc: 240, pushDoc: 760, step: 40, key: 've-qa:width' };
  var WIN = script.dataset.mode === 'window';
  var PANEL_URL = API + 'panel?doc=' + encodeURIComponent(DOC);
  var DOC_URL = '/' + DOC.split('/').map(encodeURIComponent).join('/');
  var WINDOW_NAME = 've-qa-' + DOC.replace(/[^\w-]/g, '_');
  var channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('ve-qa:' + DOC) : null;

  var rev = script.dataset.rev;
  var items = [], qa = { enabled: true, reason: null }, auth = true, online = true;
  var open = false, quote = '', unseen = 0, timer = null, swapping = false, loaded = false;
  var polling = false, pollAgain = false, sending = false;
  var detached = false, popup = null, peerSection = null, gotoTimer = null, quoteSection = null;
  var clockSkew = 0, finished = {}, rendered = {};
  var baseTitle = document.title, titleUnseen = 0, exported = null;
  var ask = null, askSig = '', askSeen = {}, askSending = false, askEvents = null;
  // 続きの質問の元（質問の id）と、選んだ文字の出どころ（'doc' は資料の本文、'answer' は元の回答）
  var replyTo = null, quoteFrom = '';

  // 合言葉（?k=）は Cookie に移ったので、アドレス欄から消す（URL をそのまま人に渡しても質問の権限が渡らないように）
  if (/[?&]k=/.test(location.search)) {
    var clean = new URL(location.href);
    clean.searchParams.delete('k');
    history.replaceState(history.state, '', clean.pathname + clean.search + clean.hash);
  }

  // ---- 表示の部品

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') e.textContent = attrs[k];
      else if (k === 'hidden') e.hidden = attrs[k];
      else e.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  }

  var root = el('div', { id: 've-qa-root', class: WIN ? 've-qa ve-qa-standalone' : 've-qa' });
  var toggle = el('button', { type: 'button', class: 've-qa-toggle', 'aria-expanded': 'false', 'aria-controls': 've-qa-panel' });
  var toggleLabel = el('span', { text: '質問する' });
  var badge = el('span', { class: 've-qa-badge', hidden: true });
  toggle.append(toggleLabel, badge);
  var panel = el('aside', { id: 've-qa-panel', class: 've-qa-panel', 'aria-label': 'この資料への質問と回答', hidden: !WIN });
  var close = el('button', { type: 'button', class: 've-qa-close', 'aria-label': '質問欄を閉じる', text: '×' });
  var detachBtn = el('button', { type: 'button', class: 've-qa-detach', text: WIN ? '資料に戻す' : '別ウィンドウで開く' });
  var notice = el('p', { class: 've-qa-notice', role: 'status', hidden: true });
  var list = el('ol', { class: 've-qa-list', 'aria-live': 'polite' });
  var empty = el('p', { class: 've-qa-empty', text: 'まだ質問はありません。資料を読んで分からない所を送ってください。' });
  var quoteBox = el('div', { class: 've-qa-quote', hidden: true });
  var quoteText = el('q');
  var quoteClear = el('button', { type: 'button', text: '外す' });
  var quoteLabel = el('span', { text: '選んだ部分: ' });
  quoteBox.append(quoteLabel, quoteText, quoteClear);
  var replyBox = el('div', { class: 've-qa-quote ve-qa-replyto', hidden: true });
  var replyLabel = el('span');
  var replyText = el('q');
  var replyClear = el('button', { type: 'button', text: '外す' });
  replyBox.append(replyLabel, replyText, replyClear);
  var where = el('span', { class: 've-qa-where' });
  var input = el('textarea', { id: 've-qa-input', rows: '3', maxlength: '4000',
    placeholder: '分からない所を書いてください（Ctrl+Enter で送る）' });
  var error = el('span', { class: 've-qa-error', role: 'alert' });
  var send = el('button', { type: 'submit', class: 've-qa-send', text: '送る' });
  var exportBtn = el('button', { type: 'button', class: 've-qa-link ve-qa-export', text: '回答を資料に書き出す',
    title: '質問と回答を資料の末尾の節に書き出します。資料のファイルを人に渡したり印刷したりしても読めます' });
  var tools = el('div', { class: 've-qa-tools', hidden: true }, [exportBtn]);
  var askBox = el('section', { class: 've-qa-ask', hidden: true, 'aria-label': 'エージェントからの質問' });
  var askOthers = el('section', { class: 've-qa-ask-others', hidden: true, 'aria-label': 'ほかに答えを待っている質問' });
  var form = el('form', { class: 've-qa-form' }, [
    replyBox,
    quoteBox,
    el('label', { for: 've-qa-input', class: 've-qa-label' }, ['質問 ', where]),
    input,
    el('div', { class: 've-qa-actions' }, [error, send])
  ]);
  var headButtons = WIN ? [detachBtn] : (channel ? [detachBtn, close] : [close]);
  var agentNote = el('p', { class: 've-qa-note', text: noteText() });
  panel.append(
    el('div', { class: 've-qa-head' }, [el('h2', { text: '質問と回答' })].concat(headButtons)),
    agentNote,
    askBox, askOthers, notice, empty, list, tools, form
  );
  var resizer = el('div', { class: 've-qa-resize', role: 'separator', 'aria-orientation': 'vertical', tabindex: '0',
    'aria-label': '質問欄の幅（ドラッグか ← → で変える。ダブルクリックで元に戻す）',
    title: 'ドラッグで幅を変える（ダブルクリックで元に戻す）' });
  if (!WIN) panel.insertBefore(resizer, panel.firstChild);
  var toast = el('div', { class: 've-qa-toast', role: 'status', hidden: true });
  if (WIN) root.append(panel, toast); else root.append(toggle, panel, toast);
  document.body.appendChild(root);

  // ---- 回答の HTML を、許可したタグ・属性だけにする

  var ALLOWED = new Set(('p ul ol li strong em b i code pre a br h3 h4 h5 table thead tbody tfoot tr th td caption ' +
    'figure figcaption blockquote details summary dl dt dd div span hr sup sub kbd mark small img ' +
    'svg g path rect circle ellipse line polyline polygon text tspan defs marker title desc').split(' '));
  var DROP = new Set(('script style iframe object embed noscript template form input button textarea select link ' +
    'meta base foreignobject use image animate set').split(' '));
  var ATTRS = new Set(('href title alt class id colspan rowspan scope open start reversed lang dir src width height ' +
    'viewbox xmlns role aria-label aria-labelledby aria-hidden d x y x1 y1 x2 y2 cx cy r rx ry points fill stroke ' +
    'stroke-width stroke-dasharray stroke-linecap stroke-linejoin font-size font-weight font-family text-anchor ' +
    'dominant-baseline transform marker-end marker-start markerwidth markerheight refx refy orient markerunits ' +
    'opacity fill-opacity stroke-opacity dx dy preserveaspectratio').split(' '));

  // 図の塗り・線・矢印の url(...) は、同じ回答の中の id（url(#...)）と色の関数だけ許す（外の資源を読ませない）
  var PAINT = new Set(['fill', 'stroke', 'marker-end', 'marker-start']);
  var PAINT_OK = /^\s*(?:url\(\s*['"]?#[\w:.-]+['"]?\s*\)|(?:rgba?|hsla?)\(\s*[\d.,%\s\/]+\))\s*$/i;

  function safePaint(value) { return !/[(\\]/.test(value) || PAINT_OK.test(value); }

  function safeUrl(name, value) {
    // ブラウザは URL の \ を / と読み、空白・制御文字を無視する（\\evil.example は //evil.example になる）
    var v = value.trim().toLowerCase().replace(/[\u0000- ]/g, '').replace(/\\/g, '/');
    if (name === 'src') return !/^[a-z][a-z0-9+.-]*:/.test(v) && v.indexOf('//') !== 0;
    return v.charAt(0) === '#' || /^https?:/.test(v) || !/^[a-z][a-z0-9+.-]*:/.test(v);
  }

  function sanitize(html) {
    var doc = new DOMParser().parseFromString('<!doctype html><body>' + (html || ''), 'text/html');
    Array.prototype.forEach.call(doc.body.querySelectorAll('*'), function (node) {
      var tag = node.localName.toLowerCase();
      if (DROP.has(tag)) { node.remove(); return; }
      if (!ALLOWED.has(tag)) { while (node.firstChild) node.parentNode.insertBefore(node.firstChild, node); node.remove(); return; }
      Array.prototype.slice.call(node.attributes).forEach(function (a) {
        var name = a.name.toLowerCase();
        var bad = !ATTRS.has(name) || name.indexOf('on') === 0 ||
          ((name === 'href' || name === 'src') && !safeUrl(name, a.value)) || (PAINT.has(name) && !safePaint(a.value));
        if (bad) node.removeAttribute(a.name);
      });
      if (tag === 'a' && /^https?:/i.test(node.getAttribute('href') || '')) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    });
    var frag = document.createDocumentFragment();
    Array.prototype.forEach.call(doc.body.childNodes, function (n) { frag.appendChild(document.importNode(n, true)); });
    return frag;
  }

  // ---- 資料の図の大きさ

  function naturalSize() {
    // width の無い図（古い雛形の資料）に viewBox の大きさを書き、広い画面で本文の幅いっぱいまで拡大しないようにする。
    // 大きさは qa.css の .diagram svg[width] で、本来の大きさまで・狭いときは縮む、にする
    Array.prototype.forEach.call(document.querySelectorAll('.diagram svg:not([width])'), function (svg) {
      if (ownNode(svg) || (svg.parentElement && svg.parentElement.closest('svg'))) return;
      var vb = svg.viewBox && svg.viewBox.baseVal;
      if (!vb || !vb.width || !vb.height) return;
      svg.setAttribute('width', String(vb.width));
      svg.setAttribute('height', String(vb.height));
    });
  }

  // ---- 読んでいる節と、選んだ文字

  function ownNode(n) { return n && (n === root || root.contains(n)); }

  function sections() {
    return Array.prototype.filter.call(document.querySelectorAll('section[id]'), function (s) { return !ownNode(s); });
  }

  function describe(sec) {
    var h = sec.querySelector('h2, h3');
    return { id: sec.id, title: h ? h.textContent.replace(/\s+/g, ' ').trim().slice(0, 200) : sec.id };
  }

  function scrolledSection() {
    // 画面の上端にある節。いちばん下までスクロールしたら最後の節
    var all = sections(), pick = null;
    var bottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
    all.forEach(function (s) { if (s.getBoundingClientRect().top <= 120) pick = s; });
    if (bottom && all.length && window.scrollY > 0) pick = all[all.length - 1];
    return pick || all[0] || null;
  }

  function currentSection() {
    if (WIN) return peerSection;
    // 文字を選んでいれば、その文字がある節。なければ画面の上端にある節
    if (quote && quoteSection && document.contains(quoteSection)) return describe(quoteSection);
    var pick = scrolledSection();
    return pick ? describe(pick) : null;
  }

  function updateToc() {
    // 資料の目次で、読んでいる節の項目に aria-current を付ける（見た目は資料と qa.css の CSS）
    var pick = scrolledSection(), href = pick ? '#' + pick.id : null;
    Array.prototype.forEach.call(document.querySelectorAll('nav a[href^="#"]'), function (a) {
      if (ownNode(a)) return;
      if (href && a.getAttribute('href') === href) a.setAttribute('aria-current', 'true');
      else a.removeAttribute('aria-current');
    });
  }

  function updateWhere() {
    var s = currentSection();
    where.textContent = s ? '（読んでいる節: ' + s.title + '）' : '';
  }

  document.addEventListener('selectionchange', function () {
    var sel = document.getSelection();
    if (!sel || sel.isCollapsed || !sel.anchorNode) return;
    var text = sel.toString().replace(/\s+/g, ' ').trim();
    if (!text) return;
    var node = sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement;
    if (ownNode(node)) {
      // 回答の中の文字を選んだら、その回答への続きの質問にする
      var a = node.closest('.ve-qa-a'), li = a && a.closest('.ve-qa-item');
      var item = li && findItem(li.getAttribute('data-id'));
      if (!canReply(item)) return;
      quote = text.slice(0, 2000);
      quoteFrom = 'answer';
      quoteSection = null;
      replyTo = item.id;
      if (WIN) post({ type: 'clear-quote' });  // 資料のウィンドウで選んでいた文字は外す
    } else if (WIN) {
      return;
    } else {
      quote = text.slice(0, 2000);
      quoteFrom = 'doc';
      quoteSection = node && node.closest('section[id]');
    }
    updateWhere();
    renderQuote();
    renderReply();
    sendContext();
  });

  function label(id) { return String(id).toUpperCase(); }

  function findItem(id) { return items.filter(function (i) { return i.id === id; })[0]; }

  function canReply(item) { return !!item && item.status === 'answered' && item.kind !== 'build'; }

  function renderReply() {
    var item = replyTo && findItem(replyTo);
    replyBox.hidden = !replyTo;
    replyLabel.textContent = replyTo ? label(replyTo) + ' の回答への質問: ' : '';
    replyText.textContent = item ? item.question : '';
  }

  function renderQuote() {
    quoteBox.hidden = !quote;
    quoteLabel.textContent = quoteFrom === 'answer' && replyTo ? label(replyTo) + ' の回答で選んだ部分: ' : '選んだ部分: ';
    quoteText.textContent = quote.length > 140 ? quote.slice(0, 140) + '…' : quote;
    toggleLabel.textContent = detached ? '質問欄（別ウィンドウ）' : quote && !open ? '選んだ所を質問する' : '質問する';
  }

  function clearQuote() {
    quote = '';
    quoteFrom = '';
    renderQuote();
    if (WIN) post({ type: 'clear-quote' }); else sendContext();
  }

  quoteClear.addEventListener('click', function () { clearQuote(); input.focus(); });
  replyClear.addEventListener('click', function () {
    replyTo = null;
    if (quoteFrom === 'answer') clearQuote();
    renderReply();
    input.focus();
  });

  // ---- 開閉

  function setOpen(v) {
    if (WIN) return;
    if (v && detached) { focusPopup(); return; }
    open = v;
    panel.hidden = !v;
    toggle.setAttribute('aria-expanded', String(v));
    document.body.classList.toggle('ve-qa-open', v);
    if (v) {
      unseen = 0;
      updateWhere();
      renderBadge();
      list.lastElementChild && list.lastElementChild.scrollIntoView({ block: 'nearest' });
      input.focus();
    }
    renderQuote();
    schedule(0);
  }

  toggle.addEventListener('click', function () { setOpen(detached || !open); });
  close.addEventListener('click', function () { setOpen(false); toggle.focus(); });
  panel.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !WIN) { setOpen(false); toggle.focus(); } });
  var scrollTimer = null, tocFrame = 0;
  window.addEventListener('scroll', function () {
    if (WIN) return;
    if (!tocFrame) tocFrame = requestAnimationFrame(function () { tocFrame = 0; updateToc(); });
    if (open) updateWhere();
    if (detached && !scrollTimer) scrollTimer = setTimeout(function () { scrollTimer = null; sendContext(); }, 300);
  }, { passive: true });

  // ---- 幅

  // 配信サーバーが覚えた幅（資料に付けて返す）を先に使う。ブラウザの保存は配信元ごとに別々なので、ほかのポート・アドレスで
  // 変えた幅を知らない
  var wantWidth = parseInt(script.dataset.width, 10) || 0, panelWidth = 0, widthTimer = null;
  if (!wantWidth) {
    try { wantWidth = parseInt(localStorage.getItem(WIDTH.key), 10) || 0; } catch (e) { /* 保存できない環境 */ }
  }
  wantWidth = wantWidth || WIDTH.def;

  // 覚えた幅（wantWidth）を、ウィンドウに収まるように縮めて出す。狭いウィンドウで開いても、広げれば覚えた幅に戻る
  function showWidth(w) {
    var max = Math.max(WIDTH.min, window.innerWidth - WIDTH.keepDoc);
    panelWidth = Math.round(Math.min(max, Math.max(WIDTH.min, w)));
    document.documentElement.style.setProperty('--ve-qa-width', panelWidth + 'px');
    resizer.setAttribute('aria-valuemin', String(WIDTH.min));
    resizer.setAttribute('aria-valuemax', String(max));
    resizer.setAttribute('aria-valuenow', String(panelWidth));
    // 資料の幅が十分残るときだけ資料を左に寄せ、足りなければ資料の上に重ねる
    document.body.classList.toggle('ve-qa-push', window.innerWidth > 600 && window.innerWidth - panelWidth >= WIDTH.pushDoc);
  }

  // 開いている人が幅を変えた
  function setWidth(w, save) {
    showWidth(w);
    wantWidth = panelWidth;
    if (save) saveWidth();
  }

  function saveWidth() {
    try { localStorage.setItem(WIDTH.key, String(wantWidth)); } catch (e) { /* 保存できない環境 */ }
    // キーを押し続けても 1 回にまとめて送る
    clearTimeout(widthTimer);
    widthTimer = setTimeout(function () { postJson('prefs', { width: wantWidth }).catch(function () { /* ブラウザの保存だけ残る */ }); }, 400);
  }

  if (!WIN) {
    showWidth(wantWidth);
    resizer.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      resizer.setPointerCapture(e.pointerId);
      document.body.classList.add('ve-qa-resizing');
    });
    resizer.addEventListener('pointermove', function (e) {
      if (resizer.hasPointerCapture && resizer.hasPointerCapture(e.pointerId)) setWidth(window.innerWidth - e.clientX, false);
    });
    var endDrag = function (e) {
      if (!document.body.classList.contains('ve-qa-resizing')) return;
      document.body.classList.remove('ve-qa-resizing');
      if (resizer.hasPointerCapture && resizer.hasPointerCapture(e.pointerId)) resizer.releasePointerCapture(e.pointerId);
      setWidth(panelWidth, true);
    };
    resizer.addEventListener('pointerup', endDrag);
    resizer.addEventListener('pointercancel', endDrag);
    resizer.addEventListener('dblclick', function () { setWidth(WIDTH.def, true); });
    resizer.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); setWidth(panelWidth + WIDTH.step, true); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); setWidth(panelWidth - WIDTH.step, true); }
    });
    window.addEventListener('resize', function () { showWidth(wantWidth); });
  }

  // ---- 別ウィンドウ

  function post(msg) { if (channel) channel.postMessage(msg); }

  function sendContext() {
    if (!WIN && detached) post({ type: 'context', section: currentSection(), quote: quote });
  }

  function setDetached(v) {
    detached = v;
    if (v && open) setOpen(false);
    if (!v) popup = null;
    renderQuote();
    if (v) sendContext();
  }

  function focusPopup() {
    // 再読み込みした後は参照が無いので、名前で既存のウィンドウを探す。無ければ空のウィンドウが開くので、質問欄を読み込む
    var w = popup && !popup.closed ? popup : window.open('', WINDOW_NAME);
    if (!w) { setDetached(false); setOpen(true); return; }
    popup = w;
    try { if (w.location.href === 'about:blank') w.location.replace(PANEL_URL); } catch (e) { /* 別の配信元 */ }
    w.focus();
  }

  detachBtn.addEventListener('click', function () {
    if (WIN) {
      post({ type: 'attach' });
      window.close();
      // スクリプトで開いたウィンドウでなければ閉じられないので、資料を開く
      setTimeout(function () { location.href = DOC_URL; }, 300);
      return;
    }
    popup = window.open(PANEL_URL, WINDOW_NAME, 'popup,width=480,height=820');
    if (!popup) { error.textContent = '別ウィンドウを開けませんでした（ブラウザがポップアップを止めた可能性があります）。'; return; }
    setDetached(true);
  });

  function goTo(id) {
    var sec = document.getElementById(id);
    if (!sec || ownNode(sec)) return;
    sec.scrollIntoView({ block: 'start' });
    sec.classList.add('ve-qa-changed');
    setTimeout(function () { sec.classList.remove('ve-qa-changed'); }, 2500);
  }

  if (channel) channel.onmessage = function (e) {
    var m = e.data || {};
    if (WIN) {
      if (m.type === 'context') {
        peerSection = m.section || null;
        // 回答で選んだ文字は、資料のウィンドウで新しく選ぶまで残す
        if (m.quote || quoteFrom !== 'answer') { quote = m.quote || ''; quoteFrom = quote ? 'doc' : ''; }
        updateWhere();
        renderQuote();
      } else if (m.type === 'went') {
        clearTimeout(gotoTimer);
      } else if (m.type === 'ping') {
        post({ type: 'hello' });
      }
      return;
    }
    if (m.type === 'hello') setDetached(true);
    else if (m.type === 'bye') setDetached(false);
    else if (m.type === 'attach') { setDetached(false); setOpen(true); }
    else if (m.type === 'clear-quote') { quote = ''; quoteFrom = ''; renderQuote(); sendContext(); }
    else if (m.type === 'sent') { quote = ''; quoteFrom = ''; renderQuote(); sendContext(); schedule(0); }
    else if (m.type === 'goto' && m.id) { goTo(m.id); post({ type: 'went' }); }
    else if (m.type === 'exported') { exported = { rev: m.rev, sections: m.sections || [] }; schedule(0); }
  };

  if (WIN) {
    // 回答の中の節へのリンクは、資料のウィンドウを動かす。資料のウィンドウが無ければ、新しいタブで資料を開く
    list.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href^="#"]');
      if (!a) return;
      e.preventDefault();
      var id = a.getAttribute('href').slice(1);
      try { id = decodeURIComponent(id); } catch (err) { /* そのまま使う */ }
      post({ type: 'goto', id: id });
      clearTimeout(gotoTimer);
      gotoTimer = setTimeout(function () { window.open(DOC_URL + '#' + encodeURIComponent(id), '_blank'); }, 800);
    });
    window.addEventListener('pagehide', function () { post({ type: 'bye' }); });
    open = true;
    post({ type: 'hello' });
  } else {
    post({ type: 'ping' });
  }

  // ---- タイトルでの知らせ

  function away() { return document.hidden || !document.hasFocus(); }

  function updateTitle() {
    // エージェントからの質問の資料では、答えを待っている質問の件数（ほかのセッションの分も）を付ける
    var n = ask ? pendingAsks.length : titleUnseen;
    var t = (n ? '(' + n + ') ' : '') + baseTitle;
    if (document.title !== t) document.title = t;
  }

  function seen() {
    if (away() || !titleUnseen) return;
    titleUnseen = 0;
    updateTitle();
  }

  window.addEventListener('focus', seen);

  // ---- 一覧

  function pending() {
    return items.filter(function (i) { return i.status === 'queued' || i.status === 'answering'; }).length;
  }

  function renderBadge() {
    var p = pending();
    var parts = [];
    if (p) parts.push('回答待ち ' + p);
    if (unseen) parts.push('新しい回答 ' + unseen);
    badge.hidden = !parts.length;
    badge.textContent = parts.join(' · ');
  }

  function elapsed(from) {
    var s = Math.max(0, Math.round((Date.now() + clockSkew - Date.parse(from)) / 1000));
    return s < 60 ? s + '秒' : Math.floor(s / 60) + '分' + (s % 60) + '秒';
  }

  function clock(iso) {
    var d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
  }

  function statusText(item) {
    var build = item.kind === 'build';
    if (item.status === 'queued') return '待機中（前の質問の回答を待っています）';
    if (item.status === 'answering') {
      return (item.cancel_requested ? '取り消しています（' : build ? '資料を作成中（' : '回答を作成中（') +
        elapsed(item.started_at) + '）';
    }
    if (item.status === 'answered') return (build ? '資料を作りました ' : '回答済み ') + clock(item.finished_at);
    if (item.status === 'canceled') return '取り消し済み ' + clock(item.finished_at);
    return '失敗 ' + clock(item.finished_at);
  }

  function stepList(steps, current) {
    var ol = el('ol', { class: 've-qa-steps' });
    steps.forEach(function (s, i) {
      var li = el('li', { text: s.text });
      if (current && i === steps.length - 1) li.className = 've-qa-step-now';
      ol.appendChild(li);
    });
    return ol;
  }

  function progressView(item) {
    var steps = Array.isArray(item.progress) ? item.progress : [];
    if (!steps.length) return null;
    if (item.status === 'answering') {
      // 作成中は直近の 3 つ。最後が今していること
      return el('div', { class: 've-qa-progress' }, [stepList(steps.slice(-3), true)]);
    }
    return el('details', { class: 've-qa-progress' }, [el('summary', { text: '調べた手順（' + steps.length + '）' }),
      stepList(steps, false)]);
  }

  function checkNote(c) {
    if (!c || !c.errors || !c.errors.length) return null;
    var d = el('details', { class: 've-qa-doc ve-qa-doc-warn' }, [
      el('summary', { text: '回答の図・リンクの検査で問題が ' + (c.count || c.errors.length) + ' 件見つかりました（表示が崩れていることがあります）' })]);
    c.errors.forEach(function (e) { d.appendChild(el('p', { text: e })); });
    return d;
  }

  function sectionLinks(secs) {
    var span = el('span');
    (secs || []).forEach(function (s, i) {
      if (i) span.appendChild(document.createTextNode('、'));
      span.appendChild(el('a', { href: '#' + s.id, text: s.title || s.id }));
    });
    return span;
  }

  function docNote(doc) {
    if (!doc) return null;
    if (doc.updated) return el('p', { class: 've-qa-doc' }, ['資料を直しました: ', sectionLinks(doc.sections)]);
    if (doc.reverted) {
      var d = el('details', { class: 've-qa-doc ve-qa-doc-warn' }, [
        el('summary', { text: '本文の修正は検査を通らなかったため取り消しました' })]);
      (doc.errors || []).forEach(function (e) { d.appendChild(el('p', { text: e })); });
      return d;
    }
    if (doc.conflict) {
      return el('p', { class: 've-qa-doc ve-qa-doc-warn',
        text: '回答の作成中に資料が別の所で更新されたため、本文の修正は反映していません（' + (doc.kept || '') + ' に残しました）。' });
    }
    return null;
  }

  function signature(item) {
    return [item.status, item.finished_at, (item.answer_html || '').length, JSON.stringify(item.doc), item.error,
      (item.progress || []).length, JSON.stringify((item.progress || []).slice(-1)), item.cancel_requested,
      JSON.stringify(item.answer_check)].join('|');
  }

  function renderItem(item) {
    var li = el('li', { class: item.reply_to ? 've-qa-item ve-qa-reply' : 've-qa-item', id: 've-qa-' + item.id,
      'data-id': item.id, 'data-status': item.status });
    var q = el('div', { class: 've-qa-q' }, [el('span', { class: 've-qa-tag', text: item.kind === 'build' ? '資料' : label(item.id) }),
      el('p', { text: item.question })]);
    var meta = [];
    if (item.reply_to) meta.push(label(item.reply_to) + ' の回答への質問');
    if (item.section && item.section.title) meta.push('節: ' + item.section.title);
    if (item.quote) meta.push('「' + (item.quote.length > 80 ? item.quote.slice(0, 80) + '…' : item.quote) + '」');
    if (meta.length) q.appendChild(el('p', { class: 've-qa-meta', text: meta.join(' · ') }));
    li.appendChild(q);
    var row = el('div', { class: 've-qa-statusrow' }, [el('p', { class: 've-qa-status', text: statusText(item) })]);
    if ((item.status === 'queued' || item.status === 'answering') && !item.cancel_requested) {
      row.appendChild(el('button', { type: 'button', class: 've-qa-link ve-qa-cancel', 'data-id': item.id, text: '取り消す' }));
    }
    li.appendChild(row);
    var progress = progressView(item);
    if (progress && item.status === 'answering') li.appendChild(progress);
    if (item.status === 'answered') {
      var a = el('div', { class: 've-qa-a' }, [el('span', { class: 've-qa-tag', text: '回答' })]);
      a.appendChild(sanitize(item.answer_html));
      li.appendChild(a);
      if (canReply(item)) {
        li.appendChild(el('div', { class: 've-qa-itemtools' }, [el('button', { type: 'button', class: 've-qa-link ve-qa-reply-btn',
          'data-id': item.id, text: 'この回答に質問する', title: 'この回答について続けて質問します（回答の中の文字を選んでも同じです）' })]));
      }
    }
    if (item.status === 'failed' && item.error) li.appendChild(el('p', { class: 've-qa-fail', text: item.error }));
    var note = docNote(item.doc);
    if (note) li.appendChild(note);
    var check = checkNote(item.answer_check);
    if (check) li.appendChild(check);
    if (progress && item.status !== 'answering') li.appendChild(progress);
    return li;
  }

  // 続きの質問（reply_to）を、元の質問のすぐ後ろに並べる。同じ元の中では聞いた順（serve.py の thread_order と同じ）
  function threaded(all) {
    var pos = {};
    all.forEach(function (i, n) { pos[i.id] = n; });
    function root(n) {
      var seen = {};
      while (!seen[n] && pos[all[n].reply_to] != null) { seen[n] = true; n = pos[all[n].reply_to]; }
      return n;
    }
    return all.map(function (i, n) { return { item: i, root: root(n), n: n }; })
      .sort(function (a, b) { return a.root - b.root || a.n - b.n; })
      .map(function (x) { return x.item; });
  }

  function render() {
    empty.hidden = items.length > 0;
    var ids = {};
    threaded(items).forEach(function (item, n) {
      ids[item.id] = true;
      var sig = signature(item), prev = rendered[item.id], li = prev && prev.li;
      if (prev && prev.sig === sig) {
        if (item.status === 'answering') li.querySelector('.ve-qa-status').textContent = statusText(item);
      } else {
        li = renderItem(item);
        if (prev) list.replaceChild(li, prev.li);
        rendered[item.id] = { sig: sig, li: li };
      }
      if (list.children[n] !== li) list.insertBefore(li, list.children[n] || null);
    });
    Object.keys(rendered).forEach(function (id) { if (!ids[id]) { rendered[id].li.remove(); delete rendered[id]; } });
    var disabled = !auth || !qa.enabled;
    tools.hidden = disabled || !items.some(function (i) { return i.status === 'answered' && i.kind !== 'build'; });
    input.disabled = disabled;
    send.disabled = disabled || sending;
    if (!auth) showNotice('質問するには、配信を始めたときに案内された URL（?k= 付き）で開き直してください。');
    else if (!qa.enabled) showNotice('この配信では質問を受け付けていません: ' + (qa.reason || '理由不明'));
    else if (!online) showNotice('配信サーバーに接続できません。配信が止まっている可能性があります。');
    else showNotice('');
    renderBadge();
  }

  function showNotice(text) { notice.hidden = !text; notice.textContent = text; }

  function showToast(children) {
    toast.textContent = '';
    children.forEach(function (c) { toast.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    toast.hidden = false;
    clearTimeout(showToast.t);
    showToast.t = setTimeout(function () { toast.hidden = true; }, 10000);
  }

  // ---- エージェントからの質問（AskUserQuestion・request_user_input）

  // 質問したエージェントの名前（Claude Code・Codex）。分からなければ「エージェント」
  function agentName(a) { return (a && a.agent_name) || 'エージェント'; }

  function noteText() {
    return '回答は、資料を配信しているマシンの ' + ((qa && qa.agent_name) || 'エージェント') +
      ' が書きます。資料の説明が足りないと分かったら、本文も直して最新版に切り替えます。' +
      (WIN ? '資料のウィンドウで読んでいる節と選んだ文字が、質問に添えられます。' : '');
  }

  function askState(a) {
    if (a.status === 'waiting' && !a.waiting) return 'gone';
    return a.status;
  }

  function askAnswers(a) {
    var dl = el('dl', { class: 've-qa-ask-answers' });
    (a.questions || []).forEach(function (q) {
      dl.append(el('dt', { text: q.header || q.question }), el('dd', { text: (a.answers || {})[q.question] || '' }));
    });
    return dl;
  }

  function askForm(a) {
    var form = el('form', { class: 've-qa-ask-form' });
    (a.questions || []).forEach(function (q, i) {
      var name = 've-qa-ask-' + i;
      var box = el('fieldset', { class: 've-qa-ask-q', 'data-question': String(i) },
        [el('legend', {}, q.header ? [el('span', { class: 've-qa-tag', text: q.header }), ' ' + q.question] : [q.question])]);
      if (q.multiSelect) box.appendChild(el('p', { class: 've-qa-meta', text: '複数選べます' }));
      q.options.forEach(function (o, j) {
        var id = name + '-' + j;
        var row = el('div', { class: 've-qa-opt' }, [
          el('input', { type: q.multiSelect ? 'checkbox' : 'radio', name: name, id: id, value: o.label }),
          el('label', { for: id }, [el('strong', { text: o.label })].concat(o.description ? [el('span', { text: o.description })] : []))
        ]);
        if (o.preview) row.appendChild(el('details', { class: 've-qa-opt-preview' }, [el('summary', { text: 'プレビュー' }), el('pre', { text: o.preview })]));
        box.appendChild(row);
      });
      var otherId = name + '-other';
      var otherText = el('input', { type: 'text', class: 've-qa-ask-other', maxlength: '2000', 'aria-label': 'その他の答え', placeholder: '自分で書く' });
      var otherPick = el('input', { type: q.multiSelect ? 'checkbox' : 'radio', name: name, id: otherId, value: '', 'data-other': '1' });
      otherText.addEventListener('input', function () { if (otherText.value.trim()) otherPick.checked = true; });
      box.appendChild(el('div', { class: 've-qa-opt' }, [otherPick, el('label', { for: otherId }, [el('strong', { text: 'その他' })]), otherText]));
      box.appendChild(el('details', { class: 've-qa-ask-notes' }, [el('summary', { text: 'メモを添える（任意）' }),
        el('textarea', { rows: '2', maxlength: '2000', 'aria-label': 'メモ' })]));
      form.appendChild(box);
    });
    var err = el('span', { class: 've-qa-error', role: 'alert' });
    var sendBtn = el('button', { type: 'submit', class: 've-qa-send', text: 'この答えを送る' });
    var termBtn = el('button', { type: 'button', class: 've-qa-link', text: 'ターミナルで答える' });
    form.appendChild(el('div', { class: 've-qa-actions' }, [err, termBtn, sendBtn]));
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (askSending) return;
      var answers = {}, problem = '';
      Array.prototype.forEach.call(form.querySelectorAll('.ve-qa-ask-q'), function (box) {
        var q = a.questions[Number(box.getAttribute('data-question'))];
        var picked = Array.prototype.filter.call(box.querySelectorAll('input:checked'), function (i) { return !i.hasAttribute('data-other'); })
          .map(function (i) { return i.value; });
        var otherOn = box.querySelector('input[data-other]').checked;
        var other = otherOn ? box.querySelector('.ve-qa-ask-other').value.trim() : '';
        if (otherOn && !other && !problem) problem = '「その他」の答えを書いてください。';
        var count = picked.length + (other ? 1 : 0);
        if (!count && !problem) problem = '「' + (q.header || q.question.slice(0, 30)) + '」を選んでください。';
        answers[q.question] = { selected: picked, other: other, notes: box.querySelector('.ve-qa-ask-notes textarea').value.trim() };
      });
      if (problem) { err.textContent = problem; return; }
      err.textContent = '';
      askSending = true;
      sendBtn.disabled = termBtn.disabled = true;
      postJson('answer', { doc: DOC, answers: answers })
        .then(function (x) {
          if (x.r.status === 200) {
            var rest = pendingAsks.filter(function (p) { return p.doc !== DOC; }).length;
            showToast(['答えを送りました。' + agentName(ask) + ' の作業が続きます。' + (rest ? 'ほかに ' + rest + ' 件が答えを待っています。' : '')]);
            renderAsk(x.b);
            refreshAsks();
            return;
          }
          if (x.r.status === 401) auth = false;
          err.textContent = x.b.error || ('送れませんでした（' + x.r.status + '）');
        })
        .catch(function () { err.textContent = '配信サーバーに接続できません。'; })
        .then(function () { askSending = false; sendBtn.disabled = termBtn.disabled = false; schedule(0); });
    });
    termBtn.addEventListener('click', function () {
      if (askSending) return;
      askSending = true;
      sendBtn.disabled = termBtn.disabled = true;
      postJson('ask-close', { doc: DOC, reason: 'terminal' })
        .then(function (x) { if (x.r.status === 200) renderAsk(x.b); else err.textContent = x.b.error || ('送れませんでした（' + x.r.status + '）'); })
        .catch(function () { err.textContent = '配信サーバーに接続できません。'; })
        .then(function () { askSending = false; sendBtn.disabled = termBtn.disabled = false; schedule(0); });
    });
    return form;
  }

  // ---- 答えを待っている質問の一覧・知らせ（質問の資料を開いているとき）
  // 質問の資料を開いている間も、受け箱と同じく配信サーバーとつないでおく（ページを見ている人がいる印になり、
  // 質問が増えた・締め切られたら知らせが届く）。答えを待っている質問の件数をタイトルに付け、ほかの質問を一覧に出す。
  // 新しい質問が来たら、デスクトップ通知（HTTPS のページで許可したとき）と音（有効にしたとき）で知らせ、
  // いま開いている質問を答え終えていれば、次の質問の資料を開く

  var pendingAsks = [], knownAsks = null, askTimer = null;

  function docHref(doc) { return '/' + doc.split('/').map(encodeURIComponent).join('/'); }

  function autoOpen() {
    try { return localStorage.getItem('ve-qa:inbox-auto') !== 'off'; } catch (e) { return true; }
  }

  function sessionLabel(a) {
    var s = a.session || {};
    return [a.where && s.branch ? a.where + '（' + s.branch + '）' : a.where, s.prompt ? '「' + s.prompt + '」' : '']
      .filter(Boolean).join(' · ');
  }

  function refreshAsks() {
    clearTimeout(askTimer);
    fetch(API + 'asks', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (st) {
        if (!st) return;
        pendingAsks = (st.asks || []).filter(function (a) { return a.status === 'waiting' && a.waiting; });
        var fresh = knownAsks ? pendingAsks.filter(function (a) { return !knownAsks[a.doc]; }) : [];
        knownAsks = knownAsks || {};
        pendingAsks.forEach(function (a) { knownAsks[a.doc] = true; });
        renderOthers();
        updateTitle();
        fresh.forEach(function (a) { alertAsk(a); });
        var next = pendingAsks.filter(function (a) { return a.doc !== DOC; })[0];
        if (next && fresh.length && autoOpen() && ask && askState(ask) !== 'waiting') { location.href = docHref(next.doc); return; }
        if (fresh.length) showToast([agentName(fresh[0]) + ' から次の質問が来ました。', el('a', { href: docHref(fresh[0].doc), class: 've-qa-link', text: '開く' })]);
      })
      .catch(function () { /* 次の知らせで取り直す */ })
      .then(function () { askTimer = setTimeout(refreshAsks, 15000); });  // 締め切り・フックが止まったことを表示に出すため
  }

  function renderOthers() {
    var others = pendingAsks.filter(function (a) { return a.doc !== DOC; });
    askOthers.textContent = '';
    askOthers.hidden = !ask;
    if (!ask) return;
    if (others.length) {
      askOthers.appendChild(el('p', { class: 've-qa-ask-others-title', text: 'ほかに答えを待っている質問 ' + others.length + ' 件' }));
      var ul = el('ul');
      others.forEach(function (a) {
        ul.appendChild(el('li', {}, [el('a', { href: docHref(a.doc), text: a.question.length > 60 ? a.question.slice(0, 60) + '…' : a.question }),
          el('span', { class: 've-qa-meta', text: ' ' + sessionLabel(a) })]));
      });
      askOthers.appendChild(ul);
    }
    askOthers.appendChild(alertControls());
  }

  // 知らせ方: デスクトップ通知は HTTPS（または localhost）のページで許可したときだけ使える。音は一度押して有効にする
  var soundOn = false, audioCtx = null;
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

  function alertAsk(a) {
    if (soundOn) chime();
    if (away()) { titleUnseen += 1; updateTitle(); }
    if (!window.isSecureContext || typeof Notification !== 'function' || Notification.permission !== 'granted') return;
    try {
      // 受け箱と質問の資料の両方が開いていても、同じ質問の通知は 1 つ（tag で置き換わる）
      var n = new Notification(agentName(a) + ' からの質問', { body: a.question + (sessionLabel(a) ? '\n' + sessionLabel(a) : ''), tag: 've-qa-ask:' + a.doc });
      n.onclick = function () { window.focus(); if (a.doc !== DOC) location.href = docHref(a.doc); n.close(); };
    } catch (e) { /* 通知を出せない環境 */ }
  }

  function alertControls() {
    var box = el('div', { class: 've-qa-alerts' });
    if (window.isSecureContext && typeof Notification === 'function') {
      if (Notification.permission === 'default') {
        var allow = el('button', { type: 'button', class: 've-qa-link', text: 'デスクトップ通知を有効にする' });
        allow.addEventListener('click', function () { Notification.requestPermission().then(renderOthers); });
        box.appendChild(allow);
      } else {
        box.appendChild(el('span', { class: 've-qa-meta', text: Notification.permission === 'granted' ? 'デスクトップ通知: 有効' : 'デスクトップ通知: ブラウザで止められています' }));
      }
    } else {
      box.appendChild(el('span', { class: 've-qa-meta', text: 'デスクトップ通知は HTTPS の受け箱で使えます（ask-config --https on）' }));
    }
    var sound = el('input', { type: 'checkbox', id: 've-qa-sound' });
    sound.checked = soundOn;
    sound.addEventListener('change', function () {
      soundOn = sound.checked;
      try { localStorage.setItem('ve-qa:sound', soundOn ? 'on' : 'off'); } catch (e) { /* 保存できない環境 */ }
      if (soundOn) chime();  // 押したときに鳴らしておくと、ブラウザがこのページで音を出せるようになる
    });
    box.append(sound, el('label', { for: 've-qa-sound', text: '音で知らせる' }));
    return box;
  }

  function watchAsks() {
    if (askEvents) return;
    askEvents = typeof EventSource === 'function' ? new EventSource(API + 'events') : {};
    if (askEvents.addEventListener) askEvents.addEventListener('asks', refreshAsks);
    refreshAsks();
  }

  function renderAsk(a) {
    ask = a;
    if (a) watchAsks();
    var state = a ? askState(a) : '';
    var sig = a ? [a.id, state].join('|') : '';
    if (sig === askSig) return;  // 選びかけの答えを消さないよう、状態が変わったときだけ作り直す
    askSig = sig;
    askBox.textContent = '';
    askBox.hidden = !a;
    if (!a) return;
    askBox.setAttribute('data-status', state);
    askBox.setAttribute('aria-label', agentName(a) + ' からの質問');
    var meta = [], session = a.session || {};
    // どのセッションの質問か: 作業ディレクトリ・ブランチ・最初の依頼
    if (a.where) meta.push('作業ディレクトリ: ' + a.where + (session.branch ? '（' + session.branch + '）' : ''));
    if (state === 'waiting' && a.expires_at) meta.push(clock(a.expires_at) + ' まで待っています');
    askBox.append(el('h3', { text: agentName(a) + ' からの質問' }), el('p', { class: 've-qa-meta' }, [meta.join(' · ') + ' ',
      el('a', { href: API + 'inbox', text: '受け箱' })]));
    if (session.prompt) askBox.appendChild(el('p', { class: 've-qa-meta ve-qa-ask-session', text: 'セッションの最初の依頼: 「' + session.prompt + '」' }));
    if (state === 'waiting') {
      askBox.appendChild(el('p', { class: 've-qa-note-inline', text: '選んで送ると、答えが ' + agentName(a) + ' の作業に戻ります（ターミナルの選択画面は出ません）。' }));
      askBox.appendChild(askForm(a));
      if (!askSeen[a.id]) {
        askSeen[a.id] = true;
        if (!WIN && !open) setOpen(true);
        var firstPick = askBox.querySelector('input');
        if (firstPick) firstPick.focus();
        if (away()) { titleUnseen += 1; updateTitle(); }
      }
      return;
    }
    var text = { answered: '答えを送りました。' + agentName(a) + ' の作業が続いています。',
      terminal: 'ターミナルで答えることにしました。ターミナルに選択画面が出ています。',
      expired: '待つ時間を過ぎました。ターミナルで答えてください。',
      gone: agentName(a) + ' がもう答えを待っていません（ターミナルで中断された可能性があります）。ターミナルで答えてください。' }[state];
    askBox.appendChild(el('p', { class: state === 'answered' ? 've-qa-doc' : 've-qa-doc ve-qa-doc-warn', text: text || '' }));
    if (state === 'answered') askBox.appendChild(askAnswers(a));
  }

  // ---- 状態の取得

  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(poll, delay != null ? delay : (pending() || open ? FAST : SLOW));
  }

  function poll() {
    // 取得は 1 本ずつ。重なると古い応答が後から届き、本文を二度差し替えることがある
    if (polling) { pollAgain = true; return; }
    polling = true;
    fetch(API + 'state?doc=' + encodeURIComponent(DOC), { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        if (r.status === 401) { auth = false; online = true; render(); return null; }
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (st) { if (st) apply(st); })
      .catch(function () { online = false; render(); })
      .then(function () {
        polling = false;
        if (pollAgain) { pollAgain = false; schedule(0); } else schedule();
      });
  }

  function apply(st) {
    auth = true; online = true;
    qa = st.qa || qa;
    agentNote.textContent = noteText();
    if (st.now) clockSkew = Date.parse(st.now) - Date.now();
    var fresh = [];
    (st.items || []).forEach(function (item) {
      var done = item.status === 'answered' || item.status === 'failed' || item.status === 'canceled';
      if (done && !finished[item.id]) {
        finished[item.id] = true;
        if (loaded && item.status !== 'canceled') fresh.push(item);
      }
    });
    items = st.items || [];
    render();
    renderAsk(st.ask || null);
    if (WIN && !loaded) list.scrollTop = list.scrollHeight;
    if (fresh.length && !open) {
      unseen += fresh.length;
      renderBadge();
      showToast([detached ? '回答が届きました（別ウィンドウ）。' : '回答が届きました。',
        el('button', { type: 'button', class: 've-qa-link', text: '見る' })]);
      toast.querySelector('button').addEventListener('click', function () { toast.hidden = true; setOpen(true); });
    }
    if (fresh.length && open) {
      var last = document.getElementById('ve-qa-' + fresh[fresh.length - 1].id);
      last && last.scrollIntoView({ block: 'nearest' });
    }
    if (fresh.length && away()) {
      titleUnseen += fresh.length;
      updateTitle();
    }
    loaded = true;
    if (!WIN && st.rev && st.rev !== rev) refreshDoc();
  }

  // ---- 本文の差し替え

  function refreshDoc() {
    if (swapping) return;
    swapping = true;
    fetch(location.pathname, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, 'text/html');
        // 版は、取り直した本文に配信サーバーが付けたもの（状態の取得の後にさらに変わっていることがある）
        var tag = doc.querySelector('script[src^="/__qa/qa.js"]');
        var newRev = tag && tag.getAttribute('data-rev');
        if (!newRev || newRev === rev) return;
        var cause = items.filter(function (i) { return i.doc && i.doc.updated && i.doc.rev === newRev; }).pop();
        var byExport = exported && exported.rev === newRev;
        var anchor = currentSection(), offset = 0;
        if (anchor) offset = document.getElementById(anchor.id).getBoundingClientRect().top;
        var checks = {};
        Array.prototype.forEach.call(document.querySelectorAll('input[type=checkbox][id]'), function (i) {
          if (!ownNode(i)) checks[i.id] = i.checked;
        });
        Array.prototype.forEach.call(document.head.querySelectorAll('style'), function (s) { s.remove(); });
        Array.prototype.forEach.call(doc.head.querySelectorAll('style'), function (s) {
          document.head.appendChild(document.importNode(s, true));
        });
        baseTitle = doc.title;
        updateTitle();
        Array.prototype.slice.call(document.body.childNodes).forEach(function (n) {
          if (n !== root && !isOurs(n)) n.remove();
        });
        Array.prototype.forEach.call(doc.body.childNodes, function (n) {
          if (!isOurs(n)) document.body.insertBefore(document.importNode(n, true), root);
        });
        Object.keys(checks).forEach(function (id) {
          var i = document.getElementById(id);
          if (i && i.type === 'checkbox') i.checked = checks[id];
        });
        var target = anchor && document.getElementById(anchor.id);
        if (target) window.scrollTo(0, target.getBoundingClientRect().top + window.scrollY - offset);
        rev = newRev;
        naturalSize();
        updateToc();
        if (cause && cause.kind === 'build') {
          // 選択肢だけのページが、作った資料に丸ごと替わった。節ごとの印は付けない
          showToast(['選ぶための資料ができました。']);
          return;
        }
        var secs = cause ? cause.doc.sections : byExport ? exported.sections : [];
        mark(secs, byExport ? '質問と回答を書き出しました' : '質問を受けて、この節を直しました');
        showToast(byExport ? ['質問と回答を、資料の末尾に書き出しました: ', sectionLinks(secs)]
          : secs.length ? ['資料を最新版に切り替えました。直した所: ', sectionLinks(secs)]
          : ['資料が更新されたため、最新版に切り替えました。']);
      })
      .catch(function () { /* 次の取得でもう一度試す */ })
      .then(function () { swapping = false; });
  }

  function isOurs(n) {
    return n.nodeType === 1 && (n.matches('script[src^="/__qa/"], link[href^="/__qa/"]') || n.id === 've-qa-root');
  }

  function mark(secs, text) {
    (secs || []).forEach(function (s) {
      var sec = document.getElementById(s.id);
      if (!sec || ownNode(sec)) return;
      sec.classList.add('ve-qa-changed');
      var label = el('p', { class: 've-qa-changed-label', text: text });
      sec.insertBefore(label, sec.firstChild);
      setTimeout(function () { sec.classList.remove('ve-qa-changed'); label.remove(); }, 12000);
    });
  }

  // ---- 送信

  function postJson(name, body) {
    return fetch(API + name, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (b) { return { r: r, b: b }; }); });
  }

  function submit(e) {
    if (e) e.preventDefault();
    var question = input.value.trim();
    if (!question || sending || send.disabled) return;
    error.textContent = '';
    sending = true;
    send.disabled = true;
    postJson('questions', { doc: DOC, question: question, section: currentSection(), quote: quote || null,
      reply_to: replyTo, quote_from: quote && replyTo && quoteFrom === 'answer' ? 'answer' : null })
      .then(function (x) {
        if (x.r.status === 202) {
          input.value = '';
          quote = '';
          quoteFrom = '';
          replyTo = null;
          renderQuote();
          renderReply();
          if (WIN) post({ type: 'sent' }); else sendContext();
          items.push(x.b);
          render();
          var sent = document.getElementById('ve-qa-' + x.b.id);
          sent && sent.scrollIntoView({ block: 'nearest' });
          return;
        }
        if (x.r.status === 401) auth = false;
        error.textContent = x.b.error || ('送れませんでした（' + x.r.status + '）');
        render();
      })
      .catch(function () { error.textContent = '配信サーバーに接続できません。'; })
      .then(function () { sending = false; render(); schedule(0); });
  }

  // 待機中・作成中の質問を取り消す。作成中なら、配信サーバーが claude を止める
  list.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.ve-qa-cancel');
    if (!b) return;
    var id = b.getAttribute('data-id');
    var item = items.filter(function (i) { return i.id === id; })[0];
    if (item && item.status === 'answering' && !window.confirm('回答の作成を止めて、この質問を取り消しますか？')) return;
    b.disabled = true;
    postJson('cancel', { doc: DOC, id: id })
      .then(function (x) {
        if (x.r.status === 200 || x.r.status === 202) {
          items = items.map(function (i) { return i.id === id ? x.b : i; });
          render();
          return;
        }
        if (x.r.status === 401) auth = false;
        showToast([x.b.error || ('取り消せませんでした（' + x.r.status + '）')]);
        b.disabled = false;
      })
      .catch(function () { showToast(['配信サーバーに接続できません。']); b.disabled = false; })
      .then(function () { schedule(0); });
  });

  // 回答に続けて質問する。送ると、元の質問と回答がエージェントに渡り、質問は元の質問のすぐ下に並ぶ
  list.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.ve-qa-reply-btn');
    if (!b || input.disabled) return;
    var id = b.getAttribute('data-id');
    if (quoteFrom === 'answer' && replyTo !== id) clearQuote();
    replyTo = id;
    renderQuote();
    renderReply();
    input.focus();
  });

  // 回答した質問を、資料の末尾の節に書き出す（資料のファイルだけを渡しても読めるように）
  exportBtn.addEventListener('click', function () {
    var n = items.filter(function (i) { return i.status === 'answered'; }).length;
    if (!window.confirm('回答した質問 ' + n + ' 件を、資料の末尾の節「読者からの質問と回答」に書き出します。\n' +
      '資料のファイルを人に渡したり印刷したりしても、質問と回答を読めるようになります（前の版は qa/history/ に残ります）。')) return;
    exportBtn.disabled = true;
    postJson('export', { doc: DOC })
      .then(function (x) {
        if (x.r.status !== 200) { showToast([x.b.error || ('書き出せませんでした（' + x.r.status + '）')]); return; }
        if (!x.b.updated) { showToast(['書き出し済みです（変更はありません）。']); return; }
        exported = { rev: x.b.rev, sections: x.b.sections || [] };
        if (WIN) post({ type: 'exported', rev: exported.rev, sections: exported.sections });
        showToast(['質問と回答 ' + x.b.count + ' 件を、資料の末尾に書き出しました。']);
      })
      .catch(function () { showToast(['配信サーバーに接続できません。']); })
      .then(function () { exportBtn.disabled = false; schedule(0); });
  });

  form.addEventListener('submit', submit);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit(e);
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { seen(); schedule(0); } });

  if (!WIN) { naturalSize(); updateToc(); }
  render();
  poll();
})();
