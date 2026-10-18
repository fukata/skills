#!/usr/bin/env python3
"""agents.py（Claude Code・Codex・OpenCode の違いを吸収するアダプタ）のテスト。
  usage: python3 -I skills/visual-explain/tests/test_agents.py
"""
import contextlib
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
FAKE_CLAUDE = os.path.join(HERE, "fixtures", "fake_claude.py")
FAKE_CODEX = os.path.join(HERE, "fixtures", "fake_codex.py")


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


@contextlib.contextmanager
def data_home():
    """OpenCode の会話の記録（XDG_DATA_HOME/opencode）を一時フォルダに向ける。"""
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, "opencode"), exist_ok=True)
    before = os.environ.get("XDG_DATA_HOME")
    os.environ["XDG_DATA_HOME"] = home
    try:
        yield home
    finally:
        if before is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = before
        shutil.rmtree(home, ignore_errors=True)


class ClaudeAgentTest(unittest.TestCase):
    def setUp(self):
        self.a = agents.ClaudeAgent(["claude"])

    def test_new_fork_resume(self):
        new = self.a.command(run())
        self.assertEqual(new[:7], ["claude", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk"])
        self.assertEqual(new[new.index("--session-id") + 1], "11111111-2222-3333-4444-555555555555")
        fork = self.a.command(run(mode="fork", session_id="origin"))
        self.assertEqual(fork[fork.index("--resume") + 1], "origin")
        self.assertIn("--fork-session", fork)
        resume = self.a.command(run(mode="resume", session_id="s2"))
        self.assertEqual(resume[resume.index("--resume") + 1], "s2")
        self.assertNotIn("--fork-session", resume)
        self.assertEqual(self.a.command(run(model="opus"))[7:9], ["--model", "opus"])

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
        self.assertIn("boom", self.a.reader().result(1, "boom", "")[3])

    def test_session_exists_and_first_prompt(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        path = os.path.join(home, "projects", "-repo", "abc.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "<command>x</command>"}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "一覧を\n直して"}]}}) + "\n")
        self.assertTrue(self.a.session_exists("abc", home))
        self.assertFalse(self.a.session_exists("nope", home))
        self.assertFalse(self.a.session_exists("../x", home))
        self.assertEqual(self.a.session_info("abc", home, path), {"prompt": "一覧を 直して"})
        # projects の下の会話の記録だけを読む
        self.assertIsNone(self.a.first_prompt(os.path.join(home, "other.jsonl")))

    def test_child_env_switches_the_account(self):
        env = self.a.child_env({"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/a", "PATH": "/bin"}, "/b")
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/b", "PATH": "/bin"})
        self.assertEqual(self.a.child_env({"CLAUDE_CONFIG_DIR": "/a"}), {"CLAUDE_CONFIG_DIR": "/a"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.a.child_env({"CLAUDE_CONFIG_DIR": "/a"}, None, keep_account=False))

    def test_reads_mcp_server_names(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        env = dict(os.environ)
        lst = lambda argv, **kw: agents.ClaudeAgent(argv).list_mcp(tmp, env, **kw)
        self.assertEqual(lst([sys.executable, FAKE_CLAUDE]), ["claude_ai_Gmail", "plugin_slack_slack", "metrics"])
        self.assertIsNone(lst([os.path.join(tmp, "no-such-claude")]))
        self.assertIsNone(lst([sys.executable, "-c", "import sys; print('a: b - c'); sys.exit(1)"]))
        self.assertIsNone(lst([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1))
        self.assertEqual(lst([sys.executable, "-c", "print('No MCP servers configured.')"]), [])

    def test_available(self):
        self.assertTrue(agents.ClaudeAgent([sys.executable]).available())
        self.assertFalse(agents.ClaudeAgent(["/nonexistent/claude"]).available())


class CodexAgentTest(unittest.TestCase):
    def setUp(self):
        self.a = agents.CodexAgent(["codex"])

    def test_fork_resume_new_read_the_prompt_from_stdin(self):
        fork = self.a.command(run(mode="fork", session_id="origin", guard_policy="/p.json"))
        self.assertEqual(fork[:4], ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertEqual(fork[-3:], ["fork", "origin", "-"])
        self.assertEqual(self.a.command(run(mode="resume", session_id="s2", guard_policy="/p.json"))[-3:], ["resume", "s2", "-"])
        new = self.a.command(run(mode="new", session_id=None, guard_policy="/p.json"))
        self.assertEqual(new[-1], "-")
        self.assertNotIn("fork", new)
        self.assertNotIn("resume", new)
        model = self.a.command(run(model="gpt-x", guard_policy="/p.json"))
        self.assertEqual(model[model.index("-m") + 1], "gpt-x")

    def guard_hook(self, cmd):
        return next(c for c in cmd if c.startswith("hooks.PreToolUse="))

    def test_sandbox_profile_writes_only_the_material(self):
        cmd = self.a.command(run(sandbox=True, guard_policy="/out/topic/qa/guard-q1.json"))
        joined = " ".join(cmd)
        self.assertIn('permissions.visual_explain.extends=":read-only"', joined)
        self.assertIn('permissions.visual_explain.filesystem={"/out/topic" = "write"}', joined)
        self.assertIn('default_permissions="visual_explain"', joined)
        self.assertNotIn("danger-full-access", joined)
        # サンドボックスが動いても、作業用コピー・回答・images/ 以外に書かせないようガードも付ける
        self.assertIn("/out/topic/qa/guard-q1.json", self.guard_hook(cmd))

    def test_without_sandbox_uses_the_guard(self):
        cmd = self.a.command(run(sandbox=False, guard_policy="/out/topic/qa/guard-q1.json"))
        self.assertEqual(cmd[cmd.index("-s") + 1], "danger-full-access")
        self.assertIn("--dangerously-bypass-hook-trust", cmd)
        hooks = self.guard_hook(cmd)
        self.assertIn('matcher = "*"', hooks)  # すべてのツール（ガードが許可リストで判定する）
        self.assertIn("codex_guard.py", hooks)
        self.assertIn(sys.executable, hooks)
        self.assertIn("/out/topic/qa/guard-q1.json", hooks)
        self.assertNotIn("default_permissions", " ".join(cmd))

    def test_guard_cannot_be_switched_off_by_the_users_config(self):
        cmd = self.a.command(run(sandbox=False, guard_policy="/p.json"))
        self.assertEqual(cmd[cmd.index("--enable") + 1], "hooks")
        disabled = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--disable"]
        for feature in ("multi_agent", "code_mode", "code_mode_only", "browser_use", "computer_use", "shell_zsh_fork"):
            self.assertIn(feature, disabled)
        # -s はサブコマンド（fork/resume）より前に置く
        fork = self.a.command(run(mode="fork", session_id="o", sandbox=False, guard_policy="/p.json"))
        self.assertLess(fork.index("-s"), fork.index("fork"))

    def test_web_and_mcp(self):
        cmd = self.a.command(run(mcp_names=["github"], guard_policy="/p.json"))
        self.assertIn('web_search="live"', cmd)
        self.assertIn('mcp_servers.github.default_tools_approval_mode="approve"', cmd)
        local = self.a.command(run(mcp_names=["github"], local_only=True, guard_policy="/p.json"))
        self.assertIn('web_search="disabled"', local)
        self.assertIn("mcp_servers.github.enabled=false", local)

    def test_reader_maps_items_to_steps(self):
        r = self.a.reader()
        lines = [{"type": "thread.started", "thread_id": "t1"},
                 {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "cat x"}},
                 {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "cat x"}},
                 {"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                                     "changes": [{"path": "/a"}, {"path": "/b"}]}},
                 {"type": "item.started", "item": {"id": "i3", "type": "web_search", "query": "q"}},
                 {"type": "item.started", "item": {"id": "i4", "type": "mcp_tool_call", "server": "github", "tool": "get_issue"}},
                 {"type": "item.completed", "item": {"id": "i5", "type": "agent_message", "text": "最後"}},
                 {"type": "turn.completed"}]
        steps = [st for line in lines for st in r.feed(json.dumps(line))]
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
        self.assertIn("thread not found", self.a.reader().result(1, "Error: thread not found: x", "")[3])

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
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": "表を足して"}]}}}) + "\n")
        self.assertTrue(self.a.session_exists("t2", home))
        self.assertEqual(self.a.session_info("t2", home), {"prompt": "表を足して"})
        self.assertFalse(self.a.session_exists("t3", home))
        self.assertFalse(self.a.session_exists("../t1", home))
        self.assertEqual(self.a.session_info("t3", home), {"prompt": None})

    def test_lists_enabled_mcp_servers_with_safe_names(self):
        names = agents.CodexAgent([sys.executable, FAKE_CODEX]).list_mcp("/", dict(os.environ))
        self.assertEqual(names, ["github"])
        self.assertIsNone(agents.CodexAgent([sys.executable, "-c", "print('x')"]).list_mcp("/", dict(os.environ)))

    def test_sandbox_probe(self):
        a = agents.CodexAgent([sys.executable, FAKE_CODEX])
        self.assertFalse(a.sandbox_works(dict(os.environ)))
        self.assertTrue(a.sandbox_works(dict(os.environ, FAKE_CODEX_SANDBOX="ok")))

    def test_child_env_drops_the_parent_thread(self):
        env = self.a.child_env({"CODEX_THREAD_ID": "t", "CODEX_SESSION_ID": "t", "CODEX_HOME": "/a", "PATH": "/bin"}, "/b")
        self.assertEqual(env, {"CODEX_HOME": "/b", "PATH": "/bin"})


class OpenCodeAgentTest(unittest.TestCase):
    def setUp(self):
        self.a = agents.OpenCodeAgent(["opencode"])

    def test_fork_resume_new_read_the_prompt_from_stdin(self):
        self.assertEqual(self.a.command(run()), ["opencode", "run", "--standalone", "--format", "json",
                                                 "--agent", agents.OPENCODE_CHILD_AGENT])
        self.assertEqual(self.a.command(run(mode="fork"))[-3:], ["--session", run().session_id, "--fork"])
        self.assertEqual(self.a.command(run(mode="resume", session_id="ses_2"))[-2:], ["--session", "ses_2"])
        model = self.a.command(run(model="x/y"))
        self.assertEqual(model[model.index("--model") + 1], "x/y")
        # 新しい会話の ID は opencode が振る（出力のイベントから受け取る）
        self.assertIsNone(self.a.new_session_id())

    def test_child_config_denies_all_then_allows_the_material(self):
        rules = self.a.child_config(run())["agents"][agents.OPENCODE_CHILD_AGENT]["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        for rule in [{"action": "read", "resource": "*", "effect": "allow"},
                     {"action": "glob", "resource": "*", "effect": "allow"},
                     {"action": "grep", "resource": "*", "effect": "allow"},
                     {"action": "external_directory", "resource": "*", "effect": "allow"},
                     {"action": "edit", "resource": "/out/topic/explanation.qa-draft.html", "effect": "allow"},
                     {"action": "edit", "resource": "/out/topic/qa/answers/q1.html", "effect": "allow"},
                     {"action": "edit", "resource": "/out/topic/images/*", "effect": "allow"},
                     {"action": "shell", "resource": "python3 -I /s/check.py*", "effect": "allow"},
                     {"action": "webfetch", "resource": "*", "effect": "allow"},
                     {"action": "websearch", "resource": "*", "effect": "allow"}]:
            self.assertIn(rule, rules)
        # 質問のツールは許さない（子の中から受け箱を開かない）
        self.assertNotIn("question", [r["action"] for r in rules])
        local = self.a.child_config(run(local_only=True))["agents"][agents.OPENCODE_CHILD_AGENT]["permissions"]
        self.assertEqual([r["action"] for r in local if r["action"] in ("websearch", "webfetch")], [])

    def test_path_rules_allow_the_relative_and_absolute_forms(self):
        rules = self.a.path_rules(["/out/topic/x.html"], "/out")
        self.assertEqual({r["resource"] for r in rules}, {"/out/topic/x.html", "topic/x.html"})
        # 作業ディレクトリの外は絶対の形だけ（子の cwd から相対で書けない）
        self.assertEqual({r["resource"] for r in self.a.path_rules(["/other/x.html"], "/out")}, {"/other/x.html"})

    def test_shell_rules_only_allow_the_check(self):
        rules = self.a.shell_rules([["python3", "-I", "/s/check.py", "/out/topic/x.html"]])
        self.assertEqual([(r["action"], r["resource"]) for r in rules], [("shell", "python3 -I /s/check.py*")])
        self.assertEqual(self.a.shell_rules([[]]), [])

    def test_run_env_carries_the_child_config(self):
        cfg = json.loads(self.a.run_env(run())["OPENCODE_CONFIG_CONTENT"])
        self.assertIn(agents.OPENCODE_CHILD_AGENT, cfg["agents"])

    def test_reader_maps_tool_events_to_steps(self):
        r = self.a.reader()
        lines = [{"type": "tool_use", "sessionID": "ses_1", "part": {"id": "call_1", "tool": "read",
                  "state": {"status": "completed", "input": {"path": "/x"}}}},
                 # 同じ呼び出しは 2 度出しても 1 回だけ
                 {"type": "tool_use", "sessionID": "ses_1", "part": {"id": "call_1", "tool": "read",
                  "state": {"status": "completed", "input": {"path": "/x"}}}},
                 {"type": "tool_use", "sessionID": "ses_1", "part": {"id": "call_2", "tool": "shell",
                  "state": {"status": "completed", "input": {"command": "cat x"}}}},
                 {"type": "tool_use", "sessionID": "ses_1", "part": {"id": "call_3", "tool": "no_such_tool",
                  "state": {"status": "completed", "input": {}}}},
                 {"type": "text", "sessionID": "ses_1", "part": {"text": "途中"}},
                 {"type": "text", "sessionID": "ses_1", "part": {"text": "最後"}}]
        steps = [st for line in lines for st in r.feed(json.dumps(line))]
        self.assertEqual(steps, [("Read", {"path": "/x"}), ("Bash", {"command": "cat x"})])
        self.assertEqual(r.result(0, "", ""), (True, "最後", "ses_1", None))

    def test_reader_failures(self):
        r = self.a.reader()
        r.feed(json.dumps({"type": "error", "sessionID": "ses_1",
                           "error": {"type": "provider.no-route", "message": "Model unavailable: x"}}))
        ok, _, _, error = r.result(0, "", "")
        self.assertFalse(ok)
        self.assertIn("Model unavailable: x", error)
        self.assertFalse(self.a.reader().result(0, "", "")[0])  # 応答の文が無い
        self.assertIn("boom", self.a.reader().result(1, "boom", "")[3])

    def test_sessions_and_first_prompt_from_sqlite(self):
        with data_home() as home:
            db = sqlite3.connect(os.path.join(home, "opencode", "opencode.db"))
            db.execute("create table session_v2 (id text, title text, directory text)")
            db.execute("create table session_message (session_id text, type text, seq integer, data text)")
            db.execute("insert into session_v2 values ('ses_abc', '題名', '/repo')")
            db.execute("insert into session_message values ('ses_abc', 'user', 1, ?)",
                       (json.dumps({"text": "一覧を\n直して"}),))
            db.commit()
            db.close()
            self.assertTrue(self.a.session_exists("ses_abc", home))
            self.assertFalse(self.a.session_exists("ses_nope", home))
            self.assertFalse(self.a.session_exists("../x", home))
            self.assertEqual(self.a.session_info("ses_abc", home), {"prompt": "一覧を 直して"})
            self.assertEqual(self.a.session_info("ses_nope", home), {"prompt": None})

    def test_without_a_database_a_session_can_be_forked_anyway(self):
        with data_home() as home:
            # 会話の記録が無い（別のデータフォルダ等）ときは、分岐を試す
            self.assertTrue(self.a.session_exists("ses_x", home))
            self.assertEqual(self.a.session_info("ses_x", home), {"prompt": None})

    def test_child_env_drops_the_parent_session(self):
        env = self.a.child_env({"OPENCODE": "1", "OPENCODE_SESSION_ID": "ses_1", "AI_AGENT": "opencode",
                                "OPENCODE_CONFIG_DIR": "/a", "PATH": "/bin"}, "/b")
        self.assertEqual(env, {"OPENCODE_CONFIG_DIR": "/b", "PATH": "/bin"})

    def test_no_mcp_servers_for_the_child(self):
        self.assertEqual(self.a.list_mcp("/", dict(os.environ)), [])
        self.assertEqual(self.a.external_label([]), "Web の検索（websearch）と取得（webfetch）")


class DetectTest(unittest.TestCase):
    def test_detects_the_running_agent(self):
        self.assertEqual(agents.detect_session_agent({"CLAUDE_CODE_SESSION_ID": "c1"}), ("claude", "c1"))
        self.assertEqual(agents.detect_session_agent({"CODEX_THREAD_ID": "x1"}), ("codex", "x1"))
        self.assertEqual(agents.detect_session_agent({"OPENCODE_SESSION_ID": "ses_1"}), ("opencode", "ses_1"))
        self.assertEqual(agents.detect_session_agent({}), (None, None))


if __name__ == "__main__":
    unittest.main()
