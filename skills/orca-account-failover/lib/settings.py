#!/usr/bin/env python3
"""setup.sh / teardown.sh から呼ぶ。Claude Code の settings.json の hooks.StopFailure に、hook.sh の登録を足す・外す。

  python3 -I settings.py add --file <settings.json> --command <hook のコマンド>
  python3 -I settings.py remove --file <settings.json>

既存の項目は変えない。コマンドに目印（MARK）を含む項目だけを扱うので、スキルの置き場所が変わっても、古い場所を指す
登録を拾える。内容が変わるときだけ、元のファイルを <settings.json>.orca-account-failover.bak に残して置き換える。
ファイルがシンボリックリンクならリンク先を書き換え、ファイルの権限を保つ。
標準出力に、書き換えたら changed、変わらなければ unchanged を出す。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

MARK = "orca-account-failover/hook.sh"


def fail(msg):
    print("settings.py: %s" % msg, file=sys.stderr)
    return 1


def without_failover(groups):
    """この hook の登録を取り除く。取り除いて hooks が空になったグループは残さない。"""
    kept = []
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            kept.append(group)
            continue
        rest = [h for h in hooks if not (isinstance(h, dict) and MARK in str(h.get("command", "")))]
        if len(rest) == len(hooks):
            kept.append(group)
        elif rest:
            kept.append(dict(group, hooks=rest))
    return kept


def main():
    ap = argparse.ArgumentParser(description="StopFailure の hook を足す・外す")
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--file", required=True)
    ap.add_argument("--command")
    args = ap.parse_args()
    if args.action == "add" and (not args.command or MARK not in args.command):
        # 解除や付け替えでは、登録をこの目印で見分ける。目印を含まないコマンドで登録すると取り除けなくなる
        return fail("--command に %s を含むコマンドを渡す" % MARK)
    path = os.path.realpath(args.file)  # dotfiles などへのリンクなら、リンク先を書き換える
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        if args.action == "remove":
            print("unchanged")
            return 0
        data = None
    except ValueError as e:
        return fail("%s を JSON として読めないので、書き換えなかった（%s）。直してから実行し直す" % (args.file, e))
    new = json.loads(json.dumps(data)) if data is not None else {}
    if not isinstance(new, dict) or not isinstance(new.get("hooks", {}), dict) \
            or not isinstance(new.get("hooks", {}).get("StopFailure", []), list):
        return fail("%s の hooks の形が想定と違うので、書き換えなかった" % args.file)
    if args.action == "add":
        hooks = new.setdefault("hooks", {})
        entry = {"matcher": "rate_limit", "hooks": [{"type": "command", "command": args.command, "timeout": 10}]}
        hooks["StopFailure"] = [entry] + without_failover(hooks.get("StopFailure", []))
    elif "StopFailure" in new.get("hooks", {}):
        rest = without_failover(new["hooks"]["StopFailure"])
        if rest:
            new["hooks"]["StopFailure"] = rest
        else:
            del new["hooks"]["StopFailure"]
    if new == data:
        print("unchanged")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="settings.json.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if data is not None:
            shutil.copymode(path, tmp)
            shutil.copy2(path, args.file + ".orca-account-failover.bak")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
