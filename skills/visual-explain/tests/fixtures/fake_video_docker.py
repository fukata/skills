#!/usr/bin/env python3
"""動画テスト用の Docker。設定・コンテナーの代わりに一時フォルダの JSON を使う。"""

import json
import os
from pathlib import Path
import sys
import time


root = Path(os.environ["VISUAL_EXPLAIN_FAKE_DOCKER"])
state = json.loads((root / "state.json").read_text())
argv = sys.argv[1:]
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps(argv) + "\n")


def finish(value=None, error=None):
    (root / "state.json").write_text(json.dumps(state))
    if value is not None:
        print(value)
    if error:
        print(error, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


if argv[:2] == ["context", "inspect"]:
    finish(json.dumps(state.get("endpoint", "unix:///fake/docker.sock")))
elif argv[0] == "info":
    finish("linux", error="daemon unavailable" if state.get("failure") == "daemon" else None)
elif argv[:2] == ["image", "inspect"]:
    finish("image-id" if state["image"] else None, error=None if state["image"] else "No such image")
elif argv[0] == "pull":
    if state.get("failure") == "pull-timeout":
        time.sleep(10)
    if state.get("failure") == "pull":
        finish(error="pull failed")
    state["image"] = True
    finish("pulled")
elif argv[0] == "create":
    name = argv[argv.index("--name") + 1]
    key, value = argv[argv.index("--label") + 1].split("=", 1)
    state["containers"][name] = {key: "someone-else" if state.get("failure") == "owner" else value}
    finish("c" * 64)
elif argv[0] == "start":
    finish(argv[-1], error="start failed" if state.get("failure") == "start" else None)
elif argv[:2] == ["container", "inspect"]:
    name = argv[-1]
    if name not in state["containers"]:
        finish(error="No such container")
    if ".Config.Labels" in argv[argv.index("--format") + 1]:
        finish(json.dumps(state["containers"][name]))
    finish(json.dumps({"50021/tcp": [{"HostIp": "127.0.0.1", "HostPort": "50599"}]}))
elif argv[0] == "rm":
    if state.get("failure") == "cleanup":
        finish(error="remove failed")
    state["containers"].pop(argv[-1], None)
    finish("removed")
finish(error="unexpected command: " + repr(argv))
