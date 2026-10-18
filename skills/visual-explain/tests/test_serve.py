#!/usr/bin/env python3
"""serve.py（資料の配信と、ページからの追加質問）のテスト。

claude は tests/fixtures/fake_claude.py で代役を立てる。ヘッドレス Chrome があれば、
質問欄が回答を表示し、危険なタグ・属性を除くことも確かめる。
  usage: python3 -I skills/visual-explain/tests/test_serve.py
"""
import http.client
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
FAKE = os.path.join(HERE, "fixtures", "fake_claude.py")
FAKE_CODEX = os.path.join(HERE, "fixtures", "fake_codex.py")
FAKE_OPENCODE = os.path.join(HERE, "fixtures", "fake_opencode.py")
DOC = "topic/explanation.html"
TOKEN = "t0ken-for-test"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


serve = load("serve", os.path.join(SCRIPTS, "serve.py"))
check = load("check_explanation", os.path.join(SCRIPTS, "check-explanation.py"))


class ServeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "out")
        self.material = os.path.join(self.root, "topic")
        shutil.copytree(os.path.join(HERE, "fixtures", "valid"), self.material)
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(os.path.join(HERE, "fixtures", "repo"), self.repo)
        self.claude_home = os.path.join(self.tmp, "claude-home")
        self.codex_home = os.path.join(self.tmp, "codex-home")
        self.opencode_home = os.path.join(self.tmp, "opencode-home")
        # OpenCode の会話の記録（XDG_DATA_HOME/opencode の SQLite）を一時フォルダに向ける
        self.data_home = os.path.join(self.tmp, "data")
        self.before_data_home = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.data_home
        self.log = os.path.join(self.tmp, "fake.jsonl")
        os.environ["FAKE_CLAUDE_LOG"] = self.log
        self.mcp_log = os.path.join(self.tmp, "mcp.log")
        os.environ["FAKE_MCP_LOG"] = self.mcp_log
        self.app = self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.app:
            self.app.stop()
        os.environ.pop("FAKE_CLAUDE_LOG", None)
        os.environ.pop("FAKE_MCP_LOG", None)
        if self.before_data_home is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self.before_data_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **kw):
        opts = dict(root=self.root, bind="127.0.0.1", port=0, repo=self.repo,
                    claude=[sys.executable, FAKE], codex=[sys.executable, FAKE_CODEX], token=TOKEN, browser_check=False,
                    answer_timeout=10, claude_home=self.claude_home, codex_home=self.codex_home,
                    opencode_home=self.opencode_home)
        opts.update(kw)
        self.app = serve.App(serve.Config(**opts))
        self.httpd = serve.make_server(self.app)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.app.start()
        return self.port

    def req(self, method, path, body=None, headers=None, cookie=True):
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = "ve_qa_%d=%s" % (self.port, TOKEN)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        if isinstance(body, dict):
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
        conn.close()
        return out

    def ask(self, question, **extra):
        self.send(question, **extra)
        self.assertTrue(self.app.wait_idle(30), "質問の処理が終わらない")
        return self.state()["items"][-1]

    def send(self, question, **extra):
        body = {"doc": DOC, "question": question}
        body.update(extra)
        status, _, data = self.req("POST", "/__qa/questions", body)
        self.assertEqual(status, 202, data)
        return json.loads(data)

    def wait_for(self, qid, pred, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            item = next(i for i in self.state()["items"] if i["id"] == qid)
            if pred(item):
                return item
            time.sleep(0.05)
        self.fail("%s が期待した状態にならない: %r" % (qid, item))

    def write_items(self, items):
        os.makedirs(os.path.join(self.material, "qa"), exist_ok=True)
        with open(os.path.join(self.material, "qa", "qa.json"), "w") as f:
            json.dump({"items": items}, f)

    def state(self):
        status, _, data = self.req("GET", "/__qa/state?doc=" + DOC)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def write(self, rel, text):
        with open(os.path.join(self.material, *rel.split("/")), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, *parts):
        with open(os.path.join(self.material, *parts), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def free_port():
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


class StaticTest(ServeTestBase):
    def test_video_range_requests_and_head(self):
        data = bytes(range(256))
        with open(os.path.join(self.material, "movie.mp4"), "wb") as out:
            out.write(data)
        self.start()
        for value, expected, content_range in [
            ("bytes=16-31", data[16:32], "bytes 16-31/256"),
            ("bytes=250-", data[250:], "bytes 250-255/256"),
            ("bytes=-8", data[-8:], "bytes 248-255/256"),
            ("bytes=250-999", data[250:], "bytes 250-255/256")]:
            with self.subTest(value=value):
                status, headers, body = self.req("GET", "/topic/movie.mp4", headers={"Range": value}, cookie=False)
                self.assertEqual(status, 206)
                self.assertEqual(body, expected)
                self.assertEqual(headers["content-range"], content_range)
                self.assertEqual(int(headers["content-length"]), len(expected))
                self.assertEqual(headers["content-type"], "video/mp4")
        for value in ["bytes=256-", "bytes=8-2", "bytes=-0", "bytes=-", "bytes=0-1,8-9"]:
            self.assertEqual(self.req("GET", "/topic/movie.mp4", headers={"Range": value})[0], 416)
        status, headers, body = self.req("HEAD", "/topic/movie.mp4")
        self.assertEqual((status, headers["accept-ranges"], headers["content-length"], body), (200, "bytes", "256", b""))
        status, _, body = self.req("GET", "/topic/movie.mp4", headers={"Range": "bytes=0-3", "If-Range": '"old"'})
        self.assertEqual((status, body), (200, data))

    def test_private_video_and_internal_files_still_require_authorization(self):
        self.write("movie.mp4", "動画")
        os.makedirs(os.path.join(self.material, "qa"), exist_ok=True)
        self.write("qa/private.mp4", "内部ファイル")
        self.start(private=True)
        self.assertEqual(self.req("GET", "/topic/movie.mp4", headers={"Range": "bytes=0-1"}, cookie=False)[0], 401)
        self.assertEqual(self.req("HEAD", "/topic/movie.mp4", cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/topic/qa/private.mp4", headers={"Range": "bytes=0-1"})[0], 404)

    def test_injects_widget_only_into_the_doc(self):
        self.start()
        status, h, body = self.req("GET", "/" + DOC, cookie=False)
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn('src="/__qa/qa.js"', text)
        self.assertIn('data-doc="topic/explanation.html"', text)
        self.assertIn('data-rev="%s"' % serve.file_rev(os.path.join(self.material, "explanation.html")), text)
        self.assertLess(text.index("/__qa/qa.js"), text.lower().rindex("</body>"))
        self.assertIn("no-store", h["cache-control"])
        status, _, body = self.req("GET", "/topic/images/diagram.svg", cookie=False)
        self.assertEqual(status, 200)
        self.assertNotIn(b"/__qa/", body)
        status, h, _ = self.req("GET", "/__qa/qa.js", cookie=False)
        self.assertEqual(status, 200)
        self.assertIn("javascript", h["content-type"])

    def test_sends_csp_with_the_doc(self):
        self.start()
        _, h, _ = self.req("GET", "/" + DOC, cookie=False)
        self.assertIn("script-src 'self'", h["content-security-policy"])
        self.assertIn("img-src 'self' data:", h["content-security-policy"])

    def test_injects_before_body_even_with_dotted_i(self):
        path = os.path.join(self.material, "explanation.html")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace("</body>", "<p>İİİİİİ</p></body>"))
        self.start()
        text = self.req("GET", "/" + DOC, cookie=False)[2].decode()
        self.assertRegex(text, r'qa\.js"[^<]*></script>\s*</body>')

    def test_blocks_qa_data_with_odd_paths(self):
        # 資料フォルダそのものを配信する（--dir が資料フォルダ）の形。古い Python は // を縮めない
        self.start(root=self.material)
        os.makedirs(os.path.join(self.material, "qa"), exist_ok=True)
        self.write("qa/qa.json", "{}")
        self.write("explanation.qa-draft.html", "draft")
        for path in ("//qa/qa.json", "///qa/qa.json", "//explanation.qa-draft.html", "/images/../qa/qa.json",
                     "/./qa/qa.json", "/%71a/qa.json"):
            self.assertEqual(self.req("GET", path, cookie=False)[0], 404, path)
            self.assertEqual(self.req("HEAD", path, cookie=False)[0], 404, path)

    def test_rejects_unknown_host_names(self):
        port = self.start(allow_host=["devbox"])
        for host in ("evil.example", "evil.example:%d" % port):
            self.assertEqual(self.req("GET", "/" + DOC, headers={"Host": host}, cookie=False)[0], 403, host)
            self.assertEqual(self.req("GET", "/__qa/state?doc=" + DOC, headers={"Host": host})[0], 403, host)
        for host in ("localhost:%d" % port, "127.0.0.1:%d" % port, "100.64.0.1:%d" % port, "[::1]:%d" % port,
                     "devbox:%d" % port, "devbox.tail1234.ts.net:%d" % port):
            self.assertEqual(self.req("GET", "/" + DOC, headers={"Host": host}, cookie=False)[0], 200, host)

    def test_serves_the_window_page(self):
        port = self.start()
        status, h, body = self.req("GET", "/__qa/panel?doc=%s&k=%s" % (DOC, TOKEN), cookie=False)
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn('data-mode="window"', text)
        self.assertIn('data-doc="topic/explanation.html"', text)
        self.assertIn("<title>質問と回答 · 検査用の資料</title>", text)
        self.assertIn("script-src 'self'", h["content-security-policy"])
        self.assertIn("ve_qa_%d=%s" % (port, TOKEN), h.get("set-cookie", ""))
        self.assertEqual(self.req("GET", "/__qa/panel?doc=topic/nope.html", cookie=False)[0], 404)
        self.assertEqual(self.req("GET", "/__qa/panel?doc=" + DOC, headers={"Host": "evil.example"}, cookie=False)[0], 403)

    def test_hides_qa_data_and_draft(self):
        self.start()
        os.makedirs(os.path.join(self.material, "qa"), exist_ok=True)
        self.write("qa/qa.json", "{}")
        self.write("explanation.qa-draft.html", "draft")
        for path in ("/topic/qa/qa.json", "/topic/qa/", "/topic/explanation.qa-draft.html", "/topic/QA/qa.json"):
            self.assertEqual(self.req("GET", path, cookie=False)[0], 404, path)


class AuthTest(ServeTestBase):
    def test_token_sets_cookie_and_api_needs_it(self):
        port = self.start()
        _, h, _ = self.req("GET", "/%s?k=%s" % (DOC, TOKEN), cookie=False)
        self.assertIn("ve_qa_%d=%s" % (port, TOKEN), h.get("set-cookie", ""))
        self.assertIn("HttpOnly", h["set-cookie"])
        self.assertIn("SameSite=Strict", h["set-cookie"])
        _, h, _ = self.req("GET", "/%s?k=wrong" % DOC, cookie=False)
        self.assertNotIn("set-cookie", h)
        self.assertEqual(self.req("GET", "/__qa/state?doc=" + DOC, cookie=False)[0], 401)
        self.assertTrue(self.state()["qa"]["enabled"])

    def test_folder_name_with_percent(self):
        shutil.copytree(self.material, os.path.join(self.root, "a%41b"))
        self.start()
        status, _, data = self.req("GET", "/__qa/state?doc=" + urllib.parse.quote("a%41b/explanation.html"))
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["doc"], "a%41b/explanation.html")

    def test_post_is_guarded(self):
        self.start()
        body = {"doc": DOC, "question": "質問"}
        self.assertEqual(self.req("POST", "/__qa/questions", body, cookie=False)[0], 401)
        self.assertEqual(self.req("POST", "/__qa/questions", json.dumps(body).encode(),
                                  {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/__qa/questions", body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/__qa/questions", {"doc": DOC, "question": "  "})[0], 400)
        self.assertEqual(self.req("POST", "/__qa/questions", {"doc": "../out/topic/explanation.html", "question": "q"})[0], 404)
        self.assertEqual(self.req("POST", "/__qa/questions", {"doc": "topic/images/diagram.svg", "question": "q"})[0], 404)
        origin = {"Origin": "http://127.0.0.1:%d" % self.port}
        self.assertEqual(self.req("POST", "/__qa/questions", body, origin)[0], 202)
        self.assertTrue(self.app.wait_idle(30))


class AnswerTest(ServeTestBase):
    def test_answers_and_reuses_a_new_session(self):
        self.start()
        item = self.ask("更新はいつ動く？", section={"id": "flow", "title": "流れ"}, quote="毎月1日")
        self.assertEqual(item["status"], "answered")
        self.assertIn("回答: 更新はいつ動く？", item["answer_html"])
        self.assertIsNone(item["doc"])
        self.assertEqual(item["section"]["id"], "flow")
        first = self.calls()[0]
        self.assertIn("--session-id", first["argv"])
        self.assertNotIn("--resume", first["argv"])
        self.assertEqual(os.path.realpath(first["cwd"]), os.path.realpath(self.repo))
        self.assertIn("--permission-mode", first["argv"])
        self.assertIn("dontAsk", first["argv"])
        sid = first["argv"][first["argv"].index("--session-id") + 1]
        self.assertEqual(json.loads(self.read("qa", "session.json"))["session_id"], sid)
        self.ask("集計は？")
        second = self.calls()[1]
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], sid)
        self.assertNotIn("--fork-session", second["argv"])
        self.assertEqual([i["id"] for i in self.state()["items"]], ["q1", "q2"])

    def test_forks_the_origin_session_once(self):
        os.makedirs(os.path.join(self.claude_home, "projects", "-some-project"))
        with open(os.path.join(self.claude_home, "projects", "-some-project", "origin-1.jsonl"), "w"):
            pass
        serve.record_origin(self.material, "origin-1", self.repo)
        self.start()
        self.ask("最初の質問")
        first = self.calls()[0]
        self.assertEqual(first["argv"][first["argv"].index("--resume") + 1], "origin-1")
        self.assertIn("--fork-session", first["argv"])
        forked = json.loads(self.read("qa", "session.json"))
        self.assertNotEqual(forked["session_id"], "origin-1")
        self.assertEqual(forked["forked_from"], "origin-1")
        self.ask("次の質問")
        second = self.calls()[1]
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], forked["session_id"])
        self.assertNotIn("--fork-session", second["argv"])

    def test_origin_without_transcript_starts_new_session(self):
        serve.record_origin(self.material, "gone-session", self.repo)
        self.start()
        self.assertEqual(self.ask("質問")["status"], "answered")
        self.assertIn("--session-id", self.calls()[0]["argv"])

    def test_failed_resume_retries_with_new_session(self):
        os.makedirs(os.path.join(self.material, "qa"))
        with open(os.path.join(self.material, "qa", "session.json"), "w") as f:
            json.dump({"session_id": "missing-session", "cwd": self.repo}, f)
        self.start()
        item = self.ask("質問")
        self.assertEqual(item["status"], "answered")
        argvs = [c["argv"] for c in self.calls()]
        self.assertEqual(len(argvs), 2)
        self.assertIn("--resume", argvs[0])
        self.assertIn("--session-id", argvs[1])
        self.assertNotEqual(json.loads(self.read("qa", "session.json"))["session_id"], "missing-session")

    def test_failed_fork_retries_with_new_session(self):
        os.makedirs(os.path.join(self.claude_home, "projects", "-some-project"))
        with open(os.path.join(self.claude_home, "projects", "-some-project", "too-long-origin.jsonl"), "w"):
            pass
        serve.record_origin(self.material, "too-long-origin", self.repo)
        self.start()
        item = self.ask("質問")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"))
        argvs = [c["argv"] for c in self.calls()]
        self.assertIn("--fork-session", argvs[0])
        self.assertIn("--session-id", argvs[1])

    def test_failed_resume_retries_with_new_session_and_keeps_it_on_failure(self):
        os.makedirs(os.path.join(self.material, "qa"))
        with open(os.path.join(self.material, "qa", "session.json"), "w") as f:
            json.dump({"session_id": "toolong-session", "cwd": self.repo}, f)
        self.start()
        item = self.ask("FAIL も添える")
        self.assertEqual(item["status"], "failed")
        self.assertEqual(json.loads(self.read("qa", "session.json"))["session_id"], "toolong-session")
        item = self.ask("次の質問")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"))
        self.assertNotEqual(json.loads(self.read("qa", "session.json"))["session_id"], "toolong-session")

    def test_retry_starts_from_clean_files(self):
        os.makedirs(os.path.join(self.claude_home, "projects", "-some-project"))
        with open(os.path.join(self.claude_home, "projects", "-some-project", "too-long-origin.jsonl"), "w"):
            pass
        serve.record_origin(self.material, "too-long-origin", self.repo)
        self.start()
        item = self.ask("DIRTY")
        self.assertEqual(item["status"], "answered")
        self.assertNotIn("stale", item["answer_html"])
        self.assertIsNone(item["doc"])
        self.assertNotIn("half edited", self.read("explanation.html"))

    def test_does_not_hang_on_an_orphan_holding_the_pipe(self):
        self.start()
        began = time.time()
        self.assertEqual(self.ask("ORPHAN")["status"], "answered")
        self.assertLess(time.time() - began, 12)

    def test_kills_claude_that_ignores_sigterm(self):
        serve.KILL_GRACE, grace = 1, serve.KILL_GRACE
        try:
            self.start(answer_timeout=1)
            began = time.time()
            item = self.ask("STUBBORN")
        finally:
            serve.KILL_GRACE = grace
        self.assertEqual(item["status"], "failed")
        self.assertLess(time.time() - began, 15)

    def test_falls_back_to_result_text(self):
        self.start()
        item = self.ask("NOANSWER")
        self.assertEqual(item["status"], "answered")
        self.assertIn("plain &lt;answer&gt; for NOANSWER", item["answer_html"])

    def test_failure_and_timeout(self):
        self.start(answer_timeout=2)
        item = self.ask("FAIL")
        self.assertEqual(item["status"], "failed")
        self.assertIn("boom", item["error"])
        item = self.ask("SLEEP")
        self.assertEqual(item["status"], "failed")
        self.assertIn("時間", item["error"])
        self.assertFalse(os.path.exists(os.path.join(self.material, "explanation.qa-draft.html")))

    def test_strips_parent_session_env(self):
        os.environ["CLAUDECODE"] = "1"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "parent"
        try:
            self.start()
            self.ask("質問")
        finally:
            os.environ.pop("CLAUDECODE", None)
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertIsNone(self.calls()[0]["claudecode"])
        self.assertIsNone(self.calls()[0]["session_env"])

    def test_prompt_carries_question_and_paths(self):
        self.start()
        prompt = self.app.build_prompt(serve.Material(self.app.cfg, DOC), {
            "id": "q9", "question": "なぜ？", "section": {"id": "flow", "title": "流れ"}, "quote": "毎月1日"})
        self.assertIn("<question>\nなぜ？\n</question>", prompt)
        self.assertIn(os.path.join(self.material, "explanation.qa-draft.html"), prompt)
        self.assertIn(os.path.join(self.material, "qa", "answers", "q9.html"), prompt)
        self.assertIn("流れ（#flow）", prompt)
        self.assertIn("check-explanation.py", prompt)
        self.assertIn("--fragment", prompt)
        self.assertNotIn("{{", prompt)
        # 質問の資料でなければ、背景の一覧を求めない
        self.assertNotIn("--ask", self.app.check_argv(serve.Material(self.app.cfg, DOC), "x.html"))

    def test_reply_to_an_answer(self):
        self.start()
        self.ask("最初の質問")
        self.ask("別の質問")
        item = self.ask("もう少し詳しく", reply_to="q1", quote="回答: 最初", quote_from="answer")
        self.assertEqual((item["id"], item["status"], item["reply_to"], item["quote_from"]), ("q3", "answered", "q1", "answer"))
        # 続きの質問も、前の質問のセッションの続きで答える
        self.assertIn("--resume", self.calls()[2]["argv"])
        prompt = self.app.build_prompt(serve.Material(self.app.cfg, DOC), item)
        self.assertIn("<question>\nもう少し詳しく\n</question>", prompt)
        self.assertIn("質問者は Q1 の回答を読み", prompt)
        self.assertIn("<previous_question>\n最初の質問\n</previous_question>", prompt)
        self.assertIn("<previous_answer>\n<p>回答: 最初の質問</p>\n</previous_answer>", prompt)
        self.assertIn("「回答: 最初」（Q1 の回答から）", prompt)
        self.assertNotIn("{{", prompt)
        # 続きの質問でなければ、元は無い。選んだ部分は資料の本文
        plain = self.app.build_prompt(serve.Material(self.app.cfg, DOC), {"id": "q9", "question": "なぜ？", "quote": "毎月1日"})
        self.assertIn("（なし。前の回答への続きの質問ではない）", plain)
        self.assertIn("「毎月1日」（資料の本文から）", plain)
        self.assertNotIn("previous_answer", plain)
        # 元の回答の中の {{...}} は置き換えない
        self.write_items([{"id": "q1", "question": "{{question}} とは", "status": "answered", "answer_html": "<p>{{doc_path}}</p>"}])
        odd = self.app.build_prompt(serve.Material(self.app.cfg, DOC), {"id": "q2", "question": "続き", "reply_to": "q1"})
        self.assertIn("<previous_question>\n{{question}} とは\n</previous_question>", odd)
        self.assertIn("<p>{{doc_path}}</p>", odd)

    def test_reply_needs_an_answered_question(self):
        self.start()
        self.ask("FAIL")
        for reply_to in ("q1", "q9", "x", "", 1):
            status, _, data = self.req("POST", "/__qa/questions", {"doc": DOC, "question": "続き", "reply_to": reply_to})
            self.assertEqual(status, 400, (reply_to, data))
            self.assertIn("回答した質問にだけ", json.loads(data)["error"])
        self.assertEqual(len(self.state()["items"]), 1)
        # 続きの質問でなければ、回答から選んだ印は付けない
        self.assertNotIn("quote_from", self.send("ふつうの質問", quote="本文", quote_from="answer"))
        self.assertTrue(self.app.wait_idle(30))

    def test_allows_both_checks(self):
        self.start()
        self.ask("検査")
        rules = self.allowed(self.calls()[0])
        self.assertEqual(len([r for r in rules if r.startswith("Bash(") and "check-explanation.py" in r]), 2)
        self.assertTrue(any("q1.html --fragment --doc" in r for r in rules), rules)

    def allowed(self, call):
        argv = call["argv"]
        return argv[argv.index("--allowedTools") + 1:]

    def test_allows_web_and_every_mcp_server(self):
        self.start()
        self.ask("なぜ？")
        self.ask("もう一つ")
        tools = self.allowed(self.calls()[0])
        for rule in ("WebSearch", "WebFetch", "Task", "Skill", "mcp__claude_ai_Gmail", "mcp__plugin_slack_slack",
                     "mcp__metrics"):
            self.assertIn(rule, tools)
        # 書き込みとコマンドは今までどおり限ったまま
        self.assertFalse([t for t in tools if t in ("Bash", "Edit", "Write") or t.startswith("Bash(git")], tools)
        # claude mcp list は作業ディレクトリごとに 1 回だけ（起動時の先読みと最初の質問で取り合わない）
        with open(self.mcp_log, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines(), [self.repo])
        prompt = self.app.build_prompt(serve.Material(self.app.cfg, DOC), {"id": "q9", "question": "?"})
        self.assertIn("claude_ai_Gmail、plugin_slack_slack、metrics", prompt)

    def test_local_only_uses_neither_web_nor_mcp(self):
        self.start(local_only=True)
        self.ask("なぜ？")
        tools = self.allowed(self.calls()[0])
        self.assertFalse([t for t in tools if t.startswith(("Web", "mcp__")) or "Mcp" in t], tools)
        self.assertIn("Read", tools)
        self.assertFalse(os.path.exists(self.mcp_log))
        prompt = self.app.build_prompt(serve.Material(self.app.cfg, DOC), {"id": "q9", "question": "?"})
        self.assertIn("外の情報源: 使えない", prompt)

    def test_retries_mcp_list_after_a_failure(self):
        self.start()
        self.assertTrue(self.app.wait_idle(5))
        self.app.agents["claude"].argv = [os.path.join(self.tmp, "no-such-claude")]
        self.app.agents["codex"].argv = [os.path.join(self.tmp, "no-such-codex")]
        self.app.agents["opencode"].argv = [os.path.join(self.tmp, "no-such-opencode")]
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.assertEqual(self.app.mcp_servers(other), [])
        self.assertFalse([k for k in self.app.mcp if k[1] == other])  # 失敗は覚えない
        self.app.agents["claude"].argv = [sys.executable, FAKE]
        self.assertEqual(self.app.mcp_servers(other), ["claude_ai_Gmail", "plugin_slack_slack", "metrics"])
        self.assertTrue([k for k in self.app.mcp if k[1] == other])


