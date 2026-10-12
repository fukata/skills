# visual-explain 公開版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** zaicodev 版 visual-explain 0.18.1 を fukata/skills の `skills/visual-explain/` に移し、Claude Code と Codex CLI の両方で資料作り・配信・ページからの質問・受け箱まで動くようにする。

**Architecture:** zaicodev 版のコードをほぼそのまま移し、エージェントごとに違う所（子プロセスのコマンド・出力の読み取り・セッション情報・hook の入出力）を `scripts/agents.py` の `ClaudeAgent` / `CodexAgent` に閉じ込める。Codex の子プロセスは、サンドボックスが動けば permission profile、動かなければ `scripts/codex_guard.py` の PreToolUse hook で絞る。hook は plugin ではなく `setup.sh` が Claude の `settings.json` と Codex の `hooks.json` に登録する。

**Tech Stack:** Python 3 標準ライブラリ（`sqlite3` を含む）、bash、unittest、既存のヘッドレス Chrome による検査（任意）。

**Spec:** `docs/superpowers/specs/2026-10-12-visual-explain-design.md`

**分岐元:** `~/dev/src/github.com/zaicodev/claude-code-plugins/plugins/visual-explain`（0.18.1、commit `c7c7c99`）。以下 `$SRC` と書く。移植先は `skills/visual-explain`（以下 `$DST`）。

## Global Constraints

- Python 標準ライブラリだけで動く（serve.py・ask_hook.py・agents.py・codex_guard.py・check-explanation.py）。setup.sh / teardown.sh も JSON の読み書きは python3 で行い、jq を使わない。
- 文言・コメント・ドキュメントは日本語。資料の本文は従来どおりユーザーの言語。
- データの置き場: `${VISUAL_EXPLAIN_HOME:-${XDG_STATE_HOME:-~/.local/state}/visual-explain}`（`VISUAL_EXPLAIN_ASK_HOME` も読む）。
- 受け箱の配信サーバーの既定ポートは 8770。
- hook の timeout は PreToolUse 7200、SessionStart 30。statusMessage は「資料のページで答えを待っています（受け箱から開けます）」。
- Codex の SessionStart の hook は標準出力に何も書かない。
- Codex の `config.toml` の `trusted_hash` を書かない。機能フラグを変えない。
- 移植後、`$DST` に `zaico`・`zaicodev`・`拠点`・`在庫`・`PLG26`・`mackerel` が残らない（`$DST/README.md` の分岐元の記載だけ `zaicodev` を許す）。
- push・公開はしない（ローカルのコミットまで）。
- 既存のコードの書き方（日本語の docstring とコメント、短い関数、`read_json`/`write_json` の使い方）に合わせる。

## Review Focus

1. **Codex の hook を信頼していない**: setup.sh の後に `/hooks` で信頼しないと、Codex の質問は黙ってターミナルに出る。setup.sh の出力に信頼の手順が必ず出ること（Task 6 のテスト）。
2. **受け箱の配信サーバーが codex を見つけられない**: 配信サーバーを起動したのが Claude のセッションで PATH に codex が無いとき、Codex の質問の資料作りは「作れない」と出して選択肢だけのページで答えられること（Task 5 のテスト）。
3. **Codex の質問の文が重複する**: `request_user_input` の質問は id で区別されるので、同じ文の質問が来ることがある。登録を断られたら hook は何も出さずに終わり、ターミナルで答えられること（Task 5 のテスト）。
4. **ガードのシェルの文字列の扱い**: 引用符の中の `|` や `>` は区切り・リダイレクトとみなさず、引用符の外の `>` は拒むこと（Task 3 のテスト）。
5. **zaicodev 版の配信サーバーが 8770 を使ったまま**: 移行した人の環境では、古い配信サーバーが同じポートで動いていることがある。`ask-config` が「別の配信サーバーが使っている」と出すこと（移植する既存のテスト `test_does_not_fight_another_server_for_the_port` が守る。Task 1 で通ることを確かめる）。

---

### Task 1: 移植の土台（コピー・パス・版・データの置き場・zaico 由来のテスト文字列）

**Files:**
- Create: `$DST/SKILL.md`, `$DST/assets/**`, `$DST/references/**`, `$DST/scripts/{serve.py,ask_hook.py,check-explanation.py}`, `$DST/tests/**`（すべて `$SRC` からコピー）
- Modify: `$DST/scripts/serve.py`（VERSION、`ask_home`）、`$DST/scripts/ask_hook.py`（`config_path`）、`$DST/tests/run.sh`・`$DST/tests/test_serve.py`（パス、zaico 由来の文字列）、`$DST/scripts/check-explanation.py`（docstring の例）、`$DST/tests/fixtures/fake_claude.py`（MCP 名の例）

**Interfaces:**
- Produces: `serve.ask_home() -> str`（優先順: `VISUAL_EXPLAIN_HOME` → `VISUAL_EXPLAIN_ASK_HOME` → `$XDG_STATE_HOME/visual-explain` → `~/.local/state/visual-explain`）、`serve.VERSION = "1.0.0"`

- [ ] **Step 1: コピーする**

```bash
DST=skills/visual-explain; SRC=~/dev/src/github.com/zaicodev/claude-code-plugins/plugins/visual-explain
mkdir -p $DST/tests
cp -R $SRC/skills/visual-explain/{SKILL.md,assets,references,scripts} $DST/
cp -R $SRC/tests/{run.sh,test_serve.py,fixtures} $DST/tests/
find $DST -name __pycache__ -prune -exec rm -rf {} +
```

- [ ] **Step 2: テストのパスを新しい配置に直す**

`$DST/tests/test_serve.py`:
```python
SCRIPTS = os.path.join(HERE, "..", "scripts")
```
`$DST/tests/run.sh`: `$HERE/../skills/visual-explain/` を `$HERE/../` に置き換える（`C=`、雛形の検査 2 か所、`check-explanation.py` の読み込み 1 か所）。usage のコメントを `bash skills/visual-explain/tests/run.sh` にする。

- [ ] **Step 3: データの置き場のテストを書く**（`test_serve.py` の `AskDefaultsTest` に足す）

```python
    def test_home_follows_the_state_directory(self):
        env = {k: v for k, v in os.environ.items() if k not in ("VISUAL_EXPLAIN_HOME", "VISUAL_EXPLAIN_ASK_HOME", "XDG_STATE_HOME")}
        code = "import importlib.util,sys;s=importlib.util.spec_from_file_location('s',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.ask_home())"
        run = lambda **e: subprocess.run([sys.executable, "-I", "-c", code, os.path.join(SCRIPTS, "serve.py")], capture_output=True,
                                         text=True, env=dict(env, HOME=self.tmp, **e)).stdout.strip()
        self.assertEqual(run(), os.path.join(self.tmp, ".local", "state", "visual-explain"))
        self.assertEqual(run(XDG_STATE_HOME="/x/state"), "/x/state/visual-explain")
        self.assertEqual(run(VISUAL_EXPLAIN_ASK_HOME="/y"), "/y")
        self.assertEqual(run(VISUAL_EXPLAIN_HOME="/z", VISUAL_EXPLAIN_ASK_HOME="/y"), "/z")
```

- [ ] **Step 4: 失敗を確かめる**

Run: `python3 -I skills/visual-explain/tests/test_serve.py AskDefaultsTest.test_home_follows_the_state_directory`
Expected: FAIL（`~/.claude/visual-explain` が返る）

- [ ] **Step 5: 実装する**

`serve.py` の `ask_home`:
```python
def ask_home():
    """設定と常駐の配信サーバーの記録を置くフォルダ。エージェントやアカウントに依らず 1 つにし、どのセッションも
    同じ配信サーバーと受け箱を使う。VISUAL_EXPLAIN_HOME（テストでは VISUAL_EXPLAIN_ASK_HOME も）で差し替える。"""
    for name in ("VISUAL_EXPLAIN_HOME", "VISUAL_EXPLAIN_ASK_HOME"):
        if os.environ.get(name):
            return os.environ[name]
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "visual-explain")
```
`ask_hook.py` の `config_path` も同じ規則にする（serve を読み込まずに使うため、同じ 4 行を `home()` として置き、`config_path()` は `os.path.join(home(), "ask.json")`。例外時のログの置き場も `home()`）。

`serve.py` の VERSION を plugin.json から読む部分（`try: with open(... ".claude-plugin", "plugin.json") ...`）を消し、次にする:
```python
VERSION = "1.0.0"  # 常駐の配信サーバーはこれより古い版なら起動し直す。serve.py・ask_hook.py・assets を変えたら上げる
```
`ASK_CONFIG` のコメントを `# <ask_home()>/ask.json` にする。

- [ ] **Step 6: zaico 由来の文字列を一般のものにする**

- `check-explanation.py` の docstring・コメントの `PLG26-33` → `ABC-123`（正規表現は変えない）。
- `tests/run.sh` のコメントの `PLG26-33` → `ABC-123`。
- `tests/fixtures/fake_claude.py` の `MCP_LIST` の `mackerel: npx -y @mackerel/mcp-server - ! Needs authentication` → `metrics: npx -y @example/metrics-mcp - ! Needs authentication`。`test_serve.py` の `mackerel` をすべて `metrics` にする。
- `test_serve.py` の `在庫の一覧に\n絞り込みを足して` → `一覧に\n絞り込みを足して`、期待値 `在庫の一覧に 絞り込みを足して` → `一覧に 絞り込みを足して`。

```bash
grep -rn -E 'zaico|拠点|在庫|PLG26|mackerel' skills/visual-explain/scripts skills/visual-explain/tests
```
Expected: 出力なし（SKILL.md・references は Task 8 で直す）

- [ ] **Step 7: 全テストを通す**

Run: `bash skills/visual-explain/tests/run.sh`
Expected: `ALL PASSED`（`test_does_not_fight_another_server_for_the_port` を含む）

- [ ] **Step 8: コミット**

```bash
git add skills/visual-explain
git commit -m "feat: visual-explain を zaicodev 版 0.18.1 から移す"
```

---

### Task 2: アダプタの導入（`agents.py` と `ClaudeAgent`。serve.py は振る舞いを変えない）

**Files:**
- Create: `$DST/scripts/agents.py`, `$DST/tests/test_agents.py`
- Modify: `$DST/scripts/serve.py`（`list_mcp_servers`、`first_prompt`、`PARENT_ENV`、`App.detect_claude`、`transcript_exists`、`claude_env`、`account_home`、`session_plan`、`new_plan`、`ask_claude`、`allowed_tools`、`external_sources`、`exec_claude`、`on_event`、`register_ask` を、アダプタを呼ぶ形にする）、`$DST/tests/run.sh`（test_agents.py を実行）

**Interfaces:**
- Produces（`agents.py`）:

```python
@dataclass
class Run:
    mode: str                 # "new" | "fork" | "resume"
    session_id: str | None    # fork/resume は元のセッション、Claude の new は新しく作った ID、Codex の new は None
    cwd: str
    material_dir: str
    write_files: list         # 作業用コピー・回答ファイル（絶対パス）
    write_dirs: list          # images/（絶対パス）
    check_argvs: list         # 検査コマンド（argv の配列）の配列
    model: str = None
    local_only: bool = False
    mcp_names: list = field(default_factory=list)
    sandbox: bool = True      # Codex: サンドボックスが動くか（False ならガードの hook で絞る）
    guard_policy: str = None  # Codex: ガードの方針を書いた JSON のパス（sandbox が False のとき）

class Agent:
    key = ""; label = ""; session_env = ""; account_env = ""; parent_env = (); ask_tool = ""; write_hint = ""
    def __init__(self, argv): self.argv = list(argv)
    def available(self) -> bool
    def default_home(self) -> str
    def home(self, config_dir=None) -> str          # config_dir か、環境変数 account_env か、default_home
    def child_env(self, base, config_dir=None, keep_account=True) -> dict  # parent_env を除き、account_env を config_dir にする
    def new_session_id(self) -> str | None            # Claude は新しい UUID、Codex は None（thread.started で受け取る）
    def session_exists(self, session_id, home) -> bool
    def session_info(self, session_id, home, transcript_path=None) -> dict  # {"prompt": str|None}
    def list_mcp(self, cwd, env, timeout=60) -> list | None
    def external_label(self, mcp_names) -> str
    def command(self, run: Run) -> list
    def reader(self) -> "Reader"

class Reader:
    def feed(self, line: str) -> list       # 途中経過 [(ツール名, 入力 dict), ...]
    def result(self, returncode: int, stderr: str, raw: str) -> tuple  # (ok, text, session_id, error)

AGENTS = {"claude": ClaudeAgent, "codex": CodexAgent}  # CodexAgent は Task 4 で足す
def detect_session_agent(env) -> tuple  # (agent の key, セッション ID) または (None, None)
```

