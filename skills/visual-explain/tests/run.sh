#!/usr/bin/env bash
# visual-explain スキルの一括テスト。python3 と git は必須、
# ヘッドレス Chrome があれば図のはみ出し・狭い幅の検査も確かめる。
#   usage: bash skills/visual-explain/tests/run.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
C="python3 -I $HERE/../scripts/check-explanation.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0
ok()   { printf '  ok   %s\n' "$1"; }
ng()   { printf '  FAIL %s\n' "$1"; fail=1; }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else ng "$1"; fi; }

# fixture のリポジトリを、git 管理のものと素のディレクトリの 2 通りで用意する
cp -R "$HERE/fixtures/repo" "$TMP/plain"
cp -R "$HERE/fixtures/repo" "$TMP/git"
git -C "$TMP/git" init -q && git -C "$TMP/git" add -A && \
  git -C "$TMP/git" -c user.name=t -c user.email=t@example.com commit -q -m fixture

# 資料の誤りを 1 つずつ仕込む。mut <名前> <置換前> <置換後>（置換前が無ければテストの誤り）
mut() {
  mkdir -p "$TMP/$1" && cp -R "$HERE/fixtures/valid/." "$TMP/$1/"
  python3 - "$TMP/$1/explanation.html" "$2" "$3" <<'PY' || { echo "  FAIL mutation '$1' の置換前が見つからない"; fail=1; }
import sys
p, a, b = sys.argv[1:4]
s = open(p, encoding="utf-8").read()
assert a in s
open(p, "w", encoding="utf-8").write(s.replace(a, b, 1))
PY
}
V="$HERE/fixtures/valid/explanation.html"

echo "== 正しい資料"
check "git のリポジトリで警告もなく通る"     "$C '$V' --repo '$TMP/git' --no-browser | grep -q 'errors=0 warnings=0'"
check "素のディレクトリでも通る"             "$C '$V' --repo '$TMP/plain' --no-browser | grep -q 'errors=0 warnings=0'"
check "ファイル名だけの参照を本文のフルパスで決める" "$C '$V' --repo '$TMP/git' --no-browser | grep -q 'resolved=4'"
check "--json は ok:true を出す"             "$C '$V' --repo '$TMP/git' --no-browser --json | python3 -c 'import json,sys; assert json.load(sys.stdin)[\"ok\"] is True'"
check "ファイルが無ければ exit 2"            "$C '$TMP/nope.html' --no-browser; [ \$? -eq 2 ]"

echo "== エラーになる誤り（exit 1）"
mut anchor   'href="#refs"' 'href="#nothing"'
mut localf   'images/diagram.svg' 'images/missing.svg'
mut alt      ' alt="補足の図"' ''
mut tmpl     '<h2>根拠</h2>' '<h2>根拠</h2><p>図の後に、重要な分岐や制約を書きます。</p>'
mut secret   '<h2>根拠</h2>' '<h2>根拠</h2><p><a href="https://user:pass@example.com/">画面</a></p>'
mut refmiss  'app/models/plan.rb:3' 'app/models/plans.rb:3'
mut refline  'lib/tasks/billing.rake:2-4' 'lib/tasks/billing.rake:2-40'
# 実際の事故: HTML の本文に Markdown の **太字** が文字のまま残り、アスタリスクが見えていた
mut mdbold   '<h2>根拠</h2>' '<h2>根拠</h2><p>**重要**: 期限を確かめる</p>'
mut mdcode   '<h2>根拠</h2>' '<h2>根拠</h2><p>期限は `GRACE_DAYS` で決まる</p>'
for spec in anchor:anchor localf:local-file alt:img-alt tmpl:template secret:secret \
            refmiss:ref-missing refline:ref-line mdbold:markdown mdcode:markdown; do
  m="${spec%%:*}"; id="${spec#*:}"
  check "'$m' は [$id] のエラー" "out=\$($C '$TMP/$m/explanation.html' --repo '$TMP/git' --no-browser); [ \$? -eq 1 ] && grep -q 'ERROR \[$id\]' <<<\"\$out\""
done

echo "== 警告になる誤り（exit 0）"
# 実際の事故: 識別子と並べた行番号がずれていた（subscription.rb:23 と書いたが定数は :27）
mut refctx   'app/models/plan.rb:3' 'app/models/plan.rb:9'
mut ident    'Billing::CollectService</code> を呼ぶ' 'Billing::CollectorService</code> を呼ぶ'
mut ambig    'app/models/plan.rb:3' 'plan.rb:3'
mut external '</main>' '</main><script src="https://cdn.example.com/x.js"></script>'
mut mermaid  '<h2>根拠</h2>' '<h2>根拠</h2><pre>sequenceDiagram
  A->>B: x</pre>'
