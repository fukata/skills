#!/usr/bin/env python3
"""setup.sh / teardown.sh から呼ぶ。Claude Code の settings.json と Codex の hooks.json に、受け箱の hook を足す・外す。

  python3 -I hooks_config.py add|remove --agent claude|codex --file <JSON> --skill <スキルのフォルダ>

既存の項目は変えない。<スキルのフォルダ>/scripts/ask_hook.py を含む項目だけを扱う（何度足しても 1 つ）。
"""
import argparse
import json
import os
import shlex

STATUS = "資料のページで答えを待っています（受け箱から開けます）"
MATCHER = {"claude": "AskUserQuestion", "codex": "request_user_input"}


def entries(skill, agent):
    hook = "python3 -I %s --agent %s" % (shlex.quote(os.path.join(skill, "scripts", "ask_hook.py")), agent)
    return {"SessionStart": {"hooks": [{"type": "command", "command": hook + " --session-start", "timeout": 30}]},
            "PreToolUse": {"matcher": MATCHER[agent],
                           "hooks": [{"type": "command", "command": hook, "timeout": 7200, "statusMessage": STATUS}]}}


def ours(group, skill):
    mark = os.path.join(skill, "scripts", "ask_hook.py")
    return any(mark in str(h.get("command", "")) for h in group.get("hooks") or [] if isinstance(h, dict))


def main():
    ap = argparse.ArgumentParser(description="受け箱の hook を足す・外す")
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--agent", choices=sorted(MATCHER), required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--skill", required=True)
    args = ap.parse_args()
    try:
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        if args.action == "remove":
            return 0
        data = {}
    hooks = data.setdefault("hooks", {})
    for event, entry in entries(args.skill, args.agent).items():
        groups = [g for g in hooks.get(event) or [] if not (isinstance(g, dict) and ours(g, args.skill))]
        if args.action == "add":
            groups.append(entry)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    os.makedirs(os.path.dirname(os.path.abspath(args.file)), exist_ok=True)
    tmp = args.file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, args.file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
