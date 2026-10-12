#!/usr/bin/env python3
"""serve.py のテスト用の claude の代役。

標準入力の指示文から回答の書き出し先と作業用コピーを読み、質問に含まれる語で振る舞いを変える。
  EDIT      作業用コピーの見出しを書き換える（検査は通る）
  BREAK     作業用コピーに飛び先の無いリンクを足す（検査でエラー）
  NOANSWER  回答ファイルを書かず、result の文章だけ返す
  FAIL      標準エラーに書いて exit 1
  SLEEP     長く眠る（時間切れの確認）
  TOUCHDOC  処理中に資料そのものを書き換える（別の所で変わった場合の確認）
  SCRIPT    作業用コピーに script とイベント属性を足す（検査は通るが、差し替えてはいけない）
  STUBBORN  SIGTERM を無視して眠る（強制終了の確認）
  DIRTY     分岐（too-long-origin）で、回答ファイルと作業用コピーを書いてから失敗する
  MDANSWER  回答に Markdown の記法を残す（回答の検査でエラー）
  ORPHAN    標準出力を持ったまま別のセッションで眠る孫を残して終わる（読み取りで止まらないことの確認）
  BUILD     作業用コピーを、検査を通る資料（fixtures/valid）に丸ごと書き換える（AskUserQuestion の資料作り）
出力は --output-format stream-json と同じ 1 行 1 イベント。資料を読む・回答を書くツールの呼び出しも出す。
--resume の ID で失敗を作る: missing-session は会話が無い、toolong-session と too-long-origin（分岐）は長すぎる。
引数と環境の一部を FAKE_CLAUDE_LOG（JSON Lines）に記録する。
「mcp list」では MCP サーバーの一覧を本物と同じ形で出し、呼ばれた作業ディレクトリを FAKE_MCP_LOG に 1 行ずつ書く。
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid


def arg(name):
    a = sys.argv[1:]
    return a[a.index(name) + 1] if name in a else None


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def tool(name, **inp):
    emit({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_%s" % uuid.uuid4().hex[:8], "name": name, "input": inp}]}})


MCP_LIST = """Checking MCP server health…

claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected
plugin:slack:slack: https://mcp.slack.com/mcp (HTTP) - ✔ Connected
metrics: npx -y @example/metrics-mcp - ! Needs authentication
"""


def main():
    if "--version" in sys.argv:
        print("0.0.0 (fake claude)")
        return 0
    if sys.argv[1:3] == ["mcp", "list"]:
        log = os.environ.get("FAKE_MCP_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(os.getcwd() + "\n")
        print(MCP_LIST, end="")
        return 0
    prompt = sys.stdin.read()
    answer = re.search(r"^- 回答の書き出し先: (.+)$", prompt, re.M).group(1).strip()
    draft = re.search(r"^- 本文の作業用コピー: (.+)$", prompt, re.M).group(1).strip()
    doc = re.search(r"^- 資料: (.+)$", prompt, re.M).group(1).strip()
    question = re.search(r"<question>\n(.*?)\n</question>", prompt, re.S).group(1)

    resume, fork, sid = arg("--resume"), "--fork-session" in sys.argv, arg("--session-id")
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "question": question,
                                "claudecode": os.environ.get("CLAUDECODE"),
                                "session_env": os.environ.get("CLAUDE_CODE_SESSION_ID"),
                                "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}, ensure_ascii=False) + "\n")

    if resume == "missing-session":
        print("No conversation found with session ID: %s" % resume, file=sys.stderr)
        return 1
    if (resume == "too-long-origin" and fork) or resume == "toolong-session":
        if "DIRTY" in question:
            with open(answer, "w", encoding="utf-8") as f:
                f.write("<p>stale answer</p>")
            with open(draft, "a", encoding="utf-8") as f:
                f.write("<!-- half edited -->")
        emit({"type": "result", "subtype": "success", "is_error": True, "result": "Prompt is too long"})
        return 1
    session = str(uuid.uuid4()) if (fork or not resume) and not sid else (sid or resume)
    emit({"type": "system", "subtype": "init", "session_id": session})
    tool("Read", file_path=doc)
    tool("Grep", pattern="GRACE_DAYS", path=os.getcwd())
    if "STUBBORN" in question:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)
    if "FAIL" in question:
        print("boom: fake failure", file=sys.stderr)
        return 1
    if "SLEEP" in question:
        time.sleep(30)
    if "ORPHAN" in question:
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"], start_new_session=True)
    if "TOUCHDOC" in question:
        with open(doc, "a", encoding="utf-8") as f:
            f.write("\n<!-- changed elsewhere -->\n")

    if "BUILD" in question:
        valid = os.path.join(os.path.dirname(os.path.abspath(__file__)), "valid")
        tool("Write", file_path=draft, content="...")
        shutil.copyfile(os.path.join(valid, "explanation.html"), draft)
        shutil.copytree(os.path.join(valid, "images"), os.path.join(os.path.dirname(draft), "images"), dirs_exist_ok=True)
    if "EDIT" in question or "BREAK" in question or "TOUCHDOC" in question or "SCRIPT" in question:
        s = open(draft, encoding="utf-8").read()
        if "SCRIPT" in question:
            s = s.replace("<h2>流れ</h2>", '<h2>流れ</h2><p onclick="alert(1)">押す</p><script>alert(2)</script>', 1)
        elif "BREAK" in question:
            s = s.replace("<h2>流れ</h2>", '<h2>流れ</h2><p><a href="#nothing">壊れたリンク</a></p>', 1)
        else:
            s = s.replace("<h2>流れ</h2>", "<h2>流れ（追記）</h2>", 1)
        tool("Edit", file_path=draft, old_string="x", new_string="y")
        open(draft, "w", encoding="utf-8").write(s)
    if "NOANSWER" not in question:
        tool("Write", file_path=answer, content="...")
        with open(answer, "w", encoding="utf-8") as f:
            f.write("<p>回答: %s</p>" % question.replace("<", "&lt;"))
            if "MDANSWER" in question:
                f.write("<p>**太字** のまま</p>")
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
          "result": "plain <answer> for %s" % question})
    return 0


if __name__ == "__main__":
    sys.exit(main())