mut svgtitle '<title id="t1">更新と集計</title>' ''
mut nocap    '<figcaption>図2: 補足</figcaption>' ''
ROWS="$(for i in 1 2 3 4 5 6 7 8; do printf '<tr><td>行%d</td></tr>' "$i"; done)"
mut listsec  '</main>' "<section><h2>一覧</h2><table><thead><tr><th>見出し</th></tr></thead><tbody>$ROWS</tbody></table></section></main>"
# 実際の事故: 会話の続きで作った資料を渡された人が、前提も要点も分からず読めなかった
mut nokey    '<p class="key">' '<p>'
mut noctx    '<section id="context">
    <h2>前提</h2>' '<section>
    <h2>概要</h2>'
mut convo    '<h2>根拠</h2>' '<h2>根拠</h2><p>さっきの案のとおり、集計は翌月に動く。</p>'
# 実際の事故: 略語・チケット番号（PdM、ABC-123）を説明なしに使い、事前知識の無い人が読めなかった
KEY='<p class="key">更新は毎月1日、集計は翌月1日に動く。</p>'
mut term     "$KEY" "$KEY<p>変えるかは PAY-123 で PdM が決める。</p>"
# 実際の事故: 決済の日程を図の無い所に文章だけで書き、読み手が日付を頭の中で並べ直すしかなかった
mut flowday  '</main>' '<section><h2>失敗したとき</h2><p>決済は1・2・4・7日に試み、払われなければ11日に停止する。</p></section></main>'
mut flowarr  '</main>' '<section><h2>操作</h2><p>一覧 → 詳細 → 編集の順に進む。</p></section></main>'
# 実際の事故: 図に width が無く、広い画面で本文の幅いっぱい（1000px 超）まで拡大された
mut svgwidth ' width="600" height="120" role="img"' ' role="img"'
for spec in refctx:ref-context ident:identifier ambig:ref-ambiguous external:external mermaid:mermaid \
            svgtitle:svg-title nocap:figcaption listsec:list-section nokey:key noctx:context convo:context-ref \
            term:term flowday:text-flow flowarr:text-flow svgwidth:svg-width; do
  m="${spec%%:*}"; id="${spec#*:}"
  check "'$m' は [$id] の警告" "out=\$($C '$TMP/$m/explanation.html' --repo '$TMP/git' --no-browser); [ \$? -eq 0 ] && grep -q 'WARN  \[$id\]' <<<\"\$out\""
done
ITEMS="$(for i in 1 2 3 4 5 6 7 8 9; do printf '<li>根拠%d</li>' "$i"; done)"
mut evidence '<ul>' "<ul>$ITEMS"
check "根拠の節は長い箇条書きでも羅列にしない" "! $C '$TMP/evidence/explanation.html' --repo '$TMP/git' --no-browser | grep -q 'list-section'"
mut figsec   '</main>' "<section><h2>一覧</h2><figure><img src=\"images/diagram.svg\" alt=\"図\"><figcaption>図</figcaption></figure><table><tbody>$ROWS</tbody></table></section></main>"
check "図のある節の表は羅列にしない"         "! $C '$TMP/figsec/explanation.html' --repo '$TMP/git' --no-browser | grep -q 'list-section'"
mut mdpre    '<h2>根拠</h2>' '<h2>根拠</h2><pre>**a** と `b`</pre>'
check "pre の中の Markdown の記法はエラーにしない" "$C '$TMP/mdpre/explanation.html' --repo '$TMP/git' --no-browser | grep -q 'errors=0'"
mut bgctx   '<section id="context">
    <h2>前提</h2>' '<section>
    <h2>背景</h2>'