- [ ] **Step 1: ClaudeAgent のテストを書く**（`$DST/tests/test_agents.py`）

```python
#!/usr/bin/env python3
"""agents.py（Claude Code と Codex の違いを吸収するアダプタ）のテスト。
  usage: python3 -I skills/visual-explain/tests/test_agents.py
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agents = load("agents", os.path.join(SCRIPTS, "agents.py"))


def run(**kw):
    base = dict(mode="new", session_id="11111111-2222-3333-4444-555555555555", cwd="/repo", material_dir="/out/topic",
                write_files=["/out/topic/explanation.qa-draft.html", "/out/topic/qa/answers/q1.html"],
                write_dirs=["/out/topic/images"], check_argvs=[["python3", "-I", "/s/check.py", "/out/topic/x.html"]])
    base.update(kw)
    return agents.Run(**base)


class ClaudeAgentTest(unittest.TestCase):
    def setUp(self):
        self.a = agents.ClaudeAgent(["claude"])

    def test_new_fork_resume(self):
        new = self.a.command(run())
        self.assertEqual(new[:7], ["claude", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk"])
        self.assertIn("--session-id", new)
        fork = self.a.command(run(mode="fork", session_id="origin"))
        self.assertEqual(fork[fork.index("--resume") + 1], "origin")
        self.assertIn("--fork-session", fork)
        resume = self.a.command(run(mode="resume", session_id="s2"))
        self.assertNotIn("--fork-session", resume)

    def test_allows_only_reads_writes_checks_web_and_mcp(self):
        cmd = self.a.command(run(mcp_names=["claude_ai_Gmail"]))
        rules = cmd[cmd.index("--allowedTools") + 1:]
        for rule in ("Read", "Grep", "Glob", "Edit(//out/topic/explanation.qa-draft.html)", "Edit(//out/topic/images/**)",
                     "Bash(python3 -I /s/check.py /out/topic/x.html:*)", "WebSearch", "mcp__claude_ai_Gmail"):
            self.assertIn(rule, rules)
        local = self.a.command(run(mcp_names=["claude_ai_Gmail"], local_only=True))
        self.assertNotIn("WebSearch", local)
        self.assertNotIn("mcp__claude_ai_Gmail", local)

    def test_add_dir_when_material_is_outside_cwd(self):
        self.assertIn("--add-dir", self.a.command(run(cwd="/repo")))
        self.assertNotIn("--add-dir", self.a.command(run(cwd="/out")))

    def test_reader_steps_and_result(self):
        r = self.a.reader()
        steps = r.feed(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}}))
        self.assertEqual(steps, [("Read", {"file_path": "/x"})])
        r.feed(json.dumps({"type": "result", "is_error": False, "session_id": "s9", "result": "要約"}))
        self.assertEqual(r.result(0, "", ""), (True, "要約", "s9", None))
        bad = self.a.reader()
        bad.feed(json.dumps({"type": "result", "is_error": True, "result": "Prompt is too long"}))
        ok, _, _, error = bad.result(1, "", "")
        self.assertFalse(ok)
        self.assertIn("Prompt is too long", error)

    def test_session_exists_and_first_prompt(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        path = os.path.join(home, "projects", "-repo", "abc.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "<command>x</command>"}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "一覧を\n直して"}]}}) + "\n")
        self.assertTrue(self.a.session_exists("abc", home))
        self.assertFalse(self.a.session_exists("nope", home))
        self.assertFalse(self.a.session_exists("../x", home))
        self.assertEqual(self.a.session_info("abc", home, path), {"prompt": "一覧を 直して"})

    def test_child_env_switches_the_account(self):
        env = self.a.child_env({"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/a", "PATH": "/bin"}, "/b")
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/b", "PATH": "/bin"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.a.child_env({"CLAUDE_CONFIG_DIR": "/a"}, None, keep_account=False))


class DetectTest(unittest.TestCase):
    def test_detects_the_running_agent(self):
        self.assertEqual(agents.detect_session_agent({"CLAUDE_CODE_SESSION_ID": "c1"}), ("claude", "c1"))
        self.assertEqual(agents.detect_session_agent({"CODEX_THREAD_ID": "x1"}), ("codex", "x1"))
        self.assertEqual(agents.detect_session_agent({}), (None, None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗を確かめる**

Run: `python3 -I skills/visual-explain/tests/test_agents.py`
Expected: FAIL（`agents.py` が無い）

- [ ] **Step 3: `agents.py` を書く**（serve.py の該当コードを移す。振る舞いは変えない）

```python
#!/usr/bin/env python3
"""エージェント（Claude Code・Codex）ごとに違う所をまとめる。serve.py と ask_hook.py はこれを通して呼ぶ。

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
import subprocess
import uuid
from dataclasses import dataclass, field

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
MCP_LIST_TIMEOUT = 60
# 読み取りは専用のツールだけにする。git のルールは --output= や --open-files-in-pager= で書き込み・実行ができてしまう
READ_TOOLS = ["Read", "Grep", "Glob"]
# 調べものの手分け・スキル・コードの定義の検索。書き込みとコマンドは、手分けした先でも許したものしかできない
HELPER_TOOLS = ["Task", "Agent", "Skill", "LSP"]
# 資料とリポジトリの外を調べるツール（--local-only では許さない）。MCP はこのほかにサーバーごとに mcp__<名前> で許す
EXTERNAL_TOOLS = ["WebSearch", "WebFetch", "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool"]
# claude mcp list の行（「名前: 接続先 - 状態」）。名前に「:」を含むもの（plugin:slack:slack）もある
MCP_LINE_RE = re.compile(r"^(\S.*?): \S.* - \S")


def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def run_listing(argv, cwd, env, timeout):
    """一覧のコマンドを動かし、標準出力を返す。時間切れ・失敗は None。接続の確認で起動したサーバーは孫ごと止める。"""
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
    mode: str
    session_id: str
    cwd: str
    material_dir: str
    write_files: list
    write_dirs: list
    check_argvs: list
    model: str = None
    local_only: bool = False
    mcp_names: list = field(default_factory=list)
    sandbox: bool = True
    guard_policy: str = None


class Agent:
    key = label = session_env = account_env = ask_tool = write_hint = ""
    parent_env = ()
    home_dir = ""

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
        config_dir が None なら、keep_account のときは base のものを残し、そうでなければ外す（既定のアカウント）。"""
        env = {k: v for k, v in base.items() if k not in self.parent_env}
        if config_dir:
            env[self.account_env] = config_dir
        elif not keep_account:
            env.pop(self.account_env, None)
        return env


class ClaudeReader:
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
                steps.append((str(block.get("name") or ""), block.get("input") if isinstance(block.get("input"), dict) else {}))
        return steps

    def result(self, returncode, stderr, raw):
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
        """（serve.py の first_prompt をそのまま移す）"""

    def list_mcp(self, cwd, env, timeout=MCP_LIST_TIMEOUT):
        """（serve.py の list_mcp_servers の解析部分を移す。run_listing(self.argv + ["mcp", "list"], ...) を使う）"""

    def external_label(self, mcp_names):
        return "Web の検索（WebSearch）と取得（WebFetch）、MCP のサーバー（%s）" % ("、".join(mcp_names) if mcp_names else "無し")

    def command(self, run):
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


AGENTS = {"claude": ClaudeAgent}


def detect_session_agent(env):
    """このプロセスを動かしているエージェントのセッション（serve.py origin が、資料を作ったセッションを記録するため）。"""
    for key, cls in AGENTS.items():
        if env.get(cls.session_env):
            return key, env[cls.session_env]
    return None, None
```

`first_prompt` と `list_mcp` の本体は serve.py の `first_prompt`・`list_mcp_servers` をそのまま移す（`list_mcp` は `out = run_listing(self.argv + ["mcp", "list"], cwd, env, timeout)`、`None` なら `None`、あとは今の名前の正規化）。

- [ ] **Step 4: serve.py をアダプタに切り替える**

- `import agents as agents_mod`（`sys.path.insert(0, SCRIPT_DIR)` の後）。`READ_TOOLS`・`HELPER_TOOLS`・`EXTERNAL_TOOLS`・`MCP_LINE_RE`・`list_mcp_servers`・`first_prompt` を serve.py から消す。`PARENT_ENV` は `ensure_ask_server` が使うので、`PARENT_ENV = agents_mod.ClaudeAgent.parent_env`（Task 4 で Codex 分を足す）。
- `App.__init__`: `self.agents = {"claude": agents_mod.ClaudeAgent(cfg.claude)}`。`detect_claude` は `self.agents["claude"].available()` を使い、文言は現行のまま。
- `transcript_exists(sid, home)` → `self.agent_for(m).session_exists(sid, home)`。`agent_for(m)` は当面 `self.agents["claude"]` を返す。
- `claude_env(m)` → `agent.child_env(os.environ, o.get("config_dir"), keep_account=o is None)`（受け箱の質問の資料なら質問したアカウント、そうでなければ配信サーバーの環境のまま）。
- `mcp_servers` の `list_mcp_servers(self.cfg.claude, cwd, env=env)` → `self.agent_for(m).list_mcp(cwd, env)`。
- `exec_claude`: コマンドは `agent.command(agents_mod.Run(mode=plan.mode, session_id=plan.session_id, cwd=plan.cwd, material_dir=m.dir, write_files=[m.draft, m.answer_path(item["id"])], write_dirs=[m.images_dir], check_argvs=[a for a in (self.check_argv(m, m.draft), self.answer_check_argv(m, m.answer_path(item["id"]))) if a], model=self.cfg.model, local_only=self.cfg.local_only, mcp_names=[] if self.cfg.local_only else self.mcp_servers(plan.cwd, m)))`。読み取りは `reader = agent.reader()`、`read_line` で `for name, inp in reader.feed(line): step = self.step_label(...)`、終了後は `ok, text, sid, error = reader.result(proc.returncode, "".join(err), "".join(raw))`。
- `Plan` を `Plan(mode, session_id, cwd, forked_from=None)` にする（`args` をやめる）。`session_plan` / `new_plan` / `ask_claude` はそれに合わせる（new のときは `agent.new_session_id()`。`sid = res.session_id or (plan.session_id if plan.mode != "fork" else None)`）。
- `register_ask` の `first_prompt(body.get("transcript_path"))` → `self.agents["claude"].session_info(sid, None, body.get("transcript_path"))["prompt"]`。
- `allowed_tools` は消す（ClaudeAgent.command に移った）。既存のテストが `self.app.allowed_tools(...)` を使っていれば、`agents_mod.ClaudeAgent(...).command(...)` の `--allowedTools` 以降を見る形に書き換える。
- `external_sources` → `self.agent_for(m).external_label(names)`（`--local-only` の文はそのまま）。

`tests/run.sh` の serve.py のテストの前に追加:
```bash
echo "== アダプタ（agents.py）"
if python3 -I "$HERE/test_agents.py" >"$TMP/agents.log" 2>&1; then
  ok "agents.py のテスト（$(grep -Eo 'Ran [0-9]+ tests' "$TMP/agents.log")）"
else
  ng "agents.py のテスト"; grep -E '^(FAIL|ERROR|Traceback|AssertionError)|Error' "$TMP/agents.log" | head -20
