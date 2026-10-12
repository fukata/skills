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
          "commands": [["python3", "-I", "/s/check-explanation.py", "/out/t/explanation.qa-draft.html"]], "mcp": True}


class BashTest(unittest.TestCase):
    def allowed(self, cmd):
        self.assertIsNone(g.check_bash(cmd, POLICY), cmd)

    def denied(self, cmd):
        self.assertIsNotNone(g.check_bash(cmd, POLICY), cmd)

    def test_reads(self):
        for cmd in ("cat /out/t/explanation.html", "sed -n '1,80p' app/models/plan.rb", "rg -n 'a|b' app",
                    "grep -rn GRACE_DAYS . | head -20", "find app -name '*.rb'", "ls -la", "wc -l a b", "nl -ba x",
                    "head -c 100 x", "tail -n 5 x", "jq . x.json", "stat x", "rg 'x > y' app", 'grep -n "a b" x',
                    "sed -n '/error/p' log", "sed -n '120,$p' a.rb", "ls app/*.rb", "cat 資料.html"):
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
                    "file -C x", "/tmp/x/cat a", "FOO=1 cat a", "cat a\\ b",
                    # sed は「-n 範囲p ファイル」の形だけ（--expression・-e・-f で渡すスクリプトは調べられない）
                    "sed -n --expression=1p x", "sed -n -e1p x", "sed -n -e 1p x", "sed -n -f images/s.sed x",
                    "sed -n --file=s.sed x", "sed -ne 1p x", "sed -n '1,80p;w x' a", "sed --sandbox -n 1p x",
                    # zsh のグロブ修飾子・波括弧・コメントなど、引用符の外の記号
                    "ls a(b)", "ls *(e:x:)", "cat {a,b}", "cat a # b", "cat [ab]", "cat a!", "rg --hostname-bin=x y",
                    # 改行で区切った検査のコマンド
                    "python3 -I /s/check-explanation.py\n/out/t/explanation.qa-draft.html"):
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

    def test_headers_are_read_like_codex_reads_them(self):
        # Codex は見出しの行の前後の空白を除いて読む
        for bad in ("*** Begin Patch\n*** Update File: /out/t/explanation.qa-draft.html\n@@\n-a\n+b\n  *** Add File: /etc/x\n+x\n*** End Patch\n",
                    "*** Begin Patch\n *** Update File: /repo/app.rb\n@@\n-a\n+b\n*** End Patch\n",
                    "*** Begin Patch\n*** Frobnicate File: /x\n*** End Patch\n"):
            self.assertIsNotNone(g.check_patch(bad, "/repo", POLICY), bad)
        ok = "  *** Begin Patch\n*** Add File: /out/t/images/a.svg\n+<svg/>\n*** End of File\n*** End Patch  \n"
        self.assertIsNone(g.check_patch(ok, "/repo", POLICY))

    def test_symlinks_out_of_the_allowed_dirs(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp)
        os.makedirs(os.path.join(tmp, "t", "images"))
        os.makedirs(os.path.join(tmp, "elsewhere"))
        os.symlink(os.path.join(tmp, "elsewhere"), os.path.join(tmp, "t", "images", "link"))
        policy = {"write_files": [], "write_dirs": [os.path.join(tmp, "t", "images")], "commands": []}
        patch = "*** Begin Patch\n*** Add File: %s/t/images/link/x.html\n+x\n*** End Patch\n" % tmp
        self.assertIsNotNone(g.check_patch(patch, "/", policy))

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
        for tool in ("mcp__github__get_issue", "update_plan", "view_image"):
            self.assertEqual(self.hook({"tool_name": tool, "tool_input": {}, "cwd": "/repo"}).stdout, "", tool)

    def test_unknown_tools_are_denied(self):
        # コードを動かせるツール（コードモード・サブエージェントなど）は、許したもの以外すべて拒む
        for tool in ("exec", "spawn_agent", "js_repl", "browser_use", "shell", "local_shell", "Write"):
            out = self.hook({"tool_name": tool, "tool_input": {}, "cwd": "/repo"}).stdout
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny", tool)

    def test_mcp_is_denied_when_local_only(self):
        out = self.hook({"tool_name": "mcp__github__get_issue", "tool_input": {}, "cwd": "/repo"},
                        policy=dict(POLICY, mcp=False)).stdout
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_broken_input_is_denied(self):
        res = subprocess.run([sys.executable, "-I", GUARD, "--policy", "/nonexistent"], input="{}", capture_output=True,
                             text=True, timeout=10)
        self.assertEqual(json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
