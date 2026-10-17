#!/usr/bin/env python3
"""visual-explain の資料を HTTP で配信し、資料のページから届いた追加の質問に claude -p で答える。

  配信:    python3 serve.py serve --dir <配信フォルダ> --port <port> [--bind 127.0.0.1] [--repo <説明したリポジトリ>]
  記録:    python3 serve.py origin <資料フォルダ> [--cwd <作業ディレクトリ>]
  書き出し: python3 serve.py export <資料フォルダ>   （質問と回答を、資料の末尾の節に書き出す）

配信は python3 -m http.server と同じく静的ファイルを返す。資料（--doc-name、既定 explanation.html）を返すときだけ
質問欄（assets/qa/qa.js）を差し込み、質問は 1 件ずつ claude -p に渡す。回答は <資料>/qa/ に保存し、
claude が直した作業用コピーは検査を通ったときだけ本文と差し替える。claude の途中経過（読んだファイル・検索・
本文の修正）は、回答を待つ間に質問欄へ出す。回答では Web と、claude に設定された MCP のサーバーも使える
（--local-only で止める）。書き込みは作業用コピー・回答・images/ だけ、コマンドは検査だけに限る。
Python 標準ライブラリだけで動く。
"""
import argparse
import functools
import hashlib
import hmac
import html
import importlib.util
import ipaddress
import json
import os
import pathlib
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.parse import quote as url_quote

try:
    import fcntl  # 資料ごとの排他（Windows には無いので、その場合は排他しない）
except ImportError:
    fcntl = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import agents as agents_mod  # noqa: E402  エージェントごとに違う所（コマンド・出力・セッション）
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
ASSET_DIR = os.path.join(SKILL_DIR, "assets", "qa")
DEFAULT_PROMPT = os.path.join(ASSET_DIR, "answer-prompt.md")
DEFAULT_CHECK = os.path.join(SCRIPT_DIR, "check-explanation.py")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
TEMPLATE = os.path.join(SKILL_DIR, "assets", "explanation.html")
BUILD_PROMPT = os.path.join(SKILL_DIR, "assets", "ask", "build-prompt.md")
SERVE_PY = os.path.abspath(__file__)
VERSION = "1.2.0"  # 常駐の配信サーバーはこれより古い版なら起動し直す。serve.py・ask_hook.py・assets を変えたら上げる
ASK_CHILD_ENV = "VISUAL_EXPLAIN_ASK_CHILD"  # 配信サーバーが動かす claude -p の印。フックはこの中の質問を扱わない

QA_DIR = "qa"                 # 資料フォルダの中で質問と回答を置くフォルダ。静的配信では返さない
DRAFT_SUFFIX = ".qa-draft"    # explanation.html → explanation.qa-draft.html（同じフォルダに置き、相対パスを保つ）
MAX_QUESTION = 4000
MAX_QUOTE = 2000
MAX_BODY = 64 * 1024
MAX_ANSWER = 512 * 1024
MAX_REPLY_CONTEXT = 20000     # 続きの質問の指示文に入れる、元の回答の長さ（文字）
CHECK_TIMEOUT = 600
KILL_GRACE = 10               # 時間切れで SIGTERM を送ってから SIGKILL までの猶予（秒）
ALL_INTERFACES = {"", "0.0.0.0", "::", "[::]"}
# 親のエージェントのセッションから引き継がない環境変数（入れ子のセッションと誤認させない）
PARENT_ENV = agents_mod.ClaudeAgent.parent_env + agents_mod.CodexAgent.parent_env
MCP_LIST_TIMEOUT = agents_mod.MCP_LIST_TIMEOUT
# 質問を受け付ける資料の HTML に付ける。同じ配信元のスクリプト（qa.js・pdf.js）以外と、外部の資源を読み込ませない
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")
BODY_END_RE = re.compile(r"</body\s*>", re.I)
PUBLIC_ASSETS = ("/__qa/qa.js", "/__qa/qa.css", "/__qa/inbox.js", "/__qa/pdf.js", "/__qa/pdf.css")
PRIVATE_MESSAGE = "この配信は、案内された URL（?k= 付き）で開いたブラウザにだけ見せる。受け箱の URL で開き直してください。\n"
SESSION_ID_RE = agents_mod.SESSION_ID_RE
SECTION_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,200}$")
MODES = {"new": "新しいセッション", "fork": "資料を作ったセッションを分岐", "resume": "前の質問のセッションの続き"}
QID_RE = re.compile(r"^q\d{1,9}$")
# AskUserQuestion の質問を資料のページで答える（ask_hook.py が登録し、答えを待つ）
ASK_CONFIG = "ask.json"           # <ask_home()>/ask.json
ASK_WAIT_DEFAULT = 1800
ASK_WAIT_MAX = 7000               # hooks.json の timeout（7200 秒）より短くする
MAX_ASK_BODY = 512 * 1024         # 選択肢のプレビュー（文字の図）が長いことがある
MAX_OTHER = 2000                  # 「その他」の答えとメモの長さ
WAITER_GRACE = 20                 # フックからの問い合わせがこの秒数来なければ、もう待っていないとみなす
BUILD_QUESTION = "選ぶための資料を作る"
ASK_KEEP_DAYS = 30                # これより古い、締め切った質問の資料フォルダは消す
# auto: 受け箱を一度でも開いたことがあれば always、無ければ open（既定）。open: 受け箱か質問の資料をブラウザで
# 開いているときだけページに出す。always: 常にページで待つ。off: 使わない
ASK_MODES = ("auto", "open", "always", "off")
INBOX_USED = ".inbox-used"        # 受け箱を開いたことがある印（受け箱の配信サーバーの置き場所に置く）
ASK_WORKERS = 3                   # 受け箱の配信サーバーが同時に作る資料の数（質問したセッションが別々なら並行して作る）
VIEWER_GRACE = 30                 # auto で、ページを見ている人がこの秒数いなければ、ターミナルで答えてもらう
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
ASK_NAME_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
VIEWER_PREFS = "viewer.json"      # 見る人の好み（質問欄の幅）。ask_home() に置き、このマシンのどの配信サーバーも使う
PANEL_WIDTH = (320, 4000)         # 覚える質問欄の幅の範囲（qa.js の最小の幅から、思わぬ値を弾く上限まで）
MAX_STEPS = 40                # 1 件の回答で残す途中経過の数
CANCELED = "取り消した。"
# 質問と回答を書き出す節。印の間を書き出すたびに置き換える
EXPORT_ID = "reader-qa"
EXPORT_TITLE = "読者からの質問と回答"
EXPORT_BEGIN = "<!-- visual-explain: reader-qa begin -->"
EXPORT_END = "<!-- visual-explain: reader-qa end -->"
EXPORT_RE = re.compile(re.escape(EXPORT_BEGIN) + r".*?" + re.escape(EXPORT_END), re.S)
MAIN_END_RE = re.compile(r"</main\s*>", re.I)
NAV_RE = re.compile(r"<nav\b([^>]*)>(.*?)</nav\s*>", re.I | re.S)
ID_RE = re.compile(r"(?<![\w-])id\s*=\s*[\"']([^\"']+)[\"']", re.I)
STAMP_RE = re.compile(r"（\d+ 件。(\d{4}-\d\d-\d\d \d\d:\d\d) に書き出し）")


def now(after=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=after)).astimezone().isoformat(timespec="seconds")


def passed(iso):
    try:
        return datetime.fromisoformat(str(iso)) <= datetime.now(timezone.utc)
    except ValueError:
        return False


def file_rev(path):
    with open(path, "rb") as f:
        return bytes_rev(f.read())


