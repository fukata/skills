#!/usr/bin/env python3
"""codex_guard.py（サンドボックスが動かないときに、子の codex のコマンドと書き込みを絞る hook）のテスト。
  usage: python3 -I skills/visual-explain/tests/test_codex_guard.py
"""
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
                    "head -c 100 x", "tail -n 5 x", "jq . x.json", "stat x", "rg 'x > y' app", 'grep -n "a b" x',
                    "sed -n '/error/p' log"):
            self.allowed(cmd)

    def test_the_exact_check_command(self):
        self.allowed("python3 -I /s/check-explanation.py /out/t/explanation.qa-draft.html")
        self.denied("python3 -I /s/check-explanation.py /etc/passwd")
        self.denied("python3 -c 'import os'")

    def test_writes_and_chains(self):
        for cmd in ("cat a > b", "cat a >> b", "echo x", "rm -rf /", "git log --output=/tmp/x", "sed -i s/a/b/ x",
                    "sed -n 'w /tmp/x' a", "sed -n '1e id' a", "sed -n 's/a/b/w out' a", "find . -delete",
                    "find . -exec rm {} ;", "cat a; rm b", "cat a && rm b", "cat a || rm b", "cat $(echo a)",
                    "cat `echo a`", "cat a &", "cat <(rm b)", "cat a\nrm b", "bash -c 'cat a'", "env rm a",
                    "awk 'BEGIN{system(\"rm a\")}'", "find . -fprint /tmp/x", "cat a | sh", "rg --pre rm x",
                    "rg --pre=rm x", "sort -o x a", 'cat "$(rm b)"', 'cat "`rm b`"', 'cat "$HOME"', "tree -o x",
                    "file -C x", "/tmp/x/cat a", "FOO=1 cat a", "cat a\\ b"):
            self.denied(cmd)

    def test_unparsable_is_denied(self):
        self.denied("cat 'a")
        self.denied("")
        self.denied(None)


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
                    "*** Begin Patch\n*** End Patch\n", None):
            self.assertIsNotNone(g.check_patch(bad, "/repo", POLICY), bad)

    def test_relative_paths_resolve_from_cwd(self):
        rel = "*** Begin Patch\n*** Update File: explanation.qa-draft.html\n@@\n-a\n+b\n*** End Patch\n"
        self.assertIsNone(g.check_patch(rel, "/out/t", POLICY))
        self.assertIsNotNone(g.check_patch(rel, "/repo", POLICY))


class HookTest(unittest.TestCase):
    def hook(self, payload, policy=POLICY):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(policy, f)
        self.addCleanup(os.remove, f.name)
        return subprocess.run([sys.executable, "-I", GUARD, "--policy", f.name], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=10)

    def test_allows_silently_and_denies_with_a_reason(self):
        ok = self.hook({"tool_name": "Bash", "tool_input": {"command": "cat a"}, "cwd": "/repo"})
        self.assertEqual((ok.returncode, ok.stdout), (0, ""))
        ng = self.hook({"tool_name": "Bash", "tool_input": {"command": "rm a"}, "cwd": "/repo"})
        out = json.loads(ng.stdout)["hookSpecificOutput"]
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
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
