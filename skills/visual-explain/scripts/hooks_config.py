#!/usr/bin/env python3
"""setup.sh / teardown.sh から呼ぶ。Claude Code の settings.json と Codex の hooks.json に、受け箱の hook を足す・外す。

  python3 -I hooks_config.py add|remove --agent claude|codex --file <JSON> --skill <スキルのフォルダ>

既存の項目は変えない。visual-explain の scripts/ask_hook.py を呼ぶ項目だけを扱う（どのパスに入れたものでも同じとみなし、
何度足しても 1 つ）。既にあれば同じ位置で置き換える（Codex は hook の信頼をグループの番号で記録するため）。
ファイルがシンボリックリンクならリンク先を書き換え、ファイルの権限を保つ。
"""
import argparse
import json
import os
import shlex
import sys

STATUS = "資料のページで答えを待っています（受け箱から開けます）"
MATCHER = {"claude": "AskUserQuestion", "codex": "request_user_input"}
MARK = "/visual-explain/scripts/ask_hook.py"


def entries(skill, agent):
    hook = "python3 -I %s --agent %s" % (shlex.quote(os.path.join(skill, "scripts", "ask_hook.py")), agent)
    return {"SessionStart": {"hooks": [{"type": "command", "command": hook + " --session-start", "timeout": 30}]},
            "PreToolUse": {"matcher": MATCHER[agent],
                           "hooks": [{"type": "command", "command": hook, "timeout": 7200, "statusMessage": STATUS}]}}


def ours(group):
    return isinstance(group, dict) and any(
        MARK in str(h.get("command", "")) for h in group.get("hooks") or [] if isinstance(h, dict))


def fail(msg):
    print("hooks_config.py: %s" % msg, file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description="受け箱の hook を足す・外す")
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--agent", choices=sorted(MATCHER), required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--skill", required=True)
    args = ap.parse_args()
    path = os.path.realpath(args.file)  # dotfiles などへのリンクなら、リンク先を書き換える
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        if args.action == "remove":
            return 0
        data = {}
    except ValueError as e:
        return fail("%s を JSON として読めないので、書き換えなかった（%s）。直してから実行し直す" % (args.file, e))
    hooks = data.setdefault("hooks", {}) if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or any(not isinstance(hooks.get(e, []), list) for e in ("SessionStart", "PreToolUse")):
        return fail("%s の hooks の形が想定と違うので、書き換えなかった" % args.file)
    for event, entry in entries(args.skill, args.agent).items():
        groups = list(hooks.get(event) or [])
        mine = [i for i, g in enumerate(groups) if ours(g)]
        if args.action == "add" and mine:
            groups[mine[0]] = entry  # 同じ位置で置き換え、重複していた分は外す
            groups = [g for i, g in enumerate(groups) if i not in mine[1:]]
        else:
            groups = [g for g in groups if not ours(g)]
            if args.action == "add":
                groups.append(entry)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
