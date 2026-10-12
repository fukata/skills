#!/usr/bin/env python3
"""check-explanation.py — visual-explain の explanation.html を機械的に検査する。

  usage: check-explanation.py EXPLANATION_HTML [--repo DIR] [--ignore-ref PREFIX ...]
                              [--inventory FILE] [--no-browser] [--chrome PATH] [--json]
                              [--print-width PX]
         check-explanation.py ANSWER_HTML --fragment [--doc EXPLANATION_HTML] [--repo DIR] ...

  exit 0: エラーなし（警告は出ることがある）
  exit 1: エラーあり（資料を渡す前に直す）
  exit 2: 使い方・ファイル読込の失敗

エラー:
  - ページ内リンク（href="#id"）の飛び先が無い / 相対パスの画像・リンク先のファイルが無い
  - img に alt が無い / 雛形（assets/explanation.html）の文が残っている
  - URL や本文に資格情報・トークンらしき文字列がある
  - コード参照（path:line）のファイルがリポジトリに無い / 行番号がファイルの行数を超える
  - [ブラウザ] SVG の文字が枠（rect）や viewBox からはみ出す / 画像が読み込めない
  - [ブラウザ] 狭い幅でページ全体が横にはみ出す
  - [ブラウザ] 印刷（A4 縦）でページが横にはみ出す / スクロール・高さの制限で枠の中が切れる
  - 本文に Markdown の記法（**太字**、`コード`、[リンク](URL)、# 見出し）が残っている
警告:
  - コード参照と隣り合って書いた識別子が、参照した行（前後2行と、囲む def / task 等の行を含む）に無い
  - 本文の識別子（クラス名・snake_case の名前など）がリポジトリに見当たらない
  - ファイル名だけの参照が複数のファイルに当たり、行番号を検査できない
  - 外部のスクリプト・スタイル・画像を読み込んでいる / 生の Mermaid 原稿が残っている
  - svg に title が無い / figure に figcaption が無い
  - svg に width が無い（広い画面で本文の幅いっぱいまで拡大される。viewBox と同じ width・height を書く）
  - 図の無い節に、表の行と箇条書きが合わせて 8 個以上ある（羅列。根拠・注意点などの節は除く）
  - 図の無い節の 1 つの段落に、日付・時刻が 4 個以上、または矢印（→）が 2 個以上ある（日程や順番を
    文章だけで追わせている。根拠などの節は除く）
  - --inventory で渡した構成要素の名前が、資料のどこにも出てこない（網羅の抜け）
  - 前提の節（id="context"、または見出しが「前提」「背景」）が無い
  - 略語・チケット番号・クラス名（PdM、ABC-123、QuoteService など）の意味が、前提の節・用語の一覧
    （dt / dd）・出てくる所の括弧のどこにも無い（事前知識の無い人には分からない。根拠などの節は除く）
  - 図のある節に要点（class="key"）が無い（重要な所と補足の見分けが付かない）
  - 会話を指す言葉（「さっき」「先ほど」「ご質問の」など）がある（資料だけを渡された人には指す先が無い）
  - [ブラウザ] SVG の文字同士が重なる / 文字が枠の線にかかる / 文字と枠の左右の余白が 2 未満
  - [ブラウザ] 1 つの箱に 3 行または 60 文字を超える文字がある（図の形をした表になっている）
  - [ブラウザ] 実線の矢印・線が文字を貫いている（破線は生存線や補助線として除く）
  - [ブラウザ] 印刷（A4 縦）で図の文字が 9px 未満になる / 閉じた details の中身が印刷されない

serve.py export が書き出した「読者からの質問と回答」の節（<!-- visual-explain: reader-qa begin --> と end の間）は
質問と回答の記録なので、資格情報だけを検査する（読み手の質問や回答の誤りで、本文の修正の検査を止めない）。

--fragment: 質問欄の回答（HTML の断片）を検査する。図のはみ出し・リンク・コード参照・Markdown の記法・
資格情報・svg の title と width だけを見る（前提・要点・用語・印刷・狭い幅は見ない）。--doc に資料を渡すと、
資料の節へのリンクと、資料からの相対パスの画像を確かめる。

--inventory の書式: 1 行に 1 要素。「名前 | 別名 | 別名」のように | で区切ると、どれか 1 つが
出てくれば載っているとみなす（テーブル名とモデル名など）。# で始まる行と空行は読み飛ばす。

検査しないもの: 説明の正しさ、図が仕組みを表しているか、文章の読みやすさ。
ブラウザの検査はヘッドレス Chrome（--chrome、環境変数 VISUAL_EXPLAIN_CHROME、chrome-headless-shell、
PATH の Chrome・Chromium、Playwright のキャッシュの順に探し、起動してページを取り出せた最初のものを使う）で行い、
見つからなければ飛ばして警告を出す。
文字幅はこのマシンのフォントで測るため、閲覧側のフォントでの見え方とは一致しない。
"""
import argparse
import glob
import html
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "..", "assets", "explanation.html")

BLOCK_TAGS = {"p", "li", "td", "th", "dt", "dd", "figcaption", "caption", "summary",
              "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "source", "track", "wbr"}
RESOURCE_TAGS = {"script", "img", "iframe", "video", "audio", "source", "embed", "object", "link"}

EXTENSIONS = ("rb|rake|erb|haml|slim|jbuilder|js|mjs|cjs|ts|tsx|jsx|vue|py|sh|bash|go|java|kt|"
              "swift|yml|yaml|json|toml|tf|sql|md|html|css|scss|rbs|gradle|xml|libsonnet|jsonnet|tsp")
BARE_FILES = r"Gemfile\.lock|Gemfile|Rakefile|Dockerfile|Makefile|Procfile|Brewfile"
REF_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*(?:[\w.-]+\.(?:%s)|%s))(?::(\d+)(?:-(\d+))?)?(?!\w)"
                    % (EXTENSIONS, BARE_FILES))
LINE_ONLY_RE = re.compile(r"^:(\d+)(?:-(\d+))?$")
SCHEME_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)
QUALIFIED_RE = re.compile(r"\b[A-Z]\w*(?:::[A-Z]\w*)+")
TOKEN_RE = re.compile(r"\w+")
CAMEL_RE = re.compile(r"^[A-Z][a-z0-9]+[A-Z]")
MERMAID_RE = re.compile(r"```mermaid|^\s*(?:(?:flowchart|graph)\s+(?:TD|TB|BT|RL|LR)\b|"
                        r"sequenceDiagram\b|stateDiagram(?:-v2)?\b|classDiagram\b|erDiagram\b|gantt\b)",
                        re.M)
