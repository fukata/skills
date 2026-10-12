#!/usr/bin/env python3
"""serve.py のテスト用の codex の代役。fake_claude.py と同じ語で振る舞いを変え、出力は codex exec --json の形にする。

  codex exec --json ... [fork|resume <id>] -   標準入力の指示文で回答を書く
  codex mcp list --json                        MCP サーバーの一覧
  codex sandbox -- true                        FAKE_CODEX_SANDBOX=ok なら 0、それ以外は bwrap のエラーで 1
語: FAIL（exit 1）、SLEEP（長く眠る）、BUILD（作業用コピーを fixtures/valid にする）、EDIT（見出しを書き換える）、
NOANSWER（回答ファイルを書かない）。
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
        with open(draft, encoding="utf-8") as f:
            s = f.read().replace("<h2>流れ</h2>", "<h2>流れ（追記）</h2>", 1)
        with open(draft, "w", encoding="utf-8") as f:
            f.write(s)
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