class ProgressTest(ServeTestBase):
    def test_records_steps_from_the_stream(self):
        self.start()
        item = self.ask("EDIT してほしい")
        self.assertIn("stream-json", self.calls()[0]["argv"])
        steps = [p["text"] for p in item["progress"]]
        self.assertEqual(steps[0], "資料を読んでいます")
        self.assertTrue(steps[1].startswith("コードを検索しています: GRACE_DAYS"), steps)
        self.assertIn("資料の本文を直しています", steps)
        self.assertEqual(steps[-1], "回答を書いています")
        self.assertIsNone(item["answer_check"])

    def test_steps_are_visible_while_answering(self):
        self.start(answer_timeout=10)
        qid = self.send("SLEEP")["id"]
        item = self.wait_for(qid, lambda i: i["status"] == "answering" and len(i.get("progress") or []) >= 2)
        self.assertEqual(item["progress"][0]["text"], "資料を読んでいます")
        self.assertEqual(self.req("POST", "/__qa/cancel", {"doc": DOC, "id": qid})[0], 202)
        self.assertTrue(self.app.wait_idle(10))

    def test_step_labels(self):
        self.start()
        m = serve.Material(self.app.cfg, DOC)
        label = lambda name, **inp: self.app.step_label(m, "q1", name, inp)
        self.assertEqual(label("Read", file_path=m.draft), "資料を読んでいます")
        self.assertEqual(label("Read", file_path=os.path.join(self.repo, "app", "models", "plan.rb")),
                         "「app/models/plan.rb」を読んでいます")
        self.assertEqual(label("Write", file_path=m.answer_path("q1")), "回答を書いています")
        self.assertEqual(label("Grep", pattern="refs", path=m.draft), "コードを検索しています: refs（資料の中）")
        self.assertEqual(label("Write", file_path=os.path.join(m.images_dir, "a.png")), "図の画像を作っています")
        self.assertEqual(label("Bash", command="python3 -I check-explanation.py a.html --fragment"), "回答を検査しています")
        self.assertEqual(label("Bash", command="python3 -I check-explanation.py draft.html"), "直した本文を検査しています")
        self.assertIsNone(label("TodoWrite", todos=[]))
        self.assertEqual(label("WebSearch", query="mutation testing"), "Web で検索しています: mutation testing")
        self.assertEqual(label("WebFetch", url="https://example.com/a?b=1"), "Web のページを読んでいます: example.com")
        self.assertEqual(label("mcp__plugin_slack_slack__slack_search_public", query="x"),
                         "MCP（slack）を使っています: slack_search_public")
        self.assertEqual(label("mcp__claude_ai_Google_Drive__search_files"), "MCP（Google Drive）を使っています: search_files")
        self.assertEqual(label("Skill", skill="review"), "スキルを読んでいます: review")
        self.assertEqual(label("Grep", pattern="x" * 100)[-1], "…")
        # Codex は読み取りもシェルで行う
        self.assertEqual(label("Bash", command="/bin/bash -lc 'sed -n 1,80p %s'" % m.path), "資料を読んでいます")
        self.assertEqual(label("Bash", command="cat %s" % m.answer_path("q1")), "回答を読み直しています")

    def test_answer_check_reports_errors(self):
        self.start()
        item = self.ask("MDANSWER")
        self.assertEqual(item["status"], "answered")
        self.assertTrue(any("Markdown" in e for e in item["answer_check"]["errors"]), item["answer_check"])


class CancelTest(ServeTestBase):
    def test_cancels_queued_and_running_questions(self):
        self.start(answer_timeout=20)
        original = self.read("explanation.html")
        first = self.send("SLEEP EDIT")["id"]
        second = self.send("あとの質問")["id"]
        self.wait_for(first, lambda i: i["status"] == "answering" and i.get("progress"))  # claude が動き出すまで待つ
        status, _, data = self.req("POST", "/__qa/cancel", {"doc": DOC, "id": second})
        self.assertEqual((status, json.loads(data)["status"]), (200, "canceled"))
        began = time.time()
        status, _, data = self.req("POST", "/__qa/cancel", {"doc": DOC, "id": first})
        self.assertEqual(status, 202)
        self.assertTrue(json.loads(data)["cancel_requested"])
        self.assertTrue(self.app.wait_idle(10))
        self.assertLess(time.time() - began, 8)
        items = self.state()["items"]
        self.assertEqual([i["status"] for i in items], ["canceled", "canceled"])
        self.assertEqual([c["question"] for c in self.calls()], ["SLEEP EDIT"])
        self.assertEqual(self.read("explanation.html"), original)
        self.assertFalse(os.path.exists(os.path.join(self.material, "explanation.qa-draft.html")))
        self.assertFalse(os.path.exists(os.path.join(self.material, "qa", "session.json")))
        self.assertEqual(self.ask("次の質問")["status"], "answered")

    def test_cancel_kills_claude_that_ignores_sigterm(self):
        serve.KILL_GRACE, grace = 1, serve.KILL_GRACE
        try:
            self.start(answer_timeout=60)
            qid = self.send("STUBBORN")["id"]
            self.wait_for(qid, lambda i: i["status"] == "answering" and i.get("progress"))
            began = time.time()
            self.assertEqual(self.req("POST", "/__qa/cancel", {"doc": DOC, "id": qid})[0], 202)
            self.assertTrue(self.app.wait_idle(15))
        finally:
            serve.KILL_GRACE = grace
        self.assertLess(time.time() - began, 6)
        self.assertEqual(self.state()["items"][-1]["status"], "canceled")

    def test_refuses_once_claude_has_finished(self):
        # claude が終わった後（本文の検査・差し替え中）は取り消さない。本文とセッションの記録が取り消しと食い違わないように
        self.start()
        self.write_items([{"id": "q1", "question": "a", "status": "answering"}])
        m = serve.Material(self.app.cfg, DOC)
        job = self.app.jobs[(m.rel, "q1")] = serve.Job()
        job.finishing = True
        self.assertEqual(self.app.cancel(m, "q1")[0], 409)
        job.finishing = False
        self.assertEqual(self.app.cancel(m, "q1")[0], 202)
        self.app.jobs.clear()

    def test_cancel_is_guarded(self):
        self.start()
        qid = self.ask("質問")["id"]
        self.assertEqual(self.req("POST", "/__qa/cancel", {"doc": DOC, "id": qid})[0], 409)
        self.assertEqual(self.req("POST", "/__qa/cancel", {"doc": DOC, "id": "q99"})[0], 404)
        self.assertEqual(self.req("POST", "/__qa/cancel", {"doc": DOC, "id": "../x"})[0], 400)
        self.assertEqual(self.req("POST", "/__qa/cancel", {"doc": DOC, "id": qid}, cookie=False)[0], 401)
        self.assertEqual(self.req("POST", "/__qa/cancel", {"doc": DOC, "id": qid}, {"Origin": "http://evil.example"})[0], 403)