def bytes_rev(data):
    return hashlib.sha256(data).hexdigest()[:12]


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def parse_duration(text):
    """'2h' '90m' '30s' '7200' を秒に直す。0 は上限なし。"""
    m = re.fullmatch(r"\s*(\d+)\s*([hms]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError("時間は 2h / 90m / 30s / 秒数 の形で指定する: %r" % text)
    return int(m.group(1)) * {"h": 3600, "m": 60, "s": 1, "": 1}[m.group(2)]


def git_toplevel(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def text_to_html(text):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return "".join("<p>%s</p>" % html.escape(p).replace("\n", "<br>") for p in paras)


class SectionCollector(HTMLParser):
    """<section id> ごとに中身（タグと文字）と最初の見出しを集める。入れ子の節は外側の節にも数える。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.content, self.titles, self.order, self.heading = [], {}, {}, [], None

    def current(self):
        return next((s for s in reversed(self.stack) if s), None)

    def emit(self, text):
        for sid in self.stack:
            if sid:
                self.content[sid].append(text)

    def handle_starttag(self, tag, attrs):
        if tag == "section":
            sid = dict(attrs).get("id")
            self.stack.append(sid)
            if sid and sid not in self.content:
                self.content[sid] = []
                self.order.append(sid)
        self.emit("<%s %s>" % (tag, sorted(attrs)))
        if tag in ("h2", "h3") and self.current() and self.current() not in self.titles and not self.heading:
            self.heading = (self.current(), [])

    def handle_endtag(self, tag):
        self.emit("</%s>" % tag)
        if tag in ("h2", "h3") and self.heading:
            sid, parts = self.heading
            self.titles[sid] = re.sub(r"\s+", " ", "".join(parts)).strip()
            self.heading = None
        if tag == "section" and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        self.emit(data)
        if self.heading:
            self.heading[1].append(data)


def collect_sections(text):
    c = SectionCollector()
    c.feed(text)
    c.close()
    return c


def changed_sections(before, after):
    """after で中身が変わった、または新しく足された節を、after の順に返す。"""
    a, b = collect_sections(before), collect_sections(after)
    norm = lambda parts: re.sub(r"\s+", " ", "".join(parts)).strip()
    out = []
    for sid in b.order:
        if sid not in a.content or norm(a.content[sid]) != norm(b.content[sid]):
            out.append({"id": sid, "title": b.titles.get(sid) or sid})
    return out


class ActiveContent(HTMLParser):
    """スクリプト・イベント属性・javascript: の URL・外部の資源を数える。本文の修正で増えていないかを見る。"""

    URL_ATTRS = {"href", "src", "xlink:href", "action", "formaction", "data", "poster", "srcset"}
    EXTERNAL_RE = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?[/\\]{2}", re.I)  # ブラウザは \ を / と読む
    CSS_EXTERNAL_RE = re.compile(r"url\s*\(\s*['\"]?\s*(?:[a-z][a-z0-9+.-]*:)?//|@import", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = Counter()
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "iframe", "frame", "object", "embed", "base"):
            self.found["<%s>" % tag] += 1
        self.in_style = tag == "style"
        for name, value in attrs:
            name, value = name.lower(), value or ""
            if name.startswith("on"):
                self.found["on*"] += 1
            elif name in self.URL_ATTRS:
                if re.sub(r"[\s\x00-\x1f]", "", value).lower().startswith(("javascript:", "vbscript:")):
                    self.found["javascript:"] += 1
                elif self.EXTERNAL_RE.match(re.sub(r"[\x00-\x20]", "", value)) and not (tag == "a" and name == "href"):
                    self.found["external"] += 1
            elif name == "style" and self.CSS_EXTERNAL_RE.search(value):
                self.found["external-css"] += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.in_style = False

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style and self.CSS_EXTERNAL_RE.search(data):
            self.found["external-css"] += 1


def active_content(text):
    p = ActiveContent()
    p.feed(text)
    p.close()
    return dict(p.found)


# 回答の HTML で残すタグと属性。qa.js の sanitize と同じ許可リスト（片方を変えたら、もう片方も変える）
ALLOWED_TAGS = set(("p ul ol li strong em b i code pre a br h3 h4 h5 table thead tbody tfoot tr th td caption "
                    "figure figcaption blockquote details summary dl dt dd div span hr sup sub kbd mark small img "
                    "svg g path rect circle ellipse line polyline polygon text tspan defs marker title desc").split())
DROP_TAGS = set(("script style iframe object embed noscript template form input button textarea select link "
                 "meta base foreignobject use image animate set").split())
ALLOWED_ATTRS = set(("href title alt class id colspan rowspan scope open start reversed lang dir src width height "
                     "viewbox xmlns role aria-label aria-labelledby aria-hidden d x y x1 y1 x2 y2 cx cy r rx ry points "
                     "fill stroke stroke-width stroke-dasharray stroke-linecap stroke-linejoin font-size font-weight "
                     "font-family text-anchor dominant-baseline transform marker-end marker-start markerwidth "
                     "markerheight refx refy orient markerunits opacity fill-opacity stroke-opacity dx dy "
                     "preserveaspectratio").split())
SVG_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "defs",
            "marker", "title", "desc"}
VOID_TAGS = {"br", "hr", "img"}
# HTML の解析で小文字になる SVG の属性を、元の書き方に戻す（ブラウザは小文字でも読むが、読み返す人のため）
ATTR_CASE = {"viewbox": "viewBox", "preserveaspectratio": "preserveAspectRatio", "markerwidth": "markerWidth",
             "markerheight": "markerHeight", "refx": "refX", "refy": "refY", "markerunits": "markerUnits"}


def safe_url(name, value):
    # ブラウザは URL の \ を / と読み、空白・制御文字を無視する（\\evil.example は //evil.example になる）
    v = re.sub(r"[\x00-\x20]", "", value.strip().lower()).replace("\\", "/")
    scheme = re.match(r"^[a-z][a-z0-9+.-]*:", v)
    if name == "src":
        return not scheme and not v.startswith("//")
    return v.startswith("#") or v.startswith(("http:", "https:")) or not scheme


PAINT_ATTRS = {"fill", "stroke", "marker-end", "marker-start"}
PAINT_FUNC_RE = re.compile(r"\s*(?:url\(\s*['\"]?#[\w:.-]+['\"]?\s*\)|(?:rgba?|hsla?)\(\s*[\d.,%\s/]+\))\s*", re.I)
PAINT_REF_RE = re.compile(r"(url\(\s*['\"]?#)([\w:.-]+)")


def safe_paint(value):
    """fill・stroke・marker の値で、外の資源を読む url(...) を許さない。url(#id) と色の関数だけ許す。"""
    return not re.search(r"[(\\]", value) or PAINT_FUNC_RE.fullmatch(value) is not None


class Sanitizer(HTMLParser):
    """回答の HTML を、許可したタグ・属性だけで組み立て直す。資料に書き出すときに使う（質問欄では qa.js が同じことをする）。
    ids を渡すと、その中に無い節へのリンク（href="#..."）は外す。prefix を渡すと、断片の id が prefix で始まらなければ
    付け足し、断片の中からの参照（href・aria-labelledby・url(#...)）も合わせる（資料の id と重ねない）。
    width の無い図には viewBox の大きさを書き足す。"""

    def __init__(self, ids=None, prefix=None, own=()):
        super().__init__(convert_charrefs=True)
        self.out, self.open, self.drop = [], [], []
        self.ids, self.prefix, self.own = ids, prefix, set(own)

    def rename(self, i):
        return self.prefix + i if self.prefix and i in self.own and not i.startswith(self.prefix) else i

    def handle_starttag(self, tag, attrs):
        self.start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self.start(tag, attrs, True)

    def start(self, tag, attrs, closed):
        if self.drop:
            if tag == self.drop[-1] and not closed:
                self.drop.append(tag)
            return
        if tag in DROP_TAGS:
            if not closed and tag not in VOID_TAGS:
                self.drop.append(tag)
            return
        # 図の中の HTML のタグは書かない（ブラウザは図の外に出して読み、図の後ろの構造が崩れる）
        if tag not in ALLOWED_TAGS or ("svg" in self.open and tag not in SVG_TAGS):
            return
        kept = []
        for name, value in attrs:
            name, value = name.lower(), value or ""
            if name not in ALLOWED_ATTRS or name.startswith("on"):
                continue
            if name in ("href", "src") and not safe_url(name, value):
                continue
            if name in PAINT_ATTRS:
                if not safe_paint(value):
                    continue
                value = PAINT_REF_RE.sub(lambda r: r.group(1) + self.rename(r.group(2)), value)
            if name == "id":
                value = self.rename(value)
            elif name == "aria-labelledby":
                value = " ".join(self.rename(t) for t in value.split())
            elif name == "href" and value.startswith("#"):
                if value[1:] in self.own:
                    value = "#" + self.rename(value[1:])
                elif self.ids is not None and value[1:] not in self.ids:
                    continue
            kept.append((ATTR_CASE.get(name, name), value))
        names = {n.lower() for n, _ in kept}
        if tag == "svg" and "svg" not in self.open and "width" not in names:
            box = dict(attrs).get("viewbox") or ""
            nums = re.split(r"[\s,]+", box.strip())
            try:
                w, h = float(nums[2]), float(nums[3])
                kept += [("width", "%g" % w), ("height", "%g" % h)] if w > 0 and h > 0 else []
            except (IndexError, ValueError):
                pass
        if tag == "a" and re.match(r"^https?:", dict(kept).get("href", ""), re.I):
            kept += [("target", "_blank"), ("rel", "noopener noreferrer")]
        text = "".join(' %s="%s"' % (n, html.escape(v, quote=True)) for n, v in kept)
        in_svg = tag == "svg" or "svg" in self.open
        if closed and in_svg:
            self.out.append("<%s%s/>" % (tag, text))
        elif closed and tag not in VOID_TAGS:
            self.out.append("<%s%s></%s>" % (tag, text, tag))
        else:
            self.out.append("<%s%s>" % (tag, text))
            if tag not in VOID_TAGS:
                self.open.append(tag)

    def handle_endtag(self, tag):
        if self.drop:
            if tag == self.drop[-1]:
                self.drop.pop()
            return
        if tag in self.open:
            while self.open:
                t = self.open.pop()
                self.out.append("</%s>" % t)
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        self.close()
        return "".join(self.out) + "".join("</%s>" % t for t in reversed(self.open))


def sanitize_fragment(text, ids=None, prefix=None):
    s = Sanitizer(None if ids is None else set(ids), prefix, ID_RE.findall(text or ""))
    s.feed(text or "")
    return s.result()


def short_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def thread_order(items):
    """続きの質問（reply_to）を、元の質問のすぐ後ろに並べる。同じ元の中では聞いた順（質問欄の並びと同じ）。"""
    pos = {i.get("id"): n for n, i in enumerate(items)}

    def root(n):
        seen = set()
        while n not in seen and items[n].get("reply_to") in pos:
            seen.add(n)
            n = pos[items[n]["reply_to"]]
        return n

    return [items[n] for n in sorted(range(len(items)), key=lambda n: (root(n), n))]


def export_section(items, ids, stamp):
    """回答した質問を、資料の末尾に置く節（印で囲む）にする。ids は資料の id（節へのリンクを残すか決める）。
    stamp は書き出した時刻の文字（YYYY-MM-DD HH:MM）。"""
    out = [EXPORT_BEGIN, '<section id="%s" class="reader-qa">' % EXPORT_ID, "  <h2>%s</h2>" % EXPORT_TITLE,
           '  <p class="note">資料を配信したページで読み手が質問し、資料を配信したマシンのエージェント（Claude Code・Codex）が答えた記録'
           "（%d 件。%s に書き出し）。</p>" % (len(items), stamp)]
    exported = {str(i.get("id")) for i in items}
    for item in thread_order(items):
        qid = str(item.get("id"))
        meta = []
        parent = item.get("reply_to")
        if parent:
            label = html.escape(str(parent).upper())
            meta.append(('<a href="#%s-%s">%s</a>' % (EXPORT_ID, html.escape(str(parent), quote=True), label)
                         if parent in exported else label) + " の回答への質問")
        sec = item.get("section") or {}
        if sec.get("id"):
            title = html.escape(sec.get("title") or sec["id"])
            meta.append("読んでいた節: " + ('<a href="#%s">%s</a>' % (html.escape(sec["id"], quote=True), title)
                                       if sec["id"] in ids else title))
        quote = re.sub(r"\s+", " ", item.get("quote") or "").strip()
        if quote:
            meta.append("選んだ部分: 「%s」" % html.escape(quote if len(quote) <= 120 else quote[:120] + "…"))
        if short_time(item.get("finished_at")):
            meta.append(short_time(item.get("finished_at")) + " に回答")
        doc = item.get("doc") or {}
        out += ['  <article id="%s-%s">' % (EXPORT_ID, html.escape(qid, quote=True)),
                "    <h3>%s. %s</h3>" % (html.escape(qid.upper()), html.escape(item.get("question") or "").replace("\n", "<br>"))]
        if meta:
            out.append('    <p class="note">%s</p>' % " · ".join(meta))
        out.append('    <div class="scroll">%s</div>' % sanitize_fragment(item.get("answer_html"), ids, qid + "-"))
        if doc.get("reverted") or doc.get("conflict"):
            out.append('    <p class="aside">この回答のときに直そうとした本文は、%s反映していない。</p>'
                       % ("検査を通らなかったため" if doc.get("reverted") else "別の所で資料が更新されていたため"))
        out.append("  </article>")
    out += ["</section>", EXPORT_END]
    return "\n".join(out)


def export_doc(text, items, stamp):
    """資料の HTML に、質問と回答の節を書き出す（前に書き出した節は置き換える）。目次に節へのリンクが無ければ足す。"""
    rest = EXPORT_RE.sub("", text)
    section = export_section(items, set(ID_RE.findall(rest)), stamp)
    if EXPORT_RE.search(text):
        text = EXPORT_RE.sub(lambda _: section, text, count=1)
    else:
        ends = list(MAIN_END_RE.finditer(text)) or list(BODY_END_RE.finditer(text))
        i = ends[-1].start() if ends else len(text)
        text = text[:i] + section + "\n" + text[i:]
    if 'href="#%s"' % EXPORT_ID not in text:
        navs = list(NAV_RE.finditer(text))
        nav = next((n for n in navs if re.search(r"\bclass\s*=\s*[\"'][^\"']*\btoc\b", n.group(1))), navs[0] if navs else None)
        if nav:
            i = nav.end() - len(re.search(r"</nav\s*>$", nav.group(0), re.I).group(0))
            text = text[:i] + '<a href="#%s">読者からの質問</a>' % EXPORT_ID + text[i:]
    return text


def export_material(m, items):
    """回答した質問を資料に書き出す。前の版を qa/history/ に残す。戻り値は質問欄に返す結果。"""
    answered = [i for i in items if i.get("status") == "answered" and i.get("kind") != "build"]
    if not answered:
        return 400, {"error": "書き出す回答が無い"}
    with open(m.path, "rb") as f:
        original = f.read()
    current = original.decode("utf-8", "replace")
    sections = [{"id": EXPORT_ID, "title": EXPORT_TITLE}]
    # 前に書き出した時刻のまま作り直して同じなら、書き出し済み（時刻だけ変えて資料を書き換えない）
    old = EXPORT_RE.search(current)
    stamp = old and STAMP_RE.search(old.group(0))
    if stamp and export_doc(current, answered, stamp.group(1)) == current:
        return 200, {"updated": False, "count": len(answered), "rev": bytes_rev(original), "sections": sections}
    text = export_doc(current, answered, short_time(now()))
    os.makedirs(m.history_dir, exist_ok=True)
    with open(os.path.join(m.history_dir, "%s.%s.html" % (m.stem, bytes_rev(original))), "wb") as f:
        f.write(original)
    tmp = "%s.%s.tmp" % (m.path, uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, m.path)
    return 200, {"updated": True, "count": len(answered), "rev": file_rev(m.path), "sections": sections}


def host_name(header):
    """Host ヘッダからポートを除いた名前を返す（IPv6 の角括弧も外す）。"""
    host = (header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@dataclass
class Config:
    root: str
    port: int = 0
    bind: str = "127.0.0.1"
    repo: str = None             # 説明したリポジトリ。検査の --repo と、新しいセッションの作業ディレクトリに使う
    claude: list = None          # claude を起動するコマンド（引数の配列）
    codex: list = None           # codex を起動するコマンド（引数の配列）
    agent: str = "auto"          # 資料を作ったセッションが分からないときに答えるエージェント（auto は使える方、両方なら claude）
    token: str = None
    doc_name: str = "explanation.html"
    prompt: str = DEFAULT_PROMPT
    check_cmd: str = None        # None は visual-explain の検査、"" は検査しない。{file} を作業用コピーのパスに置き換える
    browser_check: bool = True
    answer_timeout: int = 900
    model: str = None
    claude_home: str = None      # 会話の記録を探す場所（既定は CLAUDE_CONFIG_DIR か ~/.claude）
    codex_home: str = None       # Codex の会話の記録を探す場所（既定は CODEX_HOME か ~/.codex）
    qa: bool = True
    allow_host: list = field(default_factory=list)  # IP アドレス・localhost・このマシンの名前・*.ts.net 以外に許すホスト名
    local_only: bool = False     # 回答で Web と MCP を使わず、資料とリポジトリだけで答える
    private: bool = False        # 資料のページも含め、合言葉の Cookie が無い要求には何も返さない（受け箱の配信サーバー）
    presence: bool = True        # 受け箱と質問の資料をつないだままにする。False なら /__qa/events は 204（ブラウザはつなぎ直さない）
    workers: int = 1             # 同時に扱う質問の数（別々の資料だけ。同じ資料は 1 件ずつ）
    prefs: str = None            # 見る人の好み（質問欄の幅）を置くファイル。None なら覚えない（ブラウザだけが覚える）

    def __post_init__(self):
        self.root = os.path.abspath(self.root)
        self.repo = os.path.abspath(self.repo) if self.repo else None
        self.workdir = self.repo or git_toplevel(self.root) or self.root
        self.claude = list(self.claude or ["claude"])
        self.token = self.token or secrets.token_urlsafe(18)
        self.claude_home = self.claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        self.codex = list(self.codex or ["codex"])
        self.codex_home = self.codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        names = {"localhost", socket.gethostname(), socket.getfqdn(), self.bind.strip("[]")}
        names |= {n.split(".")[0] for n in list(names)} | set(self.allow_host or [])
        self.hosts = {n.lower().rstrip(".") for n in names if n}

    def host_allowed(self, header):
        # DNS リバインディング対策。攻撃者のドメイン名で来た要求を断る。IP アドレスで来た要求は直接の接続なので通す
        name = host_name(header).rstrip(".")
        if not name or name in self.hosts or name.endswith(".ts.net"):
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return False


class Material:
    """配信フォルダの中の 1 つの資料と、その質問・回答の置き場所。"""

    def __init__(self, cfg, rel):
        self.setup(os.path.join(cfg.root, *rel.split("/")), rel)

    @classmethod
    def at(cls, path):
        """配信していない資料（serve.py export）。"""
        m = cls.__new__(cls)
        m.setup(os.path.abspath(path), os.path.basename(path))
        return m

    def setup(self, path, rel):
        self.rel = rel
        self.path = path
        self.dir = os.path.dirname(self.path)
        stem, ext = os.path.splitext(os.path.basename(self.path))
        self.stem = stem
        self.draft = os.path.join(self.dir, stem + DRAFT_SUFFIX + ext)
        self.qa_dir = os.path.join(self.dir, QA_DIR)
        self.state_path = os.path.join(self.qa_dir, "qa.json")
        self.session_path = os.path.join(self.qa_dir, "session.json")
        self.origin_path = os.path.join(self.qa_dir, "origin.json")
        self.ask_path = os.path.join(self.qa_dir, "ask.json")  # AskUserQuestion の質問（ask_hook.py が登録したもの）
        self.answers_dir = os.path.join(self.qa_dir, "answers")
        self.history_dir = os.path.join(self.qa_dir, "history")
        self.images_dir = os.path.join(self.dir, "images")

    def answer_path(self, qid):
        return os.path.join(self.answers_dir, qid + ".html")


PDF_TIMEOUT = 120
PDF_LOCK = threading.Semaphore(2)  # 同時に動かす Chrome の数
_chrome = None


def find_chrome():
    """PDF を作る Chrome。探し方は検査（check-explanation.py）と同じ。見つかったものだけ覚える（後から入れてもよい）。"""
    global _chrome
    if _chrome is None:
        spec = importlib.util.spec_from_file_location("check_explanation", DEFAULT_CHECK)
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        found = check.find_chrome(None)
        if found:
            _chrome = (found, check.headless_flag(found))
    return _chrome


def render_pdf(path):
    """資料を印刷の CSS（A4 縦）で PDF にする。質問欄を差し込む前の、資料のファイルそのものを使う。
    Chrome が無ければ None。作れなければ RuntimeError。"""
    chrome = find_chrome()
    if not chrome:
        return None
    exe, flags = chrome
    with PDF_LOCK:
        work = tempfile.mkdtemp(prefix="ve-pdf-")
        try:
            out = os.path.join(work, "doc.pdf")
            try:
                r = subprocess.run([exe] + flags + [
                    "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                    "--user-data-dir=" + os.path.join(work, "profile"), "--no-pdf-header-footer",
                    "--virtual-time-budget=3000", "--print-to-pdf=" + out, pathlib.Path(os.path.abspath(path)).as_uri()],
                    capture_output=True, timeout=PDF_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as e:
                raise RuntimeError("Chrome を動かせなかった: %s" % e)
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                raise RuntimeError("PDF ができなかった: %s" % r.stderr.decode("utf-8", "replace").strip()[-300:])
            with open(out, "rb") as f:
                return f.read()
        finally:
            shutil.rmtree(work, ignore_errors=True)


def resolve_doc(cfg, rel):
    """クライアントから受けた資料のパスを確かめ、配信フォルダの中の資料なら Material を返す。"""
    if not isinstance(rel, str):
        return None
    rel = rel.strip().lstrip("/")
    parts = rel.split("/")
    if not rel or "\\" in rel or any(p in ("", ".", "..") for p in parts) or parts[-1] != cfg.doc_name:
        return None
    m = Material(cfg, rel)
    if not inside(m.path, cfg.root) or not os.path.isfile(m.path):
        return None
    return m


def find_docs(cfg):
    out = []
    for dirpath, dirnames, filenames in os.walk(cfg.root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() != QA_DIR and not d.startswith("."))
        if cfg.doc_name in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, cfg.doc_name), cfg.root).replace(os.sep, "/"))
    return out


def record_origin(material_dir, session_id, cwd, agent="claude", config_dir=None):
    path = os.path.join(material_dir, QA_DIR, "origin.json")
    write_json(path, {"session_id": session_id, "cwd": os.path.abspath(cwd), "agent": agent, "config_dir": config_dir,
                      "recorded_at": now()})
    return path


# ---- AskUserQuestion を資料のページで答える

def ask_home():
    """設定と常駐の配信サーバーの記録を置くフォルダ。エージェントやアカウントに依らず 1 つにし、どのセッションも
    同じ配信サーバーと受け箱を使う。VISUAL_EXPLAIN_HOME（テストでは VISUAL_EXPLAIN_ASK_HOME も）で差し替える。"""
    for name in ("VISUAL_EXPLAIN_HOME", "VISUAL_EXPLAIN_ASK_HOME"):
        if os.environ.get(name):
            return os.environ[name]
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "visual-explain")


def panel_width(value):
    """覚えてよい質問欄の幅なら整数で返す。"""
    if isinstance(value, int) and not isinstance(value, bool) and PANEL_WIDTH[0] <= value <= PANEL_WIDTH[1]:
        return value
    return None


def read_viewer_prefs(path):
    """見る人の好み（質問欄の幅）。ブラウザの保存は配信元（ポート・http/https・ホスト名）ごとに別々なので、配信している
    マシンに置き、どの配信サーバーの資料も同じ幅で開く。置き場所が無い・読めないときは空。"""
    raw = read_json(path) if path else None
    width = panel_width(raw.get("width")) if isinstance(raw, dict) else None
    return {"width": width} if width else {}


def git_branch(cwd):
    for argv in (["git", "-C", cwd, "symbolic-ref", "--short", "-q", "HEAD"], ["git", "-C", cwd, "rev-parse", "--short", "HEAD"]):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()[:100]
    return None


def tailscale_cli():
    for exe in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        path = shutil.which(exe) or (exe if os.path.isfile(exe) else None)
        if path:
            return path
    return None


def tailscale_https_name():
    """Tailscale の HTTPS の証明書を使える、このマシンの名前（dev.xxx.ts.net）。使えなければ None。"""
    cli = tailscale_cli()
    if not cli:
        return None
    try:
        status = json.loads(subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=5).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    names = status.get("CertDomains") if isinstance(status, dict) else None
    return names[0] if isinstance(names, list) and names and isinstance(names[0], str) else None


def tailscale_ip():
    """このマシンの Tailscale の IPv4 アドレス。Tailscale が無い・止まっていれば None。"""
    for path in filter(None, [tailscale_cli()]):
        try:
            out = subprocess.run([path, "ip", "-4"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for word in out.split():
            try:
                ip = ipaddress.ip_address(word)
            except ValueError:
                continue
            if ip in TAILSCALE_NET:
                return str(ip)
    return None


def read_ask_settings(home):
    """設定ファイルに書いてある値だけ（既定を補わない）。ask-config が書き換えるときに使う。"""
    raw = read_json(os.path.join(home, ASK_CONFIG))
    return raw if isinstance(raw, dict) else {}


def ask_mode(raw):
    if raw.get("mode") in ASK_MODES:
        return raw["mode"]
    if "enabled" in raw:  # 0.16.0 の設定（有効・無効だけ）
        return "always" if raw["enabled"] else "off"
    return "auto"


def ask_effective_mode(conf):
    """auto を、受け箱を開いたことがあるかで always か open にする。"""
    if conf["mode"] != "auto":
        return conf["mode"]
    return "always" if os.path.exists(os.path.join(conf["root"], INBOX_USED)) else "open"


def load_ask_config(home, create=False):
    """設定を読み、既定を補う。設定ファイルが無くても、受け箱を開いているときだけページに出す（auto）。
    待ち受けるアドレスの既定は、Tailscale が使えればそのアドレス、使えなければ 127.0.0.1。
    create なら、合言葉が無いときに作って保存する（受け箱の URL を変えないよう、一度作ったら使い続ける）。"""
    raw = read_ask_settings(home)
    if create and not raw.get("token"):
        # 初めて同時に始めた 2 つのセッションが、別々の合言葉を書かないようにする
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "ask.lock"), "a") as lock:
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            raw = read_ask_settings(home)
            if not raw.get("token"):
                raw["token"] = secrets.token_urlsafe(18)
                save_ask_config(home, raw)
    conf = dict(raw)
    conf["mode"] = ask_mode(raw)
    conf.setdefault("port", 8770)
    conf.setdefault("root", os.path.join(home, "asks"))
    conf.setdefault("wait", ASK_WAIT_DEFAULT)
    conf.setdefault("build", "always")
    conf.setdefault("allow_host", [])
    conf["bind_setting"] = raw.get("bind") or "auto"
    https = raw.get("https") if isinstance(raw.get("https"), dict) else None
    if https:
        # HTTPS（Tailscale serve）を通すときは、配信サーバーはこのマシンの中だけで待ち受ける
        conf["bind"] = "127.0.0.1"
        port = int(https.get("port") or 443)
        conf["public_url"] = "https://%s%s" % (https.get("name"), "" if port == 443 else ":%d" % port)
    else:
        if conf["bind_setting"] == "auto":
            conf["bind"] = tailscale_ip() or "127.0.0.1"
        conf["public_url"] = ask_base_url(conf)
    return conf


def save_ask_config(home, conf):
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, ASK_CONFIG)
    # 合言葉が入るので、本人だけが読めるようにする
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