fi
```

- [ ] **Step 5: 全テストを通す**

Run: `python3 -I skills/visual-explain/tests/test_agents.py && bash skills/visual-explain/tests/run.sh`
Expected: どちらも成功（serve.py の既存のテストは振る舞いを変えていないので通る）

- [ ] **Step 6: コミット**

```bash
git add skills/visual-explain
git commit -m "refactor: エージェントごとの違いを agents.py に分ける"
```

---

### Task 3: Codex のガード（`codex_guard.py`）

**Files:**
- Create: `$DST/scripts/codex_guard.py`, `$DST/tests/test_codex_guard.py`
- Modify: `$DST/tests/run.sh`（実行を足す）

**Interfaces:**
- Produces: `python3 -I codex_guard.py --policy <JSON>`（標準入力に Codex の PreToolUse の payload。許すなら何も出さず exit 0、拒むなら `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`）
- 方針の JSON: `{"write_files": [...], "write_dirs": [...], "commands": [[argv], ...]}`
- 関数: `check_bash(command: str, policy: dict) -> str | None`、`check_patch(patch: str, cwd: str, policy: dict) -> str | None`（拒む理由か None）

- [ ] **Step 1: テストを書く**（`$DST/tests/test_codex_guard.py`）

```python
#!/usr/bin/env python3
"""codex_guard.py（サンドボックスが動かないときに、子の codex のコマンドと書き込みを絞る hook）のテスト。"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "..", "scripts", "codex_guard.py")
spec = importlib.util.spec_from_file_location("g", GUARD)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

POLICY = {"write_files": ["/out/t/explanation.qa-draft.html", "/out/t/qa/answers/q1.html"], "write_dirs": ["/out/t/images"],
          "commands": [["python3", "-I", "/s/check-explanation.py", "/out/t/explanation.qa-draft.html"]]}


class BashTest(unittest.TestCase):
    def allowed(self, cmd):
        self.assertIsNone(g.check_bash(cmd, POLICY), cmd)

    def denied(self, cmd):
        self.assertIsNotNone(g.check_bash(cmd, POLICY), cmd)

    def test_reads(self):
        for cmd in ("cat /out/t/explanation.html", "sed -n '1,80p' app/models/plan.rb", "rg -n 'a|b' app",
                    "grep -rn GRACE_DAYS . | head -20", "find app -name '*.rb'", "ls -la", "wc -l a b", "nl -ba x",
                    "head -c 100 x", "tail -n 5 x", "jq . x.json", "stat x", "rg 'x > y' app", 'grep -n "a b" x'):
            self.allowed(cmd)

    def test_the_exact_check_command(self):
        self.allowed("python3 -I /s/check-explanation.py /out/t/explanation.qa-draft.html")
        self.denied("python3 -I /s/check-explanation.py /etc/passwd")
        self.denied("python3 -c 'import os'")

    def test_writes_and_chains(self):
        for cmd in ("cat a > b", "cat a >> b", "echo x", "rm -rf /", "git log --output=/tmp/x", "sed -i s/a/b/ x",
                    "sed -n 'w /tmp/x' a", "sed -n '1e id' a", "find . -delete", "find . -exec rm {} ;", "cat a; rm b",
                    "cat a && rm b", "cat a || rm b", "cat $(echo a)", "cat `echo a`", "cat a &", "cat <(rm b)",
                    "cat a\nrm b", "bash -c 'cat a'", "env rm a", "awk 'BEGIN{system(\"rm a\")}'", "find . -fprint /tmp/x",
                    "cat a | sh", "rg --pre rm x", "rg --pre=rm x", "sort -o x a", 'cat "$(rm b)"', 'cat "`rm b`"',
                    'cat "$HOME"', "tree -o x", "file -C x", "/tmp/x/cat a", "FOO=1 cat a"):
            self.denied(cmd)

    def test_unparsable_is_denied(self):
        self.denied("cat 'a")


class PatchTest(unittest.TestCase):
    def test_only_the_draft_answer_and_images(self):
        ok = "*** Begin Patch\n*** Update File: /out/t/explanation.qa-draft.html\n@@\n-a\n+b\n*** End Patch\n"
        self.assertIsNone(g.check_patch(ok, "/repo", POLICY))
        img = "*** Begin Patch\n*** Add File: /out/t/images/a.svg\n+<svg/>\n*** End Patch\n"
        self.assertIsNone(g.check_patch(img, "/repo", POLICY))
        for bad in ("*** Begin Patch\n*** Update File: app/models/plan.rb\n@@\n-a\n+b\n*** End Patch\n",
                    "*** Begin Patch\n*** Delete File: /out/t/explanation.html\n*** End Patch\n",
                    "*** Begin Patch\n*** Update File: /out/t/explanation.qa-draft.html\n*** Move to: /out/t/explanation.html\n*** End Patch\n",
                    "*** Begin Patch\n*** Add File: /out/t/images/../x.html\n+x\n*** End Patch\n",
                    "*** Begin Patch\n*** End Patch\n"):
            self.assertIsNotNone(g.check_patch(bad, "/repo", POLICY), bad)

    def test_relative_paths_resolve_from_cwd(self):
        rel = "*** Begin Patch\n*** Update File: explanation.qa-draft.html\n@@\n-a\n+b\n*** End Patch\n"
        self.assertIsNone(g.check_patch(rel, "/out/t", POLICY))
        self.assertIsNotNone(g.check_patch(rel, "/repo", POLICY))


class HookTest(unittest.TestCase):
    def hook(self, payload):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(POLICY, f)
        self.addCleanup(os.remove, f.name)
        return subprocess.run([sys.executable, "-I", GUARD, "--policy", f.name], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=10)

    def test_allows_silently_and_denies_with_a_reason(self):
        ok = self.hook({"tool_name": "Bash", "tool_input": {"command": "cat a"}, "cwd": "/repo"})
        self.assertEqual((ok.returncode, ok.stdout), (0, ""))
        ng = self.hook({"tool_name": "Bash", "tool_input": {"command": "rm a"}, "cwd": "/repo"})
        out = json.loads(ng.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("許されている", out["permissionDecisionReason"])

    def test_patch_payload_and_other_tools(self):
        bad = self.hook({"tool_name": "apply_patch", "cwd": "/repo",
                         "tool_input": {"command": "*** Begin Patch\n*** Add File: x\n+x\n*** End Patch\n"}})
        self.assertEqual(json.loads(bad.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        other = self.hook({"tool_name": "mcp__github__get_issue", "tool_input": {}, "cwd": "/repo"})
        self.assertEqual(other.stdout, "")

    def test_broken_input_is_denied(self):
        res = subprocess.run([sys.executable, "-I", GUARD, "--policy", "/nonexistent"], input="{}", capture_output=True,
                             text=True, timeout=10)
        self.assertEqual(json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗を確かめる**

Run: `python3 -I skills/visual-explain/tests/test_codex_guard.py`
Expected: FAIL（`codex_guard.py` が無い）

- [ ] **Step 3: `codex_guard.py` を書く**

```python
#!/usr/bin/env python3
"""Codex のサンドボックスが動かない環境で、配信サーバーが動かす codex exec を絞る PreToolUse の hook。

  python3 -I codex_guard.py --policy <方針の JSON>
  方針: {"write_files": [...], "write_dirs": [...], "commands": [[argv], ...]}

シェル（Bash）は読み取りのコマンドを | でつないだものと、方針の commands と完全に一致するものだけ許す。
ファイルの書き込み（apply_patch）は write_files と write_dirs の下だけ許す。ほかのツール（MCP など）は通す。
許すときは何も出さずに終わり、拒むときは deny と理由を出す。思わぬ失敗では拒む（閉じる側に倒す）。
シェルの文字列で判定するので、ツール単位で許す Claude Code より弱い。
"""
import json
import os
import re
import shlex
import sys

# 書き込み・実行のオプションを持つもの（tree -o、file -C、sort -o など）は入れない
READERS = {"cat", "head", "tail", "wc", "nl", "ls", "stat", "grep", "rg", "jq", "sed", "find"}
# 引用符の外にあれば拒む記号（リダイレクト・連結・置換・バックグラウンド）。| だけはパイプとして扱う
FORBIDDEN = (">", "<", ";", "&", "`", "$(", "\n", "\r")
FIND_BAD = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
RG_BAD = ("--pre", "--pre-glob")
SED_SCRIPT_BAD = re.compile(r"(^|[;}\s\d/])[wWe](\s|$)|[/;]\s*[wWe]\b")
ALLOWED_TEXT = ("許されているのは、読み取りのコマンド（%s）を | でつないだもの、配信サーバーが指定した検査のコマンド、"
                "作業用コピー・回答ファイル・images/ への書き込みだけ。" % "・".join(sorted(READERS)))


def split_pipeline(command):
    """引用符の外の | で区切る。引用符の外に拒む記号があれば ValueError。"""
    parts, buf, quote, i = [], "", None, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif quote == '"' and c in "$`":
                raise ValueError("二重引用符の中の %s" % c)  # 二重引用符の中でも置換・展開される
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                buf += c
                i += 1
                c = command[i]
            buf += c
        elif c in "'\"":
            quote = c
            buf += c
        elif c == "\\":
            raise ValueError("引用符の外のバックスラッシュ")
        elif c == "|":
            if command[i + 1:i + 2] == "|":
                raise ValueError("||")
            parts.append(buf)
            buf = ""
        elif any(command.startswith(f, i) for f in FORBIDDEN) or c == "$":
            raise ValueError(c)
        else:
            buf += c
        i += 1
    if quote:
        raise ValueError("引用符が閉じていない")
    parts.append(buf)
    return [shlex.split(p) for p in parts]


def check_reader(argv):
    name = argv[0]
    if "/" in name or name not in READERS:  # パスで指定したもの（images/ に置いた同名のファイルなど）は通さない
        return "「%s」は許されていない" % argv[0]
    args = argv[1:]
    if name == "find" and any(a in FIND_BAD for a in args):
        return "find の書き込み・実行のオプションは許されていない"
    if name == "rg" and any(a == b or a.startswith(b + "=") for a in args for b in RG_BAD):
        return "rg の --pre は許されていない"
    if name == "sed":
        if "-n" not in args and not any(a.startswith("-") and "n" in a[1:] and not a.startswith("--") for a in args):
            return "sed は -n を付けた表示だけ許されている"
        if any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in args):
            return "sed -i は許されていない"
        scripts = [a for a in args if not a.startswith("-")][:1]
        if any(SED_SCRIPT_BAD.search(s) for s in scripts):
            return "sed の w・W・e は許されていない"
    return None


def check_bash(command, policy):
    if not isinstance(command, str):
        return "コマンドを読めない"
    try:
        if shlex.split(command) in (policy.get("commands") or []):
            return None
    except ValueError:
        return "コマンドを読めない"
    try:
        pipeline = split_pipeline(command)
    except ValueError as e:
        return "引用符の外の「%s」は許されていない" % e
    for argv in pipeline:
        if not argv:
            return "空のコマンド"
        reason = check_reader(argv)
        if reason:
            return reason
    return None


def writable(path, policy):
    path = os.path.normpath(path)
    if path in [os.path.normpath(p) for p in policy.get("write_files") or []]:
        return True
    return any(path.startswith(os.path.normpath(d) + os.sep) for d in policy.get("write_dirs") or [])


def check_patch(patch, cwd, policy):
    if not isinstance(patch, str):
        return "パッチを読めない"
    paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M)
    moves = re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M)
    if not paths:
        return "書き込むファイルが無い"
    if re.search(r"^\*\*\* Delete File: ", patch, re.M):
        return "ファイルの削除は許されていない"
    for p in paths + moves:
        full = os.path.join(cwd or "/", p.strip())
        if ".." in p.split("/") or not writable(full, policy):
            return "「%s」には書き込めない" % p.strip()
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "%s。%s" % (reason, ALLOWED_TEXT)}},
                     ensure_ascii=False))
    return 0


def main(argv):
    try:
        policy_path = argv[argv.index("--policy") + 1]
        with open(policy_path, encoding="utf-8") as f:
            policy = json.load(f)
        payload = json.load(sys.stdin)
    except (ValueError, OSError, IndexError):
        return deny("ガードの方針か入力を読めない")
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool == "Bash":
        reason = check_bash(inp.get("command"), policy)
    elif tool == "apply_patch":
        reason = check_patch(inp.get("command"), payload.get("cwd"), policy)
    else:
        return 0
    return deny(reason) if reason else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # 思わぬ失敗では拒む
        sys.exit(deny("ガードが失敗した"))
```

- [ ] **Step 4: 通す**

Run: `python3 -I skills/visual-explain/tests/test_codex_guard.py`
Expected: OK。失敗したケースは、テストの期待（拒む・許す）を変えずに実装を直す。

- [ ] **Step 5: run.sh に足してコミット**

`tests/run.sh` のアダプタのテストの後に、同じ形で `test_codex_guard.py` を「Codex のガード（codex_guard.py）」として足す。

```bash
bash skills/visual-explain/tests/run.sh
git add skills/visual-explain
git commit -m "feat: サンドボックスが無いときに Codex の子プロセスを絞るガードを追加"
```

---

### Task 4: `CodexAgent` と、ページの質問に Codex で答える流れ

**Files:**
- Create: `$DST/tests/fixtures/fake_codex.py`
- Modify: `$DST/scripts/agents.py`（`CodexAgent`・`CodexReader`）、`$DST/scripts/serve.py`（エージェントの選び方、`--codex`・`--agent`、origin、サンドボックスの確かめ、ガードの方針、プロンプトの差し込み口、`step_label` の Bash）、`$DST/assets/qa/answer-prompt.md`、`$DST/assets/ask/build-prompt.md`、`$DST/tests/test_agents.py`、`$DST/tests/test_serve.py`

**Interfaces:**
- Consumes: Task 2 の `Agent`/`Run`/`Reader`、Task 3 の `codex_guard.py --policy`
- Produces:
  - `agents.CodexAgent`（key `codex`、label `Codex`、session_env `CODEX_THREAD_ID`、account_env `CODEX_HOME`、home_dir `~/.codex`、ask_tool `request_user_input`、write_hint `apply_patch`）
  - `agents.CodexAgent.sandbox_works(env) -> bool`（`codex sandbox -- true` が exit 0 か）
  - `serve.Config` に `codex: list`（既定 `["codex"]`、環境変数 `VISUAL_EXPLAIN_CODEX`）、`codex_home: str`、`agent: str = "auto"`
  - `serve.App.agent_for(m) -> Agent`、`serve.App.codex_sandbox() -> bool`（配信の間 1 回だけ確かめて覚える）
  - `qa/origin.json` と `qa/session.json` に `"agent"`（無ければ `claude` とみなす。zaicodev 版の資料との互換）
  - `serve.py origin` の `--agent`（既定は `agents.detect_session_agent(os.environ)`）

- [ ] **Step 1: `fake_codex.py` を書く**（`fake_claude.py` と同じ振る舞いを Codex の形で出す）

```python
#!/usr/bin/env python3
"""serve.py のテスト用の codex の代役。fake_claude.py と同じ語で振る舞いを変え、出力は codex exec --json の形にする。

  codex exec --json ... [fork|resume <id>] -   標準入力の指示文で回答を書く
  codex mcp list --json                        MCP サーバーの一覧
  codex sandbox -- true                        FAKE_CODEX_SANDBOX=ok なら 0、それ以外は bwrap のエラーで 1