class ExportTest(ServeTestBase):
    def exported(self):
        text = self.read("explanation.html")
        return text[text.index(serve.EXPORT_BEGIN):text.index(serve.EXPORT_END)]

    def test_writes_answers_into_the_doc(self):
        self.start()
        self.ask("更新はいつ動く？", section={"id": "flow", "title": "流れ"}, quote="毎月1日")
        self.ask("FAIL")
        before = self.state()["rev"]
        status, _, data = self.req("POST", "/__qa/export", {"doc": DOC})
        res = json.loads(data)
        self.assertEqual((status, res["updated"], res["count"]), (200, True, 1))
        self.assertEqual(res["sections"], [{"id": "reader-qa", "title": "読者からの質問と回答"}])
        section = self.exported()
        self.assertIn('<section id="reader-qa"', section)
        self.assertIn("Q1. 更新はいつ動く？", section)
        self.assertIn('読んでいた節: <a href="#flow">流れ</a>', section)
        self.assertIn("回答: 更新はいつ動く？", section)
        self.assertNotIn("FAIL", section)
        text = self.read("explanation.html")
        self.assertIn('<a href="#reader-qa">読者からの質問</a></nav>', text)
        self.assertLess(text.index(serve.EXPORT_END), text.index("</main>"))
        self.assertEqual(self.state()["rev"], res["rev"])
        self.assertTrue(os.path.exists(os.path.join(self.material, "qa", "history", "explanation.%s.html" % before)))
        # 書き出した資料も検査を通る
        r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "check-explanation.py"),
                            os.path.join(self.material, "explanation.html"), "--repo", self.repo, "--no-browser"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("errors=0 warnings=0", r.stdout)
        # もう一度書き出すと置き換える（節と目次の項目は 1 つのまま）
        self.ask("集計は？")
        self.assertEqual(json.loads(self.req("POST", "/__qa/export", {"doc": DOC})[2])["count"], 2)
        text = self.read("explanation.html")
        self.assertEqual(text.count(serve.EXPORT_BEGIN), 1)
        self.assertEqual(text.count('href="#reader-qa"'), 1)
        self.assertIn("Q3. 集計は？", self.exported())
        status, _, data = self.req("POST", "/__qa/export", {"doc": DOC})
        self.assertEqual((status, json.loads(data)["updated"]), (200, False))

    def test_keeps_follow_up_questions_under_their_answer(self):
        self.write_items([
            {"id": "q1", "question": "最初の質問", "status": "answered", "answer_html": "<p>一</p>"},
            {"id": "q2", "question": "別の質問", "status": "answered", "answer_html": "<p>二</p>"},
            {"id": "q3", "question": "最初の続き", "status": "answered", "answer_html": "<p>三</p>", "reply_to": "q1"},
            {"id": "q4", "question": "続きの続き", "status": "answered", "answer_html": "<p>四</p>", "reply_to": "q3"},
            {"id": "q5", "question": "失敗した質問の続き", "status": "answered", "answer_html": "<p>五</p>", "reply_to": "q0"}])
        self.start()
        self.assertEqual(json.loads(self.req("POST", "/__qa/export", {"doc": DOC})[2])["count"], 5)
        section = self.exported()
        order = [section.index("<h3>Q%d." % n) for n in (1, 3, 4, 2, 5)]
        self.assertEqual(order, sorted(order))
        self.assertIn('<a href="#reader-qa-q1">Q1</a> の回答への質問', section)
        self.assertIn('<a href="#reader-qa-q3">Q3</a> の回答への質問', section)
        self.assertIn('<p class="note">Q0 の回答への質問</p>', section)

    def test_sanitizes_answers(self):
        danger = ('<p onclick="x()">安全</p><script>alert(1)</script><style>p{}</style>'
                  '<a href="javascript:alert(1)">j</a><a href="#flow">流れ</a><a href="#gone">消えた節</a>'
                  '<a href="https://example.com/">外</a><img src="https://x.example/a.png" alt="x">'
                  '<svg viewBox="0 0 300 80"><title>図</title><marker id="q1-m" markerWidth="5"/><rect width="5"/></svg><p>続き')
        self.write_items([{"id": "q1", "question": "危ない<b>質問</b>", "status": "answered", "answer_html": danger,
                           "doc": {"reverted": True}}])
        self.start()
        self.assertEqual(self.req("POST", "/__qa/export", {"doc": DOC})[0], 200)
        section = self.exported()
        for bad in ("onclick", "<script", "alert(1)", "<style", "javascript:", "x.example", "<b>"):
            self.assertNotIn(bad, section)
        self.assertIn('<a href="#flow">流れ</a>', section)
        self.assertIn("<a>消えた節</a>", section)
        self.assertIn('target="_blank" rel="noopener noreferrer"', section)
        self.assertIn('<svg viewBox="0 0 300 80" width="300" height="80">', section)
        self.assertIn('<marker id="q1-m" markerWidth="5"/>', section)
        self.assertIn("<p>続き</p>", section)
        self.assertIn("危ない&lt;b&gt;質問&lt;/b&gt;", section)
        self.assertIn("検査を通らなかったため反映していない", section)

    def test_sanitizer_edge_cases(self):
        clean = lambda h, **kw: serve.sanitize_fragment(h, ids={"flow"}, prefix="q1-", **kw)
        for src in ("\\\\evil.example/a.png", "/\\evil.example/a.png", "\\/evil.example/a.png", "&#92;&#92;evil.example/a.png",
                    "/\t/evil.example/a.png"):
            self.assertNotIn("src=", clean('<img src="%s" alt="a">' % src), src)
        self.assertIn('src="images/a.png"', clean('<img src="images/a.png" alt="a">'))
        out = clean('<svg viewBox="0 0 10 10" width="10" height="10"><rect fill="url(https://x.example/p)" stroke="u\\72l(x)"/>'
                    '<path fill="url(#m)" stroke="rgb(1, 2, 3)" marker-end="url(#m)"/><marker id="m"/></svg>')
        self.assertNotIn("x.example", out)
        self.assertNotIn("72l", out)
        self.assertIn('<path fill="url(#q1-m)" stroke="rgb(1, 2, 3)" marker-end="url(#q1-m)"/><marker id="q1-m"/>', out)
        # 資料と同じ id は付け足して重ねない。断片の中からの参照も合わせる
        out = clean('<p id="flow">x</p><a href="#flow">上</a><svg aria-labelledby="t"><title id="t">図</title></svg>'
                    '<p id="q1-keep">y</p>')
        self.assertIn('<p id="q1-flow">', out)
        self.assertIn('<a href="#q1-flow">', out)
        self.assertIn('aria-labelledby="q1-t"', out)
        self.assertIn('id="q1-keep"', out)
        # 図の中の HTML のタグは書かない（図の外に出て構造が崩れる）
        self.assertEqual(clean('<svg viewBox="0 0 1 1" width="1" height="1"><p/><text>a</text></svg><p>b</p>'),
                         '<svg viewBox="0 0 1 1" width="1" height="1"><text>a</text></svg><p>b</p>')
        self.assertEqual(serve.ID_RE.findall('<p data-id="x" id="y">'), ["y"])

    def test_exported_records_do_not_block_later_edits(self):
        self.start()
        self.ask("`GRACE_DAYS` は何日？ app/models/nope.rb:3 のこと？")
        self.ask("MDANSWER")
        self.assertEqual(self.req("POST", "/__qa/export", {"doc": DOC})[0], 200)
        r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "check-explanation.py"),
                            os.path.join(self.material, "explanation.html"), "--repo", self.repo, "--no-browser"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        item = self.ask("EDIT してほしい")
        self.assertTrue(item["doc"]["updated"], item["doc"])
        self.assertIn("流れ（追記）", self.read("explanation.html"))
        # 記録の中でも資格情報はエラーにする（資料を人に渡すので）
        text = self.read("explanation.html").replace("回答: MDANSWER", "回答: https://u:p@example.com/ MDANSWER", 1)
        self.write("explanation.html", text)
        r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "check-explanation.py"),
                            os.path.join(self.material, "explanation.html"), "--repo", self.repo, "--no-browser"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("[secret]", r.stdout)

    def test_refuses_while_answering(self):
        self.start(answer_timeout=20)
        qid = self.send("SLEEP")["id"]
        self.wait_for(qid, lambda i: i["status"] == "answering")
        self.assertEqual(self.req("POST", "/__qa/export", {"doc": DOC})[0], 409)
        self.req("POST", "/__qa/cancel", {"doc": DOC, "id": qid})
        self.assertTrue(self.app.wait_idle(10))
        self.assertEqual(self.req("POST", "/__qa/export", {"doc": DOC})[0], 400)  # 回答した質問が無い
        self.assertEqual(self.req("POST", "/__qa/export", {"doc": DOC}, cookie=False)[0], 401)

    def test_export_command(self):
        script = os.path.join(SCRIPTS, "serve.py")
        self.write_items([{"id": "q1", "question": "端末から", "status": "answered", "answer_html": "<p>答え</p>"},
                          {"id": "q2", "question": "待ち", "status": "queued"}])
        r = subprocess.run([sys.executable, "-I", script, "export", self.material], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(serve.EXPORT_BEGIN, self.read("explanation.html"))
        self.write_items([{"id": "q1", "question": "端末から", "status": "answered", "answer_html": "<p>答え</p>"}])
        r = subprocess.run([sys.executable, "-I", script, "export", self.material], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 件", r.stdout)
        self.assertIn("Q1. 端末から", self.exported())


class DocUpdateTest(ServeTestBase):
    def test_replaces_doc_when_check_passes(self):
        self.start()
        before = self.state()["rev"]
        item = self.ask("EDIT してほしい")
        self.assertEqual(item["status"], "answered")
        self.assertTrue(item["doc"]["updated"])
        self.assertEqual([s["id"] for s in item["doc"]["sections"]], ["flow"])
        self.assertIn("流れ（追記）", self.read("explanation.html"))
        after = self.state()["rev"]
        self.assertNotEqual(before, after)
        self.assertEqual(item["doc"]["rev"], after)
        self.assertTrue(os.path.exists(os.path.join(self.material, "qa", "history", "explanation.%s.html" % before)))
        self.assertFalse(os.path.exists(os.path.join(self.material, "explanation.qa-draft.html")))

    def test_discards_draft_when_check_fails(self):
        self.start()
        original = self.read("explanation.html")
        item = self.ask("BREAK してほしい")
        self.assertEqual(item["status"], "answered")
        self.assertFalse(item["doc"]["updated"])
        self.assertTrue(item["doc"]["reverted"])
        self.assertTrue(any("anchor" in e for e in item["doc"]["errors"]), item["doc"]["errors"])
        self.assertEqual(self.read("explanation.html"), original)
        self.assertFalse(os.path.exists(os.path.join(self.material, "explanation.qa-draft.html")))

    def test_rejects_draft_with_active_content(self):
        self.start()
        original = self.read("explanation.html")
        item = self.ask("SCRIPT を入れて")
        self.assertTrue(item["doc"]["reverted"])
        self.assertTrue(any("script" in e for e in item["doc"]["errors"]), item["doc"]["errors"])
        self.assertEqual(self.read("explanation.html"), original)

    def test_active_content(self):
        a = '<p>x</p><img src="images/a.png" alt="a">'
        b = ('<p onclick="x()">x</p><img src="https://evil.example/a.png" alt="a"><script>1</script>'
             '<a href="javascript:x()">l</a><p style="background:url(https://e.example/x)">s</p>')
        self.assertEqual(serve.active_content(a), {})
        self.assertEqual(sorted(serve.active_content(b)),
                         ["<script>", "external", "external-css", "javascript:", "on*"])
        for src in ("\\\\evil.example/a.png", "/\\evil.example/a.png", "/\t/evil.example/a.png"):
            self.assertEqual(serve.active_content('<img src="%s" alt="a">' % src), {"external": 1}, src)

    def test_keeps_draft_when_doc_changed_elsewhere(self):
        self.start()
        item = self.ask("TOUCHDOC")
        self.assertFalse(item["doc"]["updated"])
        self.assertTrue(item["doc"]["conflict"])
        self.assertIn("changed elsewhere", self.read("explanation.html"))
        self.assertNotIn("流れ（追記）", self.read("explanation.html"))
        self.assertIn("流れ（追記）", self.read("qa", "history", "q1.draft.html"))

    def test_changed_sections(self):
        a = '<section id="a"><h2>A</h2><p>1</p></section><section id="b"><h2>B</h2></section>'
        b = '<section id="a"><h2>A</h2><p>2</p></section><section id="b"><h2>B</h2></section><section id="c"><h2>C</h2></section>'
        self.assertEqual(serve.changed_sections(a, b), [{"id": "a", "title": "A"}, {"id": "c", "title": "C"}])


class LifecycleTest(ServeTestBase):
    def test_marks_interrupted_items_failed_on_start(self):
        os.makedirs(os.path.join(self.material, "qa"))
        with open(os.path.join(self.material, "qa", "qa.json"), "w") as f:
            json.dump({"items": [{"id": "q1", "question": "a", "status": "answering"},
                                 {"id": "q2", "question": "b", "status": "queued"},
                                 {"id": "q3", "question": "c", "status": "answered", "answer_html": "<p>x</p>"}]}, f)
        self.write("explanation.qa-draft.html", "stale")
        self.start()
        items = self.state()["items"]
        self.assertEqual([i["status"] for i in items], ["failed", "failed", "answered"])
        self.assertIn("中断", items[0]["error"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.material, "explanation.qa-draft.html")))
        self.assertEqual(self.ask("次")["id"], "q4")

    def test_second_server_leaves_owned_material_alone(self):
        self.start()
        self.write("explanation.qa-draft.html", "in progress")
        other = serve.App(serve.Config(root=self.root, claude=[sys.executable, FAKE], token="other",
                                       claude_home=self.claude_home))
        try:
            self.assertTrue(os.path.exists(os.path.join(self.material, "explanation.qa-draft.html")))
            enabled, reason = other.qa_status(serve.Material(other.cfg, DOC))
            self.assertFalse(enabled)
            self.assertIn("別の配信", reason)
        finally:
            other.stop()
        self.assertTrue(self.state()["qa"]["enabled"])

    def test_disabled_without_claude(self):
        self.start(claude=[os.path.join(self.tmp, "no-such-claude")], codex=[os.path.join(self.tmp, "no-such-codex")],
                   opencode=[os.path.join(self.tmp, "no-such-opencode")])
        st = self.state()
        self.assertFalse(st["qa"]["enabled"])
        self.assertTrue(st["qa"]["reason"])
        self.assertEqual(self.req("POST", "/__qa/questions", {"doc": DOC, "question": "q"})[0], 503)
        self.assertEqual(self.req("GET", "/" + DOC, cookie=False)[0], 200)

    def test_origin_command(self):
        script = os.path.join(SCRIPTS, "serve.py")
        env = {k: v for k, v in os.environ.items()
               if k not in ("CODEX_THREAD_ID", "CLAUDE_CONFIG_DIR", "OPENCODE_SESSION_ID")}
        env["CLAUDE_CODE_SESSION_ID"] = "sid-123"
        subprocess.run([sys.executable, "-I", script, "origin", self.material, "--cwd", self.repo],
                       env=env, check=True, capture_output=True)
        origin = json.loads(self.read("qa", "origin.json"))
        self.assertEqual((origin["session_id"], origin["cwd"], origin["agent"]), ("sid-123", os.path.abspath(self.repo), "claude"))
        os.remove(os.path.join(self.material, "qa", "origin.json"))
        env.pop("CLAUDE_CODE_SESSION_ID")
        r = subprocess.run([sys.executable, "-I", script, "origin", self.material], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.material, "qa", "origin.json")))
        self.assertIn("CLAUDE_CODE_SESSION_ID", r.stderr)


