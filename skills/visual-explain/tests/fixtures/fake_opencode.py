#!/usr/bin/env python3
"""serve.py のテスト用の opencode の代役。fake_claude.py と同じ語で振る舞いを変え、出力は
opencode run --format json の形（1 行 1 イベント）にする。

  opencode run --standalone --format json --agent <名前> [--model M] [--session <id> [--fork]]
    標準入力の指示文で回答を書く。イベントは text / tool_use（read・grep・websearch・write・edit・shell）/ error。
  opencode mcp list ...   子の opencode では MCP を使わないので、呼ばれたら分かるように失敗させる。
語: FAIL（error を出して exit 1）、SLEEP（長く眠る）、BUILD（作業用コピーを fixtures/valid にする）、
EDIT（見出しを書き換える）、NOANSWER（回答ファイルを書かない）。
--session の ID で失敗を作る: missing-session は会話が無い、too-long-origin（--fork）は長すぎる。
引数と環境の一部を FAKE_CLAUDE_LOG（JSON Lines、"agent": "opencode"）に記録する。
"""
import json
import os
import re
import shutil
import sys
import time
import uuid


def arg(name):
    a = sys.argv[1:]
    return a[a.index(name) + 1] if name in a else None


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def tool(session, name, **inp):
    emit({"type": "tool_use", "timestamp": int(time.time() * 1000), "sessionID": session,
          "part": {"partID": "prt_%s" % uuid.uuid4().hex[:10], "sessionID": session, "type": "tool",
                   "id": "call_%s" % uuid.uuid4().hex[:10], "tool": name,
                   "state": {"status": "completed", "input": inp}}})


def main():
    a = sys.argv[1:]
    if a[:2] == ["mcp", "list"]:
        print("fake opencode: 子の opencode では MCP を使わない", file=sys.stderr)
        return 2
    if a[:1] != ["run"]:
        print("fake opencode: 不明な呼び出し %r" % (a,), file=sys.stderr)
        return 2
    prompt = sys.stdin.read()
    answer = re.search(r"^- 回答の書き出し先: (.+)$", prompt, re.M).group(1).strip()
    draft = re.search(r"^- 本文の作業用コピー: (.+)$", prompt, re.M).group(1).strip()
    doc = re.search(r"^- 資料: (.+)$", prompt, re.M).group(1).strip()
    question = re.search(r"<question>\n(.*?)\n</question>", prompt, re.S).group(1)

    sid, fork = arg("--session"), "--fork" in a
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"agent": "opencode", "argv": a, "cwd": os.getcwd(), "question": question,
                                "child": os.environ.get("VISUAL_EXPLAIN_ASK_CHILD"),
                                "session_env": os.environ.get("OPENCODE_SESSION_ID"),
                                "config_dir": os.environ.get("OPENCODE_CONFIG_DIR"),
                                "config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
                                "prompt": prompt}, ensure_ascii=False) + "\n")

    if sid == "missing-session":
        emit({"type": "error", "sessionID": "ses_none", "error": {"type": "session.not_found",
                                                                  "message": "Session not found: %s" % sid}})
        return 1
    if sid == "too-long-origin" and fork:
        emit({"type": "error", "sessionID": sid, "error": {"type": "provider.error",
                                                            "message": "context window exceeded"}})
        return 1
    # fork と新規は、opencode が新しいセッションを作る（その ID はイベントの sessionID で返る）
    session = ("ses_fake_%s" % uuid.uuid4().hex[:10]) if (fork or not sid) else sid
    tool(session, "read", path=doc)
    tool(session, "grep", pattern="GRACE_DAYS", path=os.getcwd())
    tool(session, "websearch", query="visual explain")
    if "FAIL" in question:
        emit({"type": "error", "sessionID": session, "error": {"type": "provider.error", "message": "fake failure"}})
        return 1
    if "SLEEP" in question:
        time.sleep(30)

    if "BUILD" in question:
        valid = os.path.join(os.path.dirname(os.path.abspath(__file__)), "valid")
        tool(session, "write", path=draft, content="...")
        shutil.copyfile(os.path.join(valid, "explanation.html"), draft)
        shutil.copytree(os.path.join(valid, "images"), os.path.join(os.path.dirname(draft), "images"), dirs_exist_ok=True)
    if "EDIT" in question:
        text = open(draft, encoding="utf-8").read().replace("<h2>流れ</h2>", "<h2>流れ（追記）</h2>", 1)
        tool(session, "edit", path=draft, oldString="x", newString="y")
        open(draft, "w", encoding="utf-8").write(text)
    if "NOANSWER" not in question:
        tool(session, "write", path=answer, content="...")
        with open(answer, "w", encoding="utf-8") as f:
            f.write("<p>回答: %s</p>" % question.replace("<", "&lt;"))
    emit({"type": "text", "timestamp": int(time.time() * 1000), "sessionID": session,
          "part": {"text": "plain <answer> for %s" % question}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
