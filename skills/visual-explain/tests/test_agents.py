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
        fork = self.a.command(run(mode="fork", session_id="origin"))
        self.assertEqual(fork[:4], ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertEqual(fork[-3:], ["fork", "origin", "-"])
        self.assertEqual(self.a.command(run(mode="resume", session_id="s2"))[-3:], ["resume", "s2", "-"])
        new = self.a.command(run(mode="new", session_id=None))
        self.assertEqual(new[-1], "-")
        self.assertNotIn("fork", new)
        self.assertNotIn("resume", new)
        model = self.a.command(run(model="gpt-x"))
        self.assertEqual(model[model.index("-m") + 1], "gpt-x")

    def test_sandbox_profile_writes_only_the_material(self):
        cmd = " ".join(self.a.command(run(sandbox=True)))
        self.assertIn('permissions.visual_explain.extends=":read-only"', cmd)
        self.assertIn('permissions.visual_explain.filesystem={"/out/topic" = "write"}', cmd)
        self.assertIn('default_permissions="visual_explain"', cmd)
        self.assertNotIn("danger-full-access", cmd)
        self.assertNotIn("hooks.PreToolUse", cmd)

    def test_without_sandbox_uses_the_guard(self):
        cmd = self.a.command(run(sandbox=False, guard_policy="/out/topic/qa/guard-q1.json"))
        self.assertEqual(cmd[cmd.index("-s") + 1], "danger-full-access")
        self.assertIn("--dangerously-bypass-hook-trust", cmd)
        hooks = next(c for c in cmd if c.startswith("hooks.PreToolUse="))
        self.assertIn('matcher = "Bash|apply_patch"', hooks)
        self.assertIn("codex_guard.py", hooks)
        self.assertIn("/out/topic/qa/guard-q1.json", hooks)
        self.assertNotIn("default_permissions", " ".join(cmd))
        # -s はサブコマンド（fork/resume）より前に置く
        fork = self.a.command(run(mode="fork", session_id="o", sandbox=False, guard_policy="/p.json"))
        self.assertLess(fork.index("-s"), fork.index("fork"))

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


class DetectTest(unittest.TestCase):
    def test_detects_the_running_agent(self):
        self.assertEqual(agents.detect_session_agent({"CLAUDE_CODE_SESSION_ID": "c1"}), ("claude", "c1"))
        self.assertEqual(agents.detect_session_agent({"CODEX_THREAD_ID": "x1"}), ("codex", "x1"))
        self.assertEqual(agents.detect_session_agent({}), (None, None))


if __name__ == "__main__":
    unittest.main()