fork/resume の ID で失敗を作る: missing-session は会話が無い、too-long-origin（fork）は長すぎる。
引数と環境の一部を FAKE_CLAUDE_LOG（JSON Lines、"agent": "codex"）に記録する。
"""
import json
import os
import re
import shutil
import sys
import time
import uuid


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def item(kind, **fields):
    i = dict(fields, id="item_%s" % uuid.uuid4().hex[:6], type=kind)
    emit({"type": "item.started", "item": dict(i, status="in_progress")})
    emit({"type": "item.completed", "item": dict(i, status="completed")})


def main():
    a = sys.argv[1:]
    if a[:1] == ["sandbox"]:
        if os.environ.get("FAKE_CODEX_SANDBOX") == "ok":
            return 0
        print("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", file=sys.stderr)
        return 1
    if a[:3] == ["mcp", "list", "--json"]:
        print(json.dumps([{"name": "github", "enabled": True}, {"name": "off-server", "enabled": False},
                          {"name": "bad.name", "enabled": True}]))
        return 0
    if a[:1] != ["exec"]:
        return 2
    mode = next((m for m in ("fork", "resume") if m in a), "new")
    source = a[a.index(mode) + 1] if mode != "new" else None
    prompt = sys.stdin.read()
    answer = re.search(r"^- 回答の書き出し先: (.+)$", prompt, re.M).group(1).strip()
    draft = re.search(r"^- 本文の作業用コピー: (.+)$", prompt, re.M).group(1).strip()
    doc = re.search(r"^- 資料: (.+)$", prompt, re.M).group(1).strip()
    question = re.search(r"<question>\n(.*?)\n</question>", prompt, re.S).group(1)
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent": "codex", "argv": a, "cwd": os.getcwd(), "question": question, "prompt": prompt,
                                "codex_home": os.environ.get("CODEX_HOME"),
                                "thread_env": os.environ.get("CODEX_THREAD_ID"),
                                "child": os.environ.get("VISUAL_EXPLAIN_ASK_CHILD")}, ensure_ascii=False) + "\n")
    if source == "missing-session":
        print("Error: thread not found: %s" % source, file=sys.stderr)
        return 1
    if source == "too-long-origin" and mode == "fork":
        emit({"type": "thread.started", "thread_id": str(uuid.uuid4())})
        emit({"type": "turn.failed", "error": {"message": "context window exceeded"}})
        return 1
    thread = source if mode == "resume" else str(uuid.uuid4())
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    item("command_execution", command="/bin/bash -lc 'cat %s'" % doc, aggregated_output="", exit_code=0)
    item("web_search", query="visual explain")
    item("mcp_tool_call", server="github", tool="get_issue", arguments={}, result=None, error=None)
    if "FAIL" in question:
        print("boom: fake failure", file=sys.stderr)
        return 1
    if "SLEEP" in question:
        time.sleep(30)
    if "BUILD" in question:
        valid = os.path.join(os.path.dirname(os.path.abspath(__file__)), "valid")
        shutil.copyfile(os.path.join(valid, "explanation.html"), draft)
        shutil.copytree(os.path.join(valid, "images"), os.path.join(os.path.dirname(draft), "images"), dirs_exist_ok=True)
        item("file_change", changes=[{"path": draft, "kind": "update"}])
    if "EDIT" in question:
        s = open(draft, encoding="utf-8").read().replace("<h2>流れ</h2>", "<h2>流れ（追記）</h2>", 1)
        open(draft, "w", encoding="utf-8").write(s)
        item("file_change", changes=[{"path": draft, "kind": "update"}])
    if "NOANSWER" not in question:
        with open(answer, "w", encoding="utf-8") as f:
            f.write("<p>回答: %s</p>" % question.replace("<", "&lt;"))
        item("file_change", changes=[{"path": answer, "kind": "add"}])
    emit({"type": "item.completed", "item": {"id": "item_last", "type": "agent_message",
                                             "text": "plain <answer> for %s" % question}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: `CodexAgent` のテストを書く**（`test_agents.py` に足す）

```python
class CodexAgentTest(unittest.TestCase):
    def setUp(self):
        self.a = agents.CodexAgent(["codex"])

    def test_fork_resume_new_read_the_prompt_from_stdin(self):
        fork = self.a.command(run(mode="fork", session_id="origin"))
        self.assertEqual(fork[:4], ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertEqual(fork[-3:], ["fork", "origin", "-"])
        self.assertEqual(self.a.command(run(mode="resume", session_id="s2"))[-3:], ["resume", "s2", "-"])
        new = self.a.command(run(mode="new", session_id=None))
        self.assertEqual(new[-1], "-")
        self.assertNotIn("fork", new)

    def test_sandbox_profile_writes_only_the_material(self):
        cmd = " ".join(self.a.command(run(sandbox=True)))
        self.assertIn('permissions.visual_explain.extends=":read-only"', cmd)
        self.assertIn('permissions.visual_explain.filesystem={"/out/topic" = "write"}', cmd)
        self.assertIn('default_permissions="visual_explain"', cmd)
        self.assertNotIn("danger-full-access", cmd)

    def test_without_sandbox_uses_the_guard(self):
        cmd = self.a.command(run(sandbox=False, guard_policy="/out/topic/qa/guard-q1.json"))
        self.assertEqual(cmd[cmd.index("-s") + 1], "danger-full-access")
        self.assertIn("--dangerously-bypass-hook-trust", cmd)
        hooks = next(c for c in cmd if c.startswith("hooks.PreToolUse="))
        self.assertIn('matcher = "Bash|apply_patch"', hooks)
        self.assertIn("codex_guard.py", hooks)
        self.assertIn("/out/topic/qa/guard-q1.json", hooks)

    def test_web_and_mcp(self):
        cmd = self.a.command(run(mcp_names=["github"]))
        self.assertIn('web_search="live"', cmd)
        self.assertIn('mcp_servers.github.default_tools_approval_mode="approve"', cmd)
        local = self.a.command(run(mcp_names=["github"], local_only=True))
        self.assertIn('web_search="disabled"', local)
        self.assertIn("mcp_servers.github.enabled=false", local)

    def test_reader_maps_items_to_steps(self):
        r = self.a.reader()
        lines = [{"type": "thread.started", "thread_id": "t1"},
                 {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "cat x"}},
                 {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "cat x"}},
                 {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "changes": [{"path": "/a"}, {"path": "/b"}]}},
                 {"type": "item.started", "item": {"id": "i3", "type": "web_search", "query": "q"}},
                 {"type": "item.started", "item": {"id": "i4", "type": "mcp_tool_call", "server": "github", "tool": "get_issue"}},
                 {"type": "item.completed", "item": {"id": "i5", "type": "agent_message", "text": "最後"}},
                 {"type": "turn.completed"}]
        steps = [s for line in lines for s in r.feed(json.dumps(line))]
        self.assertEqual(steps, [("Bash", {"command": "cat x"}), ("Edit", {"file_path": "/a"}), ("Edit", {"file_path": "/b"}),
                                 ("WebSearch", {"query": "q"}), ("mcp__github__get_issue", {})])
        self.assertEqual(r.result(0, "", ""), (True, "最後", "t1", None))

    def test_reader_failures(self):
        r = self.a.reader()
        r.feed(json.dumps({"type": "turn.failed", "error": {"message": "context window exceeded"}}))
        ok, _, _, error = r.result(1, "", "")
        self.assertFalse(ok)
        self.assertIn("context window exceeded", error)
        self.assertFalse(self.a.reader().result(0, "", "")[0])  # turn.completed が無い

    def test_sessions_from_sqlite_and_rollouts(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        db = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
        db.execute("create table threads (id text, rollout_path text, cwd text, git_branch text, first_user_message text)")
        db.execute("insert into threads values ('t1', '', '/repo', 'main', 'グラフを\n直して')")
        db.commit()
        db.close()
        self.assertTrue(self.a.session_exists("t1", home))
        self.assertEqual(self.a.session_info("t1", home), {"prompt": "グラフを 直して"})
        path = os.path.join(home, "sessions", "2026", "10", "12", "rollout-2026-10-12T10-00-00-t2.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": "t2", "cwd": "/repo"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed",
                                "item": {"type": "UserMessage", "content": [{"type": "text", "text": "表を足して"}]}}}) + "\n")
        self.assertTrue(self.a.session_exists("t2", home))
        self.assertEqual(self.a.session_info("t2", home), {"prompt": "表を足して"})
        self.assertFalse(self.a.session_exists("t3", home))

    def test_lists_enabled_mcp_servers_with_safe_names(self):
        fake = os.path.join(HERE, "fixtures", "fake_codex.py")
        names = agents.CodexAgent([sys.executable, fake]).list_mcp("/", dict(os.environ))
        self.assertEqual(names, ["github"])

    def test_sandbox_probe(self):
        fake = os.path.join(HERE, "fixtures", "fake_codex.py")
        a = agents.CodexAgent([sys.executable, fake])
        self.assertFalse(a.sandbox_works(dict(os.environ)))
        self.assertTrue(a.sandbox_works(dict(os.environ, FAKE_CODEX_SANDBOX="ok")))
```

`DetectTest.test_detects_the_running_agent` の `("codex", "x1")` もここで通るようになる（Task 2 では `AGENTS` に codex が無いので、Task 2 ではこの 1 行を後回しにしてよい）。

- [ ] **Step 3: 失敗を確かめる**

Run: `python3 -I skills/visual-explain/tests/test_agents.py`
Expected: FAIL（`CodexAgent` が無い）

- [ ] **Step 4: `CodexAgent` を書く**（`agents.py`）

```python
GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_guard.py")
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_str(value):
    """-c の値に書く TOML の文字列（JSON の文字列は TOML の basic string として読める）。"""
    return json.dumps(value, ensure_ascii=False)


class CodexReader:
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
            self.failure = str((err or {}).get("message") or kind)
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
        if returncode != 0 or self.failure or not self.done:
            detail = self.failure or stderr.strip()[-800:] or raw.strip()[-800:] or "終了コード %d" % returncode
            return False, "", None, "codex が失敗した: %s" % detail
        return True, self.text or "", self.thread, None


class CodexAgent(Agent):
    key, label = "codex", "Codex"
    session_env, account_env, home_dir = "CODEX_THREAD_ID", "CODEX_HOME", "~/.codex"
    ask_tool, write_hint = "request_user_input", "apply_patch"
    # 親の Codex セッションから引き継がない環境変数
    parent_env = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")

    def rollout(self, session_id, home):
        if not SESSION_ID_RE.match(session_id or ""):
            return None
        found = glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % session_id))
        return found[0] if found else None

    def thread_row(self, session_id, home):
        import sqlite3
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
        return None

    def list_mcp(self, cwd, env, timeout=MCP_LIST_TIMEOUT):
        out = run_listing(self.argv + ["mcp", "list", "--json"], cwd, env, timeout)
        try:
            servers = json.loads(out) if out is not None else None
        except ValueError:
            return None
        if not isinstance(servers, list):
            return None
        # -c の dotted key に書けない名前は扱わない（設定のまま動く）
        return [s["name"] for s in servers if isinstance(s, dict) and s.get("enabled") and SAFE_KEY_RE.match(str(s.get("name")))]

    def external_label(self, mcp_names):
        return "Web の検索（web_search）、MCP のサーバー（%s）" % ("、".join(mcp_names) if mcp_names else "無し")

    def sandbox_works(self, env):
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
            cmd += ["-c", "mcp_servers.%s.enabled=false" % name] if run.local_only else \
                   ["-c", "mcp_servers.%s.default_tools_approval_mode=%s" % (name, toml_str("approve"))]
        if run.sandbox:
            cmd += ["-c", "permissions.visual_explain.extends=%s" % toml_str(":read-only"),
                    "-c", "permissions.visual_explain.filesystem={%s = %s}" % (toml_str(run.material_dir), toml_str("write")),
                    "-c", "default_permissions=%s" % toml_str("visual_explain")]
        else:
            guard = "python3 -I %s --policy %s" % (shlex.quote(GUARD), shlex.quote(run.guard_policy))
            cmd += ["-s", "danger-full-access", "--dangerously-bypass-hook-trust",
                    "-c", 'hooks.PreToolUse=[{matcher = "Bash|apply_patch", hooks = [{type = "command", command = %s, timeout = 30}]}]'
                    % toml_str(guard)]
        if run.mode in ("fork", "resume"):
            cmd += [run.mode, run.session_id]
        return cmd + ["-"]

    def new_session_id(self):
        return None  # codex exec が作る（thread.started で受け取る）

    def reader(self):
        return CodexReader()


AGENTS = {"claude": ClaudeAgent, "codex": CodexAgent}
```

- [ ] **Step 5: agents のテストを通す**

Run: `python3 -I skills/visual-explain/tests/test_agents.py`
Expected: OK

- [ ] **Step 6: serve.py で Codex を使えるようにするテストを書く**（`test_serve.py`）

`FAKE_CODEX = os.path.join(HERE, "fixtures", "fake_codex.py")` を定義し、`ServeTestBase.start` の既定に `codex=[sys.executable, FAKE_CODEX], codex_home=self.codex_home` を足す（`setUp` で `self.codex_home = os.path.join(self.tmp, "codex-home")`）。次のクラスを足す:

```python
class CodexAnswerTest(ServeTestBase):
    def origin(self, sid, agent="codex", **extra):
        os.makedirs(os.path.join(self.material, "qa"), exist_ok=True)
        with open(os.path.join(self.material, "qa", "origin.json"), "w") as f:
            json.dump(dict({"session_id": sid, "cwd": self.repo, "agent": agent}, **extra), f)

    def codex_session(self, sid):
        path = os.path.join(self.codex_home, "sessions", "2026", "10", "12", "rollout-2026-10-12T10-00-00-%s.jsonl" % sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    def test_forks_the_codex_session_then_resumes_it(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        first = self.ask("一つ目")
        self.assertEqual((first["status"], first["session"]), ("answered", "fork"), first)
        second = self.ask("二つ目")
        self.assertEqual(second["session"], "resume")
        c1, c2 = [c for c in self.calls() if c.get("agent") == "codex"]
        self.assertEqual(c1["argv"][-3:-1], ["fork", "origin-x"])
        self.assertEqual(c2["argv"][-3], "resume")
        self.assertEqual(c1["child"], "1")
        self.assertIn("apply_patch", c1["prompt"])
        session = json.load(open(os.path.join(self.material, "qa", "session.json")))
        self.assertEqual(session["agent"], "codex")

    def test_failed_fork_retries_with_a_new_codex_session(self):
        self.codex_session("too-long-origin")
        self.origin("too-long-origin")
        self.start()
        item = self.ask("やり直す")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"), item)
        self.assertNotIn("fork", [c for c in self.calls() if c.get("agent") == "codex"][-1]["argv"])

    def test_progress_from_codex_items(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        steps = [s["text"] for s in self.ask("途中経過")["progress"]]
        self.assertIn("資料を読んでいます", steps)
        self.assertIn("Web で検索しています: visual explain", steps)
        self.assertIn("MCP（github）を使っています: get_issue", steps)
        self.assertIn("回答を書いています", steps)

    def test_uses_the_guard_without_a_sandbox_and_the_profile_with_one(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        self.ask("ガード")
        argv = [c for c in self.calls() if c.get("agent") == "codex"][-1]["argv"]
        self.assertIn("danger-full-access", argv)
        policy = json.load(open(os.path.join(self.material, "qa", "guard-q1.json")))
        self.assertIn(os.path.join(self.material, "explanation.qa-draft.html"), policy["write_files"])

    def test_sandbox_profile_when_the_sandbox_works(self):
        os.environ["FAKE_CODEX_SANDBOX"] = "ok"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX", None)
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        self.ask("サンドボックス")
        argv = " ".join([c for c in self.calls() if c.get("agent") == "codex"][-1]["argv"])
        self.assertIn("default_permissions", argv)
        self.assertNotIn("danger-full-access", argv)

    def test_old_origin_without_agent_is_claude(self):
        self.origin("origin-y", agent=None)
        self.start()
        self.ask("古い記録")
        self.assertNotIn("codex", [c.get("agent") for c in self.calls()])

    def test_agent_option_picks_codex_for_new_sessions(self):
        self.start(agent="codex")
        self.ask("新しく")
        self.assertEqual(self.calls()[-1].get("agent"), "codex")

    def test_runs_codex_with_the_recorded_account(self):
        other = os.path.join(self.tmp, "codex-other")
        os.makedirs(os.path.join(other, "sessions", "2026", "10", "12"))
        open(os.path.join(other, "sessions", "2026", "10", "12", "rollout-x-origin-z.jsonl"), "w").close()
        self.origin("origin-z", config_dir=other)
        self.start()
        self.ask("別アカウント")
        call = [c for c in self.calls() if c.get("agent") == "codex"][-1]
        self.assertEqual((call["codex_home"], call["argv"][-3:-1]), (other, ["fork", "origin-z"]))

    def test_origin_command_records_the_codex_thread(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["CODEX_THREAD_ID"] = "thread-1"
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "origin", self.material,
                              "--cwd", self.repo], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        origin = json.load(open(os.path.join(self.material, "qa", "origin.json")))
        self.assertEqual((origin["agent"], origin["session_id"]), ("codex", "thread-1"))
```

`ProgressTest.test_step_labels` に、Codex のシェルで資料を読む場合を足す:
```python
        self.assertEqual(label("Bash", command="/bin/bash -lc 'sed -n 1,80p %s'" % m.path), "資料を読んでいます")
```
（`label` は既存のテストの補助関数。資料・作業用コピーのパスを含む Bash は「資料を読んでいます」、回答ファイルなら「回答を読み直しています」。検査のコマンドは現行どおり検査の文を優先する）

- [ ] **Step 7: 失敗を確かめる**

Run: `python3 -I skills/visual-explain/tests/test_serve.py CodexAnswerTest`
Expected: FAIL（`Config` に `codex` が無い）

- [ ] **Step 8: serve.py を実装する**

- `Config`: `codex: list = None`（`__post_init__` で `list(self.codex or ["codex"])`）、`codex_home: str = None`（`os.environ.get("CODEX_HOME") or ~/.codex`）、`agent: str = "auto"`。`claude_home` は残す。
- `App.__init__`: `self.agents = {"claude": ClaudeAgent(cfg.claude), "codex": CodexAgent(cfg.codex)}`、`self.homes = {"claude": cfg.claude_home, "codex": cfg.codex_home}`、`self._sandbox = None`。
- `detect_claude` → `detect_agents`: `--no-qa` は現行どおり。どちらかの CLI があれば受け付ける。どちらも無ければ「資料を配信しているマシンに claude も codex も見つからない（claude: …、codex: …）」。
- `default_agent()`: `cfg.agent` が `claude`/`codex` ならそれ（使えなければ使える方）、`auto` なら使える方（両方なら claude）。
- `agent_for(m)`: `session.json` の `agent` → `origin.json` の `agent`（`origin.json` があって `agent` が無ければ `claude`）→ `default_agent()`。返す前に `available()` を確かめ、使えなければ `default_agent()`。
- `account_home(m, agent)`: 受け箱の質問の資料なら `o.get("config_dir") or agent.default_home()`、そうでなければ origin の `config_dir` か `self.homes[agent.key]`。
- `claude_env(m)` → `agent_env(m, agent)`: `agent.child_env(os.environ, o.get("config_dir") if o else None, keep_account=not (o and o.get("ask")))`。origin の `config_dir` は通常の資料でも使う（`serve.py origin` が記録する）。
- `session_plan`: `session.json` の `agent` が `agent_for(m).key` と同じときだけ resume。fork は `agent.session_exists(origin["session_id"], account_home)`。
- `codex_sandbox()`: 初回だけ `self.agents["codex"].sandbox_works(child_env)` を呼んで `self._sandbox` に覚える（ロックの中）。
- `exec_agent`（`exec_claude` の名前を変える）: Codex でサンドボックスが無ければ、`m.qa_dir/guard-<qid>.json` に `{"write_files": [m.draft, m.answer_path(qid)], "write_dirs": [m.images_dir], "commands": check_argvs}` を書き、`Run(sandbox=False, guard_policy=…)`。終わったら消さない（調べるときのため。`qa/` は配信しない）。失敗の文は reader の文を使う。
- `ask_claude` → `ask_agent`。`session.json` に `"agent": agent.key` を書き足す。
- `build_prompt`: `values` に `agent_name`（`agent.label`）、`write_hint`（`agent.write_hint`）、`ask_tool`（受け箱の質問なら質問したエージェントの `ask_tool`、そうでなければ `agent.ask_tool`）を足す。
- `step_label` の `Bash`: 検査の分岐の後に、`cmd` が `m.path`・`m.draft` を含めば「資料を読んでいます」、回答ファイルを含めば「回答を読み直しています」。
- `serve_main`: `Config(..., codex=shlex.split(args.codex), agent=args.agent)`。起動時の「質問: 受け付ける（%s が答える…）」は、使えるエージェントの名前を並べる。
- `main`: `serve` に `--codex`（既定 `os.environ.get("VISUAL_EXPLAIN_CODEX", "codex")`）と `--agent {auto,claude,codex}`。`origin` に `--agent`。`--session-id` の既定は `None`。
- `origin_main`: `agent, sid = args.agent, args.session_id`。どちらも無ければ `agents.detect_session_agent(os.environ)`。`--agent` だけ指定されたら、そのエージェントの `session_env` から読む。無ければ「CLAUDE_CODE_SESSION_ID も CODEX_THREAD_ID も無いため…」と出して 0。`record_origin(material, sid, cwd, agent, config_dir=os.environ.get(<agent の account_env>))` で `{"session_id", "cwd", "agent", "config_dir", "recorded_at"}` を書く。
- `ensure_ask_server`: 子の環境は `ClaudeAgent.parent_env + CodexAgent.parent_env` を除く。`VISUAL_EXPLAIN_CODEX` があれば `--codex` で渡す。
- `PARENT_ENV = agents_mod.ClaudeAgent.parent_env + agents_mod.CodexAgent.parent_env`。

`answer-prompt.md`:
- `2. 回答を、回答の書き出し先に HTML の断片で書く（Write ツール）。` → `2. 回答を、回答の書き出し先に HTML の断片で書く（{{write_hint}} で書く）。`
- 冒頭の段落の後に `- 答えるエージェント: {{agent_name}}` を、`- 資料:` の行の前に足す（fake の正規表現は `^- 資料: ` なので影響しない）。

`build-prompt.md`:
- `開発者と対話している Claude Code のセッション` → `開発者と対話している {{agent_name}} のセッション`
- `AskUserQuestion で開発者に` → `{{ask_tool}} で開発者に`、`AskUserQuestion を使わない。` → `{{ask_tool}} を使わない。`

- [ ] **Step 9: 全テストを通す**

Run: `bash skills/visual-explain/tests/run.sh`
Expected: `ALL PASSED`

- [ ] **Step 10: コミット**

```bash
git add skills/visual-explain
git commit -m "feat: ページからの質問に Codex でも答える"
```

---

### Task 5: Codex の受け箱（`ask_hook.py --agent codex`）と、ページの文言

**Files:**
- Modify: `$DST/scripts/ask_hook.py`、`$DST/scripts/serve.py`（`register_ask`・`ask_view`・`choice_page`・`answer_ask`・`send_inbox`・`/__qa/state`）、`$DST/assets/qa/qa.js`、`$DST/assets/qa/inbox.js`、`$DST/tests/test_serve.py`

**Interfaces:**
- Consumes: Task 4 の `App.agents`・`agent_for`
- Produces:
  - `ask_hook.py [--agent claude|codex] [--session-start]`（`--agent` の既定は `claude`。zaicodev 版の hook の登録と同じ動き）
  - `/__qa/ask` の本文に `"agent": "claude"|"codex"`。`ask.json` と `ask_view` に `agent`・`agent_name`。`origin.json` に `agent`
  - `/__qa/state` の `qa` に `"agent_name"`（その資料の質問に答えるエージェントの名前）
  - Codex の質問の正規化: `{"id", "header", "question", "options": [{"label", "description"}]}` → `clean_ask_questions` の形（`multiSelect: false`、`preview: ""`）。`ask_hook` が答えを id に付け直す

- [ ] **Step 1: テストを書く**（`test_serve.py` の `AskHookTest` に足す）

```python
CODEX_QUESTIONS = [
    {"id": "plan", "header": "案", "question": "どちらの案にしますか？",
     "options": [{"label": "案A", "description": "一覧で直す"}, {"label": "案B", "description": "詳細で直す"}]},
    {"id": "where", "header": "場所", "question": "直す所は？",
     "options": [{"label": "フック", "description": ""}, {"label": "配信", "description": ""}]},
]

    def codex_hook(self, payload=None, args=("--agent", "codex")):
        proc = subprocess.Popen([sys.executable, "-I", self.HOOK, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        payload = payload or {"session_id": "thread-ask", "cwd": self.repo, "hook_event_name": "PreToolUse",
                              "tool_name": "request_user_input", "tool_input": {"questions": CODEX_QUESTIONS}}
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        proc.stdin = None
        return proc

    def test_returns_the_page_answer_to_codex_as_a_deny_reason(self):
        self.start()
        self.configure()
        proc = self.codex_hook()
        doc = self.registered()
        st = json.loads(self.req("GET", "/__qa/state?doc=" + doc)[2])
        self.assertEqual((st["ask"]["agent"], st["ask"]["agent_name"]), ("codex", "Codex"))
        self.assertFalse(any(q["multiSelect"] for q in st["ask"]["questions"]))
        status, _, data = self.req("POST", "/__qa/answer", {"doc": doc, "answers": {
            "どちらの案にしますか？": {"selected": [], "other": "両方", "notes": "急ぎ"}, "直す所は？": {"selected": ["フック"]}}})
        self.assertEqual(status, 200, data)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err)
        res = json.loads(out)["hookSpecificOutput"]
        self.assertEqual((res["hookEventName"], res["permissionDecision"]), ("PreToolUse", "deny"))
        reason = res["permissionDecisionReason"]
        self.assertIn("これが回答である", reason)
        self.assertNotIn("k=", reason)  # 合言葉をモデルに渡さない
        answers = json.loads(reason[reason.index("{"):])
        self.assertEqual(answers, {"answers": {"plan": {"answers": ["両方", "user_note: 急ぎ"]},
                                               "where": {"answers": ["フック"]}}})

    def test_codex_session_start_prints_nothing(self):
        self.start()
        self.configure(port=self.free_port())
        conf = serve.load_ask_config(self.home)
        self.addCleanup(serve.stop_ask_server, self.home, conf)  # セッションの開始で起動した常駐の配信サーバーを止める
        res = subprocess.run([sys.executable, "-I", self.HOOK, "--agent", "codex", "--session-start"], input="{}",
                             capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        claude = subprocess.run([sys.executable, "-I", self.HOOK, "--session-start"], input="{}",
                                capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertIn("受け箱", json.loads(claude.stdout)["systemMessage"])  # Claude は従来どおり URL を出す

    def test_duplicate_codex_questions_fall_back_to_the_terminal(self):
        self.start()
        self.configure()
        dup = [dict(CODEX_QUESTIONS[0]), dict(CODEX_QUESTIONS[0], id="again")]
        proc = self.codex_hook({"session_id": "thread-ask", "cwd": self.repo, "tool_name": "request_user_input",
                                "tool_input": {"questions": dup}})
        out, err = proc.communicate(timeout=15)
        self.assertEqual((proc.returncode, out), (0, ""), err)

    def test_codex_question_builds_with_codex_and_its_account(self):
        account = os.path.join(self.tmp, "codex-account")
        os.makedirs(os.path.join(account, "sessions", "2026", "10", "12"))
        open(os.path.join(account, "sessions", "2026", "10", "12", "rollout-x-thread-ask.jsonl"), "w").close()
        os.environ["CODEX_HOME"] = account
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        self.start()
        self.configure(build="always")
        proc = self.codex_hook({"session_id": "thread-ask", "cwd": self.repo, "tool_name": "request_user_input",
                                "tool_input": {"questions": [dict(CODEX_QUESTIONS[0], question="BUILD どちら？")]}})
        doc = self.registered()
        end = time.time() + 20
        while time.time() < end and not [c for c in self.calls() if c.get("agent") == "codex"]:
            time.sleep(0.1)
        call = [c for c in self.calls() if c.get("agent") == "codex"][0]
        self.assertEqual((call["codex_home"], call["argv"][-3:-1]), (account, ["fork", "thread-ask"]))
        self.assertIn("request_user_input", call["prompt"])
        self.req("POST", "/__qa/ask-close", {"doc": doc, "reason": "terminal"})
        proc.communicate(timeout=15)

    def test_codex_question_without_codex_shows_only_the_choices(self):
        self.start(codex=[os.path.join(self.tmp, "no-codex")])
        self.configure(build="always")
        proc = self.codex_hook()
        doc = self.registered()
        st = json.loads(self.req("GET", "/__qa/state?doc=" + doc)[2])
        self.assertFalse(st["ask"]["build"])
        self.req("POST", "/__qa/ask-close", {"doc": doc, "reason": "terminal"})
        proc.communicate(timeout=15)

```

`free_port()` は `ServeTestBase` に置く補助（`socket` で空いているポートを取る。既存の `test_starts_and_stops_the_resident_server` の書き方を関数にする）。

`AskTest` に文言のテストを足す:
```python
    def test_pages_name_the_agent_that_asked(self):
        self.start()
        status, _, data = self.req("POST", "/__qa/ask", {"session_id": "thread-1", "cwd": self.repo, "agent": "codex",
                                                          "questions": QUESTIONS[:1], "build": "never"})
        doc = json.loads(data)["doc"]
        page = self.req("GET", "/" + doc)[2].decode()
        self.assertIn("Codex からの質問", page)
        self.assertNotIn("Claude Code", page)
        inbox = self.req("GET", "/__qa/inbox")[2].decode()
        self.assertIn("エージェントからの質問", inbox)
```

- [ ] **Step 2: 失敗を確かめる**

Run: `python3 -I skills/visual-explain/tests/test_serve.py AskHookTest AskTest`
Expected: 足したテストが FAIL

- [ ] **Step 3: `ask_hook.py` を実装する**

- 引数: `agent = "codex" if "--agent" in argv and argv[argv.index("--agent") + 1] == "codex" else "claude"`。
- `session_start(agent)`: 起動までは共通。`agent == "codex"` なら何も出力せずに 0。Claude は現行どおり `systemMessage`。
- PreToolUse: `expected = {"claude": "AskUserQuestion", "codex": "request_user_input"}[agent]`。`tool_name` が違えば 0。
- Codex の質問の正規化:

```python
def codex_questions(inp):
    """request_user_input の質問を AskUserQuestion の形にする（複数選択とプレビューは無い）。id は答えの対応付けに残す。"""
    out = []
    for q in inp.get("questions") or []:
        if not isinstance(q, dict):
            return None
        out.append({"question": q.get("question"), "header": q.get("header") or "", "multiSelect": False,
                    "options": [{"label": o.get("label"), "description": o.get("description") or ""}
                                for o in q.get("options") or [] if isinstance(o, dict)], "_id": q.get("id")})
    return out
```
  登録の本文の `questions` には `_id` を除いたものを送り、`"agent": agent`、`"config_dir": os.environ.get("CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR")` を付ける。
- `answered(view)`: Codex なら次を出す（URL は合言葉なし）。

```python
        if agent == "codex":
            result = {}
            for q, key in zip(questions, stored):
                picked = (view.get("answers") or {}).get(key)
                if picked is None:
                    continue
                values = [picked]
                note = ((view.get("annotations") or {}).get(key) or {}).get("notes")
                if note:
                    values.append("user_note: %s" % note)
                result[q["_id"]] = {"answers": values}
            reason = ("ユーザーは資料のページでこの質問に答えた。ツールは実行していないが、これが回答である。"
                      "同じ質問をし直さず、この回答で作業を続けること。資料: %s%s\n%s"
                      % (conf["public_url"], reg["url"], json.dumps({"answers": result}, ensure_ascii=False)))
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason},
                              "systemMessage": "visual-explain: 資料のページで答えました: %s%s" % (conf["public_url"], reg["url"])},
                             ensure_ascii=False))
            return 0
```
  （`systemMessage` は Codex が表示しなければ無視される。Task 9 で確かめる）

- [ ] **Step 4: serve.py を実装する**

- `register_ask`: `agent_key = body.get("agent") if body.get("agent") in self.agents else "claude"`。`build = body.get("build") != "never" and self.qa_enabled and self.agents[agent_key].available()`。`origin.json` に `"agent": agent_key`。`ask.json` に `"agent": agent_key`。最初の依頼は `self.agents[agent_key].session_info(sid, config_dir or self.agents[agent_key].home(), body.get("transcript_path"))["prompt"]`。`choice_page(questions, cwd, build, self.agents[agent_key].label)`。
- `ask_view`: `view["agent"] = ask.get("agent") or "claude"`、`view["agent_name"] = self.agents[view["agent"]].label`（`list_asks` にも `agent_name` を足す）。
- `choice_page(questions, cwd, building, agent_name)`: `Claude Code からの質問` → `%s からの質問` % agent_name、`答えが Claude Code の作業に戻ります` → `答えが %s の作業に戻ります` % agent_name。
- `answer_ask` / `close_ask` のエラーの文の `Claude Code` → `self.agents[ask.get("agent") or "claude"].label`（`この資料にはエージェントからの質問が無い` など、資料に質問が無いときは「エージェント」）。
- `send_inbox`: タイトルを「エージェントからの質問」にする。
- `/__qa/state`: `"qa": {"enabled": …, "reason": …, "agent_name": self.agent_for(m).label}`。

`qa.js` と `inbox.js`:
- 受け箱（inbox.js）: `Claude Code からの質問` → `エージェントからの質問`。説明文の `Claude Code が AskUserQuestion で質問すると` → `Claude Code（AskUserQuestion）や Codex（request_user_input）が質問すると`、`答えが Claude Code の作業に戻ります` → `答えがそのセッションの作業に戻ります`。通知のタイトルは `(a.agent_name || 'エージェント') + ' からの質問'`。接続できないときの文の `Claude Code のセッションを始めると` → `Claude Code か Codex のセッションを始めると`。
- 質問欄（qa.js）: 質問の資料の見出し・案内・トースト・状態の文の `Claude Code` を `agentName()`（`(state.ask && state.ask.agent_name) || 'エージェント'`）に置き換える。質問欄の案内 `資料を配信しているマシンの Claude Code が書きます` は `資料を配信しているマシンの ' + ((state.qa && state.qa.agent_name) || 'エージェント') + ' が書きます`。

`grep -n 'Claude Code' skills/visual-explain/assets/qa/*.js` で、コメント以外に残っていないことを確かめる。

- [ ] **Step 5: 全テストを通す**

Run: `bash skills/visual-explain/tests/run.sh`
Expected: `ALL PASSED`（ヘッドレス Chrome があれば `WidgetTest` で質問欄の表示も確かめる）

- [ ] **Step 6: コミット**

```bash
git add skills/visual-explain
git commit -m "feat: Codex の request_user_input も資料のページで答える"
```

---

### Task 6: `setup.sh` / `teardown.sh`

**Files:**
- Create: `$DST/setup.sh`, `$DST/teardown.sh`, `$DST/scripts/hooks_config.py`, `$DST/tests/setup.test.sh`
- Modify: `$DST/tests/run.sh`（実行を足す）

**Interfaces:**
- Produces:
  - `bash <skill>/setup.sh [--claude] [--codex]`（どちらも無ければ、`~/.claude`・`CLAUDE_CONFIG_DIR`、`~/.codex`・`CODEX_HOME` があるものすべて）
  - `bash <skill>/teardown.sh [--claude] [--codex]`
  - `python3 -I scripts/hooks_config.py add|remove --agent claude|codex --file <settings.json|hooks.json> --skill <skill>`（JSON の読み書き。既存の項目を壊さず、`<skill>/scripts/ask_hook.py` を含む項目だけを足す・外す）

- [ ] **Step 1: テストを書く**（`$DST/tests/setup.test.sh`）

```bash
#!/usr/bin/env bash
# setup.sh / teardown.sh のテスト。一時的な HOME で、Claude の settings.json と Codex の hooks.json への登録を確かめる。
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL="$(cd "$HERE/.." && pwd)"
ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
fail=0
ok() { printf '  ok   %s\n' "$1"; }
ng() { printf '  FAIL %s\n' "$1"; fail=1; }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else ng "$1"; fi; }
fresh() {
  T="$(mktemp -d "$ROOT/home.XXXX")"; mkdir -p "$T/.claude" "$T/.codex"
  printf '{"permissions":{"allow":["Bash(ls:*)"]},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo keep"}]}]}}\n' > "$T/.claude/settings.json"
  printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo other"}]}]}}\n' > "$T/.codex/hooks.json"
}
run() { env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME="$T" bash "$SKILL/$1" "${@:2}"; }
count() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sum(sys.argv[2] in h["command"] for g in d["hooks"].get(sys.argv[3],[]) for h in g["hooks"]))' "$@"; }

echo "== setup.sh"
fresh
out="$(run setup.sh 2>&1)"
check "Claude の SessionStart と PreToolUse を足す" "[ \$(count '$T/.claude/settings.json' ask_hook.py SessionStart) = 1 ] && [ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 1 ]"
check "Claude の matcher は AskUserQuestion" "python3 -c 'import json; d=json.load(open(\"$T/.claude/settings.json\")); assert any(g.get(\"matcher\")==\"AskUserQuestion\" and \"--agent claude\" in g[\"hooks\"][0][\"command\"] and g[\"hooks\"][0][\"timeout\"]==7200 for g in d[\"hooks\"][\"PreToolUse\"])'"
check "Codex の matcher は request_user_input" "python3 -c 'import json; d=json.load(open(\"$T/.codex/hooks.json\")); assert any(g.get(\"matcher\")==\"request_user_input\" and \"--agent codex\" in g[\"hooks\"][0][\"command\"] and g[\"hooks\"][0][\"statusMessage\"] for g in d[\"hooks\"][\"PreToolUse\"])'"
check "既存の設定を残す" "grep -q 'echo keep' '$T/.claude/settings.json' && grep -q 'Bash(ls:\*)' '$T/.claude/settings.json' && grep -q 'echo other' '$T/.codex/hooks.json'"
check "呼び出されたパスをそのまま書く" "grep -q '$SKILL/scripts/ask_hook.py' '$T/.codex/hooks.json'"
check "Codex の /hooks での信頼を案内する" "grep -q '/hooks' <<<\"\$out\""
run setup.sh >/dev/null 2>&1
check "2 回実行しても重複しない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 1 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py PreToolUse) = 1 ]"

echo "== 対象の絞り込みと検出"
fresh; rm -rf "$T/.codex"
run setup.sh >/dev/null 2>&1
check "~/.codex が無ければ Codex は登録しない" "[ ! -e '$T/.codex/hooks.json' ]"
fresh
run setup.sh --codex >/dev/null 2>&1
check "--codex なら Claude には足さない" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 0 ]"
fresh
printf '{"enabledPlugins":{"visual-explain@zaicodev":true}}\n' > "$T/.claude/settings.json"
out="$(run setup.sh 2>&1)"; code=$?
check "zaicodev 版が有効なら止める" "[ $code -ne 0 ] && grep -q 'visual-explain@zaicodev' <<<\"\$out\" && ! grep -q ask_hook.py '$T/.claude/settings.json'"

echo "== teardown.sh"
fresh
run setup.sh >/dev/null 2>&1
run teardown.sh >/dev/null 2>&1
check "足した項目だけを外す" "[ \$(count '$T/.claude/settings.json' ask_hook.py PreToolUse) = 0 ] && [ \$(count '$T/.codex/hooks.json' ask_hook.py SessionStart) = 0 ] && grep -q 'echo keep' '$T/.claude/settings.json' && grep -q 'echo other' '$T/.codex/hooks.json'"
check "空になったイベントは消す" "python3 -c 'import json; d=json.load(open(\"$T/.codex/hooks.json\")); assert \"PreToolUse\" not in d[\"hooks\"]'"

[ "$fail" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
```

- [ ] **Step 2: 失敗を確かめる**

Run: `bash skills/visual-explain/tests/setup.test.sh`
Expected: FAIL（setup.sh が無い）

- [ ] **Step 3: `scripts/hooks_config.py` を書く**

```python
#!/usr/bin/env python3
"""setup.sh / teardown.sh から呼ぶ。Claude の settings.json と Codex の hooks.json に、受け箱の hook を足す・外す。

  python3 -I hooks_config.py add|remove --agent claude|codex --file <JSON> --skill <スキルのフォルダ>
