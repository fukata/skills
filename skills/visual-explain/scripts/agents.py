#!/usr/bin/env python3
"""エージェント（Claude Code・Codex・OpenCode）ごとに違う所をまとめる。serve.py と ask_hook.py はこれを通して呼ぶ。

  子プロセスのコマンド（分岐・続き・新しいセッション、権限、Web と MCP）、出力の読み取り（途中経過・最終回答・
  セッションの ID）、セッションの有無と最初の依頼、MCP のサーバーの一覧、アカウントの設定フォルダの環境変数。
Python 標準ライブラリだけで動く。
"""
import glob
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, field

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")  # OpenCode の会話の ID は「ses_...」なので _ も許す

MCP_LIST_TIMEOUT = 60
# 読み取りは専用のツールだけにする。git のルールは --output= や --open-files-in-pager= で書き込み・実行ができてしまう
READ_TOOLS = ["Read", "Grep", "Glob"]
# 調べものの手分け・スキル・コードの定義の検索。書き込みとコマンドは、手分けした先でも許したものしかできない
HELPER_TOOLS = ["Task", "Agent", "Skill", "LSP"]
# 資料とリポジトリの外を調べるツール（--local-only では許さない）。MCP はこのほかにサーバーごとに mcp__<名前> で許す
# （mcp__* のようなまとめた書き方は効かない）
EXTERNAL_TOOLS = ["WebSearch", "WebFetch", "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool"]
# claude mcp list の行（「名前: 接続先 - 状態」）。名前に「:」を含むもの（plugin:slack:slack）もある
MCP_LINE_RE = re.compile(r"^(\S.*?): \S.* - \S")
# codex exec を絞る hook（すべてのツールに付ける）
GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_guard.py")
# 子の codex では切る機能（コードの実行やサブエージェントなど、ガードの外で動きうるもの）
CODEX_DISABLED_FEATURES = ("multi_agent", "code_mode", "code_mode_only", "browser_use", "computer_use", "shell_zsh_fork")
# codex の -c に dotted key のまま書ける名前（MCP サーバーの名前）
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# OpenCode の子に使うエージェント。子だけの設定（OPENCODE_CONFIG_CONTENT）でこの名前のエージェントを定義し、
# ツールの権限を許可リストに絞る（利用者の設定のエージェントは使わない）
OPENCODE_CHILD_AGENT = "visual-explain-child"
OPENCODE_CHILD_SYSTEM = ("visual-explain（図解の資料を作るスキル）の資料作りを手伝う子セッション。"
                         "渡された指示の作業だけを行い、ほかのことはしない。")



def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def run_listing(argv, cwd, env, timeout):
    """一覧のコマンドを動かし、標準出力を返す。起動できない・時間切れ・失敗は None。
    接続の確認で stdio のサーバーを起動するので、終わったら（時間切れのときも）孫ごと止める。"""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                start_new_session=True)
    except OSError:
        return None

    def kill_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group()
        proc.communicate()
        return None
    kill_group()
    return out if proc.returncode == 0 else None


@dataclass
class Run:
    """子プロセスで 1 件の質問に答える（資料を作る）ための材料。"""
    mode: str                 # "new" | "fork" | "resume"
    session_id: str           # fork/resume は元のセッション、Claude の new は新しく作った ID、Codex の new は None
    cwd: str
    material_dir: str
    write_files: list         # 書いてよいファイル（作業用コピー・回答ファイル）
    write_dirs: list          # 書いてよいフォルダ（images/）
    check_argvs: list         # 動かしてよい検査のコマンド（argv の配列）
    model: str = None
    local_only: bool = False
    mcp_names: list = field(default_factory=list)
    sandbox: bool = True      # Codex: サンドボックスが動くか（False なら danger-full-access で動かし、ガードだけで絞る）
    guard_policy: str = None  # Codex: ガードの方針を書いた JSON のパス（Codex では必ず付ける）


class Agent:
    key = label = session_env = account_env = ask_tool = write_hint = ""
    home_dir = ""
    parent_env = ()

    def __init__(self, argv):
        self.argv = list(argv)

    def available(self):
        exe = self.argv[0] if self.argv else ""
        return bool(shutil.which(exe) or (os.path.isfile(exe) and os.access(exe, os.X_OK)))

    def default_home(self):
        return os.path.expanduser(self.home_dir)

    def home(self, config_dir=None):
        return config_dir or os.environ.get(self.account_env) or self.default_home()

    def child_env(self, base, config_dir=None, keep_account=True):
        """子プロセスの環境。親のセッションの印を外し、アカウントの設定フォルダを config_dir にする。
        config_dir が無いとき、keep_account なら base のものを残し、そうでなければ外す（既定のアカウント）。"""
        env = {k: v for k, v in base.items() if k not in self.parent_env}
        if config_dir:
            env[self.account_env] = config_dir
        elif not keep_account:
            env.pop(self.account_env, None)
        return env

    def run_env(self, run):
        """その 1 件（run）の子プロセスにだけ足す環境変数。"""
        return {}