check "見出しが「背景」の節も前提とみなす"      "! $C '$TMP/bgctx/explanation.html' --repo '$TMP/git' --no-browser | grep -q 'WARN  \[context\]'"
check "素のディレクトリでも識別子の欠けを拾う" "$C '$TMP/ident/explanation.html' --repo '$TMP/plain' --no-browser | grep -q 'WARN  \[identifier\]'"
check "'term' は略語とチケット番号を 1 件ずつ出す" "out=\$($C '$TMP/term/explanation.html' --repo '$TMP/git' --no-browser); grep -q '「PdM」' <<<\"\$out\" && grep -q '「PAY-123」' <<<\"\$out\""
mut termpar  "$KEY" "$KEY<p>変えるかは別チケット PAY-123（集計の時刻を決める）でプロダクトマネージャー（PdM）が決める。</p>"
check "括弧で説明した略語は警告しない"           "! $C '$TMP/termpar/explanation.html' --repo '$TMP/git' --no-browser | grep -q '\[term\]'"
mut termctx  '</section>
  <section id="flow">' '<dl class="terms"><dt>PdM</dt><dd>プロダクトマネージャー</dd><dt>PAY-123</dt><dd>集計の時刻を決めるチケット</dd></dl></section>
  <section id="flow">'
python3 - "$TMP/termctx/explanation.html" "$KEY" <<'PY'
import sys
p, key = sys.argv[1:3]
s = open(p, encoding="utf-8").read()
open(p, "w", encoding="utf-8").write(s.replace(key, key + "<p>変えるかは PAY-123 で PdM が決める。</p>", 1))
PY
check "前提の節の用語の一覧にある略語は警告しない" "! $C '$TMP/termctx/explanation.html' --repo '$TMP/git' --no-browser | grep -q '\[term\]'"
mut termref  '<h2>根拠</h2>' '<h2>根拠</h2><p>PdM の判断は PAY-123 に残っている。</p>'
check "根拠の節の略語は警告しない"               "! $C '$TMP/termref/explanation.html' --repo '$TMP/git' --no-browser | grep -q '\[term\]'"
ITEMS12="$(for i in 1 2 3 4 5 6 7 8 9 10 11 12; do printf ' Term%dX' "$i"; done)"
mut termmany "$KEY" "$KEY<p>$ITEMS12</p>"
check "'term' は 10 語を超えたら残りを 1 件にまとめる" "out=\$($C '$TMP/termmany/explanation.html' --repo '$TMP/git' --no-browser); [ \$(grep -c '\[term\]' <<<\"\$out\") -eq 11 ] && grep -q 'ほかに 2 語' <<<\"\$out\""
mut flowfig  "$KEY" "$KEY<p>決済は1・2・4・7日に試み、払われなければ11日に停止する。</p>"
check "図のある節の日程の文章は警告しない"       "! $C '$TMP/flowfig/explanation.html' --repo '$TMP/git' --no-browser | grep -q '\[text-flow\]'"
mut flowone  '</main>' '<section><h2>失敗したとき</h2><p>2026-10-07 03:17 に確認した。14日間のお試しの後、4・7日 13:35 に決済する。</p></section></main>'
check "日時は 1 時点、「14日間」は数えない"        "! $C '$TMP/flowone/explanation.html' --repo '$TMP/git' --no-browser | grep -q '\[text-flow\]'"
check "雛形は用語・文章の日程の検査を通る"       "! $C '$HERE/../assets/explanation.html' --no-browser | grep -E '\[(term|text-flow)\]'"
check "--ignore-ref で別リポジトリの参照を除く" "$C '$TMP/refmiss/explanation.html' --repo '$TMP/git' --no-browser --ignore-ref app/models/plans.rb | grep -q 'errors=0'"

echo "== 回答の断片（--fragment）"
printf '<p>結論。<a href="#flow">流れ</a>を見る。根拠は <code>app/models/plan.rb:3</code>。ご質問の件。</p>\n<svg viewBox="0 0 300 80" width="300" height="80"><title>図</title><rect x="10" y="10" width="200" height="50" fill="#eef" stroke="#33a"/><text x="25" y="40" font-size="14">短い</text></svg>\n' > "$TMP/frag-ok.html"
printf '<p>**太字** と <a href="#nothing">壊れた</a> と <code>app/models/nope.rb:3</code></p>\n<svg viewBox="0 0 300 80"><title>図</title></svg>\n' > "$TMP/frag-ng.html"
check "正しい回答は警告もなく通る（前提・要点・会話の言葉は見ない）" "$C '$TMP/frag-ok.html' --fragment --doc '$V' --repo '$TMP/git' --no-browser | grep -q 'errors=0 warnings=0'"
check "回答の記法・リンク・コード参照の誤りはエラー" "out=\$($C '$TMP/frag-ng.html' --fragment --doc '$V' --repo '$TMP/git' --no-browser); [ \$? -eq 1 ] && grep -q 'ERROR \[markdown\]' <<<\"\$out\" && grep -q 'ERROR \[anchor\]' <<<\"\$out\" && grep -q 'ERROR \[ref-missing\]' <<<\"\$out\" && grep -q 'WARN  \[svg-width\]' <<<\"\$out\""
check "--doc が無ければ資料の節へのリンクはエラー" "out=\$($C '$TMP/frag-ok.html' --fragment --repo '$TMP/git' --no-browser); [ \$? -eq 1 ] && grep -q 'ERROR \[anchor\]' <<<\"\$out\""