def ask_host(conf):
    bind = str(conf.get("bind") or "127.0.0.1").strip("[]")
    return "[%s]" % bind if ":" in bind else bind


def ask_base_url(conf):
    host = ask_host(conf)
    return "http://%s:%d" % ("localhost" if host in ("127.0.0.1", "[::1]") else host, int(conf["port"]))


def ask_request(conf, method, path, body=None, timeout=10):
    """常駐の配信サーバーに要求を送る（合言葉の Cookie を付ける）。戻り値は (状態, JSON)。"""
    import http.client
    conn = http.client.HTTPConnection(ask_host(conf).strip("[]"), int(conf["port"]), timeout=timeout)
    headers = {"Cookie": "ve_qa_%d=%s" % (int(conf["port"]), conf["token"]), "Host": "%s:%d" % (ask_host(conf), int(conf["port"]))}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        res = conn.getresponse()
        raw = res.read()
        try:
            return res.status, json.loads(raw or b"{}")
        except ValueError:
            return res.status, {}
    finally:
        conn.close()


def ask_probe(conf):
    """待ち受け先にいるものを確かめる。自分の配信サーバー（合言葉が通り、置き場所が同じ）なら ping の内容、
    別の配信サーバー（別の利用者・別の設定フォルダなど）なら "foreign"、何もいなければ None。"""
    try:
        status, data = ask_request(conf, "GET", "/__qa/ping", timeout=3)
    except OSError:
        return None
    if status == 200 and data.get("root") == os.path.abspath(conf["root"]):
        return data
    return "foreign"


def ask_ping(conf):
    probe = ask_probe(conf)
    return probe if isinstance(probe, dict) else None


def ask_server_alive(conf):
    return ask_ping(conf) is not None


def version_key(version):
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return ()


def recorded_ask_server(home, conf):
    """前に起動した配信サーバーの待ち受け先（Tailscale の起動・停止で、今の既定と違うことがある）。"""
    rec = read_json(os.path.join(home, "ask-server.json")) or {}
    if not rec.get("bind") or not rec.get("port"):
        return None
    return dict(conf, bind=rec["bind"], port=rec["port"])


def ask_log(home, msg):
    try:
        with open(os.path.join(home, "ask-server.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def ensure_ask_server(home, conf, wait=15, blocking=True):
    """常駐の配信サーバーが動いていなければ起動する。
    - 同時に起動しないよう、ロックを取ってから確かめ、起動した配信サーバーが待ち受けを始める（か終わる）までロックを持つ。
      blocking が False なら、ロックを取れないとき（別のセッションが起動している）は待たずに戻る。
    - 動いている配信サーバーの版がこれより古ければ止めて起動し直す。新しいか同じならそのまま使う（版の違うセッションが
      混ざっても、起動し直し合わない）。
    - 別の待ち受け先で動いている前の配信サーバーは止める（同じ置き場所を 2 つの配信サーバーが扱わないように）。
    - 待ち受け先を別の配信サーバーが使っていれば起動しない（何度も起動して失敗しないように）。"""
    probe = ask_probe(conf)
    if isinstance(probe, dict) and version_key(probe.get("version")) >= version_key(VERSION):
        return True
    if probe == "foreign":
        ask_log(home, "%s は別の配信サーバーが使っている。ask-config --port で別のポートにする" % ask_base_url(conf))
        return False
    os.makedirs(home, exist_ok=True)
    os.makedirs(conf["root"], exist_ok=True)
    with open(os.path.join(home, "ask.lock"), "a") as lock:
        if fcntl:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                return True
        probe = ask_probe(conf)
        if isinstance(probe, dict):
            if version_key(probe.get("version")) >= version_key(VERSION) or not stop_ask_server(home, conf):
                return True
        elif probe == "foreign":
            return False
        old = recorded_ask_server(home, conf)
        if old and (old["bind"], old["port"]) != (conf["bind"], conf["port"]):
            stop_ask_server(home, old)
        # 待ち受けを始めた配信サーバー自身が ask-server.json を書く（待ち受けに失敗したものは書かない）
        argv = [sys.executable, "-I", SERVE_PY, "serve", "--dir", conf["root"], "--port", str(conf["port"]),
                "--bind", str(conf.get("bind") or "127.0.0.1"), "--token", conf["token"], "--max-time", "0",
                "--private", "--workers", str(ASK_WORKERS), "--record", os.path.join(home, "ask-server.json")]
        for name in conf.get("allow_host") or []:
            argv += ["--allow-host", name]
        if conf.get("model"):
            argv += ["--model", conf["model"]]
        env = {k: v for k, v in os.environ.items() if k not in PARENT_ENV}
        with open(os.path.join(home, "ask-server.log"), "a") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                    cwd=conf["root"], start_new_session=True)
        end = time.time() + wait
        while time.time() < end:
            if proc.poll() is not None:
                return False
            if ask_server_alive(conf):
                return True
            time.sleep(0.1)
    return False


def clean_ask_questions(questions):
    """AskUserQuestion の questions を確かめ、ページに出す形にする。形が違えば None。"""
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        return None
    out = []
    for q in questions:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"].strip():
            return None
        opts = q.get("options")
        if not isinstance(opts, list) or not 1 <= len(opts) <= 10:
            return None
        clean = []
        for o in opts:
            if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"].strip():
                return None
            clean.append({"label": o["label"][:300], "description": str(o.get("description") or "")[:4000],
                          "preview": str(o.get("preview") or "")[:20000]})
        out.append({"question": q["question"][:4000], "header": str(q.get("header") or "")[:100],
                    "multiSelect": q.get("multiSelect") is True, "options": clean})
    if len({q["question"] for q in out}) != len(out):
        return None  # 答えは質問の文で引くので、同じ文の質問は扱えない
    return out


def ask_prompt_text(questions):
    """資料を作る claude に渡す、質問と選択肢の文。"""
    lines = []
    for i, q in enumerate(questions, 1):
        lines.append("質問%d%s: %s%s" % (i, "（%s）" % q["header"] if q["header"] else "", q["question"],
                                       "（複数選べる）" if q["multiSelect"] else ""))
        for j, o in enumerate(q["options"], 1):
            lines.append("  選択肢%d-%d: %s" % (i, j, o["label"]))
            if o["description"]:
                lines.append("    説明: %s" % o["description"])
            if o["preview"]:
                lines.append("    プレビュー:\n" + "\n".join("      " + l for l in o["preview"].splitlines()))
    return "\n".join(lines)


def ask_headline(question):
    """選択肢だけのページの見出し。質問の最初の文（短ければ次の文まで）で、表示は CSS で2行までに切る。
    質問の全文は本文の節と右の欄に出るので、見出しには写さない。"""
    line = next((l.strip() for l in question.splitlines() if l.strip()), question.strip())
    headline = ""
    for part in re.split(r"(?<=[。？！?!])", line):
        headline += part
        if len(headline.strip()) >= 20:
            break
    return headline.strip()


def choice_page(questions, cwd, building, agent_name="Claude Code"):
    """質問を受けてすぐ出す、選択肢だけのページ。資料ができたら差し替わる。"""
    esc = html.escape
    with open(TEMPLATE, encoding="utf-8") as f:
        style = re.search(r"<style>.*?</style>", f.read(), re.S).group(0)
    style += ("\n<style>header h1 { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;"
              " line-clamp: 2; overflow: hidden; }</style>")
    first = questions[0]
    title = first["header"] or first["question"][:40]
    parts = []
    for i, q in enumerate(questions, 1):
        cards = []
        for j, o in enumerate(q["options"], 1):
            body = '<h3>%s</h3>' % esc(o["label"])
            if o["description"]:
                body += '<p>%s</p>' % esc(o["description"])
            if o["preview"]:
                body += '<pre>%s</pre>' % esc(o["preview"])
            cards.append('<article class="card" id="option-%d-%d">%s</article>' % (i, j, body))
        # ラベル（チケット番号など）が質問の文に含まれるときは、見出しに重ねて書かない
        tag = q["header"] if q["header"] and q["header"] not in q["question"] else ""
        parts.append('<section id="question-%d"><h2>%s%s</h2>%s<div class="cards">%s</div></section>' % (
            i, "%s · " % esc(tag) if tag else "", esc(q["question"]),
            '<p class="note">複数選べます。</p>' if q["multiSelect"] else "", "".join(cards)))
    lead = ("右の欄（質問と回答）で選んで送ると、答えが %s の作業に戻ります。" % agent_name
            + ("選ぶための図入りの資料を作っています。できたら、このページが差し替わります。" if building else ""))
    return ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>%s</title>\n%s\n</head>\n'
            '<body>\n<div class="page">\n<main>\n<header>\n<span class="badge">%s からの質問</span>\n'
            '<h1>%s</h1>\n<p class="lead">%s</p>\n<p class="note">作業ディレクトリ: <code>%s</code> · %s</p>\n</header>\n'
            '%s\n</main>\n</div>\n</body>\n</html>\n' % (
                esc(title), style, esc(agent_name), esc(ask_headline(first["question"])), esc(lead), esc(cwd),
                esc(time.strftime("%Y-%m-%d %H:%M")),
                "\n".join(parts)))


class Plan:
    def __init__(self, mode, session_id, cwd, forked_from=None):
        self.mode, self.session_id, self.cwd, self.forked_from = mode, session_id, cwd, forked_from


class Result:
    def __init__(self, ok, text="", error="", session_id=None, timed_out=False, canceled=False):
        self.ok, self.text, self.error, self.session_id = ok, text, error, session_id
        self.timed_out, self.canceled = timed_out, canceled


class Job:
    """作成中の 1 件。取り消し・停止のときに、その claude と検査を止めるために置く。"""

    def __init__(self, building=False):
        self.proc = None
        self.finishing = False   # claude が終わり、回答と本文をまとめている（もう取り消せない）
        self.building = building  # AskUserQuestion の質問のための資料作り