class ClaudeReader:
    """claude -p --output-format stream-json の出力を 1 行ずつ読む。"""

    def __init__(self):
        self.final = None

    def feed(self, line):
        try:
            ev = json.loads(line)
        except ValueError:
            return []
        # 回答に要るのは最後の result だけ。--output-format json と同じ形（配列 1 つ）で返す版にも備える
        for e in (ev if isinstance(ev, list) else [ev]):
            if isinstance(e, dict) and e.get("type") == "result":
                self.final = e
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            return []
        steps = []
        for block in (ev.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                inp = block.get("input")
                steps.append((str(block.get("name") or ""), inp if isinstance(inp, dict) else {}))
        return steps

    def result(self, returncode, stderr, raw):
        """(成功したか, 最後の応答, セッションの ID, 失敗の文)"""
        r = self.final
        if returncode != 0 or r is None or r.get("is_error"):
            detail = (r or {}).get("result") or stderr.strip()[-800:] or raw.strip()[-800:] or "終了コード %d" % returncode
            return False, "", None, "claude が失敗した: %s" % detail
        return True, r.get("result") or "", r.get("session_id"), None


class ClaudeAgent(Agent):
    key, label = "claude", "Claude Code"
    session_env, account_env, home_dir = "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "~/.claude"
    ask_tool, write_hint = "AskUserQuestion", "Write ツール"
    # 親の Claude Code セッションから引き継がない環境変数（入れ子のセッションと誤認させない）
    parent_env = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET",
                  "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT")

    def session_exists(self, session_id, home):
        if not SESSION_ID_RE.match(session_id or ""):
            return False
        return bool(glob.glob(os.path.join(home, "projects", "*", session_id + ".jsonl")))

    def session_info(self, session_id, home, transcript_path=None):
        return {"prompt": self.first_prompt(transcript_path)}

    @staticmethod
    def first_prompt(path, lines=2000):
        """セッションの会話の記録から、開発者の最初の依頼の冒頭を取る（どのセッションの質問か見分けるため）。"""
        if not isinstance(path, str) or not path.endswith(".jsonl") or "projects" not in path.split(os.sep):
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(lines), f):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    msg = ev.get("message") if isinstance(ev, dict) and ev.get("type") == "user" else None
                    content = msg.get("content") if isinstance(msg, dict) and msg.get("role") == "user" else None
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                    text = re.sub(r"\s+", " ", content or "").strip()
                    # コマンドの記録・システムからの知らせ（<...> で始まる）は依頼ではない
                    if text and not text.startswith("<"):
                        return text[:120] + ("…" if len(text) > 120 else "")
        except OSError:
            return None
        return None

    def list_mcp(self, cwd, env, timeout=MCP_LIST_TIMEOUT):
        """cwd で claude が読み込む MCP サーバーの名前を、許可のルール（mcp__<名前>）に使う形で返す。調べられなければ None。
        env は claude を動かす環境（アカウントごとの設定フォルダ CLAUDE_CONFIG_DIR で、読み込む MCP サーバーが変わる）。"""
        out = run_listing(self.argv + ["mcp", "list"], cwd, env, timeout)
        if out is None:
            return None
        names = []
        for line in out.splitlines():
            m = MCP_LINE_RE.match(line)
            # ツールの名前と同じく、英数字・_・- 以外を _ にする（「claude.ai Gmail」は mcp__claude_ai_Gmail__...）
            name = re.sub(r"[^A-Za-z0-9_-]", "_", m.group(1)) if m else ""
            if name and name not in names:
                names.append(name)
        return names

    def external_label(self, mcp_names):
        return "Web の検索（WebSearch）と取得（WebFetch）、MCP のサーバー（%s）" % ("、".join(mcp_names) if mcp_names else "無し")

    def command(self, run):
        # 途中経過を受け取るため、出力は 1 行 1 イベントの stream-json にする（-p では --verbose が要る版がある）
        cmd = self.argv + ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk"]
        if run.model:
            cmd += ["--model", run.model]
        cmd += {"new": ["--session-id", run.session_id], "fork": ["--resume", run.session_id, "--fork-session"],
                "resume": ["--resume", run.session_id]}[run.mode]
        if not inside(run.material_dir, run.cwd):
            cmd += ["--add-dir", run.material_dir]
        # 絶対パスのルールは「//」で始める。Edit のルールは Write にも効く
        rules = READ_TOOLS + HELPER_TOOLS + ["Edit(/%s)" % p for p in run.write_files]
        rules += ["Edit(/%s/**)" % d for d in run.write_dirs]
        rules += ["Bash(%s:*)" % shlex.join(argv) for argv in run.check_argvs]
        if not run.local_only:
            rules += EXTERNAL_TOOLS + ["mcp__%s" % name for name in run.mcp_names]
        return cmd + ["--allowedTools"] + rules

    def new_session_id(self):
        return str(uuid.uuid4())

    def reader(self):
        return ClaudeReader()