echo "== 網羅（--inventory）"
printf '# 課金の構成要素\nGRACE_DAYS\n更新と集計\n\nbilling:collect_monthly_sales\n返金 | Billing::CollectService\n' > "$TMP/inv-ok.txt"
cp "$TMP/inv-ok.txt" "$TMP/inv-miss.txt" && printf 'RefundService\n' >> "$TMP/inv-miss.txt"
check "一覧の要素がすべて出てくれば警告なし" "$C '$V' --repo '$TMP/git' --no-browser --inventory '$TMP/inv-ok.txt' | grep -q 'errors=0 warnings=0 .*inventory=4/4'"
check "出てこない要素は [inventory] の警告"  "out=\$($C '$V' --repo '$TMP/git' --no-browser --inventory '$TMP/inv-miss.txt'); [ \$? -eq 0 ] && grep -q 'WARN  \[inventory\].*RefundService' <<<\"\$out\" && grep -q 'inventory=4/5' <<<\"\$out\""
check "一覧のファイルが無ければ exit 2"       "$C '$V' --repo '$TMP/git' --no-browser --inventory '$TMP/nope.txt'; [ \$? -eq 2 ]"

echo "== ブラウザでの検査（ヘッドレス Chrome）"
if $C "$V" --repo "$TMP/git" --json | python3 -c 'import json,sys; b=json.load(sys.stdin)["stats"]["browser"]; sys.exit(0 if "px" in b else 1)'; then
  check "正しい資料はブラウザ込みでも警告なし" "$C '$V' --repo '$TMP/git' | grep -q 'errors=0 warnings=0'"
  # 実際の事故: 図の注記が枠から 160 はみ出していた
  mut overflow '毎月1日に更新する</text>' '毎月1日に更新して課金履歴を当月分の満額で積む</text>'
  mut viewbox  '<text x="355" y="55"' '<text x="560" y="55"'
  mut overlap  '<text x="355" y="55" font-size="14">翌月1日に集計する</text>' '<text x="355" y="55" font-size="14">翌月1日に集計する</text><text x="360" y="57" font-size="14">重なる文字</text>'
  mut narrow   '</main>' '</main><div style="width:900px">横に長い要素</div>'
  mut crossln  '</svg>' '<path d="M420 20 V100" stroke="#33a" stroke-width="2"/></svg>'
  mut crossdsh '</svg>' '<path d="M420 20 V100" stroke="#33a" stroke-width="2" stroke-dasharray="4 4"/></svg>'
  mut dense    '<text x="25" y="55" font-size="14">毎月1日に更新する</text>' '<text x="25" y="32" font-size="10"><tspan x="25">一行目</tspan><tspan x="25" dy="12">二行目</tspan><tspan x="25" dy="12">三行目</tspan><tspan x="25" dy="12">四行目</tspan></text>'
  check "'overflow' は [svg-overflow] のエラー" "out=\$($C '$TMP/overflow/explanation.html' --repo '$TMP/git'); [ \$? -eq 1 ] && grep -q 'ERROR \[svg-overflow\] L' <<<\"\$out\""
  check "'viewbox' は [svg-viewbox] のエラー"   "out=\$($C '$TMP/viewbox/explanation.html' --repo '$TMP/git'); [ \$? -eq 1 ] && grep -q 'ERROR \[svg-viewbox\]' <<<\"\$out\""
  check "'localf' は [image] のエラーも出す"     "out=\$($C '$TMP/localf/explanation.html' --repo '$TMP/git'); grep -q 'ERROR \[image\]' <<<\"\$out\""
  check "'narrow' は [narrow-overflow] のエラー" "out=\$($C '$TMP/narrow/explanation.html' --repo '$TMP/git'); [ \$? -eq 1 ] && grep -q 'ERROR \[narrow-overflow\]' <<<\"\$out\""
  check "'overlap' は [svg-overlap] の警告"      "$C '$TMP/overlap/explanation.html' --repo '$TMP/git' | grep -q 'WARN  \[svg-overlap\]'"
  check "'dense' は [svg-dense-box] の警告"       "out=\$($C '$TMP/dense/explanation.html' --repo '$TMP/git'); [ \$? -eq 0 ] && grep -q 'WARN  \[svg-dense-box\].*4 行' <<<\"\$out\""
  check "'crossln' は [svg-text-on-line] の警告"   "out=\$($C '$TMP/crossln/explanation.html' --repo '$TMP/git'); [ \$? -eq 0 ] && grep -q 'WARN  \[svg-text-on-line\].*翌月1日に集計する' <<<\"\$out\""
  check "破線は文字を貫いても警告しない"            "! $C '$TMP/crossdsh/explanation.html' --repo '$TMP/git' | grep -q 'svg-text-on-line'"
  check "--chrome に無いパスを渡しても他で見つける" "$C '$V' --repo '$TMP/git' --chrome /nonexistent | grep -q 'browser=.*px'"
  # 実際の事故: PATH で先に見つかった Chromium がページを取り出さずに止まり、検査のたびに時間切れまで待った
  printf '#!/bin/sh\nexit 0\n' >"$TMP/broken-chrome" && chmod +x "$TMP/broken-chrome"
  check "--chrome が動かないものなら他で見つける" "$C '$V' --repo '$TMP/git' --chrome '$TMP/broken-chrome' | grep -q 'browser=.*px'"
  # 実際の事故: 印刷すると、高さを制限したレスポンス例が途中で切れ、横長の図の文字が 7px まで縮んだ
  mut prclip   '</main>' '</main><pre style="max-height:40px;overflow:auto">1