class CodexAnswerTest(ServeTestBase):
    """ページの質問に Codex（codex exec。代役は fake_codex.py）で答える。"""

    def origin(self, sid, agent="codex", **extra):
        os.makedirs(os.path.join(self.material, "qa"), exist_ok=True)
        with open(os.path.join(self.material, "qa", "origin.json"), "w") as f:
            json.dump(dict({"session_id": sid, "cwd": self.repo, "agent": agent}, **extra), f)

    def codex_session(self, sid, home=None):
        path = os.path.join(home or self.codex_home, "sessions", "2026", "10", "12",
                            "rollout-2026-10-12T10-00-00-%s.jsonl" % sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    def codex_calls(self):
        return [c for c in self.calls() if c.get("agent") == "codex"]

    def test_forks_the_codex_session_then_resumes_it(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        first = self.ask("一つ目")
        self.assertEqual((first["status"], first["session"]), ("answered", "fork"), first)
        second = self.ask("二つ目")
        self.assertEqual((second["status"], second["session"]), ("answered", "resume"), second)
        c1, c2 = self.codex_calls()
        self.assertEqual(c1["argv"][-3:-1], ["fork", "origin-x"])
        self.assertEqual(c2["argv"][-3], "resume")
        self.assertEqual(c1["child"], "1")
        self.assertIn("apply_patch", c1["prompt"])
        self.assertIn("Codex", c1["prompt"])
        session = json.loads(self.read("qa", "session.json"))
        self.assertEqual(session["agent"], "codex")
        self.assertEqual(first["answer_html"], "<p>回答: 一つ目</p>")

    def test_failed_fork_retries_with_a_new_codex_session(self):
        self.codex_session("too-long-origin")
        self.origin("too-long-origin")
        self.start()
        item = self.ask("やり直す")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"), item)
        self.assertNotIn("fork", self.codex_calls()[-1]["argv"])

    def test_codex_failure_is_reported(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        item = self.ask("FAIL して")
        self.assertEqual(item["status"], "failed", item)
        self.assertIn("codex が失敗した", item["error"])

    def test_progress_from_codex_items(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        steps = [st["text"] for st in self.ask("途中経過")["progress"]]
        self.assertIn("資料を読んでいます", steps)
        self.assertIn("Web で検索しています: visual explain", steps)
        self.assertIn("MCP（github）を使っています: get_issue", steps)
        self.assertIn("回答を書いています", steps)

    def test_uses_the_guard_without_a_sandbox(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        self.ask("ガード")
        argv = self.codex_calls()[-1]["argv"]
        self.assertIn("danger-full-access", argv)
        policy = json.loads(self.read("qa", "guard-q1.json"))
        self.assertIn(os.path.join(self.material, "explanation.qa-draft.html"), policy["write_files"])
        self.assertEqual([os.path.join(self.material, "images")], policy["write_dirs"])
        self.assertEqual(len(policy["commands"]), 2)
        self.assertIn('mcp_servers.github.default_tools_approval_mode="approve"', argv)

    def test_sandbox_profile_when_the_sandbox_works(self):
        os.environ["FAKE_CODEX_SANDBOX"] = "ok"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX", None)
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        self.ask("サンドボックス")
        argv = " ".join(self.codex_calls()[-1]["argv"])
        self.assertIn("default_permissions", argv)
        self.assertNotIn("danger-full-access", argv)

    def test_local_only_switches_codex_mcp_off(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start(local_only=True)
        item = self.ask("外を見ないで")
        self.assertEqual(item["status"], "answered", item)
        argv = self.codex_calls()[-1]["argv"]
        self.assertIn("mcp_servers.github.enabled=false", argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertFalse([a for a in argv if "default_tools_approval_mode" in a])
        self.assertFalse(json.loads(self.read("qa", "guard-q1.json"))["mcp"])

    def test_guard_policy_allows_mcp_unless_local_only(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start()
        self.ask("MCP")
        self.assertTrue(json.loads(self.read("qa", "guard-q1.json"))["mcp"])

    def test_old_origin_without_agent_is_claude(self):
        self.origin("origin-y", agent=None)
        self.start()
        item = self.ask("古い記録")
        self.assertEqual(item["status"], "answered", item)
        self.assertEqual(self.codex_calls(), [])

    def test_agent_option_picks_codex_for_new_sessions(self):
        self.start(agent="codex")
        item = self.ask("新しく")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"), item)
        self.assertEqual(self.calls()[-1].get("agent"), "codex")
        self.assertEqual(self.state()["qa"]["agent_name"], "Codex")

    def test_falls_back_when_the_recorded_agent_is_missing(self):
        self.codex_session("origin-x")
        self.origin("origin-x")
        self.start(codex=[os.path.join(self.tmp, "no-codex")])
        item = self.ask("codex が無い")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"), item)
        self.assertEqual(self.codex_calls(), [])

    def test_runs_codex_with_the_recorded_account(self):
        other = os.path.join(self.tmp, "codex-other")
        self.codex_session("origin-z", home=other)
        self.origin("origin-z", config_dir=other)
        self.start()
        self.ask("別アカウント")
        call = self.codex_calls()[-1]
        self.assertEqual((call["codex_home"], call["argv"][-3:-1]), (other, ["fork", "origin-z"]))

    def test_origin_command_records_the_codex_thread(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_HOME")}
        env["CODEX_THREAD_ID"] = "thread-1"
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "origin", self.material,
                              "--cwd", self.repo], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        origin = json.loads(self.read("qa", "origin.json"))
        self.assertEqual((origin["agent"], origin["session_id"], origin["config_dir"]), ("codex", "thread-1", None))


class OpenCodeAnswerTest(ServeTestBase):
    """ページの質問に OpenCode（opencode run。代役は fake_opencode.py）で答える。"""

    def setUp(self):
        super().setUp()
        self.data_home = os.path.join(self.tmp, "opencode-data")
        self.before_data_home = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.data_home

    def tearDown(self):
        if self.before_data_home is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self.before_data_home
        super().tearDown()

    def start(self, **kw):
        kw.setdefault("opencode", [sys.executable, FAKE_OPENCODE])
        return super().start(**kw)

    def opencode_db(self):
        """会話の記録（SQLite）を開く。"""
        os.makedirs(os.path.join(self.data_home, "opencode"), exist_ok=True)
        return sqlite3.connect(os.path.join(self.data_home, "opencode", "opencode.db"))

    def opencode_session(self, sid, prompt=None):
        """会話の記録（SQLite）を 1 つ作る。"""
        db = self.opencode_db()
        db.execute("create table if not exists session_v2 (id text, title text, directory text)")
        db.execute("create table if not exists session_message (session_id text, type text, seq integer, data text)")
        db.execute("insert into session_v2 values (?, '題名', ?)", (sid, self.repo))
        if prompt is not None:
            db.execute("insert into session_message values (?, 'user', 1, ?)", (sid, json.dumps({"text": prompt})))
        db.commit()
        db.close()

    def origin(self, sid, agent="opencode", **extra):
        os.makedirs(os.path.join(self.material, "qa"), exist_ok=True)
        with open(os.path.join(self.material, "qa", "origin.json"), "w") as f:
            json.dump(dict({"session_id": sid, "cwd": self.repo, "agent": agent}, **extra), f)

    def opencode_calls(self):
        return [c for c in self.calls() if c.get("agent") == "opencode"]

    def ask_opencode(self, question, **extra):
        before = len(self.opencode_calls())
        item = self.ask(question, **extra)
        self.assertGreater(len(self.opencode_calls()), before, "opencode が動いていない")
        return item

    def test_forks_the_session_then_resumes_it(self):
        self.opencode_session("ses_origin")
        self.origin("ses_origin")
        self.start()
        first = self.ask_opencode("一つ目")
        self.assertEqual((first["status"], first["session"]), ("answered", "fork"), first)
        self.assertEqual(first["answer_html"], "<p>回答: 一つ目</p>")
        c1 = self.opencode_calls()[-1]
        self.assertEqual(c1["argv"][-3:], ["--session", "ses_origin", "--fork"])
        self.assertEqual(c1["child"], "1")
        self.assertIn("回答の書き出し先", c1["prompt"])
        forked = json.loads(self.read("qa", "session.json"))
        self.assertEqual(forked["agent"], "opencode")
        self.assertEqual(forked["forked_from"], "ses_origin")
        second = self.ask_opencode("二つ目")
        self.assertEqual((second["status"], second["session"]), ("answered", "resume"), second)
        c2 = self.opencode_calls()[-1]
        self.assertEqual(c2["argv"][-2:], ["--session", forked["session_id"]])
        self.assertNotIn("--fork", c2["argv"])

    def test_failed_fork_retries_with_a_new_session(self):
        self.opencode_session("too-long-origin")
        self.origin("too-long-origin")
        self.start()
        item = self.ask_opencode("やり直す")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"), item)
        self.assertNotIn("--session", self.opencode_calls()[-1]["argv"])

    def test_progress_and_failure_are_reported(self):
        self.opencode_session("ses_origin")
        self.origin("ses_origin")
        self.start()
        steps = [st["text"] for st in self.ask_opencode("途中経過")["progress"]]
        self.assertIn("資料を読んでいます", steps)
        self.assertIn("Web で検索しています: visual explain", steps)
        self.assertIn("回答を書いています", steps)
        self.assertTrue(any(s.startswith("コードを検索しています: GRACE_DAYS") for s in steps), steps)
        fail = self.ask("FAIL して")
        self.assertEqual(fail["status"], "failed", fail)
        self.assertIn("opencode が失敗した", fail["error"])

    def test_sends_the_child_config_and_permissions(self):
        self.opencode_session("ses_origin")
        self.origin("ses_origin")
        self.start()
        self.ask_opencode("設定")
        call = self.opencode_calls()[-1]
        self.assertEqual(call["argv"][:5], ["run", "--standalone", "--format", "json", "--agent"])
        cfg = json.loads(call["config"])
        self.assertIn("visual-explain-child", cfg["agents"])
        rules = cfg["agents"]["visual-explain-child"]["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        self.assertIn({"action": "read", "resource": "*", "effect": "allow"}, rules)
        self.assertIn({"action": "webfetch", "resource": "*", "effect": "allow"}, rules)
        # 子が許されているのは、作業用コピー・回答・画像・検査だけ
        self.assertIn({"action": "edit", "resource": os.path.join(self.material, "explanation.qa-draft.html"),
                       "effect": "allow"}, rules)
        check = os.path.join(SCRIPTS, "check-explanation.py")
        self.assertIn({"action": "shell", "resource": "python3 -I %s*" % os.path.realpath(check),
                       "effect": "allow"}, rules)
        self.assertNotIn("question", [r["action"] for r in rules])

    def test_local_only_does_not_allow_web(self):
        self.opencode_session("ses_origin")
        self.origin("ses_origin")
        self.start(local_only=True)
        item = self.ask_opencode("外を見ないで")
        self.assertEqual(item["status"], "answered", item)
        cfg = json.loads(self.opencode_calls()[-1]["config"])
        rules = cfg["agents"]["visual-explain-child"]["permissions"]
        self.assertEqual([r for r in rules if r["action"] in ("webfetch", "websearch")], [])

    def test_new_session_when_the_recorded_session_is_gone(self):
        db = self.opencode_db()  # 会話の記録は在るが、記録した ID は無い
        db.execute("create table if not exists session_v2 (id text, title text, directory text)")
        db.commit()
        db.close()
        self.origin("ses_gone")
        self.start()
        item = self.ask_opencode("新しく")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"), item)
        argv = self.opencode_calls()[-1]["argv"]
        self.assertFalse([a for a in argv if "ses_" in a], argv)

    def test_agent_option_picks_opencode_for_new_sessions(self):
        self.start(agent="opencode")
        item = self.ask_opencode("新しく")
        self.assertEqual((item["status"], item["session"]), ("answered", "new"), item)
        self.assertEqual(self.state()["qa"]["agent_name"], "OpenCode")

    def test_falls_back_when_opencode_is_missing(self):
        self.opencode_session("ses_origin")
        self.origin("ses_origin")
        self.start(opencode=[os.path.join(self.tmp, "no-opencode")])
        item = self.ask("opencode が無い")
        self.assertEqual(item["status"], "answered", item)
        self.assertEqual(self.opencode_calls(), [])

    def test_origin_command_records_the_opencode_session(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_CONFIG_DIR")}
        env["OPENCODE_SESSION_ID"] = "ses_origin"
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "origin", self.material,
                              "--cwd", self.repo], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        origin = json.loads(self.read("qa", "origin.json"))
        self.assertEqual((origin["agent"], origin["session_id"]), ("opencode", "ses_origin"))


class PdfTest(ServeTestBase):
    """資料を PDF にしてダウンロードする。Chrome は代役（--print-to-pdf の先に書いて、渡された引数を残す）。"""

    def setUp(self):
        super().setUp()
        self.fake = os.path.join(self.tmp, "fake-chrome.py")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write("import json, sys\n"
                    "out = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--print-to-pdf=')][0]\n"
                    "open(out, 'wb').write(b'%PDF-1.4 ' + json.dumps(sys.argv[1:]).encode())\n")
        self.saved = serve._chrome
        serve._chrome = (sys.executable, [self.fake])

    def tearDown(self):
        serve._chrome = self.saved
        super().tearDown()

    def test_downloads_the_document_file_as_pdf(self):
        self.start()
        status, headers, body = self.req("GET", "/__qa/pdf?doc=" + DOC)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertTrue(headers["content-disposition"].startswith("attachment;"), headers)
        self.assertIn("filename*=UTF-8''", headers["content-disposition"])
        self.assertTrue(body.startswith(b"%PDF"))
        argv = json.loads(body[len(b"%PDF-1.4 "):])
        # 質問欄を差し込んだページではなく、資料のファイルそのものを印刷する
        self.assertEqual(argv[-1], pathlib.Path(os.path.join(self.material, "explanation.html")).as_uri())
        self.assertIn("--no-pdf-header-footer", argv)

    def test_unknown_document_is_404(self):
        self.start()
        self.assertEqual(self.req("GET", "/__qa/pdf?doc=topic/nothing.html")[0], 404)
        self.assertEqual(self.req("GET", "/__qa/pdf?doc=../etc/passwd")[0], 404)

    def test_without_chrome_tells_the_page_to_print(self):
        serve._chrome = None
        saved = serve.find_chrome
        serve.find_chrome = lambda: None
        try:
            self.start()
            status, _, body = self.req("GET", "/__qa/pdf?doc=" + DOC)
        finally:
            serve.find_chrome = saved
        self.assertEqual(status, 503)
        self.assertIn("Chrome", json.loads(body)["error"])

    def test_page_has_the_button_with_or_without_qa(self):
        for qa in (True, False):
            with self.subTest(qa=qa):
                self.start(qa=qa)
                _, headers, page = self.req("GET", "/" + DOC)
                page = page.decode("utf-8")
                self.assertIn('<script src="/__qa/pdf.js" defer data-doc="%s"></script>' % DOC, page)
                self.assertIn('href="/__qa/pdf.css"', page)
                self.assertEqual("/__qa/qa.js" in page, qa)
                # 質問を受け付けない配信は、これまでどおり資料に CSP を付けない
                self.assertEqual("content-security-policy" in headers, qa)
                for asset in ("/__qa/pdf.js", "/__qa/pdf.css"):
                    self.assertEqual(self.req("GET", asset, cookie=False)[0], 200)
                self.assertEqual(self.req("GET", "/__qa/pdf?doc=" + DOC)[0], 200)
                self.httpd.shutdown()
                self.httpd.server_close()
                self.app.stop()
                self.httpd = self.app = None


class PanelWidthTest(ServeTestBase):
    """質問欄の幅を配信しているマシンに覚え、どの配信サーバー（ポート・アドレス）の資料も同じ幅で開く。"""

    def setUp(self):
        super().setUp()
        self.prefs = os.path.join(self.tmp, "ask-home", "viewer.json")
        self.others = []

    def tearDown(self):
        for httpd, app in self.others:
            httpd.shutdown()
            httpd.server_close()
            app.stop()
        super().tearDown()

    def doc_tag(self, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        conn.request("GET", "/" + DOC, headers={"Cookie": "ve_qa_%d=%s" % (port or self.port, TOKEN)})
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        return re.search(r'<script src="/__qa/qa.js"[^>]*>', body).group(0)

    def test_width_is_shared_by_every_server_on_the_machine(self):
        self.start(prefs=self.prefs)
        self.assertNotIn("data-width", self.doc_tag())
        status, _, data = self.req("POST", "/__qa/prefs", {"width": 700})
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data), {"saved": True, "width": 700})
        self.assertEqual(serve.read_json(self.prefs), {"width": 700})
        self.assertIn('data-width="700"', self.doc_tag())
        # 別のポートで配信している資料も、覚えた幅で開く（ブラウザの保存は配信元ごとに別々なので引き継がれない）
        other = serve.App(serve.Config(root=self.root, bind="127.0.0.1", port=0, repo=self.repo, token=TOKEN,
                                       claude=[sys.executable, FAKE], browser_check=False, prefs=self.prefs))
        httpd = serve.make_server(other)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        other.start()
        self.others.append((httpd, other))
        self.assertIn('data-width="700"', self.doc_tag(httpd.server_address[1]))

    def test_rejects_widths_it_should_not_keep(self):
        self.start(prefs=self.prefs)
        for width in ("700", 700.5, True, 100, 99999, None):
            status, _, _ = self.req("POST", "/__qa/prefs", {"width": width})
            self.assertEqual(status, 400, width)
        self.assertEqual(self.req("POST", "/__qa/prefs", {"width": 700}, cookie=False)[0], 401)
        self.assertEqual(self.req("POST", "/__qa/prefs", {"width": 700}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertFalse(os.path.exists(self.prefs))

    def test_broken_or_missing_file_falls_back_to_the_browser(self):
        os.makedirs(os.path.dirname(self.prefs))
        with open(self.prefs, "w") as f:
            f.write("{壊れた")
        self.start(prefs=self.prefs)
        self.assertNotIn("data-width", self.doc_tag())
        self.assertEqual(self.req("POST", "/__qa/prefs", {"width": 520})[0], 200)
        self.assertIn('data-width="520"', self.doc_tag())

    def test_without_a_place_only_the_browser_remembers(self):
        self.start()
        status, _, data = self.req("POST", "/__qa/prefs", {"width": 700})
        self.assertEqual((status, json.loads(data)), (200, {"saved": False}))
        self.assertNotIn("data-width", self.doc_tag())


@unittest.skipUnless(check.find_chrome(None), "ヘッドレス Chrome が見つからない")
class WidgetTest(ServeTestBase):
    def test_renders_answers_safely(self):
        os.makedirs(os.path.join(self.material, "qa"))
        danger = ('<p>安全な回答</p><script>document.title="pwned"</script>'
                  '<p style="background-image:\\75 rl(https://x.example/)">スタイル</p>'
                  '<img src="x" onerror="document.title=\'pwned\'"><a href="javascript:alert(1)">リンク</a>'
                  '<svg viewBox="0 0 10 10"><title>図</title><rect width="5" height="5"/></svg>')
        with open(os.path.join(self.material, "qa", "qa.json"), "w") as f:
            json.dump({"items": [{"id": "q1", "question": "質問の本文", "status": "answered", "answer_html": danger,
                                  "doc": {"updated": True, "sections": [{"id": "flow", "title": "流れ"}]},
                                  "progress": [{"text": "資料を読んでいます"}, {"text": "回答を書いています"}],
                                  "answer_check": {"errors": ["Markdown の記法"], "count": 1}},
                                 {"id": "q2", "question": "取り消した質問", "status": "canceled"},
                                 {"id": "q3", "question": "続きの質問", "status": "answered", "answer_html": "<p>続きの回答</p>",
                                  "reply_to": "q1", "quote": "安全な", "quote_from": "answer"}]}, f)
        doc = self.read("explanation.html").replace('<nav><a href="#flow">', '<nav><a href="#context">前提</a><a href="#flow">', 1)
        # 古い雛形の図（width が無く、幅いっぱいに広げる CSS）
        doc = doc.replace("</main>", '<div class="diagram"><svg viewBox="0 0 640 90"><title>古い図</title></svg></div></main>')
        self.write("explanation.html", doc)
        port = self.start()
        url = "http://127.0.0.1:%d/%s?k=%s" % (port, DOC, TOKEN)
        user_data = os.path.join(self.tmp, "chrome")
        out = subprocess.run([check.find_chrome(None), "--headless", "--no-sandbox", "--disable-gpu",
                              "--user-data-dir=" + user_data, "--virtual-time-budget=8000", "--dump-dom", url],
                             capture_output=True, text=True, timeout=60).stdout
        root = out[out.index('id="ve-qa-root"'):]
        self.assertIn("質問の本文", root)
        self.assertIn("安全な回答", root)
        self.assertIn("<svg", root)
        self.assertIn("流れ", root)
        self.assertNotIn("onerror", root)
        self.assertNotIn("style=", root)
        self.assertNotIn("javascript:", root)
        self.assertNotIn("pwned", out)
        self.assertIn("別ウィンドウで開く", root)
        self.assertIn('class="ve-qa-resize"', root)
        self.assertIn('role="separator"', root)
        self.assertIn("調べた手順（2）", root)
        self.assertIn("回答の図・リンクの検査で問題が 1 件", root)
        self.assertIn("取り消し済み", root)
        self.assertIn("回答を資料に書き出す", root)
        self.assertNotIn("ve-qa-cancel", root)
        # 続きの質問は元の質問のすぐ下に並び、回答した質問には続けて質問できる
        self.assertLess(root.index("続きの質問"), root.index("取り消した質問"))
        self.assertIn("Q1 の回答への質問 · 「安全な」", root)
        self.assertRegex(root, r'class="ve-qa-item ve-qa-reply" id="ve-qa-q3"')
        self.assertEqual(root.count("この回答に質問する"), 2)
        self.assertIn('<svg viewBox="0 0 640 90" width="640" height="90">', out)
        # 読んでいる節（先頭の「前提」）の目次の項目が強調される
        self.assertRegex(out, r'<a href="#context"[^>]*aria-current="true"')
        self.assertNotRegex(out, r'<a href="#flow"[^>]*aria-current')

    def test_window_mode_renders_the_list(self):
        os.makedirs(os.path.join(self.material, "qa"))
        with open(os.path.join(self.material, "qa", "qa.json"), "w") as f:
            json.dump({"items": [{"id": "q1", "question": "別ウィンドウの質問", "status": "answered",
                                  "answer_html": "<p>別ウィンドウの回答</p>"}]}, f)
        port = self.start()
        url = "http://127.0.0.1:%d/__qa/panel?doc=%s&k=%s" % (port, DOC, TOKEN)
        out = subprocess.run([check.find_chrome(None), "--headless", "--no-sandbox", "--disable-gpu",
                              "--user-data-dir=" + os.path.join(self.tmp, "chrome"), "--virtual-time-budget=8000",
                              "--dump-dom", url], capture_output=True, text=True, timeout=60).stdout
        root = out[out.index('id="ve-qa-root"'):]
        self.assertIn("ve-qa-standalone", out)
        self.assertIn("別ウィンドウの質問", root)
        self.assertIn("別ウィンドウの回答", root)
        self.assertIn("資料に戻す", root)
        self.assertNotIn("ve-qa-toggle", root)
        self.assertNotIn("ve-qa-resize", root)

    def test_opens_with_the_width_the_machine_remembers(self):
        prefs = os.path.join(self.tmp, "ask-home", "viewer.json")
        serve.write_json(prefs, {"width": 700})
        port = self.start(prefs=prefs)
        url = "http://127.0.0.1:%d/%s?k=%s" % (port, DOC, TOKEN)
        out = subprocess.run([check.find_chrome(None), "--headless", "--no-sandbox", "--disable-gpu", "--window-size=1600,900",
                              "--user-data-dir=" + os.path.join(self.tmp, "chrome"), "--virtual-time-budget=8000",
                              "--dump-dom", url], capture_output=True, text=True, timeout=60).stdout
        self.assertIn("--ve-qa-width: 700px", out)
        self.assertIn('aria-valuenow="700"', out)


CODEX_QUESTIONS = [
    {"id": "plan", "header": "案", "question": "どちらの案にしますか？",
     "options": [{"label": "案A", "description": "一覧で直す"}, {"label": "案B", "description": "詳細で直す"}]},
    {"id": "where", "header": "場所", "question": "直す所は？",
     "options": [{"label": "フック", "description": ""}, {"label": "配信", "description": ""}]},
]

# OpenCode の question ツールの質問（plugin が ask_hook.py --agent opencode に渡す形。id が無く multiple がある）
OPENCODE_QUESTIONS = [
    {"header": "案", "question": "どちらの案にしますか？",
     "options": [{"label": "案A", "description": "一覧で直す"}, {"label": "案B", "description": "詳細で直す"}]},
    {"header": "範囲", "question": "直す所は？", "multiple": True,
     "options": [{"label": "フック", "description": ""}, {"label": "配信", "description": ""}]},
]

QUESTIONS = [
    {"question": "どちらの案にしますか？", "header": "案", "multiSelect": False,
     "options": [{"label": "案A", "description": "フックが待つ"},
                 {"label": "案B", "description": "裏で待つ", "preview": "A  B\n1  2"}]},
    {"question": "直す所は？", "header": "範囲", "multiSelect": True,
     "options": [{"label": "サーバー", "description": ""}, {"label": "ページ", "description": ""},
                 {"label": "フック", "description": ""}]},
]


class AskTest(ServeTestBase):
    """AskUserQuestion の質問を資料のページで答える（ask_hook.py が使う API）。"""

    def register(self, questions=None, **extra):
        body = {"session_id": "origin-ask", "cwd": self.repo, "questions": QUESTIONS if questions is None else questions,
                "wait": 60}
        body.update(extra)
        status, _, data = self.req("POST", "/__qa/ask", body)
        return status, json.loads(data)

    def ask_state(self, doc):
        status, _, data = self.req("GET", "/__qa/state?doc=" + doc)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def answer(self, doc, answers):
        status, _, data = self.req("POST", "/__qa/answer", {"doc": doc, "answers": answers})
        return status, json.loads(data)

    def good(self):
        return {"どちらの案にしますか？": {"selected": ["案B"], "notes": "速いから"},
                "直す所は？": {"selected": ["ページ", "サーバー"], "other": "テスト"}}

    def test_pages_name_the_agent_that_asked(self):
        self.start()
        status, data = self.register(QUESTIONS[:1], session_id="thread-1", agent="codex", build="never")
        self.assertEqual(status, 201, data)
        page = self.req("GET", "/" + data["doc"])[2].decode()
        self.assertIn("Codex からの質問", page)
        self.assertNotIn("Claude Code", page)
        status, data = self.register(QUESTIONS[1:], build="never")
        self.assertIn("Claude Code からの質問", self.req("GET", "/" + data["doc"])[2].decode())
        inbox = self.req("GET", "/__qa/inbox")[2].decode()
        self.assertIn("エージェントからの質問", inbox)
        asks = json.loads(self.req("GET", "/__qa/asks")[2])["asks"]
        self.assertEqual(sorted(a["agent_name"] for a in asks), ["Claude Code", "Codex"])

    def test_headline_is_the_leading_sentence_not_the_whole_question(self):
        long = ("【DEV-1】拠点切替後に元のユーザーが削除された場合の振る舞いをどれにしますか？"
                "（状態ごとに検討した結果。①は切り戻せる。②は戻る先が無い）")
        self.assertEqual(serve.ask_headline(long), "【DEV-1】拠点切替後に元のユーザーが削除された場合の振る舞いをどれにしますか？")
        # 最初の文が短ければ次の文まで、改行があれば最初の行まで、句点が無ければそのまま
        self.assertEqual(serve.ask_headline("確認です。この変更をどのファイルに入れますか？補足です。"),
                         "確認です。この変更をどのファイルに入れますか？")
        self.assertEqual(serve.ask_headline("\n どの案にしますか\n案Aは速い"), "どの案にしますか")
        self.assertEqual(serve.ask_headline("どちらの案にしますか？"), "どちらの案にしますか？")
        self.start()
        questions = json.loads(json.dumps(QUESTIONS))
        questions[0].update(question=long, header="DEV-1")
        status, reg = self.register(questions, build="never")
        self.assertEqual(status, 201, reg)
        page = self.req("GET", "/" + reg["doc"])[2].decode()
        self.assertIn("<h1>【DEV-1】拠点切替後に元のユーザーが削除された場合の振る舞いをどれにしますか？</h1>", page)
        self.assertIn("-webkit-line-clamp: 2", page)
        # 全文は本文の節に出る。質問の文に含まれるラベルは重ねず、含まれないラベルは残す
        self.assertIn("<h2>%s</h2>" % long, page)
        self.assertIn("<h2>範囲 · 直す所は？</h2>", page)

    def test_shows_the_choices_and_builds_a_doc_from_the_session(self):
        os.makedirs(os.path.join(self.claude_home, "projects", "-some-project"))
        with open(os.path.join(self.claude_home, "projects", "-some-project", "origin-ask.jsonl"), "w"):
            pass
        self.start()
        questions = json.loads(json.dumps(QUESTIONS))
        questions[0]["options"][0]["label"] = "案A BUILD"
        # 質問したセッションのアカウント（設定フォルダ）で、そのアカウントの会話を分岐する
        status, reg = self.register(questions, config_dir=self.claude_home)
        self.assertEqual(status, 201, reg)
        doc = reg["doc"]
        self.assertRegex(doc, r"^\d{8}-\d{6}-[0-9a-f]{6}/explanation\.html$")
        status, headers, page = self.req("GET", "/" + doc, cookie=False)
        page = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("どちらの案にしますか？", page)
        self.assertIn("A  B\n1  2", page)
        self.assertIn("content-security-policy", headers)
        st = self.ask_state(doc)
        self.assertEqual(st["ask"]["status"], "waiting")
        self.assertTrue(st["ask"]["waiting"])
        self.assertEqual(st["ask"]["where"], "repo")
        self.assertEqual(st["items"][0]["kind"], "build")
        # 資料作りは、質問したセッションを分岐し、その作業ディレクトリで動く。検査はそのリポジトリで行う
        self.assertTrue(self.app.wait_idle(30))
        call = self.calls()[0]
        self.assertIn("--fork-session", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--resume") + 1], "origin-ask")
        self.assertEqual(call["cwd"], self.repo)
        self.assertEqual(call["config_dir"], self.claude_home)
        self.assertIn("質問1（案）: どちらの案にしますか？", call["question"])
        self.assertIn("選択肢1-1: 案A BUILD", call["question"])
        item = self.ask_state(doc)["items"][0]
        self.assertEqual(item["status"], "answered", item)
        self.assertTrue(item["doc"]["updated"], item)
        m = serve.Material(self.app.cfg, doc)
        self.assertIn("--repo", self.app.check_argv(m, m.draft))
        self.assertIn(self.repo, self.app.check_argv(m, m.draft))
        self.assertIn("--ask", self.app.check_argv(m, m.draft))
        with open(m.path, encoding="utf-8") as f:
            self.assertIn("検査用の資料", f.read())
        self.assertIn("資料を書いています", [p["text"] for p in item["progress"]])

    def test_answer_has_the_askuserquestion_shape(self):
        self.start()
        _, reg = self.register(build="never")
        doc = reg["doc"]
        self.assertEqual(self.ask_state(doc)["items"], [])
        status, view = self.answer(doc, self.good())
        self.assertEqual(status, 200, view)
        self.assertEqual(view["status"], "answered")
        # 複数選んだときは選択肢の順に「, 」でつなぎ、「その他」は最後に足す
        self.assertEqual(view["answers"], {"どちらの案にしますか？": "案B", "直す所は？": "サーバー, ページ, テスト"})
        self.assertEqual(view["annotations"], {"どちらの案にしますか？": {"notes": "速いから"}})
        status, _, data = self.req("GET", "/__qa/ask-status?doc=" + doc)
        self.assertEqual(json.loads(data)["answers"]["どちらの案にしますか？"], "案B")
        status, view = self.answer(doc, self.good())
        self.assertEqual(status, 409)

    def test_rejects_answers_that_do_not_fit(self):
        self.start()
        _, reg = self.register(build="never")
        doc = reg["doc"]
        bad = [
            {"どちらの案にしますか？": {"selected": ["案C"]}, "直す所は？": {"selected": ["ページ"]}},
            {"どちらの案にしますか？": {"selected": ["案A", "案B"]}, "直す所は？": {"selected": ["ページ"]}},
            {"どちらの案にしますか？": {"selected": ["案A"], "other": "どちらでも"}, "直す所は？": {"selected": ["ページ"]}},
            {"どちらの案にしますか？": {"selected": ["案A"]}, "直す所は？": {"selected": []}},
            {"どちらの案にしますか？": {"selected": ["案A"]}},
        ]
        for answers in bad:
            status, data = self.answer(doc, answers)
            self.assertEqual(status, 400, (answers, data))
        self.assertEqual(self.ask_state(doc)["ask"]["status"], "waiting")
        status, data = self.answer(doc, {"どちらの案にしますか？": {"other": "案Aを少し変えて"}, "直す所は？": {"other": "全部"}})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["answers"], {"どちらの案にしますか？": "案Aを少し変えて", "直す所は？": "全部"})

    def test_refuses_answers_when_the_hook_stopped_waiting(self):
        self.start()
        _, reg = self.register(build="never")
        doc = reg["doc"]
        m = serve.Material(self.app.cfg, doc)
        self.app.waiters[m.dir] = 0
        self.assertFalse(self.ask_state(doc)["ask"]["waiting"])
        status, data = self.answer(doc, self.good())
        self.assertEqual(status, 409)
        self.assertIn("待っていない", data["error"])
        # フックの問い合わせが来れば、また答えられる
        self.req("GET", "/__qa/ask-status?doc=" + doc)
        self.assertEqual(self.answer(doc, self.good())[0], 200)

    def test_terminal_and_expiry_close_the_question(self):
        self.start()
        _, reg = self.register(build="never")
        status, _, data = self.req("POST", "/__qa/ask-close", {"doc": reg["doc"], "reason": "terminal"})
        self.assertEqual(json.loads(data)["status"], "terminal")
        self.assertEqual(self.answer(reg["doc"], self.good())[0], 409)
        _, reg = self.register(build="never")
        m = serve.Material(self.app.cfg, reg["doc"])
        data = serve.read_json(m.ask_path)
        data["expires_at"] = serve.now(-5)
        serve.write_json(m.ask_path, data)
        self.assertEqual(self.ask_state(reg["doc"])["ask"]["status"], "expired")
        status, data = self.answer(reg["doc"], self.good())
        self.assertEqual(status, 409)
        self.assertEqual(serve.read_json(m.ask_path)["status"], "expired")

    def test_answering_stops_the_doc_being_built(self):
        self.start(answer_timeout=10)
        questions = json.loads(json.dumps(QUESTIONS))
        questions[0]["options"][0]["label"] = "案A SLEEP"
        _, reg = self.register(questions)
        doc = reg["doc"]
        end = time.time() + 10
        while time.time() < end and self.ask_state(doc)["items"][0]["status"] != "answering":
            time.sleep(0.05)
        good = self.good()
        good["どちらの案にしますか？"] = {"selected": ["案A SLEEP"]}
        self.assertEqual(self.answer(doc, good)[0], 200)
        self.assertTrue(self.app.wait_idle(15))
        self.assertEqual(self.ask_state(doc)["items"][0]["status"], "canceled")

    def test_lists_questions_for_the_inbox(self):
        self.start()
        _, first = self.register(build="never")
        time.sleep(1.1)  # フォルダ名は秒まで
        _, second = self.register(build="never")
        self.answer(first["doc"], self.good())
        status, _, data = self.req("GET", "/__qa/asks")
        asks = json.loads(data)["asks"]
        self.assertEqual([a["doc"] for a in asks], [second["doc"], first["doc"]])
        self.assertEqual([a["status"] for a in asks], ["waiting", "answered"])
        self.assertEqual(asks[0]["question"], "どちらの案にしますか？")
        status, headers, page = self.req("GET", "/__qa/inbox")
        self.assertEqual(status, 200)
        self.assertIn(b"/__qa/inbox.js", page)
        self.assertEqual(self.req("GET", "/__qa/inbox.js")[0], 200)
        self.assertEqual(self.req("GET", "/__qa/asks", cookie=False)[0], 401)

    def test_build_stops_when_the_hook_stops_waiting(self):
        self.start(answer_timeout=20)
        questions = json.loads(json.dumps(QUESTIONS))
        questions[0]["options"][0]["label"] = "案A SLEEP"
        _, reg = self.register(questions)
        doc = reg["doc"]
        end = time.time() + 10
        while time.time() < end and self.ask_state(doc)["items"][0]["status"] != "answering":
            time.sleep(0.05)
        self.app.waiters[serve.Material(self.app.cfg, doc).dir] = 0  # フックが止められた
        self.assertTrue(self.app.wait_idle(15))
        self.assertEqual(self.ask_state(doc)["items"][0]["status"], "canceled")
        # 資料作りの項目は、読者の質問と回答として書き出さない
        self.assertEqual(self.req("POST", "/__qa/export", {"doc": doc})[0], 400)

    def test_page_questions_still_work_after_answering(self):
        self.start()
        _, reg = self.register(build="never")
        self.answer(reg["doc"], self.good())
        status, _, data = self.req("POST", "/__qa/questions", {"doc": reg["doc"], "question": "案Bの欠点は？"})
        self.assertEqual(status, 202, data)
        self.assertTrue(self.app.wait_idle(30))
        item = self.ask_state(reg["doc"])["items"][-1]
        self.assertEqual(item["status"], "answered", item)
        self.assertEqual(self.calls()[-1]["cwd"], self.repo)  # 質問したセッションの作業ディレクトリで答える

    def test_prunes_old_closed_questions(self):
        self.start()
        _, old = self.register(build="never")
        _, kept = self.register(build="never")
        self.req("POST", "/__qa/ask-close", {"doc": old["doc"], "reason": "terminal"})
        past = time.time() - 40 * 86400
        for reg in (old, kept):
            m = serve.Material(self.app.cfg, reg["doc"])
            os.utime(m.ask_path, (past, past))
        self.register(build="never")
        self.assertFalse(os.path.exists(os.path.join(self.root, old["doc"].split("/")[0])))
        # 古くても、フックが待っている質問は残す
        self.assertTrue(os.path.exists(os.path.join(self.root, kept["doc"].split("/")[0])))
        self.assertTrue(os.path.isdir(self.material))  # 質問ではない資料には触らない

    def test_rejects_bad_registrations(self):
        self.start()
        self.assertEqual(self.req("POST", "/__qa/ask", {"session_id": "s", "cwd": self.repo, "questions": QUESTIONS},
                                  cookie=False)[0], 401)
        for body in ({"questions": []}, {"questions": [{"question": "q", "options": []}]},
                     {"questions": [QUESTIONS[0], QUESTIONS[0]]}, {"cwd": os.path.join(self.tmp, "nope")},
                     {"session_id": "../x"}):
            status, data = self.register(**body)
            self.assertEqual(status, 400, (body, data))

    def dump(self, path):
        url = "http://127.0.0.1:%d/%s?k=%s" % (self.port, path, TOKEN)
        return subprocess.run([check.find_chrome(None), "--headless", "--no-sandbox", "--disable-gpu",
                               "--user-data-dir=" + os.path.join(self.tmp, "chrome"), "--virtual-time-budget=6000",
                               "--dump-dom", url], capture_output=True, text=True, timeout=60).stdout

    @unittest.skipUnless(check.find_chrome(None), "Chrome が無い")
    def test_page_shows_the_choices_and_the_answer(self):
        # つないだままの接続があると、ヘッドレス Chrome の仮想時間が進まない（取り出せない）ので切る
        self.start(presence=False)
        _, reg = self.register(build="never")
        out = self.dump(reg["doc"])
        root = out[out.index('id="ve-qa-root"'):]
        self.assertIn("Claude Code からの質問", root)
        self.assertIn('data-status="waiting"', root)
        self.assertIn('type="radio" name="ve-qa-ask-0"', root)
        self.assertIn('type="checkbox" name="ve-qa-ask-1"', root)
        self.assertIn("この答えを送る", root)
        self.assertIn("ターミナルで答える", root)
        self.assertIn("A  B\n1  2", root)
        self.assertRegex(out, r'<aside id="ve-qa-panel"[^>]*>')  # 質問が来たら質問欄を開く
        self.assertNotRegex(out, r'<aside id="ve-qa-panel"[^>]*hidden')
        self.answer(reg["doc"], self.good())
        out = self.dump(reg["doc"])
        root = out[out.index('id="ve-qa-root"'):]
        self.assertIn("答えを送りました", root)
        self.assertIn("サーバー, ページ, テスト", root)
        self.assertNotIn("この答えを送る", root)
        inbox = self.dump("__qa/inbox")
        self.assertIn("エージェントからの質問", inbox)
        self.assertIn("どちらの案にしますか？", inbox)
        self.assertIn("音で知らせる", inbox)
        self.assertIn("デスクトップ通知", inbox)

    @unittest.skipUnless(check.find_chrome(None), "Chrome が無い")
    def test_page_names_codex_when_codex_asked(self):
        self.start(presence=False)
        _, reg = self.register(QUESTIONS[:1], session_id="thread-1", agent="codex", build="never")
        out = self.dump(reg["doc"])
        root = out[out.index('id="ve-qa-root"'):]
        self.assertIn("Codex からの質問", root)
        self.assertIn("答えが Codex の作業に戻ります", root)
        self.assertNotIn("Claude Code", root)

    @unittest.skipUnless(check.find_chrome(None), "Chrome が無い")
    def test_page_lists_the_other_waiting_questions(self):
        self.start(presence=False)
        _, first = self.register(build="never")
        q = json.loads(json.dumps(QUESTIONS))
        q[0]["question"] = "別のセッションの質問"
        self.register(q, build="never", session_id="other-session")
        out = self.dump(first["doc"])
        root = out[out.index('id="ve-qa-root"'):]
        self.assertIn("ほかに答えを待っている質問 1 件", root)
        self.assertIn("別のセッションの質問", root)
        self.assertIn("音で知らせる", root)
        self.assertIn("デスクトップ通知", root)  # 127.0.0.1 は安全な配信元なので、デスクトップ通知の切り替えが出る
        self.assertRegex(out, r"<title>\(2\) ")  # 答えを待っている質問の件数


class AskHookTest(ServeTestBase):
    """ask_hook.py（AskUserQuestion の直前に動くフック）。"""
    HOOK = os.path.join(SCRIPTS, "ask_hook.py")

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "ask-home")
        os.environ["VISUAL_EXPLAIN_ASK_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("VISUAL_EXPLAIN_ASK_HOME", None)
        super().tearDown()

    def configure(self, **conf):
        base = {"mode": "always", "bind": "127.0.0.1", "port": self.port, "token": TOKEN, "root": self.root,
                "wait": 30, "build": "never"}
        base.update(conf)
        serve.save_ask_config(self.home, base)

    def hook(self, payload=None, timeout=40):
        payload = payload or {"session_id": "origin-ask", "cwd": self.repo, "tool_name": "AskUserQuestion",
                              "tool_input": {"questions": QUESTIONS}}
        return subprocess.Popen([sys.executable, "-I", self.HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=dict(os.environ))

    def registered(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            status, _, data = self.req("GET", "/__qa/asks")
            asks = json.loads(data)["asks"]
            if asks:
                return asks[0]["doc"]
            time.sleep(0.1)
        self.fail("フックが質問を登録しない")

    def run_hook(self, payload=None):
        proc = self.hook()
        payload = payload or {"session_id": "origin-ask", "cwd": self.repo, "tool_name": "AskUserQuestion",
                              "tool_input": {"questions": QUESTIONS}}
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        proc.stdin = None  # communicate() が閉じた標準入力に書かないように
        return proc

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
        self.assertIn("http://localhost:%d/%s" % (self.port, doc), reason)
        self.assertNotIn(TOKEN, reason)  # 合言葉をモデルに渡さない
        answers = json.loads(reason[reason.index("{"):])
        self.assertEqual(answers, {"answers": {"plan": {"answers": ["両方", "user_note: 急ぎ"]},
                                               "where": {"answers": ["フック"]}}})

    def test_codex_session_start_prints_nothing(self):
        self.start()
        self.configure(port=self.free_port())
        self.addCleanup(serve.stop_ask_server, self.home, serve.load_ask_config(self.home))
        res = subprocess.run([sys.executable, "-I", self.HOOK, "--agent", "codex", "--session-start"], input="{}",
                             capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        claude = subprocess.run([sys.executable, "-I", self.HOOK, "--session-start"], input="{}",
                                capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertIn("受け箱", json.loads(claude.stdout)["systemMessage"])  # Claude は今までどおり URL を出す

    def test_duplicate_codex_questions_fall_back_to_the_terminal(self):
        self.start()
        self.configure()
        dup = [dict(CODEX_QUESTIONS[0]), dict(CODEX_QUESTIONS[0], id="again")]
        proc = self.codex_hook({"session_id": "thread-ask", "cwd": self.repo, "tool_name": "request_user_input",
                                "tool_input": {"questions": dup}})
        out, err = proc.communicate(timeout=15)
        self.assertEqual((proc.returncode, out), (0, ""), err)

    def test_codex_hook_ignores_other_tools(self):
        self.start()
        self.configure()
        proc = self.codex_hook({"session_id": "thread-ask", "cwd": self.repo, "tool_name": "Bash",
                                "tool_input": {"command": "ls"}})
        out, err = proc.communicate(timeout=15)
        self.assertEqual((proc.returncode, out), (0, ""), err)
        self.assertEqual(json.loads(self.req("GET", "/__qa/asks")[2])["asks"], [])

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

    def test_ignores_questions_inside_the_servers_own_claude(self):
        self.start()
        self.configure()
        os.environ["VISUAL_EXPLAIN_ASK_CHILD"] = "1"
        self.addCleanup(os.environ.pop, "VISUAL_EXPLAIN_ASK_CHILD", None)
        proc = self.run_hook()
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, out), (0, ""), err)
        self.assertEqual(json.loads(self.req("GET", "/__qa/asks")[2])["asks"], [])

    def test_rejects_a_zero_wait(self):
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "ask-config", "--wait", "0"],
                             capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertEqual(out.returncode, 2)
        self.assertNotIn("wait", serve.read_ask_settings(self.home))

    def test_disabled_hook_does_nothing(self):
        self.start()
        self.configure(mode="off")
        proc = self.run_hook()
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, out), (0, ""), err)
        self.assertEqual(json.loads(self.req("GET", "/__qa/asks")[2])["asks"], [])

    def test_returns_the_page_answer_to_askuserquestion(self):
        self.start()
        self.configure()
        proc = self.run_hook()
        doc = self.registered()
        status, _, data = self.req("POST", "/__qa/answer", {"doc": doc, "answers": {
            "どちらの案にしますか？": {"selected": ["案A"], "notes": "メモ"}, "直す所は？": {"selected": ["フック"]}}})
        self.assertEqual(status, 200, data)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err)
        res = json.loads(out)["hookSpecificOutput"]
        self.assertIn("資料のページで答えました: http://localhost:%d/%s" % (self.port, doc), json.loads(out)["systemMessage"])
        self.assertEqual(res["permissionDecision"], "allow")
        self.assertEqual(res["updatedInput"]["questions"], QUESTIONS)
        self.assertEqual(res["updatedInput"]["answers"], {"どちらの案にしますか？": "案A", "直す所は？": "フック"})
        self.assertEqual(res["updatedInput"]["annotations"], {"どちらの案にしますか？": {"notes": "メモ"}})

    def test_terminal_choice_lets_the_terminal_ask(self):
        self.start()
        self.configure()
        proc = self.run_hook()
        doc = self.registered()
        self.req("POST", "/__qa/ask-close", {"doc": doc, "reason": "terminal"})
        out, err = proc.communicate(timeout=15)
        self.assertEqual((proc.returncode, out), (0, ""), err)

    def test_stopped_hook_closes_the_question(self):
        self.start()
        self.configure()
        proc = self.run_hook()
        doc = self.registered()
        time.sleep(0.5)
        proc.terminate()
        proc.communicate(timeout=10)
        st = json.loads(self.req("GET", "/__qa/state?doc=" + doc)[2])
        self.assertEqual(st["ask"]["status"], "terminal")

    def test_starts_and_stops_the_resident_server(self):
        os.environ["VISUAL_EXPLAIN_CLAUDE"] = FAKE
        self.addCleanup(os.environ.pop, "VISUAL_EXPLAIN_CLAUDE", None)
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "ask-config", "--enable",
                              "--bind", "127.0.0.1", "--port", str(port), "--wait", "2m", "--build", "never"],
                             capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("/__qa/inbox?k=", out.stdout)
        self.assertIn("動いている", out.stdout)
        self.assertIn("エージェントの質問（AskUserQuestion・request_user_input・question）を資料のページで答える", out.stdout)
        conf = serve.load_ask_config(self.home)
        self.assertEqual(os.stat(os.path.join(self.home, "ask.json")).st_mode & 0o777, 0o600)
        self.assertEqual((conf["port"], conf["wait"], conf["build"]), (port, 120, "never"))
        self.assertTrue(serve.ask_server_alive(conf))
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "ask-config", "--disable"],
                             capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertIn("off", out.stdout)
        self.assertFalse(serve.ask_server_alive(conf))
        # 受け箱の配信サーバーは、合言葉の無い要求には資料のページも返さない
        self.assertIn("--private", open(os.path.join(SCRIPTS, "serve.py"), encoding="utf-8").read())


class OpenCodeHookTest(ServeTestBase):
    """ask_hook.py --agent opencode（OpenCode の plugin が question ツールの実行を差し替えて呼ぶ入り口）。"""
    HOOK = os.path.join(SCRIPTS, "ask_hook.py")

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "ask-home")
        os.environ["VISUAL_EXPLAIN_ASK_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("VISUAL_EXPLAIN_ASK_HOME", None)
        super().tearDown()

    def configure(self, **conf):
        base = {"mode": "always", "bind": "127.0.0.1", "port": self.port, "token": TOKEN, "root": self.root,
                "wait": 30, "build": "never"}
        base.update(conf)
        serve.save_ask_config(self.home, base)

    def hook(self, payload=None, env=None):
        e = dict(os.environ)
        e.update(env or {})
        proc = subprocess.Popen([sys.executable, "-I", self.HOOK, "--agent", "opencode"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        payload = payload if payload is not None else {"session_id": "ses_ask", "cwd": self.repo,
                                                       "questions": OPENCODE_QUESTIONS}
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        proc.stdin = None
        return proc

    def registered(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            asks = json.loads(self.req("GET", "/__qa/asks")[2])["asks"]
            if asks:
                return asks[0]["doc"]
            time.sleep(0.1)
        self.fail("フックが質問を登録しない")

    def last_json(self, out):
        return json.loads([ln for ln in out.strip().split("\n") if ln.strip()][-1])

    def test_returns_the_page_answer_as_the_question_result(self):
        self.start()
        self.configure()
        proc = self.hook()
        doc = self.registered()
        st = json.loads(self.req("GET", "/__qa/state?doc=" + doc)[2])
        self.assertEqual(st["ask"]["agent_name"], "OpenCode")
        self.assertEqual([q["multiSelect"] for q in st["ask"]["questions"]], [False, True])
        status, _, data = self.req("POST", "/__qa/answer", {"doc": doc, "answers": {
            "どちらの案にしますか？": {"selected": [], "other": "案A"},
            "直す所は？": {"selected": ["配信", "フック"], "notes": "急ぎ"}}})
        self.assertEqual(status, 200, data)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(self.last_json(out), {"answers": [["案A"], ["フック", "配信", "user_note: 急ぎ"]]})
        self.assertNotIn("fallback", out)  # 答えが返ったら、ターミナルには任せない

    def test_terminal_choice_falls_back(self):
        self.start()
        self.configure()
        proc = self.hook()
        doc = self.registered()
        self.req("POST", "/__qa/ask-close", {"doc": doc, "reason": "terminal"})
        out, err = proc.communicate(timeout=15)
        self.assertEqual((proc.returncode, self.last_json(out)), (0, {"fallback": True}), err)

    def test_falls_back_when_disabled(self):
        self.start()
        self.configure(mode="off")
        proc = self.hook()
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, self.last_json(out)), (0, {"fallback": True}), err)
        self.assertEqual(json.loads(self.req("GET", "/__qa/asks")[2])["asks"], [])

    def test_falls_back_inside_the_servers_own_child(self):
        self.start()
        self.configure()
        proc = self.hook(env={"VISUAL_EXPLAIN_ASK_CHILD": "1"})
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, self.last_json(out)), (0, {"fallback": True}), err)
        self.assertEqual(json.loads(self.req("GET", "/__qa/asks")[2])["asks"], [])

    def test_falls_back_for_a_question_that_cannot_be_shown(self):
        self.start()
        self.configure()
        proc = self.hook({"session_id": "ses_ask", "cwd": self.repo, "questions": "なにか"})
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, self.last_json(out)), (0, {"fallback": True}), err)
        self.assertEqual(json.loads(self.req("GET", "/__qa/asks")[2])["asks"], [])


class AskDefaultsTest(ServeTestBase):
    """既定の使い方（受け箱を開いているときだけページに出す）と、待ち受けるアドレスの既定。"""
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "ask-home")
        os.environ["VISUAL_EXPLAIN_ASK_HOME"] = self.home
        self.path = os.environ["PATH"]

    def tearDown(self):
        os.environ.pop("VISUAL_EXPLAIN_ASK_HOME", None)
        os.environ["PATH"] = self.path
        super().tearDown()

    def fake_tailscale(self, output, code=0):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        path = os.path.join(bin_dir, "tailscale")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nprintf '%s'\nexit %d\n" % (output, code))
        os.chmod(path, 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + self.path

    def test_home_follows_the_state_directory(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("VISUAL_EXPLAIN_HOME", "VISUAL_EXPLAIN_ASK_HOME", "XDG_STATE_HOME")}
        code = ("import importlib.util,sys;s=importlib.util.spec_from_file_location('s',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.ask_home())")
        run = lambda **e: subprocess.run([sys.executable, "-I", "-c", code, os.path.join(SCRIPTS, "serve.py")],
                                         capture_output=True, text=True, env=dict(env, HOME=self.tmp, **e)).stdout.strip()
        self.assertEqual(run(), os.path.join(self.tmp, ".local", "state", "visual-explain"))
        self.assertEqual(run(XDG_STATE_HOME="/x/state"), "/x/state/visual-explain")
        self.assertEqual(run(VISUAL_EXPLAIN_ASK_HOME="/y"), "/y")
        self.assertEqual(run(VISUAL_EXPLAIN_HOME="/z", VISUAL_EXPLAIN_ASK_HOME="/y"), "/z")

    def test_binds_to_tailscale_when_it_is_up(self):
        self.fake_tailscale("100.101.102.103\n")
        conf = serve.load_ask_config(self.home)
        self.assertEqual((conf["mode"], conf["bind"], conf["bind_setting"], conf["port"]), ("auto", "100.101.102.103", "auto", 8770))
        self.assertFalse(os.path.exists(os.path.join(self.home, "ask.json")))
        self.fake_tailscale("Tailscale is stopped.\n", 1)
        self.assertEqual(serve.load_ask_config(self.home)["bind"], "127.0.0.1")
        self.fake_tailscale("192.168.1.5\n")  # Tailscale の範囲（100.64.0.0/10）でないアドレスは使わない
        self.assertEqual(serve.load_ask_config(self.home)["bind"], "127.0.0.1")
        serve.save_ask_config(self.home, {"bind": "10.0.0.8"})
        self.assertEqual(serve.load_ask_config(self.home)["bind"], "10.0.0.8")

    def test_reads_old_settings(self):
        self.assertEqual(serve.ask_mode({}), "auto")
        self.assertEqual(serve.ask_mode({"enabled": True}), "always")
        self.assertEqual(serve.ask_mode({"enabled": False}), "off")
        self.assertEqual(serve.ask_mode({"enabled": True, "mode": "auto"}), "auto")

    def test_private_server_hides_pages_without_the_token(self):
        self.start(private=True)
        self.assertEqual(self.req("GET", "/" + DOC, cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/topic/images/diagram.svg", cookie=False)[0], 401)
        self.assertEqual(self.req("HEAD", "/" + DOC, cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/__qa/qa.js", cookie=False)[0], 200)
        status, headers, _ = self.req("GET", "/%s?k=%s" % (DOC, TOKEN), cookie=False)
        self.assertEqual(status, 200)
        self.assertIn("set-cookie", headers)
        self.assertEqual(self.req("GET", "/" + DOC)[0], 200)
        self.assertEqual(self.req("GET", "/topic/")[0], 404)  # フォルダの一覧は出さない

    def open_events(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/__qa/events", headers={"Cookie": "ve_qa_%d=%s" % (self.port, TOKEN)})
        res = conn.getresponse()
        self.assertEqual(res.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(res.fp.readline(), b"retry: 3000\n")
        res.fp.readline()
        return conn, res

    def viewers(self):
        return json.loads(self.req("GET", "/__qa/ping")[2])["viewers"]

    def test_events_count_viewers_and_tell_new_questions(self):
        self.start()
        self.assertEqual(self.viewers(), 0)
        conn, res = self.open_events()
        self.assertEqual(self.viewers(), 1)
        status, _, data = self.req("POST", "/__qa/ask", {"session_id": "s1", "cwd": self.repo, "questions": QUESTIONS,
                                                         "build": "never"})
        self.assertEqual(status, 201, data)
        self.assertEqual(res.fp.readline(), b"event: asks\n")
        res.close()
        conn.close()
        end = time.time() + 10
        while time.time() < end and self.viewers():
            self.app.notify_changes()  # 書き込みに失敗した所で、閉じた接続だと分かる
            time.sleep(0.2)
        self.assertEqual(self.viewers(), 0)
        self.assertEqual(self.req("GET", "/__qa/events", cookie=False)[0], 401)

    def configure(self, **conf):
        base = {"mode": "auto", "bind": "127.0.0.1", "port": self.port, "token": TOKEN, "root": self.root,
                "wait": 30, "build": "never"}
        base.update(conf)
        serve.save_ask_config(self.home, base)

    def run_hook(self, *args):
        proc = subprocess.Popen([sys.executable, "-I", os.path.join(SCRIPTS, "ask_hook.py")] + list(args),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ))
        proc.stdin.write(json.dumps({"session_id": "origin-ask", "cwd": self.repo, "tool_name": "AskUserQuestion",
                                     "tool_input": {"questions": QUESTIONS}}))
        proc.stdin.close()
        proc.stdin = None
        return proc

    def asks(self):
        return json.loads(self.req("GET", "/__qa/asks")[2])["asks"]

    def test_auto_leaves_questions_to_the_terminal_when_nobody_watches(self):
        self.start()
        self.configure()
        started = time.time()
        proc = self.run_hook()
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, out), (0, ""), err)
        self.assertLess(time.time() - started, 5)
        self.assertEqual(self.asks(), [])

    def test_auto_uses_the_page_while_someone_watches(self):
        self.start()
        self.configure()
        conn, res = self.open_events()
        proc = self.run_hook()
        end = time.time() + 10
        while time.time() < end and not self.asks():
            time.sleep(0.1)
        doc = self.asks()[0]["doc"]
        status, _, data = self.req("POST", "/__qa/answer", {"doc": doc, "answers": {
            "どちらの案にしますか？": {"selected": ["案B"]}, "直す所は？": {"selected": ["ページ"]}}})
        self.assertEqual(status, 200, data)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["updatedInput"]["answers"]["どちらの案にしますか？"], "案B")
        res.close()
        conn.close()

    def test_auto_gives_up_when_the_page_is_closed(self):
        self.start()
        self.configure(viewer_grace=2)
        conn, res = self.open_events()
        proc = self.run_hook()
        end = time.time() + 10
        while time.time() < end and not self.asks():
            time.sleep(0.1)
        doc = self.asks()[0]["doc"]
        res.close()
        conn.close()
        stop = time.time() + 15
        while time.time() < stop and proc.poll() is None:
            self.app.notify_changes()
            time.sleep(0.3)
        out, err = proc.communicate(timeout=5)
        self.assertEqual((proc.returncode, out), (0, ""), err)
        self.assertEqual(json.loads(self.req("GET", "/__qa/state?doc=" + doc)[2])["ask"]["status"], "terminal")

    def test_auto_waits_on_the_page_once_the_inbox_was_opened(self):
        self.start()
        self.configure()
        self.assertEqual(serve.ask_effective_mode(serve.load_ask_config(self.home)), "open")
        open(os.path.join(self.root, serve.INBOX_USED), "w").close()
        self.assertEqual(serve.ask_effective_mode(serve.load_ask_config(self.home)), "always")
        proc = self.run_hook()  # 受け箱を開いている人がいなくても、ページで待つ
        end = time.time() + 10
        while time.time() < end and not self.asks():
            time.sleep(0.1)
        doc = self.asks()[0]["doc"]
        time.sleep(3)
        self.assertIsNone(proc.poll())
        self.req("POST", "/__qa/answer", {"doc": doc, "answers": {
            "どちらの案にしますか？": {"selected": ["案A"]}, "直す所は？": {"selected": ["フック"]}}})
        out, err = proc.communicate(timeout=15)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["updatedInput"]["answers"]["直す所は？"], "フック")
        # open にすれば、受け箱を開いたことがあっても、開いているときだけ
        self.configure(mode="open")
        proc = self.run_hook()
        out, err = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, out), (0, ""), err)

    def test_opening_the_inbox_marks_it_used(self):
        self.start(private=True)
        self.assertEqual(self.req("GET", "/__qa/inbox", cookie=False)[0], 401)
        self.assertFalse(os.path.exists(os.path.join(self.root, serve.INBOX_USED)))
        self.assertEqual(self.req("GET", "/__qa/inbox")[0], 200)
        self.assertTrue(os.path.exists(os.path.join(self.root, serve.INBOX_USED)))

    @unittest.skipUnless(check.find_chrome(None), "Chrome が無い")
    def test_inbox_opens_a_question_that_is_already_waiting(self):
        self.start(presence=False)
        status, _, data = self.req("POST", "/__qa/ask", {"session_id": "s1", "cwd": self.repo, "questions": QUESTIONS,
                                                         "build": "never"})
        doc = json.loads(data)["doc"]
        url = "http://127.0.0.1:%d/__qa/inbox?k=%s" % (self.port, TOKEN)
        out = subprocess.run([check.find_chrome(None), "--headless", "--no-sandbox", "--disable-gpu",
                              "--user-data-dir=" + os.path.join(self.tmp, "chrome"), "--virtual-time-budget=8000",
                              "--dump-dom", url], capture_output=True, text=True, timeout=60).stdout
        self.assertIn('class="ve-qa-ask"', out)  # 受け箱から質問の資料に移った
        self.assertIn("この答えを送る", out)
        self.assertIn(doc.split("/")[0], json.dumps(self.app.list_asks()))

    def test_session_start_creates_the_token_and_starts_the_server(self):
        os.environ["VISUAL_EXPLAIN_CLAUDE"] = FAKE
        self.addCleanup(os.environ.pop, "VISUAL_EXPLAIN_CLAUDE", None)
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        serve.save_ask_config(self.home, {"bind": "127.0.0.1", "port": port})
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "ask_hook.py"), "--session-start"],
                             input="{}", capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertEqual((out.returncode, out.stdout), (0, ""), out.stderr)  # 受け箱を開いたことが無ければ何も出さない
        conf = serve.load_ask_config(self.home)
        self.assertTrue(conf.get("token"))
        end = time.time() + 15
        while time.time() < end and not serve.ask_server_alive(conf):
            time.sleep(0.2)
        try:
            self.assertTrue(serve.ask_server_alive(conf))
            self.assertEqual(serve.ask_ping(conf)["version"], serve.VERSION)
            self.assertEqual(serve.ask_request(conf, "GET", "/__qa/inbox")[0], 200)
            # 受け箱を開いた後は、セッションの開始で受け箱の URL をターミナルに出す
            out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "ask_hook.py"), "--session-start"],
                                 input="{}", capture_output=True, text=True, timeout=30, env=dict(os.environ))
            self.assertIn("/__qa/inbox?k=%s" % conf["token"], json.loads(out.stdout)["systemMessage"])
        finally:
            serve.stop_ask_server(self.home, conf)
        serve.save_ask_config(self.home, dict(serve.read_ask_settings(self.home), mode="off"))
        out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "ask_hook.py"), "--session-start"],
                             input="{}", capture_output=True, text=True, timeout=30, env=dict(os.environ))
        time.sleep(1)
        self.assertFalse(serve.ask_server_alive(conf))