def toml_str(value):
    """codex の -c の値に書く TOML の文字列（JSON の文字列は TOML の basic string として読める）。"""
    return json.dumps(value, ensure_ascii=False)


class CodexReader:
    """codex exec --json の出力を 1 行ずつ読む。シェル・ファイルの変更・Web 検索・MCP を、Claude と同じ
    「ツール名と入力」の形に直して途中経過にする（途中経過の文は serve.py が Claude と共通で作る）。"""

    def __init__(self):
        self.thread, self.text, self.done, self.failure, self.seen = None, None, False, None, set()

    def feed(self, line):
        try:
            ev = json.loads(line)
        except ValueError:
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("type")
        if kind == "thread.started":
            self.thread = ev.get("thread_id")
        elif kind == "turn.completed":
            self.done = True
        elif kind in ("turn.failed", "error"):
            err = ev.get("error") if kind == "turn.failed" else ev
            self.failure = str((err if isinstance(err, dict) else {}).get("message") or kind)
        if kind not in ("item.started", "item.completed"):
            return []
        it = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if it.get("type") == "agent_message" and kind == "item.completed":
            self.text = str(it.get("text") or "")
        if it.get("id") in self.seen:
            return []
        steps = self.steps(it)
        if steps:
            self.seen.add(it.get("id"))
        return steps

    @staticmethod
    def steps(it):
        t = it.get("type")
        if t == "command_execution":
            return [("Bash", {"command": str(it.get("command") or "")})]
        if t == "file_change":
            return [("Edit", {"file_path": str(c.get("path") or "")}) for c in it.get("changes") or [] if isinstance(c, dict)]
        if t == "web_search":
            return [("WebSearch", {"query": str(it.get("query") or "")})]
        if t == "mcp_tool_call":
            return [("mcp__%s__%s" % (it.get("server"), it.get("tool")), {})]
        return []

    def result(self, returncode, stderr, raw):
        """(成功したか, 最後の応答, セッションの ID, 失敗の文)"""
        if returncode != 0 or self.failure or not self.done:
            detail = self.failure or stderr.strip()[-800:] or raw.strip()[-800:] or "終了コード %d" % returncode
            return False, "", None, "codex が失敗した: %s" % detail
        return True, self.text or "", self.thread, None