2
3
4
5
6</pre>'
  mut prwide   '</main>' '</main><style>@media print { .p { width: 800px } }</style><div class="p">印刷だけ広い</div>'
  mut prsmall  '</main>' '</main><svg viewBox="0 0 1600 100" style="display:block;width:100%"><title>横長の図</title><text x="10" y="50" font-size="12">小さい文字</text></svg>'
  mut prdet    '</main>' '</main><details><summary>補足</summary><p>畳んだ中身</p></details>'
  check "'prclip' は [print-clipped] のエラー"      "out=\$($C '$TMP/prclip/explanation.html' --repo '$TMP/git'); [ \$? -eq 1 ] && grep -q 'ERROR \[print-clipped\]' <<<\"\$out\""
  check "'prwide' は [print-overflow] のエラーだけ"  "out=\$($C '$TMP/prwide/explanation.html' --repo '$TMP/git'); [ \$? -eq 1 ] && grep -q 'ERROR \[print-overflow\]' <<<\"\$out\" && ! grep -q 'narrow-overflow' <<<\"\$out\""
  check "'prsmall' は [print-small-text] の警告"    "out=\$($C '$TMP/prsmall/explanation.html' --repo '$TMP/git'); [ \$? -eq 0 ] && grep -q 'WARN  \[print-small-text\].*横長の図' <<<\"\$out\""
  check "'prdet' は [print-details] の警告"         "out=\$($C '$TMP/prdet/explanation.html' --repo '$TMP/git'); [ \$? -eq 0 ] && grep -q 'WARN  \[print-details\]' <<<\"\$out\""
  printf '<svg viewBox="0 0 300 80" width="300" height="80"><title>図</title><rect x="10" y="10" width="150" height="50" fill="#eef"/><text x="20" y="40" font-size="14">枠に収まらない長い長い文字</text></svg>\n' > "$TMP/frag-over.html"
  check "回答の図のはみ出しは [svg-overflow] のエラー" "out=\$($C '$TMP/frag-over.html' --fragment --doc '$V' --repo '$TMP/git'); [ \$? -eq 1 ] && grep -q 'ERROR \[svg-overflow\]' <<<\"\$out\""
  # 書き出した質問と回答の記録は、資格情報のほかは検査しない（読み手の質問や回答の誤りで本文の修正を止めない）
  mut record   '</main>' '<!-- visual-explain: reader-qa begin --><section id="reader-qa"><h2>読者からの質問と回答</h2><h3>Q1. `GRACE_DAYS` は？</h3><svg viewBox="0 0 300 80"><rect x="10" y="10" width="150" height="50" fill="#eef"/><text x="20" y="40" font-size="14">枠に収まらない長い長い文字</text></svg><a href="#gone">消えた節</a></section><!-- visual-explain: reader-qa end --></main>'
  check "記録の節の誤りはエラー・警告にしない"      "$C '$TMP/record/explanation.html' --repo '$TMP/git' | grep -q 'errors=0 warnings=0'"
  check "雛形は印刷・前提・要点の検査を通る"        "! $C '$HERE/../assets/explanation.html' | grep -E '\[(print-|narrow-|context|key|markdown|svg-|term|text-flow)'"