既存の項目は変えない。<スキル>/scripts/ask_hook.py を含む項目だけを扱う。
"""
import argparse
import json
import os
import shlex

STATUS = "資料のページで答えを待っています（受け箱から開けます）"
MATCHER = {"claude": "AskUserQuestion", "codex": "request_user_input"}


def entries(skill, agent):
    hook = "python3 -I %s --agent %s" % (shlex.quote(os.path.join(skill, "scripts", "ask_hook.py")), agent)
    return {"SessionStart": {"hooks": [{"type": "command", "command": hook + " --session-start", "timeout": 30}]},
            "PreToolUse": {"matcher": MATCHER[agent],
                           "hooks": [{"type": "command", "command": hook, "timeout": 7200, "statusMessage": STATUS}]}}


def ours(group, skill):
    mark = os.path.join(skill, "scripts", "ask_hook.py")
    return any(mark in str(h.get("command", "")) for h in group.get("hooks") or [] if isinstance(h, dict))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--agent", choices=("claude", "codex"), required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--skill", required=True)
    args = ap.parse_args()
    try:
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    hooks = data.setdefault("hooks", {})
    for event, entry in entries(args.skill, args.agent).items():
        groups = [g for g in hooks.get(event) or [] if not (isinstance(g, dict) and ours(g, args.skill))]
        if args.action == "add":
            groups.append(entry)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    os.makedirs(os.path.dirname(os.path.abspath(args.file)), exist_ok=True)
    tmp = args.file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, args.file)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: `setup.sh` と `teardown.sh` を書く**

`setup.sh`:
```bash
#!/usr/bin/env bash
# visual-explain の受け箱の hook を、Claude Code の settings.json と Codex の hooks.json に登録する。
#   usage: bash <skill>/setup.sh [--claude] [--codex]   （どちらも無ければ、設定フォルダがあるものすべて）
set -euo pipefail
SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 呼び出されたパスをそのまま登録する
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
want_claude=0; want_codex=0
for a in "$@"; do
  case "$a" in
    --claude) want_claude=1 ;;
    --codex) want_codex=1 ;;
    *) echo "不明な引数: $a" >&2; exit 2 ;;
  esac
done
if [ $want_claude = 0 ] && [ $want_codex = 0 ]; then
  [ -d "$CLAUDE_DIR" ] && want_claude=1
  [ -d "$CODEX_DIR" ] && want_codex=1
fi
command -v python3 >/dev/null || { echo "python3 が見つからない" >&2; exit 1; }
if [ $want_claude = 1 ] && [ -f "$CLAUDE_DIR/settings.json" ] && python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if (d.get("enabledPlugins") or {}).get("visual-explain@zaicodev") else 1)' "$CLAUDE_DIR/settings.json"; then
  echo "zaicodev 版（visual-explain@zaicodev）が有効になっている。受け箱の hook が二重に動くので、先に外す:" >&2
  echo "  /plugin uninstall visual-explain@zaicodev" >&2
  exit 1
fi
[ $want_claude = 0 ] && [ $want_codex = 0 ] && { echo "Claude Code（$CLAUDE_DIR）も Codex（$CODEX_DIR）も見つからない" >&2; exit 1; }
if [ $want_claude = 1 ]; then
  python3 -I "$SKILL/scripts/hooks_config.py" add --agent claude --file "$CLAUDE_DIR/settings.json" --skill "$SKILL"
  echo "Claude Code: $CLAUDE_DIR/settings.json に SessionStart と PreToolUse（AskUserQuestion）の hook を登録した"
fi
if [ $want_codex = 1 ]; then
  python3 -I "$SKILL/scripts/hooks_config.py" add --agent codex --file "$CODEX_DIR/hooks.json" --skill "$SKILL"
  echo "Codex: $CODEX_DIR/hooks.json に SessionStart と PreToolUse（request_user_input）の hook を登録した"
  echo "  Codex は信頼していない hook を動かさない。Codex を開いて /hooks で visual-explain の 2 つの hook を信頼する"
  echo "  （Codex の質問は既定では Plan モードでだけ出る）"
fi
echo "受け箱の URL: python3 $SKILL/scripts/serve.py ask-config"
```

`teardown.sh`: 引数と対象の決め方は setup.sh と同じ（zaicodev 版の検出はしない）。`hooks_config.py remove` を呼び、「… から visual-explain の hook を外した」と出す。最後に「受け箱の配信サーバーも止めるなら: python3 $SKILL/scripts/serve.py ask-config --stop」と出す。

`chmod +x setup.sh teardown.sh`。

- [ ] **Step 5: 通す**

Run: `bash skills/visual-explain/tests/setup.test.sh`
Expected: `ALL PASSED`

- [ ] **Step 6: run.sh に足してコミット**

`tests/run.sh` の最後のまとめの前に:
```bash
echo "== setup.sh / teardown.sh"
if bash "$HERE/setup.test.sh" >"$TMP/setup.log" 2>&1; then ok "setup.sh / teardown.sh のテスト"; else ng "setup.sh / teardown.sh のテスト"; grep FAIL "$TMP/setup.log"; fi
```

```bash
bash skills/visual-explain/tests/run.sh
git add skills/visual-explain
git commit -m "feat: 受け箱の hook を登録・解除する setup.sh / teardown.sh を追加"
```

---

### Task 7: ドキュメント（SKILL.md・references・README）と zaico 由来の除去

**Files:**
- Modify: `$DST/SKILL.md`, `$DST/references/{qa.md,ask.md,viewing.md,orca.md,examples.md}`
- Create: `$DST/README.md`
- Modify: `README.md`（ルートの一覧）

- [ ] **Step 1: SKILL.md**

- frontmatter を次にする（description は zaicodev 版の文を元に、`AskUserQuestion` の所を「AskUserQuestion（Codex では request_user_input）」にする。1,024 文字以内）:

```yaml
---
name: visual-explain
description: <zaicodev 版の description。AskUserQuestion の所だけ「AskUserQuestion（Codex では request_user_input）」>
license: MIT
---
```
- 本文の冒頭（`# Visual Explain` の直後）に「以下の `<skill>` は、この SKILL.md があるディレクトリを指す。」を足す。
- `${CLAUDE_PLUGIN_ROOT}/skills/visual-explain/` をすべて `<skill>/` にする。
- 「入れ子・所属・1対多の構成（組織の中の拠点、拠点の中のユーザー）」→「（組織の中の部署、部署の中のユーザー）」。
- 「図を見ながら質問する」の節: 「AskUserQuestion の質問は…プラグインのフックが」→「AskUserQuestion（Codex では request_user_input）の質問は…setup.sh で登録した hook が」。Codex には `preview` と複数選択が無いことを 1 行足す。
- 「出力と確認」の origin のコマンドに `--cwd <このセッションの作業ディレクトリ>` のまま、`serve.py origin` は Claude Code と Codex のどちらのセッションかを環境変数から判定することを書く。

- [ ] **Step 2: references**

- `qa.md`:
  - 「Claude Code（`claude -p`。そのマシンでログインしているサブスクを使う）」→「資料を作ったエージェント（Claude Code は `claude -p`、Codex は `codex exec`。そのマシンでログインしているアカウントを使う）」。仕組みの図の `claude -p` も同様。
  - 「作成したセッションを記録する」: 「セッションの ID は環境変数 `CLAUDE_CODE_SESSION_ID`（Codex では `CODEX_THREAD_ID`）から読む」。
  - 「安全」に Codex の節を足す: サンドボックスが動けば読み取り専用＋資料フォルダだけ書き込み、動かなければ `codex_guard.py` で読み取りのコマンド・検査・資料フォルダへの書き込みだけを通すこと、シェルの文字列で判定するので Claude より弱いこと、`--dangerously-bypass-hook-trust` で利用者の信頼していない hook も子で動くこと。
  - 「うまく動かないとき」の「claude コマンドが見つからない」を「claude も codex も見つからない」にし、`--claude <パス>` / `--codex <パス>` を書く。
  - 「他のプラグインで使うとき」→「他のスキルで使うとき」。設計の参照を `fukata/skills` の `docs/superpowers/specs/2026-10-12-visual-explain-design.md` にする。
  - `~/.claude/visual-explain/viewer.json` → `<データの置き場>/viewer.json`（`~/.local/state/visual-explain/`）。
- `ask.md`: 題を「エージェントの質問に資料のページで答える」にし、Claude Code（AskUserQuestion）と Codex（request_user_input）の両方を書く。`${CLAUDE_PLUGIN_ROOT}/…` → `<skill>/…`。設定の置き場を `~/.local/state/visual-explain/ask.json` にする。Codex の制約（Plan モード、複数選択とプレビューが無い、`/hooks` での信頼、答えは hook の deny の理由として届く、SessionStart で URL を出さない）を「Codex のとき」の節にまとめる。「うまく動かないとき」に「Codex で質問がページに出ない → `/hooks` で信頼したか、Plan モードか」を足す。
- `viewing.md`・`orca.md`: `${CLAUDE_PLUGIN_ROOT}` を `<skill>` にし、「プラグイン」を「スキル」にする（Orca と Tailscale の手順は残す）。
- `examples.md`: 「在庫クラウド」→「備品の貸出を管理するWebサービス」、「在庫編集／在庫一覧／在庫詳細」→「備品編集／備品一覧／備品詳細」、「注文の確定から在庫の引当」→「注文の確定から発送の手配」、「受注サービスが在庫サービスへ引当を依頼し、在庫が足りなければ」→「受注サービスが発送サービスへ手配を依頼し、手配できなければ」。文字の表の列の揃え（全角 2 桁）を崩さない。

- [ ] **Step 3: `$DST/README.md` を書く**

zaicodev 版の README を元に、次の構成にする（文はユーザー向けに短く）:
1. 概要（1 段落。Claude Code と Codex で使える）
2. 導入（`npx skills add fukata/skills -g -a claude-code -a codex -s visual-explain` → `bash ~/.agents/skills/visual-explain/setup.sh` → Codex なら `/hooks` で信頼）。外すとき（`teardown.sh`、`npx skills remove`）。zaicodev 版を入れているなら先に外すこと
3. 依存（python3 必須、ヘッドレス Chrome 任意、Tailscale 任意）
4. 使い方（自動で呼ばれる例。zaicodev 版の例から在庫の例を「注文の確定から発送の手配までどう流れるの？」にする）
5. ページから追加の質問をする（zaicodev 版の箇条書きを、エージェントに依らない書き方にする。Codex の権限の違いを 1 項目）
6. エージェントの質問に資料のページで答える（受け箱。Claude Code と Codex の違いの表: 対象のツール、モード、複数選択・プレビュー、答えの返り方、信頼の要否）
7. 資料の検査（zaicodev 版の表をそのまま）
8. 出力（フォルダの構成）
9. 資料を開く（zaicodev 版の表）
10. テスト（`bash skills/visual-explain/tests/run.sh`）
11. 分岐元: 「zaicodev/claude-code-plugins の visual-explain 0.18.1（commit `c7c7c99`）から分岐」
12. ライセンス MIT

- [ ] **Step 4: ルートの README に行を足す**

```markdown
| [visual-explain](skills/visual-explain/README.md) | 仕様・変更・仕組みを図解の HTML 資料にして配信し、ページからの質問にエージェントが答える。Claude Code と Codex の質問（AskUserQuestion / request_user_input）にもページで答えられる。インストール後に `setup.sh` で hook を登録する |
```

- [ ] **Step 5: zaico 由来が残っていないことを確かめる**

```bash
grep -rn -E 'zaico|拠点|在庫|PLG26|mackerel|CLAUDE_PLUGIN_ROOT|\.claude-plugin|marketplace' skills/visual-explain | grep -v -E '^skills/visual-explain/README.md:.*(zaicodev/claude-code-plugins|visual-explain@zaicodev)'
```
Expected: 出力なし（README の分岐元と zaicodev 版を外す案内、setup.sh の検出だけが残る。setup.sh と setup.test.sh の `visual-explain@zaicodev` も許す）

- [ ] **Step 6: 雛形・例の検査を通してコミット**

```bash
bash skills/visual-explain/tests/run.sh
git add README.md skills/visual-explain
git commit -m "docs: visual-explain の導入・使い方を Claude Code と Codex 向けに書く"
```

---

### Task 8: このマシンでの確かめ（本物の Claude Code と Codex）

**Files:**
- Modify: 見つかった不具合に応じて `$DST/scripts/*`・`$DST/references/*`・`$DST/README.md`

このマシンには zaicodev 版の plugin が入っているので、確かめる間だけ zaicodev 版を無効にし、終わったら戻す（`/plugin disable visual-explain@zaicodev` 相当を settings.json の `enabledPlugins` で行い、元の値を控えておく）。受け箱の配信サーバー（ポート 8770）が zaicodev 版のものなら、`python3 ~/.claude/plugins/cache/zaicodev/visual-explain/0.18.1/skills/visual-explain/scripts/serve.py ask-config --stop` で止めてから確かめ、終わったら zaicodev 版の受け箱を戻す。

- [ ] **Step 1: 入れる**

```bash
npx skills add ./ -g -a claude-code -a codex -s visual-explain   # 作業ブランチの内容を入れる
bash ~/.agents/skills/visual-explain/setup.sh
```
Codex を開いて `/hooks` で 2 つの hook を信頼する（ユーザーに頼む）。

- [ ] **Step 2: Codex の受け箱**（ユーザーと一緒に）

1. `python3 ~/.agents/skills/visual-explain/scripts/serve.py ask-config` の受け箱を開く。
2. Codex を Plan モードにして、選択肢のある質問をさせる（例:「README の構成を2案出して、どちらにするか request_user_input で聞いて」）。
3. 受け箱に資料が開き、ページで答えると、Codex が質問し直さずに続けるかを見る。だめなら `ask_hook.py` の deny の文面を直して繰り返す。3 回直しても続けなければ、Codex の受け箱は動かないと報告する。
4. Codex の画面に `systemMessage` が出るかを記録し、README・ask.md に反映する。
5. Codex の資料作り（`codex exec fork` が質問の途中のセッションを分岐できたか）を `qa/qa.json` の `session` で見る。`new` に落ちていれば README の制約に書く。

- [ ] **Step 3: Codex のページの質問**

1. Codex のセッションで「このリポジトリの setup.sh の流れを図で説明して」と頼み、資料を作って配信させる（`serve.py origin` が `agent: codex` を記録すること）。
2. ページで質問し、`codex exec fork` が答えること、途中経過が出ること、このマシンでは `qa/guard-q1.json` が書かれガードの経路を通ることを見る。
3. ページから「この資料のファイルを消して」「git log を出して」と頼み、ガードが拒むこと（回答が「できない」になる）を見る。

- [ ] **Step 4: Claude Code**

1. 新しい Claude Code のセッションで、AskUserQuestion の質問をページで答えられること。
2. 資料を作って配信し、ページの質問に `claude -p` が答えること。

- [ ] **Step 5: 後片付けと記録**

- zaicodev 版を元に戻すか、ユーザーに確かめる（公開版を使い続けるならそのまま）。
- 確かめた結果（動いた・動かなかった・未確認）を spec の「未確認」の節に追記する。
- 直した所があれば `bash skills/visual-explain/tests/run.sh` を通してコミットする。

```bash
git add -A skills/visual-explain docs/superpowers/specs/2026-10-12-visual-explain-design.md
git commit -m "fix: 本物の Claude Code と Codex で確かめた結果を反映する"
```