class CodexAgent(Agent):
    key, label = "codex", "Codex"
    session_env, account_env, home_dir = "CODEX_THREAD_ID", "CODEX_HOME", "~/.codex"
    ask_tool, write_hint = "request_user_input", "apply_patch"
    # 親の Codex セッションから引き継がない環境変数（子は自分のセッションの ID を受け取る）
    parent_env = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")

    def rollout(self, session_id, home):
        """会話の記録のファイル（sessions/YYYY/MM/DD/rollout-<時刻>-<ID>.jsonl）。"""
        if not SESSION_ID_RE.match(session_id or ""):
            return None
        found = glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % session_id))
        return found[0] if found else None

    def thread_row(self, session_id, home):
        """state_5.sqlite の threads の行（最初の依頼）。無ければ None。"""
        path = os.path.join(home, "state_5.sqlite")
        if not SESSION_ID_RE.match(session_id or "") or not os.path.isfile(path):
            return None
        try:
            db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
            try:
                return db.execute("select first_user_message from threads where id = ?", (session_id,)).fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return None

    def session_exists(self, session_id, home):
        return bool(self.thread_row(session_id, home) or self.rollout(session_id, home))

    def session_info(self, session_id, home, transcript_path=None):
        home = home or self.home()
        row = self.thread_row(session_id, home)
        text = row[0] if row and row[0] else self.rollout_prompt(self.rollout(session_id, home) or transcript_path)
        text = re.sub(r"\s+", " ", text or "").strip()
        return {"prompt": (text[:120] + ("…" if len(text) > 120 else "")) if text else None}

    @staticmethod
    def rollout_prompt(path, lines=2000):
        if not isinstance(path, str) or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(lines), f):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    p = ev.get("payload") if isinstance(ev, dict) and ev.get("type") == "event_msg" else None
                    it = p.get("item") if isinstance(p, dict) and p.get("type") == "item_completed" else None
                    if isinstance(it, dict) and it.get("type") == "UserMessage":
                        return " ".join(c.get("text", "") for c in it.get("content") or [] if isinstance(c, dict))
        except OSError:
            return None
        return None

    def list_mcp(self, cwd, env, timeout=MCP_LIST_TIMEOUT):
        """codex に設定された、有効な MCP サーバーの名前。調べられなければ None。-c の dotted key に書けない名前は
        扱わない（そのサーバーは利用者の設定のまま動く）。"""
        out = run_listing(self.argv + ["mcp", "list", "--json"], cwd, env, timeout)
        try:
            servers = json.loads(out) if out is not None else None
        except ValueError:
            return None
        if not isinstance(servers, list):
            return None
        return [s["name"] for s in servers
                if isinstance(s, dict) and s.get("enabled") and SAFE_KEY_RE.match(str(s.get("name")))]

    def external_label(self, mcp_names):
        return "Web の検索（web_search）、MCP のサーバー（%s）" % ("、".join(mcp_names) if mcp_names else "無し")

    def sandbox_works(self, env):
        """このマシンで Codex のサンドボックスが動くか（AppArmor が bwrap を止める環境では動かない）。"""
        try:
            r = subprocess.run(self.argv + ["sandbox", "--", "true"], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0

    def command(self, run):
        cmd = self.argv + ["exec", "--json", "--skip-git-repo-check"]
        if run.model:
            cmd += ["-m", run.model]
        cmd += ["-c", "web_search=%s" % toml_str("disabled" if run.local_only else "live")]
        for name in run.mcp_names:
            cmd += (["-c", "mcp_servers.%s.enabled=false" % name] if run.local_only else
                    ["-c", "mcp_servers.%s.default_tools_approval_mode=%s" % (name, toml_str("approve"))])
        if run.sandbox:
            # 読み取り専用を基にし、資料フォルダだけ書けるようにする（その中で書いてよい所はガードが絞る）
            cmd += ["-c", "permissions.visual_explain.extends=%s" % toml_str(":read-only"),
                    "-c", "permissions.visual_explain.filesystem={%s = %s}" % (toml_str(run.material_dir), toml_str("write")),
                    "-c", "default_permissions=%s" % toml_str("visual_explain")]
        else:
            cmd += ["-s", "danger-full-access"]  # サンドボックスが動かない。ガードだけで絞る
        # 子の codex だけに、すべてのツールを許可リストで絞るガードの hook を付ける。利用者の設定で hooks を
        # 切っていても効くよう有効にし、ガードの外で動きうる機能は切る（-c で付けた hook は信頼されていないので、
        # 信頼の確認を飛ばす）
        guard = "%s -I %s --policy %s" % (shlex.quote(sys.executable), shlex.quote(GUARD), shlex.quote(run.guard_policy))
        cmd += ["--enable", "hooks", "--dangerously-bypass-hook-trust", "-c",
                'hooks.PreToolUse=[{matcher = "*", hooks = [{type = "command", command = %s, timeout = 30}]}]'
                % toml_str(guard)]
        for feature in CODEX_DISABLED_FEATURES:
            cmd += ["--disable", feature]
        if run.mode in ("fork", "resume"):
            cmd += [run.mode, run.session_id]
        return cmd + ["-"]  # 指示文は標準入力から読ませる

    def new_session_id(self):
        return None  # codex exec が作る（thread.started で受け取る）

    def reader(self):
        return CodexReader()


class OpenCodeReader:
    """opencode run --format json の出力（1 行 1 イベント）を読む。ツールの名前と入力を Claude・Codex と
    同じ「ツール名と入力」の形に直して途中経過にする（途中経過の文は serve.py が共通で作る）。"""

    # opencode のツール名 → 途中経過を作る serve.py が知っている名前
    TOOLS = {"read": "Read", "grep": "Grep", "glob": "Glob", "write": "Write", "edit": "Edit", "patch": "Edit",
             "shell": "Bash", "webfetch": "WebFetch", "websearch": "WebSearch", "skill": "Skill",
             "task": "Task", "subagent": "Task"}

    def __init__(self):
        self.session, self.text, self.failure, self.seen = None, None, None, set()

    def feed(self, line):
        try:
            ev = json.loads(line)
        except ValueError:
            return []
        if not isinstance(ev, dict):
            return []
        sid = ev.get("sessionID")
        if isinstance(sid, str) and sid:
            self.session = sid
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        kind = ev.get("type")
        if kind == "text":
            text = part.get("text")
            if isinstance(text, str):
                self.text = text  # 最後の応答（途中の説明は、後ろの応答で上書きされる）
        elif kind == "error":
            err = ev.get("error")
            self.failure = str((err if isinstance(err, dict) else {}).get("message") or err or "エラー")
        if kind != "tool_use":
            return []
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        key = (part.get("id") or part.get("partID"), state.get("status"))
        if key[0] is None or key in self.seen:
            return []
        self.seen.add(key)
        name = self.TOOLS.get(str(part.get("tool") or ""))
        if not name:
            return []
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        return [(name, inp)]

    def result(self, returncode, stderr, raw):
        """(成功したか, 最後の応答, セッションの ID, 失敗の文)"""
        if returncode != 0 or self.failure:
            detail = self.failure or stderr.strip()[-800:] or raw.strip()[-800:] or "終了コード %d" % returncode
            return False, "", None, "opencode が失敗した: %s" % detail
        if not self.text:
            return False, "", None, "opencode が失敗した: 応答が無かった"
        return True, self.text, self.session, None


class OpenCodeAgent(Agent):
    key, label = "opencode", "OpenCode"
    session_env, account_env, home_dir = "OPENCODE_SESSION_ID", "OPENCODE_CONFIG_DIR", "~/.config/opencode"
    ask_tool, write_hint = "question", "write ツール"
    # 親の opencode セッションから引き継がない環境変数（入れ子のセッションと誤認させない）
    parent_env = ("OPENCODE", "OPENCODE_SESSION_ID", "OPENCODE_TERMINAL", "AI_AGENT")

    def data_home(self):
        """会話と資格情報が入っているフォルダ（設定フォルダとは別。XDG_DATA_HOME/opencode）。"""
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(base, "opencode")

    def db_paths(self):
        """会話が入っている SQLite のファイル。版ごとに名前が変わるので、あるものを全部見る。"""
        override = os.environ.get("OPENCODE_DB")
        if override == ":memory:":
            return []
        if override:
            return [override if os.path.isabs(override) else os.path.join(self.data_home(), override)]
        return sorted(glob.glob(os.path.join(self.data_home(), "opencode*.db")))

    def session_row(self, session_id):
        """会話の 1 行（題名・作業ディレクトリ）。無ければ None。"""
        if not SESSION_ID_RE.match(session_id or ""):
            return None
        for path in self.db_paths():
            if not os.path.isfile(path):
                continue
            try:
                db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
                try:
                    return db.execute("select title, directory from session_v2 where id = ?", (session_id,)).fetchone()
                finally:
                    db.close()
            except sqlite3.Error:  # 別の版の形・読み取り中のデータベース
                continue
        return None

    def session_exists(self, session_id, home):
        if not SESSION_ID_RE.match(session_id or ""):
            return False
        if self.session_row(session_id) is not None:
            return True
        # 会話の記録が見つからない（別のデータフォルダ等）ときは、分岐できるものとして扱う。分けられなければ
        # serve.py が新しいセッションでやり直す
        return not any(os.path.isfile(path) for path in self.db_paths())

    def first_prompt(self, session_id, limit=5):
        """会話の最初の依頼（session_message の 1 つ目の user）。"""
        if not SESSION_ID_RE.match(session_id or ""):
            return None
        for path in self.db_paths():
            if not os.path.isfile(path):
                continue
            try:
                db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
                try:
                    rows = db.execute("select data from session_message where session_id = ? and type = 'user'"
                                      " order by seq limit ?", (session_id, limit)).fetchall()
                finally:
                    db.close()
            except sqlite3.Error:
                continue
            for (data,) in rows:
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                text = msg.get("text") if isinstance(msg, dict) else None
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return None

    def session_info(self, session_id, home, transcript_path=None):
        row = self.session_row(session_id)
        if row is None:
            return {"prompt": None}
        text = re.sub(r"\s+", " ", self.first_prompt(session_id) or row[0] or "").strip()
        return {"prompt": (text[:120] + ("…" if len(text) > 120 else "")) if text else None}

    def list_mcp(self, cwd, env, timeout=MCP_LIST_TIMEOUT):
        """子の opencode では MCP のツールを許さない（サーバーごとの名前を、権限に使える形では取れないため）。
        外部の情報源には Web だけを出す。"""
        return []

    def external_label(self, mcp_names):
        return "Web の検索（websearch）と取得（webfetch）"

    @staticmethod
    def shell_rules(argvs):
        """子が動かしてよい検査のコマンド。opencode は「区切りの無い文字列」で照合するので、素の形と引用した形の
        両方を、後ろに何が付いても一致するように（* を付けて）許す。検査は python3 -I <検査> で始まるものだけ、
        先頭 3 つまでを許す（後ろの引数は自由）。"""
        rules = []
        for argv in argvs:
            if not argv:
                continue
            head = argv[:3] if argv[:2] == ["python3", "-I"] else argv
            for pattern in (" ".join(head) + "*", shlex.join(head) + "*"):
                rule = {"action": "shell", "resource": pattern, "effect": "allow"}
                if rule not in rules:
                    rules.append(rule)
        return rules

    @staticmethod
    def path_rules(paths, cwd):
        """書いてよいパス（子の作業ディレクトリから見た相対の形と、絶対の形の両方を許す）。"""
        rules = []
        for path in paths:
            path = os.path.abspath(path)
            forms = {path, os.path.realpath(path)}
            rel = os.path.relpath(path, cwd)
            if not rel.startswith(".."):
                forms.add(os.path.normpath(rel))
            for form in sorted(forms):
                rule = {"action": "edit", "resource": form, "effect": "allow"}
                if rule not in rules:
                    rules.append(rule)
        return rules

    def child_config(self, run):
        """子の opencode に渡す設定。この名前のエージェントを定義し、ツールの権限を許可リストに絞る。
        既定ですべて断り、後ろに書いたものだけを許す（最後に一致したものが効く）。"""
        rules = [{"action": "*", "resource": "*", "effect": "deny"},
                 {"action": "read", "resource": "*", "effect": "allow"},
                 {"action": "glob", "resource": "*", "effect": "allow"},
                 {"action": "grep", "resource": "*", "effect": "allow"},
                 {"action": "external_directory", "resource": "*", "effect": "allow"}]
        rules += self.path_rules(run.write_files, run.cwd)
        rules += self.path_rules([d.rstrip("/") + "/*" for d in run.write_dirs], run.cwd)
        rules += self.shell_rules(run.check_argvs)
        if not run.local_only:
            rules += [{"action": "webfetch", "resource": "*", "effect": "allow"},
                      {"action": "websearch", "resource": "*", "effect": "allow"}]
        return {"agents": {OPENCODE_CHILD_AGENT: {"description": "visual-explain の資料を作る子（配信サーバーが起動する）",
                                                  "system": OPENCODE_CHILD_SYSTEM, "permissions": rules}}}

    def run_env(self, run):
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(self.child_config(run), ensure_ascii=False)}

    def command(self, run):
        # 子だけの設定（エージェントと権限）を効かせるため、自分のサーバーで動かす（--standalone）。
        # 設定は OPENCODE_CONFIG_CONTENT（run_env）で渡す。指示文は標準入力から読ませる
        cmd = self.argv + ["run", "--standalone", "--format", "json", "--agent", OPENCODE_CHILD_AGENT]
        if run.model:
            cmd += ["--model", run.model]
        cmd += {"new": [], "fork": ["--session", run.session_id, "--fork"],
                "resume": ["--session", run.session_id]}[run.mode]
        return cmd

    def new_session_id(self):
        return None  # opencode run が作る（イベントの sessionID で受け取る）

    def reader(self):
        return OpenCodeReader()


AGENTS = {"claude": ClaudeAgent, "codex": CodexAgent, "opencode": OpenCodeAgent}


def detect_session_agent(env):
    """このプロセスを動かしているエージェントのセッション（serve.py origin が、資料を作ったセッションを記録するため）。"""
    for key, cls in AGENTS.items():
        if env.get(cls.session_env):
            return key, env[cls.session_env]
    return None, None
