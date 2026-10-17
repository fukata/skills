// visual-explain の「PDF をダウンロード」（serve.py が配信時にだけ差し込む。質問を受け付けない配信（--no-qa）でも出す）。
// 配信しているマシンの Chrome が、資料のファイルを印刷の CSS（A4 縦）で PDF にする。Chrome が無ければ、ブラウザの印刷画面を開く
(function () {
  'use strict';
  var script = document.currentScript;
  var DOC = script.dataset.doc;
  var LABEL = 'PDF をダウンロード';
  var busy = false;

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 've-pdf';
  btn.textContent = LABEL;
  btn.title = '資料を A4 縦の PDF にしてダウンロードする（質問欄は入らない）';

  // 資料の .toolbar（「横幅いっぱいに表示」の切替）に置く。無い資料では本文の先頭に置く
  function place() {
    if (document.body.contains(btn)) return;
    var bar = document.querySelector('.toolbar');
    if (bar) { bar.appendChild(btn); return; }
    var host = document.querySelector('main') || document.body;
    var wrap = document.createElement('div');
    wrap.className = 've-pdfbar';
    wrap.appendChild(btn);
    host.insertBefore(wrap, host.firstChild);
  }

  function fileName() {
    var title = document.title.replace(/^\(\d+\)\s*/, '');  // 質問欄がタイトルの先頭に付ける件数を除く
    return (title.replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').slice(0, 100) || 'document') + '.pdf';
  }

  function done(text) {
    btn.textContent = text;
    setTimeout(function () { busy = false; btn.disabled = false; btn.textContent = LABEL; }, text === LABEL ? 0 : 4000);
  }

  btn.addEventListener('click', function () {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    btn.textContent = 'PDF を作っています…';
    fetch('/__qa/pdf?doc=' + encodeURIComponent(DOC), { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        if (r.ok) return r.blob();
        return r.json().catch(function () { return {}; }).then(function (data) {
          var e = new Error(data.error || ('PDF を作れなかった（' + r.status + '）'));
          e.status = r.status;
          throw e;
        });
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = fileName();
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
        done(LABEL);
      })
      .catch(function (e) {
        btn.title = e.message || 'PDF を作れなかった';
        if (e.status === 503) {
          // 配信しているマシンに Chrome が無い。印刷の画面で「PDF に保存」を選んでもらう
          done(LABEL);
          window.print();
        } else {
          done('PDF を作れませんでした');
        }
      });
  });

  place();
  // 質問欄が本文を最新版に差し替えると、ボタンも消える。置き直す
  new MutationObserver(place).observe(document.body, { childList: true });
})();
