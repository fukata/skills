#!/usr/bin/env python3
"""agents.py（Claude Code と Codex の違いを吸収するアダプタ）のテスト。
  usage: python3 -I skills/visual-explain/tests/test_agents.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
FAKE_CLAUDE = os.path.join(HERE, "fixtures", "fake_claude.py")


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


class DetectTest(unittest.TestCase):
    def test_detects_the_running_agent(self):
        self.assertEqual(agents.detect_session_agent({"CLAUDE_CODE_SESSION_ID": "c1"}), ("claude", "c1"))
        self.assertEqual(agents.detect_session_agent({}), (None, None))


if __name__ == "__main__":
    unittest.main()