else
  echo "  skip ヘッドレス Chrome が見つからないため飛ばす（npx playwright install chromium-headless-shell で有効化）"
fi
echo "== 図の判定（計測値を合成して確かめる。フォントに依存しない）"
check "余白・はみ出し・viewBox・枠の線・重なりの判定" "python3 -I - '$HERE/../scripts/check-explanation.py' <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('c', sys.argv[1]); c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
class D: svgs = [{'line': 7, 'title': True}]
def run(texts, rects):
    f = c.Findings()
    c.check_svgs({'svgs': [{'index': 1, 'title': 't', 'vb': [0, 0, 600, 120], 'rects': rects, 'texts': texts}]}, f, D())
    return [x['check'] for x in f.errors], [x['check'] for x in f.warnings]
box = {'x': 10, 'y': 20, 'w': 140, 'h': 60}
t = lambda x, w, y=40, h=16: {'x': x, 'y': y, 'w': w, 'h': h, 'label': 'a'}
assert run([t(20, 100)], [box]) == ([], []), 'enough margin'
assert run([t(20, 129)], [box]) == ([], ['svg-tight']), 'right margin 1'
assert run([t(20, 135)], [box]) == (['svg-overflow'], []), 'overflow 5'
assert run([t(560, 100)], []) == (['svg-viewbox'], []), 'outside viewBox'
assert run([t(140, 40)], [box]) == ([], ['svg-border']), 'on the border'
assert run([t(200, 50), t(210, 50, y=45)], []) == ([], ['svg-overlap']), 'overlap'
d = lambda y, lines, chars: dict(t(20, 100, y=y, h=10), lines=lines, chars=chars)
assert run([d(25, 3, 40)], [box]) == ([], []), '3 lines is fine'
assert run([d(25, 2, 30), d(40, 2, 30)], [box]) == ([], ['svg-dense-box']), '4 lines in one box'
assert run([d(25, 1, 61)], [box]) == ([], ['svg-dense-box']), '61 chars'
assert run([d(25, 5, 80)], [{'x': 0, 'y': 0, 'w': 600, 'h': 120}]) == ([], []), 'background rect is ignored'
PY"
check "--no-browser ならブラウザの検査を飛ばす" "$C '$V' --repo '$TMP/git' --no-browser | grep -q 'browser=skipped'"

echo "== アダプタ（agents.py）"
if python3 -I "$HERE/test_agents.py" >"$TMP/agents.log" 2>&1; then
  ok "agents.py のテスト（$(grep -Eo 'Ran [0-9]+ tests' "$TMP/agents.log")）"
else
  ng "agents.py のテスト"; grep -E '^(FAIL|ERROR|Traceback|AssertionError)|Error' "$TMP/agents.log" | head -20
fi

echo "== Codex のガード（codex_guard.py）"
if python3 -I "$HERE/test_codex_guard.py" >"$TMP/guard.log" 2>&1; then
  ok "codex_guard.py のテスト（$(grep -Eo 'Ran [0-9]+ tests' "$TMP/guard.log")）"
else
  ng "codex_guard.py のテスト"; grep -E '^(FAIL|ERROR|Traceback|AssertionError)|Error' "$TMP/guard.log" | head -20
fi

echo "== 配信サーバー（serve.py。claude は代役。Chrome があれば質問欄の表示も確かめる）"
if python3 -I "$HERE/test_serve.py" >"$TMP/serve.log" 2>&1; then
  ok "serve.py のテスト（$(grep -Eo 'Ran [0-9]+ tests' "$TMP/serve.log")$(grep -Eo 'skipped=[0-9]+' "$TMP/serve.log" | sed 's/^/, /')）"
else
  ng "serve.py のテスト"; grep -E '^(FAIL|ERROR|Traceback|AssertionError)|Error' "$TMP/serve.log" | head -20
fi

echo "== setup.sh / teardown.sh"
if bash "$HERE/setup.test.sh" >"$TMP/setup.log" 2>&1; then
  ok "setup.sh / teardown.sh のテスト（$(grep -c '^  ok' "$TMP/setup.log") 件）"
else
  ng "setup.sh / teardown.sh のテスト"; grep FAIL "$TMP/setup.log"
fi

[ "$fail" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