class AskServerTest(unittest.TestCase):
    """常駐の配信サーバーの起動と停止（同時の起動・版の違い・ポートの取り合い）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "ask-home")
        os.environ["VISUAL_EXPLAIN_ASK_HOME"] = self.home
        os.environ["VISUAL_EXPLAIN_CLAUDE"] = FAKE
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        serve.save_ask_config(self.home, {"bind": "127.0.0.1", "port": self.port})
        self.conf = serve.load_ask_config(self.home, create=True)

    def run(self, result=None):
        # テストが失敗したら、tearDown が置き場所を消す前に配信サーバーの様子を出す
        name = self._testMethodName
        method = getattr(self, name)

        def reporting():
            try:
                return method()
            except unittest.SkipTest:
                raise
            except BaseException:
                self.dump_state()
                raise
        setattr(self, name, reporting)
        try:
            return super().run(result)
        finally:
            delattr(self, name)

    def tearDown(self):
        serve.stop_ask_server(self.home, self.conf)
        os.environ.pop("VISUAL_EXPLAIN_ASK_HOME", None)
        os.environ.pop("VISUAL_EXPLAIN_CLAUDE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dump_state(self):
        """失敗したテストの、配信サーバーの様子を標準エラーに出す。

        実際の事故: CI で、同時に起動した 3 つがどれも「起動できなかった」を返したが、理由がログに残らなかった。
        行頭の LOG は、tests/run.sh が失敗の要約に拾う目印。"""
        import socket
        lines = ["%s（待ち受け先 127.0.0.1:%d）" % (self.id(), self.port)]
        try:
            probe = serve.ask_probe(self.conf)
            lines.append("待ち受け先にいるもの: %s" % ("何もいない" if probe is None else "別の配信サーバー" if probe == "foreign"
                                                else json.dumps(probe, ensure_ascii=False)))
        except Exception as e:  # noqa: BLE001  調べている途中の失敗で、元の失敗を隠さない
            lines.append("待ち受け先を確かめられなかった: %r" % e)
        with socket.socket() as sock:
            sock.settimeout(2)
            lines.append("ポートへの接続: %s" % ("つながる" if sock.connect_ex(("127.0.0.1", self.port)) == 0 else "つながらない"))
        for name in ("ask-server.json", "ask-server.log"):
            path = os.path.join(self.home, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    body = f.read().splitlines()[-60:]
            except OSError as e:
                lines.append("%s: 読めない（%s）" % (name, e.strerror))
                continue
            lines.append("%s（%d 行）:" % (name, len(body)))
            lines += ["  " + line for line in body]
        sys.stderr.write("".join("LOG %s\n" % line for line in lines))
        sys.stderr.flush()

    def test_resident_server_keeps_the_width_next_to_its_settings(self):
        self.assertTrue(serve.ensure_ask_server(self.home, self.conf))
        status, data = serve.ask_request(self.conf, "POST", "/__qa/prefs", {"width": 640})
        self.assertEqual((status, data), (200, {"saved": True, "width": 640}))
        self.assertEqual(serve.read_json(os.path.join(self.home, "viewer.json")), {"width": 640})

    def test_starting_together_leaves_one_server_that_can_be_stopped(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(serve.ensure_ask_server(self.home, self.conf)))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(results, [True, True, True])
        ping = serve.ask_ping(self.conf)
        # 記録は、待ち受けを始めた配信サーバー自身が書く
        self.assertEqual(serve.read_json(os.path.join(self.home, "ask-server.json"))["pid"], ping["pid"])
        serve.write_json(os.path.join(self.home, "ask-server.json"), {"pid": 1})  # 記録が古くても止められる
        self.assertTrue(serve.stop_ask_server(self.home, self.conf))
        self.assertFalse(serve.ask_server_alive(self.conf))

    def test_session_start_does_not_wait_for_the_lock(self):
        import fcntl
        with open(os.path.join(self.home, "ask.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            started = time.time()
            self.assertTrue(serve.ensure_ask_server(self.home, self.conf, blocking=False))
            self.assertLess(time.time() - started, 5)
        self.assertFalse(serve.ask_server_alive(self.conf))

    def test_keeps_a_newer_server_and_replaces_an_older_one(self):
        self.assertTrue(serve.ensure_ask_server(self.home, self.conf))
        pid = serve.ask_ping(self.conf)["pid"]
        real = serve.VERSION
        try:
            serve.VERSION = "0.0.1"  # 古い版のセッションから見ると、動いているのは新しい版
            self.assertTrue(serve.ensure_ask_server(self.home, self.conf))
            self.assertEqual(serve.ask_ping(self.conf)["pid"], pid)
            serve.VERSION = "999.0.0"  # 新しい版のセッションから見ると古い。止めて起動し直す
            self.assertTrue(serve.ensure_ask_server(self.home, self.conf))
            self.assertNotEqual(serve.ask_ping(self.conf)["pid"], pid)
        finally:
            serve.VERSION = real
        self.assertEqual(serve.version_key("0.17.1"), (0, 17, 1))
        self.assertLess(serve.version_key("0.9.0"), serve.version_key("0.17.0"))

    def test_does_not_fight_another_server_for_the_port(self):
        other = serve.App(serve.Config(root=self.tmp, port=self.port, bind="127.0.0.1", token="someone-else",
                                       claude=[sys.executable, FAKE], qa=False))
        httpd = serve.make_server(other)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.assertEqual(serve.ask_probe(self.conf), "foreign")
            self.assertFalse(serve.ensure_ask_server(self.home, self.conf))
            self.assertFalse(serve.stop_ask_server(self.home, self.conf))
            with open(os.path.join(self.home, "ask-server.log"), encoding="utf-8") as f:
                self.assertIn("別の配信サーバーが使っている", f.read())
            out = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "ask-config"],
                                 capture_output=True, text=True, timeout=30, env=dict(os.environ))
            self.assertIn("起動できない", out.stdout)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_first_starts_share_one_token(self):
        serve.save_ask_config(self.home, {"bind": "127.0.0.1", "port": self.port})
        tokens = []
        threads = [threading.Thread(target=lambda: tokens.append(serve.load_ask_config(self.home, create=True)["token"]))
                   for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(serve.read_ask_settings(self.home)["token"], tokens[0])
        self.conf = serve.load_ask_config(self.home)


class AskSessionsTest(ServeTestBase):
    """複数の Claude Code セッション・アカウントから質問が来るとき。"""

    def register(self, questions=None, **extra):
        body = {"session_id": "origin-ask", "cwd": self.repo, "questions": questions or QUESTIONS, "wait": 60}
        body.update(extra)
        status, _, data = self.req("POST", "/__qa/ask", body)
        self.assertEqual(status, 201, data)
        return json.loads(data)["doc"]

    def items(self, doc):
        return json.loads(self.req("GET", "/__qa/state?doc=" + doc)[2])["items"]

    def sleeper(self, name):
        q = json.loads(json.dumps(QUESTIONS))
        q[0]["question"] = "%sの質問 SLEEP" % name
        return q

    def test_builds_docs_for_different_sessions_at_the_same_time(self):
        self.start(workers=3, answer_timeout=20)
        docs = [self.register(self.sleeper("A"), session_id="session-a"),
                self.register(self.sleeper("B"), session_id="session-b")]
        end = time.time() + 10
        while time.time() < end and not all(self.items(d)[0]["status"] == "answering" for d in docs):
            time.sleep(0.05)
        self.assertEqual([self.items(d)[0]["status"] for d in docs], ["answering", "answering"])
        self.assertEqual(len(self.app.jobs), 2)
        # 片方を取り消しても、もう片方は作り続ける
        self.req("POST", "/__qa/ask-close", {"doc": docs[0], "reason": "terminal"})
        end = time.time() + 15
        while time.time() < end and self.items(docs[0])[0]["status"] != "canceled":
            time.sleep(0.05)
        self.assertEqual(self.items(docs[0])[0]["status"], "canceled")
        self.assertEqual(self.items(docs[1])[0]["status"], "answering")
        self.req("POST", "/__qa/ask-close", {"doc": docs[1], "reason": "terminal"})
        self.assertTrue(self.app.wait_idle(20))

    def test_questions_on_the_same_doc_wait_for_each_other(self):
        self.start(workers=3, answer_timeout=20)
        doc = self.register(self.sleeper("A"))
        end = time.time() + 10
        while time.time() < end and self.items(doc)[0]["status"] != "answering":
            time.sleep(0.05)
        self.req("POST", "/__qa/questions", {"doc": doc, "question": "案Bの欠点は？"})
        time.sleep(1)
        self.assertEqual(self.items(doc)[1]["status"], "queued")  # 資料作りが終わるまで、作業用コピーを取り合わない
        self.req("POST", "/__qa/ask-close", {"doc": doc, "reason": "terminal"})
        self.assertTrue(self.app.wait_idle(30))
        self.assertEqual([i["status"] for i in self.items(doc)], ["canceled", "answered"])

    def test_labels_the_question_with_the_branch_and_first_request(self):
        subprocess.run(["git", "-C", self.repo, "init", "-q", "-b", "feature-x"], check=True)
        transcript = os.path.join(self.tmp, "projects", "-repo", "s1.jsonl")
        os.makedirs(os.path.dirname(transcript))
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "<local-command-caveat>x</local-command-caveat>"}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "一覧に\n絞り込みを足して"}]}}) + "\n")
        self.start()
        doc = self.register(transcript_path=transcript)
        session = json.loads(self.req("GET", "/__qa/state?doc=" + doc)[2])["ask"]["session"]
        self.assertEqual(session, {"id": "origin-a", "branch": "feature-x", "prompt": "一覧に 絞り込みを足して"})
        self.assertEqual(json.loads(self.req("GET", "/__qa/asks")[2])["asks"][0]["session"]["branch"], "feature-x")

    def test_runs_claude_with_the_asking_sessions_account(self):
        other = os.path.join(self.tmp, "claude-sub1")
        os.makedirs(other)
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "server-account")
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        self.start()
        q = json.loads(json.dumps(QUESTIONS))
        q[0]["question"] = "BUILD する？"
        self.register(q, config_dir=other)
        q = json.loads(json.dumps(q))
        q[0]["question"] = "BUILD もう一度？"
        self.register(q)  # 既定のアカウントのセッション
        self.assertTrue(self.app.wait_idle(30))
        dirs = sorted(c["config_dir"] or "" for c in self.calls())
        # 配信サーバーを起動したセッションのアカウントは引き継がない
        self.assertEqual(dirs, ["", other])

    def test_one_config_for_every_account(self):
        # Claude Code・Codex のどのアカウント（設定フォルダ）のセッションも、同じ置き場所を使う
        keys = ("VISUAL_EXPLAIN_ASK_HOME", "VISUAL_EXPLAIN_HOME", "XDG_STATE_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HOME")
        saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
        os.environ.update(CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude-sub1"),
                          CODEX_HOME=os.path.join(self.tmp, "codex-sub1"), HOME=self.tmp)
        try:
            self.assertEqual(serve.ask_home(), os.path.join(self.tmp, ".local", "state", "visual-explain"))
        finally:
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update(saved)


class AskAlertsTest(ServeTestBase):
    """質問が来たことの知らせ: Orca の未読の印と、Tailscale の HTTPS（デスクトップ通知のため）。"""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "ask-home")
        os.environ["VISUAL_EXPLAIN_ASK_HOME"] = self.home
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.path = os.environ["PATH"]

    def tearDown(self):
        os.environ.pop("VISUAL_EXPLAIN_ASK_HOME", None)
        os.environ["PATH"] = self.path
        for name in ("ORCA_WORKTREE_ID", "ORCA_CLI_BIN_DIR", "VISUAL_EXPLAIN_CLAUDE"):
            os.environ.pop(name, None)
        super().tearDown()

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def read_log(self, name):
        path = os.path.join(self.tmp, name + ".log")
        return open(path).read() if os.path.exists(path) else ""

    def wait_log(self, name, text, timeout=10):
        end = time.time() + timeout
        while time.time() < end and text not in self.read_log(name):
            time.sleep(0.1)
        return self.read_log(name)

    def run_hook(self):
        proc = subprocess.Popen([sys.executable, "-I", os.path.join(SCRIPTS, "ask_hook.py")], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        proc.stdin.write(json.dumps({"session_id": "origin-ask", "cwd": self.repo, "tool_name": "AskUserQuestion",
                                     "tool_input": {"questions": QUESTIONS}}))
        proc.stdin.close()
        proc.stdin = None
        return proc

    def first_doc(self):
        end = time.time() + 10
        while time.time() < end and not json.loads(self.req("GET", "/__qa/asks")[2])["asks"]:
            time.sleep(0.1)
        return json.loads(self.req("GET", "/__qa/asks")[2])["asks"][0]["doc"]

    def always(self):
        serve.save_ask_config(self.home, {"mode": "always", "bind": "127.0.0.1", "port": self.port, "token": TOKEN,
                                          "root": self.root, "wait": 30, "build": "never"})

    def test_marks_the_orca_worktree_while_waiting(self):
        self.start()
        self.always()
        self.fake("orca", 'echo "$@" >> %s/orca.log\n' % self.tmp)
        os.environ["ORCA_WORKTREE_ID"] = "repo-1::/work/tree"
        os.environ["ORCA_CLI_BIN_DIR"] = self.bin
        proc = self.run_hook()
        self.assertIn("worktree set --worktree id:repo-1::/work/tree --unread --json", self.wait_log("orca", "--unread"))
        self.req("POST", "/__qa/answer", {"doc": self.first_doc(), "answers": {
            "どちらの案にしますか？": {"selected": ["案A"]}, "直す所は？": {"selected": ["フック"]}}})
        proc.communicate(timeout=15)
        self.assertIn("--read --json", self.wait_log("orca", "--read"))

    def test_leaves_orca_alone_outside_orca(self):
        self.start()
        self.always()
        self.fake("orca", 'echo "$@" >> %s/orca.log\n' % self.tmp)
        os.environ["PATH"] = self.bin + os.pathsep + self.path  # orca が PATH にあっても、Orca の端末でなければ使わない
        proc = self.run_hook()
        self.req("POST", "/__qa/ask-close", {"doc": self.first_doc(), "reason": "terminal"})
        proc.communicate(timeout=15)
        time.sleep(0.5)
        self.assertEqual(self.read_log("orca"), "")

    def ask_config(self, *args):
        return subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "serve.py"), "ask-config"] + list(args),
                              capture_output=True, text=True, timeout=60, env=dict(os.environ))

    def test_https_through_tailscale_serve(self):
        os.environ["VISUAL_EXPLAIN_CLAUDE"] = FAKE
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.fake("tailscale", 'echo "$@" >> %s/tailscale.log\n'
                               'case "$1" in\n'
                               '  status) echo \'{"CertDomains": ["dev.example.ts.net"]}\' ;;\n'
                               '  ip) echo 100.64.1.2 ;;\n'
                               'esac\n' % self.tmp)
        os.environ["PATH"] = self.bin + os.pathsep + self.path
        out = self.ask_config("--port", str(port), "--https", "on", "--build", "never")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("受け箱: https://dev.example.ts.net:%d/__qa/inbox?k=" % port, out.stdout)
        self.assertIn("serve --bg --https=%d http://127.0.0.1:%d" % (port, port), self.read_log("tailscale"))
        conf = serve.load_ask_config(self.home)
        self.assertEqual(conf["bind"], "127.0.0.1")  # HTTPS を通すときは、このマシンの中だけで待ち受ける
        self.assertTrue(serve.ask_server_alive(conf))
        self.ask_config("--https", "off", "--mode", "off")
        self.assertIn("serve --https=%d off" % port, self.read_log("tailscale"))
        self.assertNotIn("https", serve.read_ask_settings(self.home))
        self.assertFalse(serve.ask_server_alive(conf))

    def test_https_needs_tailscale_certificates(self):
        self.fake("tailscale", 'case "$1" in status) echo \'{"CertDomains": null}\' ;; esac\n')
        os.environ["PATH"] = self.bin + os.pathsep + self.path
        out = self.ask_config("--https", "on")
        self.assertEqual(out.returncode, 2)
        self.assertIn("Tailscale の HTTPS が使えない", out.stderr)

if __name__ == "__main__":
    unittest.main(verbosity=1)