SECRET_PATTERNS = [
    ("URL に Basic 認証の資格情報", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@\"'<>]+:[^/\s@\"'<>]+@", re.I)),
    ("URL にトークンらしきクエリ", re.compile(
        r"[?&](?:access_?token|token|api_?key|apikey|password|passwd|secret|signature|sig|"
        r"x-amz-signature|x-amz-credential)=[^&\s\"'<>]+", re.I)),
    ("AWS のアクセスキー", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack のトークン", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}")),
    ("GitHub のトークン", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}")),
    ("HubSpot のトークン", re.compile(r"\bpat-[a-z0-9]+-[0-9a-f-]{20,}")),
    ("秘密鍵", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

TEMPLATE_MIN_LEN = 15   # 雛形の文として照合する最小の長さ（短い見出しは流用してよい）
REF_WINDOW = 2          # 参照した行の前後何行まで識別子を探すか
PAIR_GAP = 6            # 識別子と参照を「隣り合う」とみなす間の文字数（「 の 」「（」「）、」程度）
DEFINITION_RE = re.compile(r"^\s*(?:def|task|class|module|scope|namespace|describe|context|function|"
                           r"export|const|resource|module\.exports)\b")
DEFINITION_LOOKBACK = 80  # 参照した行を囲む定義を何行上まで探すか
SVG_TOLERANCE = 0.5     # viewBox 単位。これを超えたらはみ出し
SVG_TIGHT = 2.0         # 枠の左右の余白がこれ未満なら警告（閲覧側のフォントではみ出しうる）
SVG_OVERLAP_AREA = 4.0  # 文字同士の重なりとみなす面積（viewBox 単位の二乗）
DENSE_BOX_LINES = 3     # 1 つの箱の行数がこれを超えたら詰め込み（SKILL.md の目安は 2〜3 行）
DENSE_BOX_CHARS = 60    # 1 つの箱の文字数（空白を除く）がこれを超えたら詰め込み
BACKGROUND_RATIO = 0.5  # viewBox の面積に対してこの割合を超える rect は背景とみなし、詰め込みを数えない
LIST_SECTION_ITEMS = 8  # 図の無い節で、表の行と箇条書きの合計がこれ以上なら羅列
# 列挙が本来の役目の節。「読者からの質問」は serve.py export が書き出す、質問と回答の記録
LIST_EXEMPT_RE = re.compile(r"根拠|出典|参考|確認範囲|注意|読者からの質問")
EVIDENCE_RE = re.compile(r"根拠|出典|参考|確認範囲|読者からの質問")  # 確かめたい人向けの節。用語・日程の検査から外す
CONTEXT_HEADING_RE = re.compile(r"^(?:前提|背景)")
# 略語（PdM・API）、チケット番号（ABC-123）、クラス名（QuoteService）。大文字を 2 つ以上含む英数字の語。
# 前後は ASCII だけで区切る（Python の \w は日本語の文字にも当たり、「のAPIを」を取りこぼすため）
TERM_RE = re.compile(r"(?<![A-Za-z0-9_./:#@-])([A-Z][A-Z0-9]+-\d+|[A-Za-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)"
                     r"(?![A-Za-z0-9_./:#@-])")
TERM_KNOWN = {"PDF", "URL", "CSV", "HTML", "ID", "ON", "OFF", "OK", "NG", "PC", "OS", "iOS", "AI"}  # 説明の要らない語
TERM_MAX_REPORTS = 10   # これを超えた分は 1 件にまとめて報告する
# 日付・時刻の 1 時点。「1 日 11:05」「2026-10-07 03:17」は 1 時点、「1・2・4・7 日」は 4 時点と数える
TIME_RE = re.compile(r"(?:\d{4}-\d{1,2}-\d{1,2}|(?<!\d)\d{1,2}/\d{1,2}(?![\d/])|"
                     r"(?P<days>\d{1,2}(?:\s*[・、,]\s*\d{1,2})*)\s*日(?!間|目))(?:\s*\d{1,2}:\d{2})?|\d{1,2}:\d{2}")
ARROW_RE = re.compile(r"→|⇒|->")
TEXT_FLOW_TIMES = 4     # 図の無い節の 1 段落に、日付・時刻がこれ以上あれば日程を文章で追わせている
TEXT_FLOW_ARROWS = 2    # 同じく矢印がこれ以上（A → B → C）あれば順番を文章で追わせている
CONVERSATION_RE = re.compile(r"さっき|先ほど|先程|ご質問|ご指摘|お尋ね|おっしゃ|この会話|会話の中で|"
                             r"会話で(?:話|触れ|決め|伝え|述べ)|上で話した")
MARKDOWN_PATTERNS = [
    ("**太字**", re.compile(r"\*\*[^*\n]+\*\*")),
    ("`コード`", re.compile(r"`[^`\n]+`")),
    ("[リンク](URL)", re.compile(r"\[[^\]\n]+\]\([^)\s]+\)")),
    ("# 見出し", re.compile(r"^[ \t]*#{1,6}[ \t]+\S", re.M)),
]
PRINT_MIN_PX = 9.0      # 印刷したときの図の文字の大きさの下限（9px ≒ 6.75pt）
# serve.py export が書き出す、質問と回答の記録の節を囲む印（serve.py の EXPORT_BEGIN / EXPORT_END と同じ）
EXPORT_MARKS = ("visual-explain: reader-qa begin", "visual-explain: reader-qa end")


class Doc(HTMLParser):
    """検査に要る要素だけを集める。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # (tag, block_id)
        self.ids = set()
        self.attrs = []          # (tag, name, value, line, attrs)
        self.imgs_without_alt = []
        self.svgs = []           # {"line", "title", "width"}
        self.figures = []        # {"line", "caption"}
        self.mermaid_class = []
        self.texts = []          # {"text", "line", "block", "in_svg", "in_code", "section", "in_context", "in_terms"}
        self.codes = []          # {"text", "line", "block"}
        self.events = []         # 出現順の {"kind": "code"|"text", "text", "line", "block", "in_svg"}
        self._block_seq = 0
        self._code = None
        self._figure_stack = []
        self.sections = []       # h2 ごと {"heading", "line", "figures", "items", "keys"}
        self.context = False     # 前提の節（id="context" / class="context"）があるか
        self._context_depth = None  # 前提の節の要素を開いたときの stack の深さ（中にいる間だけ値を持つ）
        self.in_export = False   # 質問と回答の記録の節の中か。中の属性と文字は資格情報の検査にだけ使う
        self.export_values = []  # (文字, 行)

    def handle_comment(self, data):
        if data.strip() == EXPORT_MARKS[0]:
            self.in_export = True
        elif data.strip() == EXPORT_MARKS[1]:
            self.in_export = False

    def _in(self, tag):
        return any(t == tag for t, _ in self.stack)

    def _block(self):
        for _, block in reversed(self.stack):
            if block is not None:
                return block
        return None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        line = self.getpos()[0]
        if a.get("id"):
            self.ids.add(a["id"])
        if self.in_export:
            self.export_values += [(v, line) for v in a.values()]
            if tag == "svg" and not self._in("svg"):
                self.svgs.append({"line": line, "title": True, "width": True, "hidden": False, "export": True})
            if tag not in VOID_TAGS:
                self.stack.append((tag, None))
            return
        for name in ("href", "src", "poster", "data"):
            if name in a:
                self.attrs.append((tag, name, a[name], line, a))
        if tag == "img" and not a.get("alt", "").strip() and a.get("role") != "presentation" \
                and a.get("aria-hidden") != "true":
            self.imgs_without_alt.append((a.get("src", ""), line))
        classes = a.get("class", "").split()
        if "mermaid" in classes:
            self.mermaid_class.append(line)
        if a.get("id") == "context" or "context" in classes:
            self.context = True
            if self._context_depth is None and tag not in VOID_TAGS:
                self._context_depth = len(self.stack)
        if tag == "svg" and not self._in("svg"):
            self.svgs.append({"line": line, "title": False, "width": re.fullmatch(r"\s*[\d.]+\s*(?:px)?\s*",
                                                                                 a.get("width", "")) is not None,
                              "hidden": a.get("aria-hidden") == "true"})
        if tag == "title" and self._in("svg") and self.svgs:
            self.svgs[-1]["title"] = True
        if tag == "figure":
            self.figures.append({"line": line, "caption": False})
            self._figure_stack.append(len(self.figures) - 1)
        if tag == "figcaption" and self._figure_stack:
            self.figures[self._figure_stack[-1]]["caption"] = True
        if tag == "h2":
            self.sections.append({"heading": "", "line": line, "figures": 0, "items": 0, "keys": 0})
        if self.sections:
            if "key" in classes:
                self.sections[-1]["keys"] += 1
            if tag in ("svg", "img") and not self._in("svg"):
                self.sections[-1]["figures"] += 1
            if (tag == "li" and not self._in("nav")) or (tag == "tr" and not self._in("thead")):
                self.sections[-1]["items"] += 1
        block = None
        if tag in BLOCK_TAGS:
            self._block_seq += 1
            block = self._block_seq
        if tag not in VOID_TAGS:
            self.stack.append((tag, block))
        if tag == "code" and self._code is None:
            self._code = {"text": "", "line": line, "block": self._block()}
            self.events.append(dict(self._code, kind="code", in_svg=self._in("svg"), in_pre=self._in("pre"),
                                    ref=self._code))

    def handle_endtag(self, tag):
        if self.in_export:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break
            return
        if tag == "code" and self._code is not None:
            self.codes.append(self._code)
            self._code = None
        if tag == "figure" and self._figure_stack:
            self._figure_stack.pop()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if self._context_depth is not None and len(self.stack) <= self._context_depth:
            self._context_depth = None

    def handle_data(self, data):
        if self._in("script") or self._in("style"):
            return
        if self.in_export:
            if data.strip():
                self.export_values.append((data, self.getpos()[0]))
            return
        if self._in("h2") and self.sections:
            self.sections[-1]["heading"] += data
        if self._code is not None:
            self._code["text"] += data
        else:
            self.events.append({"kind": "text", "text": data, "line": self.getpos()[0], "block": self._block(),
                                "in_svg": self._in("svg"), "in_pre": self._in("pre")})
        if data.strip():
            self.texts.append({"text": data, "line": self.getpos()[0], "block": self._block(),
                               "in_svg": self._in("svg"), "in_code": self._code is not None or self._in("pre"),
                               "section": len(self.sections) - 1, "in_context": self._context_depth is not None,
                               "in_terms": self._in("dt") or self._in("dd")})


class Findings:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, check, message, line=None):
        self.errors.append({"check": check, "message": message, "line": line})

    def warn(self, check, message, line=None):
        self.warnings.append({"check": check, "message": message, "line": line})


def parse(text):
    doc = Doc()
    doc.feed(text)
    doc.close()
    return doc


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def mask(secret):
    return secret[:8] + "…" if len(secret) > 8 else "…"


# ---------------------------------------------------------------- 静的な検査

def check_links(doc, html_dir, f):
    for tag, name, value, line, attrs in doc.attrs:
        v = value.strip()
        if not v or tag == "base":
            continue
        if v.startswith("#"):
            if len(v) > 1 and v[1:] not in doc.ids:
                f.error("anchor", 'href="%s" の飛び先（id="%s"）が無い' % (v, v[1:]), line)
            continue
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", v, re.I):
            is_resource = tag in RESOURCE_TAGS and not (tag == "link" and "stylesheet" not in attrs.get("rel", "")
                                                       and "icon" not in attrs.get("rel", ""))
            if is_resource and re.match(r"^(?:https?:)?//", v, re.I):
                f.warn("external", "外部の資源を読み込んでいる（<%s %s=\"%s\">）。HTML 単体で読めるよう同梱する" %
                       (tag, name, v), line)
            continue
        target = os.path.normpath(os.path.join(html_dir, re.split(r"[?#]", v)[0]))
        if not os.path.exists(target):
            f.error("local-file", "%s=\"%s\" のファイルが無い（%s）" % (name, v, target), line)


def check_structure(doc, f, fragment=False):
    for src, line in doc.imgs_without_alt:
        f.error("img-alt", 'img（src="%s"）に alt が無い' % src, line)
    for svg in doc.svgs:
        if not svg["title"]:
            f.warn("svg-title", "svg に title が無い（図の要点を代替テキストとして書く）", svg["line"])
        if not svg["width"] and not svg["hidden"] and not svg.get("export"):
            f.warn("svg-width", "svg に width・height が無い（または %% で書いている）。広い画面で本文の幅いっぱいまで拡大される。"
                   "viewBox と同じ値の width・height を書く（狭い画面では縮む）", svg["line"])
    for fig in doc.figures:
        if not fig["caption"]:
            f.warn("figcaption", "figure に figcaption が無い", fig["line"])
    for line in doc.mermaid_class:
        f.warn("mermaid", 'class="mermaid" の要素がある。原稿のままでは図にならない（描画済みの SVG を載せる）', line)
    if fragment:
        for t in doc.texts:
            if MERMAID_RE.search(t["text"]):
                f.warn("mermaid", "生の Mermaid 原稿らしき文がある（描画済みの SVG を載せる）", t["line"])
        return
    for sec in doc.sections:
        heading = normalize(sec["heading"])
        if sec["figures"] == 0 and sec["items"] >= LIST_SECTION_ITEMS and not LIST_EXEMPT_RE.search(heading):
            f.warn("list-section", "「%s」の節に図が無く、表の行と箇条書きが %d 個ある。図で示すか、どの図のどこを"
                   "詳しくした表かを書く" % (heading, sec["items"]), sec["line"])
        if sec["figures"] and not sec["keys"] and not LIST_EXEMPT_RE.search(heading):
            f.warn("key", "「%s」の節に要点（class=\"key\"）が無い。図の前に、この図から読み取る結論を要点として置く" %
                   heading, sec["line"])
    if not doc.context and not any(CONTEXT_HEADING_RE.match(normalize(s["heading"])) for s in doc.sections):
        f.warn("context", "前提の節（id=\"context\"）が無い。何の話か・なぜ読むのか・用語を、資料だけで読めるように書く")
    for t in doc.texts:
        if MERMAID_RE.search(t["text"]):
            f.warn("mermaid", "生の Mermaid 原稿らしき文がある（描画済みの SVG を載せる）", t["line"])
        if not t["in_code"]:
            m = CONVERSATION_RE.search(t["text"])
            if m:
                f.warn("context-ref", "「%s」は会話を指している。資料だけを渡された人には指す先が無いので、指すものを書く" %
                       m.group(0), t["line"])


def section_heading(doc, index):
    return normalize(doc.sections[index]["heading"]) if index >= 0 else ""


def check_terms(doc, f):
    """略語・チケット番号・クラス名のうち、意味がどこにも書かれていないものを報告する。

    前提の節・用語の一覧（dt / dd）の中に出てくるか、どこかで「PdM（プロダクトマネージャー）」のように
    直後に括弧が続くか「プロダクトマネージャー（PdM）」のように括弧の中に単独で書かれていれば、説明があるとみなす。"""
    context_sections = {i for i, s in enumerate(doc.sections) if CONTEXT_HEADING_RE.match(normalize(s["heading"]))}
    explained, first = set(), {}
    for t in doc.texts:
        if t["in_code"]:
            continue
        text = t["text"]
        for m in TERM_RE.finditer(text):
            term = m.group(1)
            if term in TERM_KNOWN:
                continue
            start, end = m.span(1)
            if t["in_context"] or t["in_terms"] or t["section"] in context_sections or \
                    text[end:end + 1] in ("（", "(") or (text[start - 1:start], text[end:end + 1]) in (("（", "）"), ("(", ")")):
                explained.add(term)
            if not EVIDENCE_RE.search(section_heading(doc, t["section"])):
                first.setdefault(term, t["line"])
    missing = [(term, line) for term, line in first.items() if term not in explained]
    for term, line in missing[:TERM_MAX_REPORTS]:
        f.warn("term", "「%s」の意味が書かれていない。前提の節の用語の一覧か、初めて出る所の括弧で、"
               "何のことかを読み手が知っている言葉で書く" % term, line)
    rest = missing[TERM_MAX_REPORTS:]
    if rest:
        f.warn("term", "ほかに %d 語の意味が書かれていない（%s）。コードの名前なら、言葉で説明してから code 要素で添える" %
               (len(rest), "、".join(t for t, _ in rest[:10]) + ("…" if len(rest) > 10 else "")), rest[0][1])


def check_text_flow(doc, f):
    """図の無い節で、日程や順番を文章だけで追わせている段落を報告する。"""
    blocks = {}
    for t in doc.texts:
        if t["in_svg"] or t["in_code"]:
            continue
        sec = t["section"]
        if sec >= 0 and (doc.sections[sec]["figures"] or EVIDENCE_RE.search(section_heading(doc, sec))):
            continue
        key = t["block"] if t["block"] is not None else ("loose", t["line"])  # 段落の外の文字は 1 つずつ見る
        b = blocks.setdefault(key, {"text": "", "line": t["line"]})
        b["text"] += t["text"]
    for b in blocks.values():
        times = sum(len(re.findall(r"\d+", m.group("days"))) if m.group("days") else 1
                    for m in TIME_RE.finditer(b["text"]))
        arrows = len(ARROW_RE.findall(b["text"]))
        if times >= TEXT_FLOW_TIMES or arrows >= TEXT_FLOW_ARROWS:
            f.warn("text-flow", "「%s…」の段落は、日付・時刻 %d 個・矢印 %d 個の日程や順番を文章だけで追わせている。"
                   "時間軸の図や流れの図にし、文章は図から読み取る結論にする" %
                   (normalize(b["text"])[:24], times, arrows), b["line"])


def check_markdown(doc, f):
    """HTML の本文に残った Markdown の記法を探す。code 要素は「x」に置き換え、要素をまたいだ記法も拾う。"""
    blocks = {}
    for i, e in enumerate(doc.events):
        if e.get("in_pre"):
            continue
        key = e["block"] if e["block"] is not None else ("loose", i)  # 段落の外の文字（図の中など）は 1 つずつ見る
        b = blocks.setdefault(key, {"text": "", "line": e["line"]})
        b["text"] += "x" if e["kind"] == "code" else e["text"]
    for b in blocks.values():
        for label, pattern in MARKDOWN_PATTERNS:
            m = pattern.search(b["text"])
            if m:
                f.error("markdown", "Markdown の記法（%s）が文字のまま残っている:「%s」。HTML の要素（strong・code・a・h2 等）で書く" %
                        (label, normalize(m.group(0))[:40]), b["line"])


def check_template(doc, f):
    if not os.path.isfile(TEMPLATE_PATH):
        f.warn("template", "雛形が見つからないので、雛形の文の残りは検査しなかった（%s）" % TEMPLATE_PATH)
        return
    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        template = parse(fh.read())
    body = " ".join(normalize(t["text"]) for t in doc.texts)
    seen = set()
    for t in template.texts:
        sentence = normalize(t["text"])
        if len(sentence) >= TEMPLATE_MIN_LEN and sentence not in seen and sentence in body:
            seen.add(sentence)
            line = next((x["line"] for x in doc.texts if sentence in normalize(x["text"])), None)
            f.error("template", "雛形の文が残っている:「%s」" % sentence, line)


def check_inventory(doc, path, f, stats):
    """構成要素の一覧のうち、資料のどこにも出てこない名前を報告する。"""
    with open(path, encoding="utf-8") as fh:
        entries = [line.strip() for line in fh.read().splitlines()]
    spaced = normalize(" ".join(t["text"] for t in doc.texts))
    joined = "".join(t["text"].strip() for t in doc.texts)  # 図の中で行をまたいだ名前も拾う
    total = found = 0
    for entry in entries:
        if not entry or entry.startswith("#"):
            continue
        names = [n.strip() for n in entry.split("|") if n.strip()]
        total += 1
        if any(n in spaced or n in joined for n in names):
            found += 1
        else:
            f.warn("inventory", "「%s」が資料のどこにも出てこない。全体像の図に載せるか、扱わないものとして示す" %
                   names[0])
    stats["inventory"] = "%d/%d" % (found, total)


def check_secrets(doc, f):
    sources = [(v, line) for _, _, v, line, _ in doc.attrs] + [(t["text"], t["line"]) for t in doc.texts] + doc.export_values
    for value, line in sources:
        for label, pattern in SECRET_PATTERNS:
            m = pattern.search(value)
            if m:
                f.error("secret", "%s らしき文字列がある（%s）" % (label, mask(m.group(0))), line)


# ---------------------------------------------------------------- コード参照・識別子

class Repo:
    SKIP_DIRS = {".git", "node_modules", "vendor", "tmp", "log", ".bundle", "coverage", "__pycache__"}

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.git = subprocess.run(["git", "-C", self.root, "rev-parse", "--is-inside-work-tree"],
                                  capture_output=True, text=True).returncode == 0
        if self.git:
            # まだコミットしていないファイルを参照することもあるので、無視されていない未追跡も含める
            out = subprocess.run(["git", "-C", self.root, "ls-files", "-z", "--cached", "--others",
                                  "--exclude-standard"], capture_output=True).stdout
            self.files = [p for p in out.decode("utf-8", "replace").split("\0") if p]
        else:
            self.files = []
            for d, dirs, names in os.walk(self.root):
                dirs[:] = [x for x in dirs if x not in self.SKIP_DIRS]
                for n in names:
                    self.files.append(os.path.relpath(os.path.join(d, n), self.root))
        self.file_set = set(self.files)
        self._lines = {}

    def resolve(self, path):
        """参照のパスをリポジトリ内のファイルに解決する。戻り値は (確定したパス, 候補の一覧)。"""
        path = path.lstrip("./") if path.startswith("./") else path
        if path in self.file_set:
            return path, [path]
        candidates = [p for p in self.files if p.endswith("/" + path)]
        return (candidates[0] if len(candidates) == 1 else None), candidates

    def lines(self, rel):
        if rel not in self._lines:
            with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as fh:
                self._lines[rel] = fh.read().splitlines()
        return self._lines[rel]

    def find_words(self, words):
        """リポジトリに単語として現れる語を返す。"""
        words = sorted(set(words))
        if not words:
            return set()
        if self.git:
            found = set()
            for i in range(0, len(words), 200):
                args = ["git", "-C", self.root, "grep", "--untracked", "-I", "-o", "-h", "-w", "-F"]
                for w in words[i:i + 200]:
                    args += ["-e", w]
                out = subprocess.run(args, capture_output=True).stdout.decode("utf-8", "replace")
                found.update(out.split("\n"))
            return found & set(words)
        pattern = re.compile(r"(?<!\w)(%s)(?!\w)" % "|".join(re.escape(w) for w in words))
        found = set()
        for rel in self.files:
            path = os.path.join(self.root, rel)
            try:
                if os.path.getsize(path) > 2_000_000:
                    continue
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    found.update(pattern.findall(fh.read()))
            except OSError:
                continue
        return found

    def find_definitions(self, names):
        """class / module として定義されている名前を返す。"""
        return {m.split()[-1] for m in self.find_words(["class " + n for n in names] +
                                                       ["module " + n for n in names])}


def identifier_tokens(code_text):
    """code 要素の文字列から、リポジトリで確かめられる識別子を取り出す。"""
    qualified = QUALIFIED_RE.findall(code_text)
    rest = QUALIFIED_RE.sub(" ", code_text)
    tokens = []
    for tok in TOKEN_RE.findall(rest):
        if tok.startswith("_") or tok.endswith("_") or not re.search(r"[A-Za-z]", tok):
            continue
        if ("_" in tok and len(tok) >= 4) or CAMEL_RE.match(tok):
            tokens.append(tok)
    return qualified, tokens


def context_tokens(code_text):
    """参照した行に現れるはずの語。パスらしき文字列からは取らない。"""
    if "/" in code_text:
        return []
    qualified, tokens = identifier_tokens(code_text)
    return [q.split("::")[-1] for q in qualified] + tokens


def is_ref_code(code_text):
    return bool(REF_RE.search(code_text) or LINE_ONLY_RE.match(code_text.strip()))


def collect_refs(doc, ignore):
    """コード参照を出現順に集める。code 要素の中の参照は、イベント列の位置（event）を持つ。"""
    refs = []
    last_path = {}
    for i, e in enumerate(doc.events):
        if e["in_svg"]:
            continue
        text = e["ref"]["text"] if e["kind"] == "code" else e["text"]
        m = LINE_ONLY_RE.match(text.strip())
        if e["kind"] == "code" and m:
            path = last_path.get(e["block"])
            if path:
                refs.append({"path": path, "start": int(m.group(1)), "end": int(m.group(2) or m.group(1)),
                             "raw": text.strip(), "line": e["line"], "block": e["block"], "event": i})
            continue
        for m in REF_RE.finditer(SCHEME_URL_RE.sub(" ", text)):
            path, start, end = m.group(1), m.group(2), m.group(3)
            if "/" not in path and not start:
                continue  # 行番号の無いファイル名だけの言及は参照とみなさない
            if any(path.startswith(p) for p in ignore):
                continue
            refs.append({"path": path, "start": int(start) if start else None,
                         "end": int(end or start) if start else None, "raw": m.group(0), "line": e["line"],
                         "block": e["block"], "event": i if e["kind"] == "code" else None})
            last_path[e["block"]] = path
    return refs


def neighbor_identifiers(doc, index):
    """参照の code 要素と隣り合う（間の文字が PAIR_GAP 以下の）code 要素を返す。[(位置, 識別子の一覧)]"""
    block = doc.events[index]["block"]
    found = []
    for step in (-1, 1):
        gap = ""
        j = index + step
        while 0 <= j < len(doc.events) and doc.events[j]["block"] == block:
            e = doc.events[j]
            if e["kind"] == "text":
                gap += e["text"]
                if len(gap.strip()) > PAIR_GAP:
                    break
            else:
                code = e["ref"]["text"]
                tokens = context_tokens(code)
                if not is_ref_code(code) and tokens:
                    found.append((j, tokens))
                break
            j += step
    return found


def ref_window(lines, start, end):
    """参照した行の前後 REF_WINDOW 行に、その行を囲む定義（def / task 等）の行を足した文字列。"""
    lo, hi = max(1, start - REF_WINDOW), min(len(lines), end + REF_WINDOW)
    window = lines[lo - 1:hi]
    for n in range(lo - 1, max(0, lo - 1 - DEFINITION_LOOKBACK), -1):
        if DEFINITION_RE.match(lines[n - 1]):
            window.insert(0, lines[n - 1])
            break
    return "\n".join(window)


def check_code(doc, repo, ignore, f, stats):
    refs = collect_refs(doc, ignore)
    stats["refs"] = len(refs)
    doc_paths = {r["path"] for r in refs if "/" in r["path"]}
    resolved = 0
    paired = {}  # 識別子の code 要素の位置 -> {"tokens", "windows": [(参照, 行の文字列)]}
    for r in refs:
        rel, candidates = repo.resolve(r["path"])
        if rel is None and len(candidates) > 1:
            # ファイル名だけの参照は、資料の別の箇所にフルパスで書いたものがあればそれに決める
            narrowed = [c for c in candidates if any(c == p or c.endswith("/" + p) for p in doc_paths)]
            rel = narrowed[0] if len(narrowed) == 1 else None
        if rel is None:
            if candidates:
                f.warn("ref-ambiguous", "「%s」は %d 個のファイルに当たり、どれか決められない（例: %s）。パスを書く" %
                       (r["raw"], len(candidates), ", ".join(candidates[:3])), r["line"])
            else:
                f.error("ref-missing", "「%s」のファイルがリポジトリに無い（別リポジトリなら --ignore-ref で除く）" %
                        r["raw"], r["line"])
            continue
        resolved += 1
        if r["start"] is None:
            continue
        lines = repo.lines(rel)
        if r["start"] < 1 or r["end"] < r["start"] or r["end"] > len(lines):
            f.error("ref-line", "「%s」の行番号が範囲外（%s は %d 行）" % (r["raw"], rel, len(lines)), r["line"])
            continue
        if r["event"] is None:
            continue
        window = ref_window(lines, r["start"], r["end"])
        for position, tokens in neighbor_identifiers(doc, r["event"]):
            group = paired.setdefault(position, {"tokens": tokens, "windows": []})
            group["windows"].append((r, window))
    stats["refs_resolved"] = resolved

    # 参照と隣り合って書いた識別子が、組になったどの参照の行にも無ければ、行番号のずれを疑う
    for position, group in sorted(paired.items()):
        if not any(tok in text for tok in group["tokens"] for _, text in group["windows"]):
            f.warn("ref-context", "「%s」と並べた参照（%s）の行に、その識別子が無い。行番号を確かめる" %
                   (doc.events[position]["ref"]["text"].strip(), ", ".join(r["raw"] for r, _ in group["windows"])),
                   doc.events[position]["line"])

    # 識別子がリポジトリに実在するか
    qualified_all, words = set(), set()
    first_line = {}
    for c in doc.codes:
        if "*" in c["text"] or "/" in c["text"] or is_ref_code(c["text"]):
            continue
        qualified, tokens = identifier_tokens(c["text"])
        for q in qualified:
            qualified_all.add(q)
            first_line.setdefault(q, c["line"])
        for tok in tokens:
            words.add(tok)
            first_line.setdefault(tok, c["line"])
    found = repo.find_words(list(words) + list(qualified_all))
    unresolved_q = [q for q in qualified_all if q not in found]
    defined = repo.find_definitions({q.split("::")[-1] for q in unresolved_q})
    missing = sorted([w for w in words if w not in found] +
                     [q for q in unresolved_q if q.split("::")[-1] not in defined])
    stats["identifiers"] = len(words) + len(qualified_all)
    stats["identifiers_missing"] = len(missing)
    for name in missing:
        f.warn("identifier", "「%s」がリポジトリに見当たらない（名前の誤り、または別リポジトリの名前）" % name,
               first_line.get(name))


# ---------------------------------------------------------------- ブラウザでの検査

MEASURE_JS = r"""
(function () {
  function label(el) { var t = (el.textContent || '').replace(/\s+/g, ' ').trim(); return t.length > 40 ? t.slice(0, 40) + '…' : t; }
  function desc(el) { var s = el.tagName.toLowerCase(); if (el.id) s += '#' + el.id; if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\s+/).join('.'); return s; }
  function scrolls(el) { for (var p = el.parentElement; p && p !== document.body; p = p.parentElement) { var o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden') return true; } return false; }
  function lines(el) { var tops = {}, ts = el.querySelectorAll('tspan'); [].forEach.call(ts, function (t) { if ((t.textContent || '').trim()) tops[Math.round(t.getBoundingClientRect().top)] = 1; }); return Math.max(1, Object.keys(tops).length); }
  function box(el, b) { var r = el.getBoundingClientRect(); return {x: (r.left - b.left) / b.sx + b.vx, y: (r.top - b.top) / b.sy + b.vy, w: r.width / b.sx, h: r.height / b.sy}; }
  function run() {
    var res = {innerWidth: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: [], images: [], svgs: []};
    [].forEach.call(document.images, function (img) { if (!img.complete || img.naturalWidth === 0) res.images.push(img.getAttribute('src')); });
    if (res.scrollWidth > innerWidth + 1) {
      var all = document.body.getElementsByTagName('*');
      for (var i = 0; i < all.length && res.overflow.length < 5; i++) {
        var r = all[i].getBoundingClientRect();
        if (r.width > 0 && r.right > innerWidth + 1 && !scrolls(all[i]) && !(all[i].closest && all[i].closest('svg') && all[i].tagName.toLowerCase() !== 'svg')) res.overflow.push(desc(all[i]) + ' (right=' + Math.round(r.right) + 'px)');
      }
    }
    var top = 0;
    [].forEach.call(document.querySelectorAll('svg'), function (svg) {
      if (svg.parentElement && svg.parentElement.closest('svg')) return;
      var i = top++;
      var sr = svg.getBoundingClientRect(), vb = svg.viewBox && svg.viewBox.baseVal, has = vb && vb.width > 0;
      var b = {left: sr.left, top: sr.top, vx: has ? vb.x : 0, vy: has ? vb.y : 0, w: has ? vb.width : sr.width, h: has ? vb.height : sr.height};
      b.sx = sr.width / b.w || 1; b.sy = sr.height / b.h || 1;
      var t = svg.querySelector('title');
      var s = {index: i + 1, title: t ? t.textContent.trim() : '', vb: [b.vx, b.vy, b.w, b.h], rects: [], texts: []};
      [].forEach.call(svg.querySelectorAll('rect'), function (el) { var x = box(el, b); if (x.w > 0 && x.h > 0) s.rects.push(x); });
      [].forEach.call(svg.querySelectorAll('text'), function (el) { var x = box(el, b); if (x.w > 0) { x.label = label(el); x.lines = lines(el); x.chars = (el.textContent || '').replace(/\s+/g, '').length; x.px = parseFloat(getComputedStyle(el).fontSize) * b.sx; s.texts.push(x); } });
      // 実線の矢印・線が文字を貫いていないか。線上の点を 2 単位ごとに取り、文字の範囲に入るかを見る
      s.crossings = [];
      [].forEach.call(svg.querySelectorAll('path, line, polyline'), function (p) {
        if (p.closest('marker, defs, clipPath, pattern') || !p.getTotalLength) return;
        var cs = getComputedStyle(p);
        if (cs.stroke === 'none' || parseFloat(cs.strokeWidth) === 0 || (cs.strokeDasharray && cs.strokeDasharray !== 'none')) return;
        var m = p.getScreenCTM(), len = p.getTotalLength(), step = 2 * b.sx;
        for (var l = 0; l <= len; l += step) {
          var q = p.getPointAtLength(l), x = ((q.x * m.a + q.y * m.c + m.e) - b.left) / b.sx + b.vx, y = ((q.x * m.b + q.y * m.d + m.f) - b.top) / b.sy + b.vy;
          for (var k = 0; k < s.texts.length; k++) {
            var t = s.texts[k];
            if (x > t.x + 1 && x < t.x + t.w - 1 && y > t.y + 1 && y < t.y + t.h - 1 && s.crossings.indexOf(t.label) < 0) s.crossings.push(t.label);
          }
        }
      });
      res.svgs.push(s);
    });
    // 枠の中が切れている要素（スクロール・高さの制限）。印刷ではスクロールできないので、そのまま切れる
    res.clipped = [];
    [].forEach.call(document.body.getElementsByTagName('*'), function (el) {
      if (res.clipped.length >= 5 || el.closest('svg')) return;
      var cs = getComputedStyle(el);
      if (!/auto|scroll|hidden|clip/.test(cs.overflowX + ' ' + cs.overflowY)) return;
      if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) res.clipped.push(desc(el) + ' (中身 ' + el.scrollWidth + '×' + el.scrollHeight + 'px、枠 ' + el.clientWidth + '×' + el.clientHeight + 'px)');
    });
    // 閉じた details のうち、中身が表示されないもの
    res.hiddenDetails = 0;
    [].forEach.call(document.querySelectorAll('details:not([open])'), function (d) {
      var c = [].filter.call(d.children, function (x) { return x.tagName.toLowerCase() !== 'summary'; })[0];
      if (c && c.checkVisibility && !c.checkVisibility()) res.hiddenDetails++;
    });
    var out = document.createElement('script');
    out.type = 'application/json'; out.id = '__ve_check_result';
    out.textContent = JSON.stringify(res).replace(/</g, '\\u003c');
    document.documentElement.appendChild(out);
  }
  if (document.readyState === 'complete') run(); else window.addEventListener('load', run);
})();
"""


def headless_flag(chrome):
    """通常の Chrome・Chromium に付ける引数（chrome-headless-shell には要らない）。"""
    return [] if "headless" in os.path.basename(chrome).lower() else ["--headless=new"]


CHROME_WORKS = {}


def chrome_works(chrome):
    """起動して、ページを取り出せるかを確かめる（同じプロセスでは 1 回だけ）。

    実行ファイルがあっても動くとは限らない（snap の案内だけのスクリプト、ページを取り出さずに止まる Chromium）。
    動かないものを選ぶと、検査のたびに時間切れまで待つことになる。
    """
    if chrome not in CHROME_WORKS:
        work = tempfile.mkdtemp(prefix="ve-probe-")
        try:
            out = subprocess.run([chrome] + headless_flag(chrome) + [
                "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--user-data-dir=" + os.path.join(work, "profile"), "--virtual-time-budget=1000",
                "--dump-dom", "data:text/html,<p id=ve-probe>ok</p>"], capture_output=True, timeout=20).stdout
            CHROME_WORKS[chrome] = b"ve-probe" in out
        except (OSError, subprocess.SubprocessError):
            CHROME_WORKS[chrome] = False
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return CHROME_WORKS[chrome]


def find_chrome(explicit):
    """ページを取り出せる最初の Chrome を返す。無ければ None。

    順番: --chrome、環境変数、chrome-headless-shell（PATH・Playwright のキャッシュ）、PATH の Chrome・Chromium、
    macOS のアプリ、Playwright のキャッシュの Chromium。
    """
    def cached(*patterns):
        found = []
        for cache in (os.path.expanduser("~/.cache/ms-playwright"), os.path.expanduser("~/Library/Caches/ms-playwright")):
            for pattern in patterns:
                hits = glob.glob(os.path.join(cache, pattern))
                hits.sort(key=lambda p: [int(n) for n in re.findall(r"-(\d+)/", p)] or [0], reverse=True)
                found += hits
        return found

    candidates = [explicit, os.environ.get("VISUAL_EXPLAIN_CHROME"), shutil.which("chrome-headless-shell")]
    candidates += cached("chromium_headless_shell-*/*/chrome-headless-shell", "chromium_headless_shell-*/*/headless_shell")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        candidates.append(shutil.which(name))
    candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    candidates += cached("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium")
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK) and chrome_works(c):
            return c
    return None


def as_print(src):
    """印刷の CSS を画面で再現する。style の @media の print を all に、screen を一致しない型（tty）に置き換える。"""
    def media(m):
        q = re.sub(r"\bprint\b", "all", m.group(1), flags=re.I)
        return "@media" + re.sub(r"\bscreen\b", "tty", q, flags=re.I) + "{"

    def style(m):
        return m.group(1) + re.sub(r"@media\b([^{]*)\{", media, m.group(2), flags=re.I) + m.group(3)
    return re.sub(r"(<style\b[^>]*>)(.*?)(</style>)", style, src, flags=re.I | re.S)


def measure(chrome, html_path, width, height, print_media=False, base_dir=None, src=None, nonce=None):
    if src is None:
        with open(html_path, encoding="utf-8") as fh:
            src = fh.read()
    if print_media:
        src = as_print(src)
    base_dir = base_dir or os.path.dirname(os.path.abspath(html_path))
    base = '<base href="%s">' % html.escape(pathlib.Path(base_dir).as_uri() + "/")
    m = re.search(r"<head[^>]*>", src, re.I)
    src = src[:m.end()] + base + src[m.end():] if m else base + src
    i = src.lower().rfind("</body>")
    script = ('<script nonce="%s">' % nonce if nonce else "<script>") + MEASURE_JS + "</script>"
    src = src[:i] + script + src[i:] if i >= 0 else src + script
    work = tempfile.mkdtemp(prefix="ve-check-")
    try:
        page = os.path.join(work, "page.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(src)
        args = [chrome] + headless_flag(chrome)
        args += ["--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                 "--no-default-browser-check", "--allow-file-access-from-files",
                 "--user-data-dir=" + os.path.join(work, "profile"),
                 "--window-size=%d,%d" % (width, height), "--virtual-time-budget=5000",
                 "--dump-dom", pathlib.Path(page).as_uri()]
        proc = subprocess.run(args, capture_output=True, timeout=120)
        out = proc.stdout.decode("utf-8", "replace")
        m = re.search(r'<script type="application/json" id="__ve_check_result">(.*?)</script>', out, re.S)
        if not m:
            raise RuntimeError("計測結果を取り出せなかった（exit=%d）" % proc.returncode)
        return json.loads(m.group(1))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def area(b):
    return b["w"] * b["h"]


def intersection(a, b):
    w = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return w * h if w > 0 and h > 0 else 0.0


def contains(outer, inner, tol=0.0):
    return (inner["x"] >= outer["x"] - tol and inner["y"] >= outer["y"] - tol and
            inner["x"] + inner["w"] <= outer["x"] + outer["w"] + tol and
            inner["y"] + inner["h"] <= outer["y"] + outer["h"] + tol)


def recorded(doc, s):
    """書き出した質問と回答の記録の中の図か。"""
    return s["index"] <= len(doc.svgs) and doc.svgs[s["index"] - 1].get("export")


def check_svgs(result, f, doc):
    for s in result["svgs"]:
        if recorded(doc, s):
            continue
        name = "図「%s」" % s["title"] if s["title"] else "%d 番目の svg" % s["index"]
        line = doc.svgs[s["index"] - 1]["line"] if s["index"] <= len(doc.svgs) else None
        vx, vy, vw, vh = s["vb"]
        view = {"x": vx, "y": vy, "w": vw, "h": vh}
        boxes = {}  # rect の位置 -> 中の文字の行数・文字数
        for t in s["texts"]:
            cx, cy = t["x"] + t["w"] / 2, t["y"] + t["h"] / 2
            holders = [(i, r) for i, r in enumerate(s["rects"])
                       if r["x"] <= cx <= r["x"] + r["w"] and r["y"] <= cy <= r["y"] + r["h"]]
            if holders:
                k, r = min(holders, key=lambda h: area(h[1]))
                if area(r) <= BACKGROUND_RATIO * vw * vh:
                    agg = boxes.setdefault(k, {"lines": 0, "chars": 0, "label": t["label"]})
                    agg["lines"] += t.get("lines", 1)
                    agg["chars"] += t.get("chars", len(t["label"]))
                over = max(r["x"] - t["x"], t["x"] + t["w"] - r["x"] - r["w"],
                           r["y"] - t["y"], t["y"] + t["h"] - r["y"] - r["h"])
                if over > SVG_TOLERANCE:
                    f.error("svg-overflow", "%s: 「%s」が枠から %.1f はみ出す（枠 x=%.0f〜%.0f, 文字 x=%.0f〜%.0f）" %
                            (name, t["label"], over, r["x"], r["x"] + r["w"], t["x"], t["x"] + t["w"]), line)
                elif min(t["x"] - r["x"], r["x"] + r["w"] - t["x"] - t["w"]) < SVG_TIGHT:
                    f.warn("svg-tight", "%s: 「%s」と枠の左右の余白が %.1f 未満（閲覧側のフォントではみ出しうる）" %
                           (name, t["label"], SVG_TIGHT), line)
                continue
            if not contains(view, t, SVG_TOLERANCE):
                f.error("svg-viewbox", "%s: 「%s」が viewBox（%g %g %g %g）の外にはみ出す" %
                        (name, t["label"], vx, vy, vw, vh), line)
            for r in s["rects"]:
                if intersection(r, t) > SVG_OVERLAP_AREA and not contains(r, t):
                    f.warn("svg-border", "%s: 「%s」が枠の線にかかっている" % (name, t["label"]), line)
                    break
        for agg in boxes.values():
            if agg["lines"] > DENSE_BOX_LINES or agg["chars"] > DENSE_BOX_CHARS:
                f.warn("svg-dense-box", "%s: 「%s」の箱に %d 行・%d 文字ある。箱は 2〜3 行にし、細部は図の外の表や根拠へ出す" %
                       (name, agg["label"], agg["lines"], agg["chars"]), line)
        for label in s.get("crossings", []):
            f.warn("svg-text-on-line", "%s: 「%s」を実線の矢印・線が貫いている（文字か線をずらす）" % (name, label), line)
        texts = s["texts"]
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                if intersection(texts[i], texts[j]) > SVG_OVERLAP_AREA:
                    f.warn("svg-overlap", "%s: 「%s」と「%s」が重なっている" % (name, texts[i]["label"], texts[j]["label"]), line)


# 回答の断片を、質問欄（qa.css）と同じ見た目で測るためのページ。回答は claude が書いたまま（質問欄が取り除く前）なので、
# CSP で計測のスクリプト（nonce 付き）以外を動かさず、外の資源も読ませない
FRAGMENT_PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'">
<style>
body { margin: 0; font: 15px/1.7 system-ui, sans-serif; }
.ve-qa-a { width: 600px; padding: 12px 18px; overflow-wrap: anywhere; }
.ve-qa-a svg { display: block; max-width: 100%; height: auto; margin: 8px 0; }
.ve-qa-a svg:not([width]) { width: 100%; max-width: 720px; }
.ve-qa-a img { max-width: 100%; height: auto; }
</style></head><body><div class="ve-qa-a">{body}</div></body></html>"""


def check_fragment_browser(args, text, base_dir, doc, f, stats):
    if not doc.svgs and "<img" not in text.lower():
        stats["browser"] = "skipped (図・画像なし)"
        return
    chrome = find_chrome(args.chrome)
    if not chrome:
        f.warn("browser", "ヘッドレス Chrome が見つからないので、図のはみ出しの検査を飛ばした（--chrome で指定できる）")
        stats["browser"] = "skipped"
        return
    try:
        nonce = secrets.token_hex(12)
        page = FRAGMENT_PAGE.replace("{nonce}", nonce).replace("{body}", text)
        wide = measure(chrome, args.html, args.wide_width, 900, base_dir=base_dir, src=page, nonce=nonce)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        f.warn("browser", "ブラウザでの検査に失敗した: %s" % e)
        stats["browser"] = "failed"
        return
    stats["browser"] = "%s fragment" % os.path.basename(chrome)
    for src in wide["images"]:
        f.error("image", "画像を読み込めない（src=\"%s\"）" % src)
    check_svgs(wide, f, doc)


def check_browser(args, doc, f, stats):
    chrome = find_chrome(args.chrome)
    if not chrome:
        f.warn("browser", "ヘッドレス Chrome が見つからないので、図のはみ出しと狭い幅の検査を飛ばした（--chrome で指定できる）")
        stats["browser"] = "skipped"
        return
    try:
        wide = measure(chrome, args.html, args.wide_width, 900)
        narrow = measure(chrome, args.html, args.narrow_width, 844)
        printed = measure(chrome, args.html, args.print_width, 1000, print_media=True)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        f.warn("browser", "ブラウザでの検査に失敗した: %s" % e)
        stats["browser"] = "failed"
        return
    stats["browser"] = "%s %dpx/%dpx/print %dpx" % (os.path.basename(chrome), args.wide_width, args.narrow_width,
                                                    args.print_width)
    for src in wide["images"]:
        f.error("image", "画像を読み込めない（src=\"%s\"）" % src)
    check_svgs(wide, f, doc)
    if narrow["scrollWidth"] > narrow["innerWidth"] + 1:
        f.error("narrow-overflow", "幅 %dpx でページが横にはみ出す（scrollWidth=%dpx）。はみ出す要素: %s" %
                (narrow["innerWidth"], narrow["scrollWidth"], ", ".join(narrow["overflow"]) or "不明"))
    check_print(printed, f, doc)


def check_print(result, f, doc):
    """印刷（A4 縦）の CSS で測った結果を見る。印刷ではスクロールできないので、はみ出しと枠の中の切れは欠ける。"""
    if result["scrollWidth"] > result["innerWidth"] + 1:
        f.error("print-overflow", "印刷（幅 %dpx）でページが横にはみ出し、右側が切れる（scrollWidth=%dpx）。はみ出す要素: %s。"
                "印刷用の CSS で min-width を外す" %
                (result["innerWidth"], result["scrollWidth"], ", ".join(result["overflow"]) or "不明"))
    for c in result.get("clipped", []):
        f.error("print-clipped", "印刷すると枠の中が切れる: %s。印刷用の CSS で overflow と max-height を外す" % c)
    for s in result["svgs"]:
        if recorded(doc, s):
            continue
        sizes = [t["px"] for t in s["texts"] if t.get("px")]
        if sizes and min(sizes) < PRINT_MIN_PX:
            name = "図「%s」" % s["title"] if s["title"] else "%d 番目の svg" % s["index"]
            line = doc.svgs[s["index"] - 1]["line"] if s["index"] <= len(doc.svgs) else None
            f.warn("print-small-text", "%s: 印刷（幅 %dpx）で文字が %.1fpx まで小さくなる。viewBox の幅を 1000 以下にするか、"
                   "文字を大きくする" % (name, result["innerWidth"], min(sizes)), line)
    if result.get("hiddenDetails"):
        f.warn("print-details", "閉じた details が %d 個あり、印刷すると中身が出ない。雛形の印刷用の CSS"
               "（details::details-content）を残すか、大事な内容を details の外に出す" % result["hiddenDetails"])


# ---------------------------------------------------------------- 入口

def default_repo(html_dir):
    for d in (html_dir, os.getcwd()):
        p = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout.strip()
    return None


def main():
    ap = argparse.ArgumentParser(description="visual-explain の explanation.html を機械的に検査する")
    ap.add_argument("html", help="検査する explanation.html")
    ap.add_argument("--repo", help="コード参照を確かめるリポジトリ（既定: HTML の置き場所の git リポジトリ）")
    ap.add_argument("--ignore-ref", action="append", default=[], metavar="PREFIX",
                    help="この接頭辞で始まる参照は検査しない（別リポジトリのパスなど。複数指定可）")
    ap.add_argument("--inventory", metavar="FILE",
                    help="資料に載っているべき構成要素の一覧（1 行 1 要素、| で別名）。出てこない名前を警告する")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザでの検査を飛ばす")
    ap.add_argument("--chrome", help="ヘッドレス Chrome の実行ファイル")
    ap.add_argument("--wide-width", type=int, default=1280, help="図を測る幅（既定: 1280）")
    ap.add_argument("--narrow-width", type=int, default=390, help="横のはみ出しを見る幅（既定: 390）")
    ap.add_argument("--print-width", type=int, default=700,
                    help="印刷の本文の幅（既定: 700。A4 縦で左右の余白 12mm）")
    ap.add_argument("--json", action="store_true", help="結果を JSON で出す")
    ap.add_argument("--fragment", action="store_true",
                    help="質問欄の回答（HTML の断片）として検査する。図・リンク・コード参照・記法・資格情報だけを見る")
    ap.add_argument("--doc", help="--fragment のとき、回答を表示する資料。節へのリンクと画像の相対パスをこの資料で確かめる")
    args = ap.parse_args()

    try:
        with open(args.html, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        print("check-explanation: %s を読めない: %s" % (args.html, e), file=sys.stderr)
        return 2
    html_dir = os.path.dirname(os.path.abspath(args.doc if args.fragment and args.doc else args.html))
    doc = parse(text)
    f = Findings()
    stats = {}

    if args.fragment and args.doc:
        try:
            with open(args.doc, encoding="utf-8") as fh:
                doc.ids |= parse(fh.read()).ids
        except OSError as e:
            print("check-explanation: %s を読めない: %s" % (args.doc, e), file=sys.stderr)
            return 2
    check_links(doc, html_dir, f)
    check_structure(doc, f, fragment=args.fragment)
    if not args.fragment:
        check_terms(doc, f)
        check_text_flow(doc, f)
    check_markdown(doc, f)
    if not args.fragment:
        check_template(doc, f)
    check_secrets(doc, f)
    if args.inventory and not args.fragment:
        try:
            check_inventory(doc, args.inventory, f, stats)
        except OSError as e:
            print("check-explanation: %s を読めない: %s" % (args.inventory, e), file=sys.stderr)
            return 2

    repo_root = args.repo or default_repo(html_dir)
    if repo_root and os.path.isdir(repo_root):
        stats["repo"] = os.path.abspath(repo_root)
        check_code(doc, Repo(repo_root), args.ignore_ref, f, stats)
    else:
        f.warn("repo", "リポジトリが分からないので、コード参照と識別子を検査しなかった（--repo で指定する）")
    if args.no_browser:
        stats["browser"] = "skipped (--no-browser)"
    elif args.fragment:
        check_fragment_browser(args, text, html_dir, doc, f, stats)
    else:
        check_browser(args, doc, f, stats)

    if args.json:
        print(json.dumps({"ok": not f.errors, "errors": f.errors, "warnings": f.warnings, "stats": stats},
                         ensure_ascii=False, indent=2))
    else:
        for level, items in (("ERROR", f.errors), ("WARN ", f.warnings)):
            for item in items:
                where = " L%d" % item["line"] if item["line"] else ""
                print("%s [%s]%s: %s" % (level, item["check"], where, item["message"]))
        print("summary: errors=%d warnings=%d refs=%s resolved=%s identifiers=%s missing=%s inventory=%s browser=%s" % (
            len(f.errors), len(f.warnings), stats.get("refs", "-"), stats.get("refs_resolved", "-"),
            stats.get("identifiers", "-"), stats.get("identifiers_missing", "-"), stats.get("inventory", "-"),
            stats.get("browser", "-")))
    return 1 if f.errors else 0


if __name__ == "__main__":
    sys.exit(main())