class App:
    """質問の列と、claude -p を動かす作業スレッド。作業スレッドが複数なら別々の資料を同時に扱い、
    同じ資料の質問は資料ごとのロックで 1 件ずつにする（作業用コピーを取り合わない）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.stopping = threading.Event()
        self.workers = []
        self.jobs = {}           # 作成中の (資料, 質問の id) → Job
        self.local = threading.local()  # 作業スレッドごとの、作成中の Job
        self.material_locks = {}  # 資料フォルダ → その資料の質問を 1 件ずつにするロック
        self.cancel_ids = set()  # 取り消しを頼まれた (資料, 質問の id)
        self.owned = {}  # 資料フォルダ → 排他のために開いたロックファイル
        self.mcp = {}    # 作業ディレクトリ → {"done": 調べ終えたか, "names": MCP サーバーの名前}
        self.waiters = {}  # AskUserQuestion の質問の資料フォルダ → フックが最後に答えを問い合わせた時刻
        self.viewers = 0   # 受け箱か質問の資料をブラウザで開いている数（/__qa/events につないだままの接続）
        self.changes = 0   # AskUserQuestion の質問が増えた・締め切られた回数。受け箱に知らせる
        self.changed = threading.Condition()
        self.agents = {"claude": agents_mod.ClaudeAgent(cfg.claude), "codex": agents_mod.CodexAgent(cfg.codex)}
        self.homes = {"claude": cfg.claude_home, "codex": cfg.codex_home}  # 会話の記録を探す既定の場所
        self.sandbox = None  # Codex のサンドボックスが動くか（配信の間、最初に答えるときに 1 回だけ確かめる）
        self.sandbox_lock = threading.Lock()
        self.qa_enabled, self.qa_reason = self.detect_agents()
        for rel in find_docs(cfg):
            self.owns(Material(cfg, rel))

    # ---- 起動と停止

    def detect_agents(self):
        if not self.cfg.qa:
            return False, "質問の受付を無効にして配信している（--no-qa）"
        if not any(a.available() for a in self.agents.values()):
            return False, "資料を配信しているマシンに claude も codex も見つからない（%s、%s）" % (
                self.cfg.claude[0], self.cfg.codex[0])
        return True, None

    def owns(self, m):
        """この配信が資料の質問を受け付けるかを返す。同じ資料を別の serve.py が受け付けていれば受け付けない。
        初めて受け付けるときに、前の配信で処理の途中だった質問を片付ける。"""
        with self.lock:
            if m.dir in self.owned:
                return True
            if not self.qa_enabled:
                return False
            lock = None
            # AskUserQuestion の質問の資料は常駐の配信サーバーだけが配信するので、ロックを取らない
            # （質問ごとにファイルを開いたままにすると、溜まって開けるファイルの数の上限に届く）
            if fcntl and not os.path.isfile(m.ask_path):
                os.makedirs(m.qa_dir, exist_ok=True)
                lock = open(os.path.join(m.qa_dir, ".serve.lock"), "a")
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    lock.close()
                    return False
            self.owned[m.dir] = lock
            self.recover(m)
            return True

    def qa_status(self, m):
        if not self.qa_enabled:
            return False, self.qa_reason
        if not self.owns(m):
            return False, "この資料は別の配信（serve.py）が質問を受け付けている。そちらの配信の URL で開いてください。"
        return True, None

    def recover(self, m):
        """前回の配信で処理の途中だった質問を「中断」にし、残った作業用コピーを消す。勝手に再実行はしない。"""
        remove(m.draft)
        data = read_json(m.state_path)
        if not data or not isinstance(data.get("items"), list):
            return
        changed = False
        for item in data["items"]:
            if item.get("status") in ("queued", "answering"):
                item.update(status="failed", finished_at=now(), error="配信の再起動で中断した。もう一度送ってください。")
                changed = True
        if changed:
            write_json(m.state_path, data)

    def start(self):
        for i in range(max(1, self.cfg.workers)):
            worker = threading.Thread(target=self.work, name="qa-worker-%d" % i, daemon=True)
            worker.start()
            self.workers.append(worker)
        if self.qa_enabled and not self.cfg.local_only:
            # claude mcp list は接続を確かめるので数秒かかる。最初の質問を待たせないよう先に調べておく
            threading.Thread(target=self.mcp_servers, args=(self.cfg.workdir,), name="qa-mcp", daemon=True).start()

    def stop(self):
        self.stopping.set()
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            self.kill(job)
        for _ in self.workers or [None]:
            self.queue.put(None)
        self.notify_changes()
        with self.lock:
            for lock in self.owned.values():
                if lock:
                    lock.close()
            self.owned.clear()

    def job(self):
        """この作業スレッドが作成中の Job（作業スレッドの外から呼ばれたときは、どこにも登録しない Job）。"""
        return getattr(self.local, "job", None) or Job()

    def kill(self, job, sig=signal.SIGTERM):
        proc = job.proc if job else None
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, sig)
            except (OSError, AttributeError):
                proc.kill() if sig != signal.SIGTERM else proc.terminate()

    def wait_idle(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self.queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False

    # ---- 質問と回答の保存

    def load(self, m):
        data = read_json(m.state_path)
        return data if data and isinstance(data.get("items"), list) else {"items": []}

    def items(self, m):
        with self.lock:
            return self.load(m)["items"]

    def reply_target(self, m, qid):
        """続きの質問を受けられる項目（回答した質問）。無ければ None。"""
        item = next((i for i in self.items(m) if i.get("id") == qid), None)
        return item if item and item.get("status") == "answered" and item.get("kind") != "build" else None

    def enqueue(self, m, question, section=None, quote=None, kind=None, reply_to=None, quote_from=None):
        with self.lock:
            data = self.load(m)
            numbers = [int(i["id"][1:]) for i in data["items"] if re.fullmatch(r"q\d+", str(i.get("id", "")))]
            item = {"id": "q%d" % (max(numbers, default=0) + 1), "question": question, "section": section,
                    "quote": quote, "status": "queued", "asked_at": now(), "started_at": None, "finished_at": None,
                    "answer_html": None, "doc": None, "error": None, "session": None}
            if kind:
                item["kind"] = kind  # "build": AskUserQuestion の質問のために資料を作る
            if reply_to:
                item["reply_to"] = reply_to  # 続きの質問: 元の質問の id
            if quote_from:
                item["quote_from"] = quote_from  # "answer": 選んだ部分は、元の回答の中の文字
            data["items"].append(item)
            write_json(m.state_path, data)
        self.queue.put((m.rel, item["id"]))
        return item

    def update(self, m, qid, only=None, **fields):
        """質問の項目を書き換える。only を渡すと、その状態のときだけ書き換える（取り消した項目を上書きしない）。"""
        with self.lock:
            data = self.load(m)
            for item in data["items"]:
                if item.get("id") == qid:
                    if only and item.get("status") not in only:
                        return None
                    item.update(fields)
                    write_json(m.state_path, data)
                    if fields.get("status") in ("answering", "answered", "failed", "canceled"):
                        self.log(m, item)
                    return item
        return None

    def add_step(self, m, qid, text):
        """回答を作成中の質問に、途中経過を 1 つ足す（質問欄に出す）。"""
        with self.lock:
            data = self.load(m)
            item = next((i for i in data["items"] if i.get("id") == qid), None)
            if not item or item.get("status") != "answering":
                return
            steps = item.get("progress") if isinstance(item.get("progress"), list) else []
            if steps and steps[-1].get("text") == text:
                return
            steps.append({"at": now(), "text": text})
            item["progress"] = steps[-MAX_STEPS:]
            write_json(m.state_path, data)

    def cancel(self, m, qid):
        """質問を取り消す。待機中ならその場で、作成中なら claude を止めて取り消す。戻り値は (HTTP の状態, 項目)。"""
        with self.lock:
            data = self.load(m)
            item = next((i for i in data["items"] if i.get("id") == qid), None)
            if not item:
                return 404, {"error": "質問が見つからない"}
            if item.get("status") == "queued":
                item.update(status="canceled", finished_at=now())
                write_json(m.state_path, data)
                self.log(m, item)
                return 200, item
            if item.get("status") == "answering":
                job = self.jobs.get((m.rel, qid))
                if job and job.finishing:
                    return 409, {"error": "回答をまとめているため、もう取り消せない"}
                self.cancel_ids.add((m.rel, qid))
                item["cancel_requested"] = True
                write_json(m.state_path, data)
                if job:
                    self.kill(job)
                return 202, item
            return 409, {"error": "回答が終わった質問は取り消せない"}

    def canceled(self, m, qid):
        return (m.rel, qid) in self.cancel_ids

    # ---- AskUserQuestion の質問（ask_hook.py が登録し、ページで選ばれた答えを問い合わせる）

    def register_ask(self, body):
        """質問ごとに資料フォルダを作り、選択肢だけのページを置く。資料を作るなら、その依頼を列に積む。"""
        questions = clean_ask_questions(body.get("questions"))
        sid, cwd = str(body.get("session_id") or ""), str(body.get("cwd") or "")
        if not questions:
            return 400, {"error": "質問の形が違う"}
        if not SESSION_ID_RE.match(sid) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            return 400, {"error": "セッションの ID か作業ディレクトリが無い"}
        try:
            wait = max(10, min(int(body.get("wait") or ASK_WAIT_DEFAULT), ASK_WAIT_MAX))
        except (TypeError, ValueError):
            wait = ASK_WAIT_DEFAULT
        agent = self.agents.get(body.get("agent")) or self.agents["claude"]
        # 資料作りは質問したセッションのエージェントで行う。そのエージェントが無ければ、選択肢だけのページにする
        build = body.get("build") != "never" and self.qa_enabled and agent.available()
        self.prune_asks()
        name = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))
        m = Material(self.cfg, "%s/%s" % (name, self.cfg.doc_name))
        os.makedirs(m.qa_dir, exist_ok=True)
        with open(m.path, "w", encoding="utf-8") as f:
            f.write(choice_page(questions, cwd, build, agent.label))
        config_dir = body.get("config_dir")
        config_dir = config_dir if isinstance(config_dir, str) and os.path.isabs(config_dir) and os.path.isdir(config_dir) else None
        # 質問欄の回答と資料作りは、質問したセッションを分岐して、その作業ディレクトリのリポジトリ・そのアカウントで行う
        write_json(m.origin_path, {"ask": True, "session_id": sid, "cwd": cwd, "repo": git_toplevel(cwd) or cwd,
                                   "agent": agent.key, "config_dir": config_dir, "recorded_at": now()})
        # どのセッションの質問か見分けられるよう、ブランチと、セッションの最初の依頼を添える
        session = {"id": sid[:8], "branch": git_branch(cwd), "prompt": agent.session_info(sid, config_dir or agent.default_home(),
                                                                                     body.get("transcript_path"))["prompt"]}
        write_json(m.ask_path, {"id": name, "status": "waiting", "agent": agent.key, "questions": questions, "created_at": now(),
                                "expires_at": now(wait), "cwd": cwd, "session": session, "answers": None,
                                "annotations": None, "closed_at": None, "build": build})
        with self.lock:
            self.waiters[m.dir] = time.time()
        if self.owns(m) and build:
            self.enqueue(m, BUILD_QUESTION, kind="build")
        self.notify_changes()
        return 201, {"doc": m.rel, "url": "/" + m.rel}

    def notify_changes(self):
        with self.changed:
            self.changes += 1
            self.changed.notify_all()

    def prune_asks(self):
        """古い質問の資料フォルダを消す。常駐の配信サーバーは資料ごとにロックを開いたままにするので、溜め続けない。"""
        limit = time.time() - ASK_KEEP_DAYS * 86400
        with self.lock:
            for name in os.listdir(self.cfg.root):
                if not ASK_NAME_RE.match(name):
                    continue
                m = Material(self.cfg, "%s/%s" % (name, self.cfg.doc_name))
                try:
                    if os.path.getmtime(m.ask_path) > limit:
                        continue
                    ask = read_json(m.ask_path)
                    if isinstance(ask, dict) and ask.get("status") == "waiting" and self.waiter_alive(m):
                        continue
                    if any(i.get("status") in ("queued", "answering") for i in self.load(m)["items"]):
                        continue
                except (OSError, AttributeError, TypeError):
                    continue
                self.owned.pop(m.dir, None)
                self.waiters.pop(m.dir, None)
                shutil.rmtree(os.path.join(self.cfg.root, name), ignore_errors=True)

    def ask_view(self, m):
        ask = read_json(m.ask_path)
        if not isinstance(ask, dict) or not isinstance(ask.get("questions"), list):
            return None
        view = {k: ask.get(k) for k in ("id", "status", "questions", "created_at", "expires_at", "answers",
                                        "annotations", "closed_at", "build", "session")}
        view["where"] = os.path.basename(str(ask.get("cwd") or "").rstrip("/"))
        view["agent"] = ask.get("agent") if ask.get("agent") in self.agents else "claude"
        view["agent_name"] = self.agents[view["agent"]].label
        view["waiting"] = self.waiter_alive(m)
        view["viewers"] = self.viewers
        if view["status"] == "waiting" and passed(ask.get("expires_at")):
            view["status"] = "expired"
        return view

    def waiter_alive(self, m):
        with self.lock:
            return time.time() - self.waiters.get(m.dir, 0) < WAITER_GRACE

    def poll_ask(self, m):
        """フックからの問い合わせ。フックがまだ待っている印を付けて、状態を返す。"""
        with self.lock:
            self.waiters[m.dir] = time.time()
        return self.ask_view(m)

    def answer_ask(self, m, body):
        """ページで選ばれた答えを確かめて残す。フックが次の問い合わせで受け取り、AskUserQuestion の答えにする。"""
        given = body.get("answers")
        if not isinstance(given, dict):
            return 400, {"error": "答えが無い"}
        with self.lock:
            ask = read_json(m.ask_path)
            if not isinstance(ask, dict) or not isinstance(ask.get("questions"), list):
                return 404, {"error": "この資料にはエージェントからの質問が無い"}
            name = self.agents.get(ask.get("agent"), self.agents["claude"]).label
            if ask.get("status") != "waiting":
                return 409, {"error": "この質問はもう締め切った"}
            if passed(ask.get("expires_at")):
                ask.update(status="expired", closed_at=now())
                write_json(m.ask_path, ask)
                return 409, {"error": "待つ時間を過ぎた。ターミナルで答えてください"}
            if not self.waiter_alive(m):
                return 409, {"error": "%s がもう答えを待っていない（ターミナルで中断された可能性がある）。ターミナルで答えてください" % name}
            answers, notes = {}, {}
            for q in ask["questions"]:
                a = given.get(q["question"])
                a = a if isinstance(a, dict) else {}
                labels = [o["label"] for o in q["options"]]
                picked = a.get("selected") if isinstance(a.get("selected"), list) else []
                if any(p not in labels for p in picked) or len(set(picked)) != len(picked):
                    return 400, {"error": "選択肢に無い答えがある"}
                other = a["other"].strip()[:MAX_OTHER] if isinstance(a.get("other"), str) else ""
                count = len(picked) + (1 if other else 0)
                if not count or (not q["multiSelect"] and count != 1):
                    return 400, {"error": "「%s」は%s" % (q["question"][:60], "1つ以上選ぶ" if q["multiSelect"] else "1つだけ選ぶ")}
                # 複数選んだときは、選択肢の順に「, 」でつなぐ（ターミナルで選んだときと同じ形）
                answers[q["question"]] = ", ".join([l for l in labels if l in picked] + ([other] if other else []))
                note = a["notes"].strip()[:MAX_OTHER] if isinstance(a.get("notes"), str) else ""
                if note:
                    notes[q["question"]] = {"notes": note}
            ask.update(status="answered", answers=answers, annotations=notes or None, closed_at=now())
            write_json(m.ask_path, ask)
        self.stop_build(m)
        self.notify_changes()
        return 200, self.ask_view(m)

    def close_ask(self, m, reason):
        """ページで「ターミナルで答える」を選んだ（terminal）か、フックが待つのをやめた（expired）。"""
        if reason not in ("terminal", "expired"):
            return 400, {"error": "締め切る理由が違う"}
        with self.lock:
            ask = read_json(m.ask_path)
            if not isinstance(ask, dict):
                return 404, {"error": "この資料にはエージェントからの質問が無い"}
            if ask.get("status") == "waiting":
                ask.update(status=reason, closed_at=now())
                write_json(m.ask_path, ask)
        self.stop_build(m)
        self.notify_changes()
        return 200, self.ask_view(m)

    def stop_build(self, m):
        """答えが決まったら、作っている途中の資料は要らないので止める。"""
        for item in self.items(m):
            if item.get("kind") == "build" and item.get("status") in ("queued", "answering"):
                self.cancel(m, item["id"])

    def list_asks(self, limit=50):
        out = []
        for rel in sorted(find_docs(self.cfg), reverse=True):  # フォルダ名は日時で始まるので、新しい順
            view = self.ask_view(Material(self.cfg, rel))
            if view:
                out.append({"doc": rel, "status": view["status"], "where": view["where"], "waiting": view["waiting"],
                            "question": view["questions"][0]["question"][:200], "created_at": view["created_at"],
                            "expires_at": view["expires_at"], "session": view.get("session"),
                            "agent": view["agent"], "agent_name": view["agent_name"]})
                if len(out) >= limit:
                    break
        return out

    def export(self, m):
        """回答した質問を資料の末尾に書き出す。回答の作成中は、本文の差し替えとぶつかるので断る。"""
        with self.lock:
            items = self.load(m)["items"]
            if any(i.get("status") in ("queued", "answering") for i in items):
                return 409, {"error": "回答を待っている質問がある。回答が終わってから書き出す"}
            return export_material(m, items)

    @staticmethod
    def log(m, item):
        doc = item.get("doc") or {}
        note = ("、本文を更新" if doc.get("updated") else "、本文の修正を取り消し" if doc.get("reverted")
                else "、本文は別の所で更新済みのため反映せず" if doc.get("conflict") else "")
        detail = {"answering": "回答を作成中", "answered": "回答した%s（%s）" % (note, MODES.get(item.get("session"), "")),
                  "failed": "失敗: %s" % (item.get("error") or "")[:200], "canceled": "取り消した"}[item["status"]]
        print("[%s] %s %s: %s" % (time.strftime("%H:%M:%S"), m.rel, item["id"], detail), file=sys.stderr, flush=True)

    # ---- 作業スレッド

    def work(self):
        while True:
            task = self.queue.get()
            try:
                if task is None:
                    return
                if self.stopping.is_set():
                    self.update(Material(self.cfg, task[0]), task[1], only=("queued",), status="failed",
                                finished_at=now(), error="配信を止めたため中断した。")
                    continue
                self.process(*task)
            except Exception as e:  # 1 件の失敗で作業スレッドを止めない
                self.update(Material(self.cfg, task[0]), task[1], only=("queued", "answering"), status="failed",
                            finished_at=now(), error="配信サーバーの内部エラー: %r" % e)
            finally:
                self.queue.task_done()

    def claim(self, m, qid):
        """待機中の質問を「作成中」にする。取り消された質問は飛ばす（None を返す）。"""
        with self.lock:
            data = self.load(m)
            item = next((i for i in data["items"] if i.get("id") == qid), None)
            if not item or item.get("status") != "queued":
                return None
            item.update(status="answering", started_at=now(), progress=[])
            write_json(m.state_path, data)
            self.log(m, item)
            self.local.job = self.jobs[(m.rel, qid)] = Job(building=item.get("kind") == "build")
            return item

    def material_lock(self, m):
        with self.lock:
            return self.material_locks.setdefault(m.dir, threading.Lock())

    def process(self, rel, qid):
        m = Material(self.cfg, rel)
        with self.material_lock(m):  # 同じ資料の質問は 1 件ずつ（別の作業スレッドが扱っていれば、終わるまで待機中のまま）
            self.process_one(m, rel, qid)

    def process_one(self, m, rel, qid):
        item = self.claim(m, qid)
        if not item:
            return
        try:
            view = (self.ask_view(m) or {}) if item.get("kind") == "build" else None
            if view is not None and (view.get("status") != "waiting" or not view.get("waiting")):
                # 資料を作り始める前に答えが決まった、またはフックがもう待っていない
                self.update(m, qid, status="canceled", finished_at=now())
                return
            os.makedirs(m.answers_dir, exist_ok=True)
            answer = m.answer_path(qid)
            with open(m.path, "rb") as f:
                original = f.read()
            self.reset_files(m, qid, original)
            plan, res = self.ask_agent(m, item, original)
            if res.canceled or self.canceled(m, qid):
                self.update(m, qid, status="canceled", finished_at=now(), session=plan.mode)
                return
            if not res.ok:
                self.update(m, qid, status="failed", finished_at=now(), error=res.error, session=plan.mode)
                return
            answer_html = self.read_answer(answer)
            check = self.check_answer(m, answer) if answer_html else None
            answer_html = answer_html or text_to_html(res.text) or "<p>（回答が空だった）</p>"
            if self.canceled(m, qid):
                self.update(m, qid, status="canceled", finished_at=now(), session=plan.mode)
                return
            doc = self.apply_draft(m, qid, original)
            self.update(m, qid, status="answered", finished_at=now(), answer_html=answer_html, doc=doc,
                        session=plan.mode, answer_check=check)
        finally:
            remove(m.draft)
            with self.lock:
                self.jobs.pop((rel, qid), None)
                self.local.job = None
                self.cancel_ids.discard((rel, qid))

    @staticmethod
    def reset_files(m, qid, original):
        """作業用コピーを本文と同じにし、回答ファイルを消す（やり直しの前にも呼ぶ）。"""
        with open(m.draft, "wb") as f:
            f.write(original)
        remove(m.answer_path(qid))

    def read_answer(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(MAX_ANSWER).strip()
        except OSError:
            return ""

    # ---- エージェント（claude -p / codex exec）

    def default_agent(self):
        """資料を作ったセッションが分からないときに答えるエージェント。--agent のもの（使えなければもう一方）。"""
        order = [self.cfg.agent] if self.cfg.agent in self.agents else []
        order += [k for k in ("claude", "codex") if k not in order]
        return next((self.agents[k] for k in order if self.agents[k].available()), self.agents[order[0]])

    @staticmethod
    def recorded_agent(m):
        """資料に記録したエージェント（質問に答えているセッション、無ければ資料を作ったセッション）。記録が無ければ None。
        エージェントを書いていない記録（Claude Code 専用だった版で作ったもの）は Claude Code。"""
        s = read_json(m.session_path)
        if isinstance(s, dict) and SESSION_ID_RE.match(str(s.get("session_id", ""))):
            return s.get("agent") or "claude"
        o = read_json(m.origin_path)
        return (o.get("agent") or "claude") if isinstance(o, dict) else None

    def agent_for(self, m=None):
        """その資料の質問に答えるエージェント。記録したエージェントが使えなければ、既定のエージェント。"""
        key = self.recorded_agent(m) if m is not None else None
        if key in self.agents and self.agents[key].available():
            return self.agents[key]
        return self.default_agent()

    @staticmethod
    def ask_origin(m):
        """AskUserQuestion の質問の資料なら、その記録（質問したセッション・作業ディレクトリ・アカウント）。"""
        o = read_json(m.origin_path) or {}
        return o if o.get("ask") or o.get("repo") else None

    @staticmethod
    def origin_account(m, agent):
        """資料を作ったセッションのアカウント（設定フォルダ）。そのエージェントの記録でなければ None。"""
        o = (read_json(m.origin_path) or {}) if m else {}
        if not isinstance(o, dict) or (o.get("agent") or "claude") != agent.key:
            return None
        cd = o.get("config_dir")
        return cd if isinstance(cd, str) and os.path.isabs(cd) and os.path.isdir(cd) else None

    def agent_env(self, m, agent):
        """エージェントを動かす環境。資料を作ったセッションのアカウント（設定フォルダ）で動かす（その会話を分岐でき、
        使用量もそのアカウントにかかる）。AskUserQuestion の質問の資料では、配信サーバーを起動したセッションのアカウントは
        引き継がない。"""
        o = self.ask_origin(m) if m else None
        return agent.child_env(os.environ, self.origin_account(m, agent), keep_account=o is None)

    def account_home(self, m, agent):
        if self.ask_origin(m) is not None:
            return self.origin_account(m, agent) or agent.default_home()
        return self.origin_account(m, agent) or self.homes[agent.key]

    def session_plan(self, m, agent):
        s = read_json(m.session_path) or {}
        if SESSION_ID_RE.match(str(s.get("session_id", ""))) and (s.get("agent") or "claude") == agent.key:
            cwd = s.get("cwd") if os.path.isdir(str(s.get("cwd"))) else self.cfg.workdir
            return Plan("resume", s["session_id"], cwd, s.get("forked_from"))
        o = read_json(m.origin_path) or {}
        if (o.get("agent") or "claude") == agent.key and agent.session_exists(str(o.get("session_id", "")),
                                                                              self.account_home(m, agent)):
            cwd = o.get("cwd") if os.path.isdir(str(o.get("cwd"))) else self.cfg.workdir
            return Plan("fork", o["session_id"], cwd, o["session_id"])
        return self.new_plan(m, agent)

    def new_plan(self, m, agent):
        # AskUserQuestion の質問の資料は配信フォルダの外のリポジトリの話なので、質問したセッションの作業ディレクトリで答える
        o = (read_json(m.origin_path) or {}) if m else {}
        cwd = o["cwd"] if o.get("repo") and os.path.isdir(str(o.get("cwd"))) else self.cfg.workdir
        return Plan("new", agent.new_session_id(), cwd)

    def ask_agent(self, m, item, original):
        agent = self.agent_for(m)
        plan = self.session_plan(m, agent)
        res = self.exec_agent(m, item, self.build_prompt(m, item, plan.cwd), plan, agent)
        # 分岐・続きのセッションで失敗した（会話が無い、長すぎる等）ときは、新しいセッションで 1 回だけやり直す。
        # やり直しも失敗したら、次の質問のために元のセッションの記録を残す
        if not res.ok and plan.mode != "new" and not res.timed_out and not res.canceled and not self.stopping.is_set():
            with self.lock:
                self.job().finishing = False
            self.add_step(m, item["id"], "%sを使えなかったため、新しいセッションでやり直しています" % MODES[plan.mode])
            self.reset_files(m, item["id"], original)
            plan = self.new_plan(m, agent)
            res = self.exec_agent(m, item, self.build_prompt(m, item, plan.cwd), plan, agent)
        if res.ok:
            sid = res.session_id or (plan.session_id if plan.mode != "fork" else None)
            if sid:
                prev = read_json(m.session_path) or {}
                write_json(m.session_path, {"session_id": sid, "cwd": plan.cwd, "agent": agent.key,
                                            "forked_from": plan.forked_from or prev.get("forked_from"),
                                            "created_at": prev.get("created_at") or now(), "updated_at": now()})
        return plan, res

    def repo_for(self, m):
        """コード参照を確かめるリポジトリ。AskUserQuestion の質問の資料は、質問したセッションのリポジトリ。"""
        o = read_json(m.origin_path) or {}
        return o["repo"] if o.get("repo") and os.path.isdir(str(o["repo"])) else self.cfg.repo

    def check_argv(self, m, path):
        if self.cfg.check_cmd == "":
            return None
        if self.cfg.check_cmd:
            return [a.replace("{file}", path) for a in shlex.split(self.cfg.check_cmd)]
        argv = ["python3", "-I", DEFAULT_CHECK, path]
        if (read_json(m.origin_path) or {}).get("ask"):
            argv.append("--ask")
        if self.repo_for(m):
            argv += ["--repo", self.repo_for(m)]
        inventory = os.path.join(m.dir, "inventory.txt")
        if os.path.isfile(inventory):
            argv += ["--inventory", inventory]
        if not self.cfg.browser_check:
            argv.append("--no-browser")
        return argv

    def answer_check_argv(self, m, path):
        """回答の断片の検査。検査を差し替えた・無くした配信（--check-cmd）では、回答は検査しない。"""
        if self.cfg.check_cmd is not None:
            return None
        argv = ["python3", "-I", DEFAULT_CHECK, path, "--fragment", "--doc", m.draft]
        if self.repo_for(m):
            argv += ["--repo", self.repo_for(m)]
        if not self.cfg.browser_check:
            argv.append("--no-browser")
        return argv

    def run_check(self, argv, cwd):
        """検査を動かす。配信を止めたときに一緒に止められるよう、作成中の Job に置く。"""
        job = self.job()
        with self.lock:
            if self.stopping.is_set():
                raise subprocess.SubprocessError("配信を止めた")
            proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", start_new_session=True)
            job.proc = proc
        try:
            out, err = proc.communicate(timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.kill(job, signal.SIGKILL)
            proc.communicate()
            raise
        finally:
            with self.lock:
                job.proc = None
        return proc.returncode, out or "", err or ""

    def check_answer(self, m, path):
        """回答の図のはみ出し・節へのリンク・コードの場所を検査し、エラーがあれば質問欄に出す内容を返す。"""
        argv = self.answer_check_argv(m, path)
        if not argv:
            return None
        try:
            _, out, _ = self.run_check(argv + ["--json"], m.dir)
            errors = [e.get("message", "") for e in json.loads(out).get("errors", [])]
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            return None
        return {"errors": errors[:5], "count": len(errors)} if errors else None

    def mcp_servers(self, cwd, m=None, even_local=False):
        """cwd でエージェントが読み込む MCP サーバーの名前。作業ディレクトリ（と、質問したセッションのアカウント）ごとに
        1 回だけ調べ、同時に頼まれたら結果を待つ。調べられなかったときは覚えず、次の質問で調べ直す。
        --local-only では調べない（even_local なら調べる。Codex は名前ごとに無効にするため）。"""
        if self.cfg.local_only and not even_local:
            return []
        agent = self.agent_for(m)
        env = self.agent_env(m, agent)
        key = (agent.key, cwd, env.get(agent.account_env, ""))
        with self.lock:
            entry = self.mcp.get(key)
            first = entry is None
            if first:
                entry = self.mcp[key] = {"done": threading.Event(), "names": []}
        if first:
            names = None
            try:
                names = agent.list_mcp(cwd, env)
            finally:
                entry["names"] = names or []
                if names is None:
                    with self.lock:
                        if self.mcp.get(key) is entry:
                            del self.mcp[key]
                entry["done"].set()
        entry["done"].wait(MCP_LIST_TIMEOUT + 10)
        return list(entry["names"])

    def external_sources(self, cwd, m=None):
        if self.cfg.local_only:
            return "使えない（配信の設定で止めてある）。資料とリポジトリだけで答える"
        return self.agent_for(m).external_label(self.mcp_servers(cwd, m))

    def reply_context(self, m, item):
        """続きの質問のとき、元の質問と回答を指示文に入れる（新しいセッションで答えるときも、何の続きかが分かるように）。"""
        parent = self.reply_target(m, item.get("reply_to")) if item.get("reply_to") else None
        if not parent:
            return "（なし。前の回答への続きの質問ではない）"
        answer = parent.get("answer_html") or ""
        if len(answer) > MAX_REPLY_CONTEXT:
            answer = answer[:MAX_REPLY_CONTEXT] + "\n（長いので、ここまで）"
        return ("質問者は %s の回答を読み、その回答について続けて質問している。質問の中の「これ」「その」などは、下の質問と回答を指す。"
                "下の回答で足りなかった所・分かりにくかった所に答え、同じ説明を繰り返さない。\n\n"
                "<previous_question>\n%s\n</previous_question>\n\n<previous_answer>\n%s\n</previous_answer>"
                % (parent["id"].upper(), (parent.get("question") or "").replace("</previous_question>", "&lt;/previous_question>"),
                   answer.replace("</previous_answer>", "&lt;/previous_answer>")))

    def build_prompt(self, m, item, cwd=None):
        build = item.get("kind") == "build"
        agent = self.agent_for(m)
        with open(BUILD_PROMPT if build else self.cfg.prompt, encoding="utf-8") as f:
            text = f.read()
        section = item.get("section") or {}
        quote = re.sub(r"\s+", " ", item.get("quote") or "").strip()
        argv = self.check_argv(m, m.draft)
        answer_argv = self.answer_check_argv(m, m.answer_path(item["id"]))
        values = {
            "doc_path": m.path, "draft_path": m.draft, "answer_path": m.answer_path(item["id"]), "qa_id": item["id"],
            "section": "%s（#%s）" % (section.get("title") or section["id"], section["id"]) if section.get("id") else "（不明）",
            "quote": "「%s」（%s）" % (quote, "%s の回答から" % item["reply_to"].upper()
                                    if item.get("quote_from") == "answer" and item.get("reply_to") else "資料の本文から")
                     if quote else "（なし）",
            "reply_to": self.reply_context(m, item),
            "check_command": shlex.join(argv) if argv else "（検査は無い。HTML の構造を壊していないか読み直す）",
            "answer_check_command": shlex.join(answer_argv) if answer_argv else "（検査は無い。図の文字が枠に収まっているか読み直す）",
            "skill_path": SKILL_MD,
            "template_path": TEMPLATE,
            "external_sources": self.external_sources(cwd or self.cfg.workdir, m),
            "agent_name": agent.label,
            "write_hint": agent.write_hint,
            # 資料作りでは、質問したセッションの質問のツール
            "ask_tool": self.agents.get((read_json(m.origin_path) or {}).get("agent") or "claude", agent).ask_tool,
        }
        # 資料作りでは AskUserQuestion の質問と選択肢
        question = ask_prompt_text((self.ask_view(m) or {}).get("questions") or []) if build else item["question"]
        values["question"] = question.replace("</question>", "&lt;/question>")
        # 一度に埋める（質問・元の回答の中の {{...}} を置き換えないため）
        return re.sub(r"\{\{(\w+)\}\}", lambda k: values.get(k.group(1), k.group(0)), text)

    def agent_run(self, m, item, plan, agent):
        """エージェントに 1 件の質問を答えさせる（資料を作らせる）ための材料。書いてよいのは作業用コピー・回答・images/、
        動かしてよいのは検査だけ。"""
        checks = [a for a in (self.check_argv(m, m.draft), self.answer_check_argv(m, m.answer_path(item["id"]))) if a]
        # Codex は --local-only でも MCP のサーバーの名前を調べる（名前ごとに無効にするため）
        names = self.mcp_servers(plan.cwd, m, even_local=agent.key == "codex")
        return agents_mod.Run(mode=plan.mode, session_id=plan.session_id, cwd=plan.cwd, material_dir=m.dir,
                              write_files=[m.draft, m.answer_path(item["id"])], write_dirs=[m.images_dir],
                              check_argvs=checks, model=self.cfg.model, local_only=self.cfg.local_only,
                              mcp_names=names)

    def codex_sandbox(self, env):
        """Codex のサンドボックスが動くか。配信の間、最初の 1 回だけ確かめる。"""
        with self.sandbox_lock:
            if self.sandbox is None:
                self.sandbox = self.agents["codex"].sandbox_works(env)
            return self.sandbox

    def exec_agent(self, m, item, prompt, plan, agent):
        env = self.agent_env(m, agent)
        run = self.agent_run(m, item, plan, agent)
        if agent.key == "codex":
            # 子の codex のツール・コマンド・書き込み先をガードの hook で絞る（方針は qa/ に残す）。サンドボックスが
            # 動けば、その上で読み取り専用＋資料フォルダだけ書ける設定にする
            run.sandbox = self.codex_sandbox(env)
            run.guard_policy = os.path.join(m.qa_dir, "guard-%s.json" % item["id"])
            write_json(run.guard_policy, {"write_files": run.write_files, "write_dirs": run.write_dirs,
                                          "commands": run.check_argvs, "mcp": not run.local_only})
        cmd = agent.command(run)
        env[ASK_CHILD_ENV] = "1"
        job = self.job()
        with self.lock:
            # 取り消しと起動が行き違わないよう、取り消しの確認と job.proc の設定を同じロックの中で行う
            if self.canceled(m, item["id"]):
                return Result(False, error=CANCELED, canceled=True)
            try:
                proc = subprocess.Popen(cmd, cwd=plan.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, start_new_session=True,
                                        text=True, encoding="utf-8", errors="replace")
            except OSError as e:
                return Result(False, error="%s を起動できなかった: %s" % (agent.argv[0] if agent.argv else agent.key, e))
            job.proc = proc
        reader, raw, err = agent.reader(), [], []

        def read_out():
            with proc.stdout:  # 読み終えたらこのスレッドで閉じる（読んでいる途中に別のスレッドから閉じると待たされる）
                for line in proc.stdout:
                    read_line(line)

        def read_err():
            with proc.stderr:
                err.append(proc.stderr.read())

        def read_line(line):
            raw.append(line)
            del raw[:-20]
            try:
                for name, inp in reader.feed(line):
                    text = self.step_label(m, item["id"], name, inp)
                    if text:
                        self.add_step(m, item["id"], text)
            except Exception:  # 途中経過の失敗で回答を止めない
                pass

        readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
        for t in readers:
            t.start()
        timed_out = False
        try:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except OSError:  # 先に終わった（起動に失敗した等）。結果は終了コードと標準エラーで見る
                pass
            timed_out = self.wait_agent(m, item["id"], proc, job)
        finally:
            # 終わった後も、claude が残した子プロセスごと止める（パイプを持ったままだと読み取りが終わらない）
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                pass
            with self.lock:
                job.proc = None
                canceled = self.canceled(m, item["id"])
                job.finishing = not canceled  # ここから先は取り消しを受けない（本文の差し替え・セッションの記録と行き違わない）
        end = time.time() + 5  # 別のセッションに逃げた孫がパイプを持っていると読み終わらないので、待つのは 5 秒まで
        for t in readers:
            t.join(timeout=max(0, end - time.time()))
        if canceled:
            return Result(False, error=CANCELED, canceled=True)
        if timed_out:
            return Result(False, error="回答が時間内（%d秒）に終わらなかったため止めた。質問を分けて送り直してください。"
                          % self.cfg.answer_timeout, timed_out=True)
        if self.stopping.is_set():
            return Result(False, error="配信を止めたため中断した。")
        ok, text, session_id, error = reader.result(proc.returncode, "".join(err), "".join(raw))
        if not ok:
            return Result(False, error=error)
        return Result(True, text=text, session_id=session_id)

    def wait_agent(self, m, qid, proc, job):
        """エージェントの終わりを待つ。時間切れ・取り消し・配信の停止では SIGTERM を送り、KILL_GRACE 秒後も残っていれば
        SIGKILL で止める。戻り値は時間切れだったか。"""
        deadline = time.time() + self.cfg.answer_timeout if self.cfg.answer_timeout else None
        timed_out, term_at, next_check = False, None, 0
        while True:
            try:
                proc.wait(timeout=0.2)
                return timed_out
            except subprocess.TimeoutExpired:
                pass
            if term_at is None:
                if deadline and time.time() >= deadline:
                    timed_out = True
                if job.building and time.time() >= next_check:
                    # 資料作りの途中で、答えが決まった・フックがもう待っていないなら、作っても使われないので止める
                    next_check = time.time() + 2
                    view = self.ask_view(m) or {}
                    if view.get("status") != "waiting" or not view.get("waiting"):
                        with self.lock:
                            self.cancel_ids.add((m.rel, qid))
                if timed_out or self.canceled(m, qid) or self.stopping.is_set():
                    term_at = time.time()
                    self.kill(job)
            elif time.time() - term_at >= KILL_GRACE:
                self.kill(job, signal.SIGKILL)

    def building_job(self, m, qid):
        job = self.jobs.get((m.rel, qid))
        return bool(job and job.building)

    def step_label(self, m, qid, name, inp):
        inp = inp if isinstance(inp, dict) else {}
        clip = lambda v, n=60: (lambda t: t if len(t) <= n else t[:n] + "…")(re.sub(r"\s+", " ", str(v or "")).strip())
        given = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
        path = os.path.abspath(str(given)) if given else ""

        def shown(p):
            for base in (m.dir, self.cfg.workdir):
                if p and inside(p, base):
                    return os.path.relpath(p, base)
            return os.path.basename(p) or p

        if name == "Read":
            if path in (m.path, m.draft):
                return "資料を読んでいます"
            if path == m.answer_path(qid):
                return "回答を読み直しています"
            return "「%s」を読んでいます" % shown(path)
        if name == "Grep":
            where = "（資料の中）" if path in (m.path, m.draft) else "（%s の中）" % shown(path) if path else ""
            return "コードを検索しています: %s%s" % (clip(inp.get("pattern")), where)
        if name == "Glob":
            return "ファイルを探しています: %s" % clip(inp.get("pattern"))
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            if path == m.draft:
                return "資料を書いています" if self.building_job(m, qid) else "資料の本文を直しています"
            if path == m.answer_path(qid):
                return "回答を書いています"
            if path and inside(path, m.images_dir):
                return "図の画像を作っています"
            return "「%s」に書き込もうとしています" % shown(path)
        if name == "Bash":
            cmd = str(inp.get("command") or "")
            if "check-explanation" in cmd:
                building = self.building_job(m, qid)
                return ("回答を検査しています" if "--fragment" in cmd else
                        "作った資料を検査しています" if building else "直した本文を検査しています")
            # Codex は読み取りもシェルで行う
            if m.path in cmd or m.draft in cmd:
                return "資料を読んでいます"
            if m.answer_path(qid) in cmd:
                return "回答を読み直しています"
            return "コマンドを実行しています: %s" % clip(cmd)
        if name in ("Task", "Agent"):
            return "調べものを手分けしています"
        if name == "WebSearch":
            return "Web で検索しています: %s" % clip(inp.get("query"))
        if name == "WebFetch":
            url = str(inp.get("url") or "")
            return "Web のページを読んでいます: %s" % clip(urlsplit(url).netloc or url)
        if name == "Skill":
            return "スキルを読んでいます: %s" % clip(inp.get("skill") or inp.get("name"))
        if name == "LSP":
            return "コードの定義を調べています"
        if name in ("TodoWrite", "ToolSearch") or not name:
            return None
        if name.startswith("mcp__"):
            server, _, action = name[len("mcp__"):].partition("__")
            # 「claude_ai_Google_Drive」は Google Drive、「plugin_slack_slack」は slack と出す
            server = re.sub(r"^(?:claude_ai_|plugin_[A-Za-z0-9-]+_)", "", server).replace("_", " ")
            return "MCP（%s）を使っています: %s" % (server, action)
        if name.endswith("McpResourcesTool") or name.startswith("ReadMcpResource"):
            return "MCP の資源を読んでいます"
        return "%s を使っています" % name

    # ---- 本文の差し替え

    def apply_draft(self, m, qid, original):
        try:
            with open(m.draft, "rb") as f:
                draft = f.read()
        except OSError:
            return None
        if draft == original:
            return None
        if not self.unchanged(m, original):
            return self.keep_conflict(m, qid)
        before = Counter(active_content(original.decode("utf-8", "replace")))
        added = Counter(active_content(draft.decode("utf-8", "replace"))) - before
        if added:
            return {"updated": False, "reverted": True, "conflict": False, "sections": [],
                    "errors": ["本文に script・イベント属性・外部の資源が増えたため、修正を取り消した（%s）"
                               % "、".join(sorted(added))]}
        argv = self.check_argv(m, m.draft)
        if argv:
            try:
                code, out, err = self.run_check(argv, m.dir)
                failed, output = code != 0, out + err
            except (OSError, subprocess.SubprocessError) as e:
                failed, output = True, "検査を実行できなかった: %s" % e
            if failed:
                errors = [line.strip() for line in output.splitlines() if line.lstrip().startswith("ERROR")][:5]
                return {"updated": False, "reverted": True, "conflict": False, "sections": [],
                        "errors": errors or [output.strip()[-500:] or "検査が失敗した"]}
        if not self.unchanged(m, original):
            return self.keep_conflict(m, qid)
        os.makedirs(m.history_dir, exist_ok=True)
        with open(os.path.join(m.history_dir, "%s.%s.html" % (m.stem, bytes_rev(original))), "wb") as f:
            f.write(original)
        sections = changed_sections(original.decode("utf-8", "replace"), draft.decode("utf-8", "replace"))
        os.replace(m.draft, m.path)
        return {"updated": True, "reverted": False, "conflict": False, "sections": sections,
                "rev": file_rev(m.path), "errors": []}

    @staticmethod
    def unchanged(m, original):
        with open(m.path, "rb") as f:
            return f.read() == original

    def keep_conflict(self, m, qid):
        os.makedirs(m.history_dir, exist_ok=True)
        kept = os.path.join(m.history_dir, qid + ".draft.html")
        shutil.copyfile(m.draft, kept)
        return {"updated": False, "reverted": False, "conflict": True, "sections": [], "errors": [],
                "kept": os.path.relpath(kept, m.dir).replace(os.sep, "/")}


class Handler(SimpleHTTPRequestHandler):
    app = None
    server_version = "visual-explain-serve"

    # ---- 共通

    def log_request(self, code="-", size="-"):
        # 状態の取得は数秒おきに来るので、エラーと質問の送信だけを残す
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        if self.command == "POST" or status >= 400:
            super().log_request(code, size)

    def end_headers(self):
        for name, value in getattr(self, "_extra_headers", []):
            self.send_header(name, value)
        self._extra_headers = []
        super().end_headers()

    def cookie_name(self):
        return "ve_qa_%d" % self.server.server_address[1]

    def authorized(self):
        token = self.app.cfg.token
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.cookie_name() and hmac.compare_digest(value.encode(), token.encode()):
                return True
        return False

    def send_bytes(self, status, body, content_type, cache="no-store", headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, data):
        self.send_bytes(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def blocked(self, path):
        """qa/ の下と作業用コピーを返さない。URL のパスと、実際に読むファイルのパスの両方で見る。"""
        rel = os.path.relpath(self.translate_path(path), self.app.cfg.root)
        for parts in ([p for p in unquote(path).split("/") if p], rel.split(os.sep)):
            parts = [p.lower() for p in parts]
            if QA_DIR in parts or (parts and DRAFT_SUFFIX + "." in parts[-1]):
                return True
        return False

    def prepare(self):
        """パスの先頭の // を 1 つにし（古い Python は縮めない。// の後ろをホスト名と読み違える）、ホスト名を確かめる。"""
        self.path = "/" + self.path.lstrip("/")
        if self.app.cfg.host_allowed(self.headers.get("Host")):
            return True
        name = host_name(self.headers.get("Host"))
        self.send_bytes(403, ("ホスト名 %s からの要求は受け付けない。IP アドレスか localhost で開くか、"
                              "--allow-host %s を付けて配信し直す。\n" % (name, name)).encode("utf-8"),
                        "text/plain; charset=utf-8")
        return False

    # ---- GET / HEAD

    def do_GET(self):
        if not self.prepare():
            return
        url = urlsplit(self.path)
        path = unquote(url.path)
        k = parse_qs(url.query).get("k", [None])[0]
        given = bool(k) and hmac.compare_digest(k.encode(), self.app.cfg.token.encode())
        if given:
            self._extra_headers = [("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                                    % (self.cookie_name(), self.app.cfg.token))]
        if self.app.cfg.private and not given and not self.authorized() and path not in PUBLIC_ASSETS:
            return self.send_bytes(401, PRIVATE_MESSAGE.encode("utf-8"), "text/plain; charset=utf-8")
        if path.startswith("/__qa/"):
            return self.api_get(path, parse_qs(url.query))
        if self.blocked(url.path):
            return self.send_error(404)
        fs = self.translate_path(url.path)
        if os.path.basename(fs) == self.app.cfg.doc_name and os.path.isfile(fs):
            return self.send_doc(fs)
        return super().do_GET()

    def do_HEAD(self):
        if not self.prepare():
            return
        if self.app.cfg.private and not self.authorized():
            return self.send_bytes(401, b"", "text/plain; charset=utf-8")
        if urlsplit(self.path).path.startswith("/__qa/") or self.blocked(urlsplit(self.path).path):
            return self.send_error(404)
        return super().do_HEAD()

    def list_directory(self, path):
        if self.app.cfg.private:  # 質問の一覧は受け箱で見る。フォルダの中身は見せない
            self.send_error(404)
            return None
        return super().list_directory(path)

    def send_events(self):
        """受け箱と質問の資料がつないだままにする接続。つないでいる間は「ページを見ている人がいる」とみなし、
        質問が増えた・締め切られたら知らせる。裏のタブでもブラウザは接続を止めない（タイマーは間引かれる）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sock = self.connection
        try:
            # 閲覧側が黙って消えた（スリープ・回線の切り替えなど）ときも、数十秒で切れたと分かるようにする。
            # 5 秒ごとに書き込むので、確認応答の来ない書き込みを 15 秒で諦める（TCP_USER_TIMEOUT。無ければ再送の上限まで
            # 十数分かかる）。keepalive は書き込みが無い接続のための保険
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for name, value in (("TCP_USER_TIMEOUT", 15000), ("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5),
                                ("TCP_KEEPCNT", 3), ("TCP_KEEPALIVE", 10)):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        except OSError:
            pass
        app = self.app
        with app.changed:
            app.viewers += 1
            seen = app.changes
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not app.stopping.is_set():
                with app.changed:
                    # 5 秒ごとに書き込み、閉じたページを早く見つける（書き込みに失敗したら切れたと分かる）
                    app.changed.wait_for(lambda: app.changes != seen or app.stopping.is_set(), timeout=5)
                    current = app.changes
                if current != seen:
                    seen = current
                    self.wfile.write(b"event: asks\ndata: %d\n\n" % seen)
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with app.changed:
                app.viewers -= 1
            self.close_connection = True

    def send_doc(self, fs):
        with open(fs, "rb") as f:
            raw = f.read()
        rel = os.path.relpath(fs, self.app.cfg.root).replace(os.sep, "/")
        # 覚えた質問欄の幅で開く（どの配信サーバー・配信元の資料でも同じ幅にする）
        width = read_viewer_prefs(self.app.cfg.prefs).get("width")
        doc = html.escape(rel, quote=True)
        # PDF のボタンは、質問を受け付けない配信（--no-qa）でも出す
        snippet = ('\n<link rel="stylesheet" href="/__qa/pdf.css">\n'
                   '<script src="/__qa/pdf.js" defer data-doc="%s"></script>\n' % doc)
        if self.app.cfg.qa is not False:
            snippet += ('<link rel="stylesheet" href="/__qa/qa.css">\n'
                        '<script src="/__qa/qa.js" defer data-doc="%s" data-rev="%s"%s></script>\n'
                        % (doc, bytes_rev(raw), ' data-width="%d"' % width if width else ""))
        text = raw.decode("utf-8", "replace")
        ends = list(BODY_END_RE.finditer(text))
        i = ends[-1].start() if ends else len(text)
        text = text[:i] + snippet + text[i:]
        headers = [("Content-Security-Policy", CSP)] if self.app.cfg.qa is not False else []
        self.send_bytes(200, text.encode("utf-8"), "text/html; charset=utf-8", headers=headers)

    def send_window(self, query):
        """質問欄だけを別ウィンドウで開くページ。中身は qa.js が組み立て、質問と回答は状態の API から読む。"""
        m = resolve_doc(self.app.cfg, (query.get("doc") or [None])[0])
        if not m or self.app.cfg.qa is False:
            return self.send_error(404)
        with open(m.path, "rb") as f:
            found = re.search(r"<title[^>]*>(.*?)</title>", f.read(64 * 1024).decode("utf-8", "replace"), re.I | re.S)
        title = html.unescape(re.sub(r"\s+", " ", found.group(1)).strip()) if found else m.rel
        body = ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                '<title>質問と回答 · %s</title>\n<link rel="stylesheet" href="/__qa/qa.css">\n</head>\n'
                '<body class="ve-qa-window">\n<script src="/__qa/qa.js" defer data-doc="%s" data-mode="window"></script>\n'
                '</body>\n</html>\n' % (html.escape(title), html.escape(m.rel, quote=True)))
        self.send_bytes(200, body.encode("utf-8"), "text/html; charset=utf-8", headers=[("Content-Security-Policy", CSP)])

    def send_pdf(self, query):
        """資料を PDF にして返す。資料のページを見られる人なら誰でも取れる（資料と同じ扱い）。"""
        m = resolve_doc(self.app.cfg, (query.get("doc") or [None])[0])
        if not m:
            return self.send_json(404, {"error": "資料が見つからない"})
        try:
            pdf = render_pdf(m.path)
        except RuntimeError as e:
            return self.send_json(500, {"error": str(e)})
        if pdf is None:
            return self.send_json(503, {"error": "配信しているマシンに Chrome が無いので PDF を作れない"})
        with open(m.path, "rb") as f:
            found = re.search(r"<title[^>]*>(.*?)</title>", f.read(64 * 1024).decode("utf-8", "replace"), re.I | re.S)
        title = html.unescape(re.sub(r"\s+", " ", found.group(1)).strip()) if found else ""
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title)[:100] or os.path.basename(m.dir) or "document"
        disposition = "attachment; filename=\"document.pdf\"; filename*=UTF-8''%s" % url_quote(name + ".pdf", safe="")
        self.send_bytes(200, pdf, "application/pdf", headers=[("Content-Disposition", disposition)])

    def send_inbox(self):
        """エージェントの質問（AskUserQuestion・request_user_input）の受け箱。開いておくと、質問が来たときにその資料を開く（中身は inbox.js が作る）。"""
        body = ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                '<title>エージェントからの質問</title>\n<link rel="stylesheet" href="/__qa/qa.css">\n</head>\n'
                '<body class="ve-qa-inbox-page">\n<script src="/__qa/inbox.js" defer></script>\n</body>\n</html>\n')
        self.send_bytes(200, body.encode("utf-8"), "text/html; charset=utf-8", headers=[("Content-Security-Policy", CSP)])

    def api_get(self, path, query):
        if path in PUBLIC_ASSETS:
            name = path.rsplit("/", 1)[1]
            with open(os.path.join(ASSET_DIR, name), "rb") as f:
                body = f.read()
            ctype = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
            return self.send_bytes(200, body, ctype, cache="no-cache")
        if path == "/__qa/panel":
            return self.send_window(query)
        if path == "/__qa/pdf":
            return self.send_pdf(query)
        if path == "/__qa/inbox":
            if self.app.cfg.private:
                # 受け箱を開いたことがある印。auto の人は、これ以降の質問を常にページで待つ（ask_effective_mode）
                try:
                    open(os.path.join(self.app.cfg.root, INBOX_USED), "a").close()
                except OSError:
                    pass
            return self.send_inbox()
        if path not in ("/__qa/state", "/__qa/ping", "/__qa/asks", "/__qa/ask-status", "/__qa/events"):
            return self.send_json(404, {"error": "not found"})
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        if path == "/__qa/events":
            return self.send_events() if self.app.cfg.presence else self.send_bytes(204, b"", "text/plain")
        if path == "/__qa/ping":
            return self.send_json(200, {"ok": True, "root": self.app.cfg.root, "pid": os.getpid(), "version": VERSION,
                                        "viewers": self.app.viewers})
        if path == "/__qa/asks":
            return self.send_json(200, {"now": now(), "asks": self.app.list_asks()})
        m = resolve_doc(self.app.cfg, (query.get("doc") or [None])[0])
        if not m:
            return self.send_json(404, {"error": "資料が見つからない"})
        if path == "/__qa/ask-status":
            view = self.app.poll_ask(m)
            return self.send_json(200 if view else 404, view or {"error": "この資料にはエージェントからの質問が無い"})
        enabled, reason = self.app.qa_status(m)
        self.send_json(200, {"doc": m.rel, "rev": file_rev(m.path), "now": now(),
                             "qa": {"enabled": enabled, "reason": reason, "agent_name": self.app.agent_for(m).label},
                             "items": self.app.items(m),
                             "ask": self.app.ask_view(m)})

    # ---- POST

    def do_POST(self):
        if not self.prepare():
            return
        path = unquote(urlsplit(self.path).path)
        route = {"/__qa/questions": self.post_question, "/__qa/cancel": self.post_cancel,
                 "/__qa/export": self.post_export, "/__qa/ask": self.post_ask, "/__qa/answer": self.post_answer,
                 "/__qa/ask-close": self.post_ask_close, "/__qa/prefs": self.post_prefs}.get(path)
        if not route:
            return self.send_json(404, {"error": "not found"})
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != (self.headers.get("Host") or ""):
            return self.send_json(403, {"error": "別のサイトからの送信は受け付けない"})
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self.send_json(415, {"error": "Content-Type は application/json にする"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > (MAX_ASK_BODY if path == "/__qa/ask" else MAX_BODY):
            return self.send_json(413, {"error": "質問が長すぎる"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self.send_json(400, {"error": "JSON を読めない"})
        if path in ("/__qa/ask", "/__qa/prefs"):
            return route(None, body)
        m = resolve_doc(self.app.cfg, body.get("doc"))
        if not m:
            return self.send_json(404, {"error": "資料が見つからない"})
        if route in (self.post_answer, self.post_ask_close):
            return route(m, body)  # 答えるのに claude は要らない
        enabled, reason = self.app.qa_status(m)
        if not enabled:
            return self.send_json(503, {"error": reason})
        route(m, body)

    def post_question(self, m, body):
        question = body.get("question")
        question = question.strip() if isinstance(question, str) else ""
        if not question or len(question) > MAX_QUESTION:
            return self.send_json(400, {"error": "質問は 1〜%d 文字で書く" % MAX_QUESTION})
        section = body.get("section") if isinstance(body.get("section"), dict) else {}
        sid, title = section.get("id"), section.get("title")
        section = ({"id": sid, "title": title[:200] if isinstance(title, str) else ""}
                   if isinstance(sid, str) and SECTION_ID_RE.match(sid) else None)
        quote = body.get("quote")
        quote = quote.strip()[:MAX_QUOTE] if isinstance(quote, str) and quote.strip() else None
        reply_to = body.get("reply_to")
        if reply_to is not None and not (isinstance(reply_to, str) and QID_RE.match(reply_to)
                                         and self.app.reply_target(m, reply_to)):
            return self.send_json(400, {"error": "続きの質問は、回答した質問にだけできる"})
        quote_from = "answer" if quote and reply_to and body.get("quote_from") == "answer" else None
        self.send_json(202, self.app.enqueue(m, question, section, quote, reply_to=reply_to, quote_from=quote_from))

    def post_cancel(self, m, body):
        qid = body.get("id")
        if not isinstance(qid, str) or not QID_RE.match(qid):
            return self.send_json(400, {"error": "取り消す質問の id が無い"})
        self.send_json(*self.app.cancel(m, qid))

    def post_export(self, m, body):
        self.send_json(*self.app.export(m))

    def post_ask(self, m, body):
        self.send_json(*self.app.register_ask(body))

    def post_answer(self, m, body):
        self.send_json(*self.app.answer_ask(m, body))

    def post_ask_close(self, m, body):
        self.send_json(*self.app.close_ask(m, body.get("reason")))

    def post_prefs(self, m, body):
        """質問欄の幅を覚える。このマシンのどの配信サーバーの資料も、次に開いたときにこの幅で開く。"""
        width = panel_width(body.get("width"))
        if width is None:
            return self.send_json(400, {"error": "幅は %d〜%d の整数にする" % PANEL_WIDTH})
        if not self.app.cfg.prefs:
            return self.send_json(200, {"saved": False})
        try:
            write_json(self.app.cfg.prefs, dict(read_viewer_prefs(self.app.cfg.prefs), width=width))
        except OSError as e:
            return self.send_json(500, {"error": "幅を覚えられなかった: %s" % e})
        self.send_json(200, {"saved": True, "width": width})


def make_server(app):
    cfg = app.cfg
    server_class = ThreadingHTTPServer
    if ":" in cfg.bind:
        server_class = type("ThreadingHTTPServerV6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = server_class((cfg.bind.strip("[]"), cfg.port), functools.partial(handler, directory=cfg.root))
    httpd.daemon_threads = True
    return httpd


def serve_main(args):
    if args.bind in ALL_INTERFACES:
        print("全てのインターフェース（%r）では待ち受けない。127.0.0.1 かプライベートネットワークのアドレスを --bind に指定する"
              % args.bind, file=sys.stderr)
        return 2
    if not os.path.isdir(args.dir):
        print("配信フォルダが無い: %s" % args.dir, file=sys.stderr)
        return 2
    cfg = Config(root=args.dir, port=args.port, bind=args.bind, repo=args.repo, claude=shlex.split(args.claude),
                 codex=shlex.split(args.codex), agent=args.agent, token=args.token, doc_name=args.doc_name, prompt=args.prompt, check_cmd=args.check_cmd,
                 browser_check=not args.no_browser_check, answer_timeout=args.answer_timeout, model=args.model,
                 qa=not args.no_qa, allow_host=args.allow_host, local_only=args.local_only, private=args.private,
                 workers=args.workers, prefs=os.path.join(ask_home(), VIEWER_PREFS))
    app = App(cfg)
    try:
        httpd = make_server(app)
    except OSError as e:
        print("%s:%d で待ち受けられない（%s）。空いているポートかアドレスを選び直す" % (cfg.bind, cfg.port, e), file=sys.stderr)
        return 2
    port = httpd.server_address[1]
    host = "localhost" if cfg.bind in ("127.0.0.1", "::1", "localhost") else (
        "[%s]" % cfg.bind.strip("[]") if ":" in cfg.bind else cfg.bind)
    base = "http://%s:%d" % (host, port)
    if args.record:
        write_json(args.record, {"pid": os.getpid(), "started_at": now(), "version": VERSION, "bind": cfg.bind,
                                 "port": port, "url": base})
    limit = "上限 %d 分で自動停止" % (args.max_time // 60) if args.max_time else "上限なし"
    print("配信中: %s/ （フォルダ: %s、%s、PID %d）" % (base, cfg.root, limit, os.getpid()))
    for rel in find_docs(cfg):
        print("資料: %s/%s?k=%s" % (base, rel, cfg.token))
    if app.qa_enabled:
        names = "、".join("%s（%s）" % (a.label, shlex.join(a.argv)) for a in app.agents.values() if a.available())
        print("質問: 受け付ける（資料を作ったエージェントが答える。使えるもの: %s。資料を作ったセッションが分からないとき: %s。"
              "作業ディレクトリの既定: %s）" % (names, app.default_agent().label, cfg.workdir))
        print("回答で使う外の情報源: %s" % ("無し（--local-only）" if cfg.local_only else
                                          "Web と、エージェントに設定された MCP のサーバーすべて（--local-only で止める）"))
    else:
        print("質問: 受け付けない（%s）" % app.qa_reason)
    sys.stdout.flush()

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    if args.max_time:
        timer = threading.Timer(args.max_time, shutdown)
        timer.daemon = True
        timer.start()
    app.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.server_close()
        print("配信を止めた: %s/" % base)
    return 0


def origin_main(args):
    if not os.path.isdir(args.material):
        print("資料フォルダが無い: %s" % args.material, file=sys.stderr)
        return 2
    # どのエージェントのセッションかは、そのエージェントがコマンドに渡す環境変数で分かる
    agent, sid = args.agent, args.session_id
    if not agent:
        detected, env_sid = agents_mod.detect_session_agent(os.environ)
        agent, sid = detected or "claude", sid or env_sid
    cls = agents_mod.AGENTS[agent]
    sid = sid or os.environ.get(cls.session_env)
    if not sid:
        print("CLAUDE_CODE_SESSION_ID も CODEX_THREAD_ID も無いため、資料を作ったセッションを記録しなかった。"
              "ページからの質問には新しいセッションで答える。", file=sys.stderr)
        return 0
    path = record_origin(args.material, sid, args.cwd, agent, os.environ.get(cls.account_env) or None)
    print("資料を作ったセッションを記録した: %s" % path)
    return 0


def export_main(args):
    path = os.path.join(args.material, args.doc_name) if os.path.isdir(args.material) else args.material
    if not os.path.isfile(path):
        print("資料が無い: %s" % path, file=sys.stderr)
        return 2
    m = Material.at(path)
    data = read_json(m.state_path) or {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    if any(i.get("status") in ("queued", "answering") for i in items):
        print("回答を待っている質問がある。回答が終わってから書き出す（%s）" % m.state_path, file=sys.stderr)
        return 1
    status, res = export_material(m, items)
    if status != 200:
        print(res["error"], file=sys.stderr)
        return 1
    print("質問と回答 %d 件を、資料の末尾の節「%s」（#%s）に%s: %s" % (
        res["count"], EXPORT_TITLE, EXPORT_ID, "書き出した" if res["updated"] else "書き出し済みだった（変更なし）", m.path))
    return 0


def tailscale_serve(https, port):
    """Tailscale serve で、https の名前・ポートへの HTTPS を配信サーバー（127.0.0.1:port）に中継する。port が None なら外す。
    失敗したら理由を返す。"""
    cli = tailscale_cli()
    if not cli:
        return "tailscale が見つからない"
    flag = "--https=%d" % int(https["port"])
    argv = [cli, "serve", flag, "off"] if port is None else [cli, "serve", "--bg", flag, "http://127.0.0.1:%d" % int(port)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-300:]


def stop_ask_server(home, conf):
    """常駐の配信サーバーを止める。合言葉が通り、置き場所が同じ（自分の配信サーバー）ときだけ、その PID に送る。"""
    ping = ask_ping(conf)
    if not ping or not isinstance(ping.get("pid"), int):
        return False
    try:
        os.kill(ping["pid"], signal.SIGTERM)
    except OSError:
        return False
    end = time.time() + 5
    while time.time() < end and ask_server_alive(conf):
        time.sleep(0.2)
    return True


def ask_config_main(args):
    home = ask_home()
    existed = bool(read_ask_settings(home))
    current = load_ask_config(home, create=True)  # 合言葉はロックを取って作る（セッションの開始と取り合わない）
    before = current if existed else None
    raw = read_ask_settings(home)
    if args.bind is not None and args.bind in ALL_INTERFACES:
        print("全てのインターフェース（%r）では待ち受けない。auto（Tailscale か 127.0.0.1）かプライベートネットワークの"
              "アドレスを指定する" % args.bind, file=sys.stderr)
        return 2
    mode = args.mode or ("always" if args.enable else "off" if args.disable else None)
    if mode:
        raw["mode"] = mode
        raw.pop("enabled", None)  # 0.16.0 の書き方は mode に置き換える
    if args.bind is not None:
        if args.bind == "auto":
            raw.pop("bind", None)
        else:
            raw["bind"] = args.bind
    for key in ("port", "wait", "build", "model"):
        if getattr(args, key) is not None:
            raw[key] = getattr(args, key)
    if args.allow_host:
        raw["allow_host"] = args.allow_host
    if args.https == "on":
        name = tailscale_https_name()
        if not name:
            print("Tailscale の HTTPS が使えない（tailscale が無い、止まっている、またはこの tailnet で HTTPS の証明書が"
                  "有効でない）", file=sys.stderr)
            return 2
        raw["https"] = {"name": name, "port": args.https_port or int(raw.get("port") or 8770)}
    elif args.https == "off":
        raw.pop("https", None)
    if not isinstance(raw.get("wait", ASK_WAIT_DEFAULT), int) or not 10 <= raw.get("wait", ASK_WAIT_DEFAULT) <= ASK_WAIT_MAX:
        print("待つ時間は 10 秒〜%d 分にする" % (ASK_WAIT_MAX // 60), file=sys.stderr)
        return 2
    if raw != read_ask_settings(home):
        save_ask_config(home, raw)
    conf = load_ask_config(home)
    moved = before and any(before.get(k) != conf.get(k) for k in ("bind", "port", "root", "allow_host", "model", "token"))
    if args.stop or conf["mode"] == "off" or moved:
        # 止めた・待ち受け先を変えたときは、前の設定で動いている配信サーバーを止める
        stop_ask_server(home, before if moved else conf)
    old_https = (before or {}).get("https") if before else None
    if old_https and old_https != conf.get("https"):
        tailscale_serve(old_https, None)  # 前の HTTPS の中継を外す
    if conf.get("https") and conf.get("https") != old_https:
        error = tailscale_serve(conf["https"], conf["port"])
        if error:
            print("Tailscale serve で HTTPS を通せなかった: %s" % error, file=sys.stderr)
            return 2
    running = conf["mode"] != "off" and not args.stop and ensure_ask_server(home, conf)
    label = {"open": "受け箱か質問の資料をブラウザで開いているときだけ、質問をページに出す",
             "always": "常に質問をページに出して待つ", "off": "使わない（いつもどおりターミナルで答える）"}
    effective = ask_effective_mode(conf)
    print("エージェントの質問（AskUserQuestion・request_user_input）を資料のページで答える: %s（%s%s）" % (
        conf["mode"], label[effective], "。受け箱を開いたことがあるため" if conf["mode"] == "auto" and effective == "always"
        else "。受け箱を一度開くと、それ以降は常にページで待つ" if conf["mode"] == "auto" else ""))
    print("受け箱: %s/__qa/inbox?k=%s （開いておくと、質問が来たときにその資料を開く）" % (conf["public_url"], conf["token"]))
    if conf.get("https"):
        print("待ち受けるアドレス: 127.0.0.1（Tailscale serve の HTTPS で中継。デスクトップ通知が使える）")
    else:
        print("待ち受けるアドレス: %s" % (conf["bind"] if conf["bind_setting"] != "auto" else "%s（自動: %s）" % (
            conf["bind"], "Tailscale" if conf["bind"] != "127.0.0.1" else "Tailscale が無いので、このマシンだけ")))
        if tailscale_https_name():
            print("デスクトップ通知: --https on で、受け箱を Tailscale の HTTPS で開けるようにすると使える")
    print("待つ時間: %d 分（過ぎたら、ターミナルにいつもの選択画面が出る）" % (int(conf["wait"]) // 60))
    print("資料: %s" % ("質問ごとに図入りの資料を作る" if conf["build"] == "always" else "作らない（選択肢のページだけ）"))
    foreign = not running and conf["mode"] != "off" and ask_probe(conf) == "foreign"
    print("配信サーバー: %s" % ("動いている（%s）" % ask_base_url(conf) if running else
                               "起動できない（%s を別の配信サーバーが使っている。--port で別のポートにする）"
                               % ask_base_url(conf) if foreign else
                               "止まっている（セッションを始めたときに起動する）" if conf["mode"] != "off" else "止まっている"))
    print("質問と資料の置き場所: %s" % conf["root"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="visual-explain の資料を配信し、ページからの追加の質問に claude -p で答える")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="資料のフォルダを配信する")
    s.add_argument("--dir", required=True, help="配信するフォルダ（資料、または資料をまとめた親フォルダ）")
    s.add_argument("--port", type=int, required=True, help="待ち受けるポート。0 なら空いているポートを選ぶ")
    s.add_argument("--bind", default="127.0.0.1", help="待ち受けるアドレス（既定 127.0.0.1）")
    s.add_argument("--repo", help="説明したリポジトリ。検査の --repo と、新しいセッションの作業ディレクトリに使う")
    s.add_argument("--max-time", type=parse_duration, default=parse_duration("2h"), help="この時間で自動停止（既定 2h、0 は上限なし）")
    s.add_argument("--answer-timeout", type=parse_duration, default=parse_duration("15m"), help="1 件の回答の上限（既定 15m）")
    s.add_argument("--claude", default=os.environ.get("VISUAL_EXPLAIN_CLAUDE", "claude"), help="claude を起動するコマンド")
    s.add_argument("--codex", default=os.environ.get("VISUAL_EXPLAIN_CODEX", "codex"), help="codex を起動するコマンド")
    s.add_argument("--agent", choices=("auto", "claude", "codex"), default="auto",
                   help="資料を作ったセッションが分からないときに答えるエージェント（既定 auto: 使える方、両方なら claude）")
    s.add_argument("--model", help="回答に使うモデル（既定はエージェントの既定）")
    s.add_argument("--doc-name", default="explanation.html", help="質問欄を差し込む HTML のファイル名")
    s.add_argument("--prompt", default=DEFAULT_PROMPT, help="claude に渡す指示文の雛形")
    s.add_argument("--check-cmd", help="本文を直した後の検査コマンド（{file} が作業用コピー）。空文字なら検査しない")
    s.add_argument("--no-browser-check", action="store_true", help="検査でヘッドレス Chrome を使わない")
    s.add_argument("--token", help="URL の合言葉（既定は起動ごとに作る）")
    s.add_argument("--no-qa", action="store_true", help="質問を受け付けず、静的配信だけ行う")
    s.add_argument("--local-only", action="store_true", help="回答で Web と MCP を使わず、資料とリポジトリだけで答える")
    s.add_argument("--private", action="store_true", help="資料のページも、合言葉付きの URL で開いたブラウザにだけ返す")
    s.add_argument("--workers", type=int, default=1, help="同時に扱う質問の数（別々の資料だけ。既定 1）")
    s.add_argument("--record", help="待ち受けを始めたら、PID・版・待ち受け先をこのファイルに書く（受け箱の配信サーバー）")
    s.add_argument("--allow-host", action="append", default=[],
                   help="IP アドレス・localhost・このマシンの名前・*.ts.net 以外で開くときのホスト名（複数可）")
    o = sub.add_parser("origin", help="資料を作ったセッションを記録する（ページからの質問でその文脈を使い回す）")
    o.add_argument("material", help="資料のフォルダ（explanation.html がある所）")
    o.add_argument("--cwd", default=os.getcwd(), help="資料を作ったセッションの作業ディレクトリ")
    o.add_argument("--agent", choices=sorted(agents_mod.AGENTS), help="資料を作ったエージェント（既定は環境変数から判定）")
    o.add_argument("--session-id", help="既定は環境変数 CLAUDE_CODE_SESSION_ID（Codex は CODEX_THREAD_ID）")
    e = sub.add_parser("export", help="回答した質問を、資料の末尾の節に書き出す（ファイルを渡しても読めるように）")
    e.add_argument("material", help="資料のフォルダ、または資料の HTML")
    e.add_argument("--doc-name", default="explanation.html", help="資料の HTML のファイル名（フォルダを渡したとき）")
    a = sub.add_parser("ask-config", help="エージェントの質問（AskUserQuestion・request_user_input）を資料のページで答える設定（引数なしで今の設定と受け箱の URL を表示）")
    on = a.add_mutually_exclusive_group()
    on.add_argument("--mode", choices=ASK_MODES,
                    help="auto: 受け箱を一度でも開いたら常にページで待ち、それまでは open と同じ（既定）。"
                         "open: 受け箱か質問の資料を開いているときだけページに出す。always: 常にページで待つ。off: 使わない")
    on.add_argument("--enable", action="store_true", help="--mode always と同じ")
    on.add_argument("--disable", action="store_true", help="--mode off と同じ（配信サーバーも止める）")
    a.add_argument("--bind", help="受け箱と資料を配信するアドレス。既定 auto（Tailscale が使えればそのアドレス、無ければ 127.0.0.1）")
    a.add_argument("--port", type=int, help="配信するポート（既定 8770）")
    a.add_argument("--wait", type=parse_duration, help="ページの答えを待つ時間（既定 30m、最長 %dm）" % (ASK_WAIT_MAX // 60))
    a.add_argument("--build", choices=("always", "never"), help="質問ごとに図入りの資料を作るか（既定 always）")
    a.add_argument("--model", help="資料作りと質問欄の回答に使うモデル")
    a.add_argument("--allow-host", action="append", help="IP アドレス・localhost 以外で開くときのホスト名（複数可）")
    a.add_argument("--https", choices=("on", "off"), help="受け箱を Tailscale serve の HTTPS で開けるようにする（デスクトップ通知が使える）")
    a.add_argument("--https-port", type=int, help="HTTPS のポート（既定は配信サーバーと同じ）")
    a.add_argument("--stop", action="store_true", help="常駐の配信サーバーを止める（使う設定なら、次のセッションの開始で起動する）")
    args = ap.parse_args(argv)
    return {"serve": serve_main, "origin": origin_main, "export": export_main,
            "ask-config": ask_config_main}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
