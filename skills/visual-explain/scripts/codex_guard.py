#!/usr/bin/env python3
"""Codex のサンドボックスが動かない環境で、配信サーバーが動かす codex exec を絞る PreToolUse の hook。

  python3 -I codex_guard.py --policy <方針の JSON>
  方針: {"write_files": [...], "write_dirs": [...], "commands": [[argv], ...]}

シェル（Bash）は、読み取りのコマンドを | でつないだものと、方針の commands と完全に一致するものだけ許す。
ファイルの書き込み（apply_patch）は、write_files と write_dirs の下だけ許す。ほかのツール（MCP など）は通す。
許すときは何も出さずに終わり、拒むときは deny と理由を出す。思わぬ失敗では拒む（閉じる側に倒す）。
シェルの文字列で判定するので、ツール単位で許す Claude Code より弱い。
"""
import json
import os
import re
import shlex
import sys

# 書き込み・実行のオプションを持つもの（tree -o、file -C、sort -o、awk の system など）は入れない
READERS = {"cat", "head", "tail", "wc", "nl", "ls", "stat", "grep", "rg", "jq", "sed", "find"}
# 引用符の外にあれば拒む記号（リダイレクト・連結・置換・展開・バックグラウンド・改行）。| だけはパイプとして扱う
FORBIDDEN = set("><;&`$\n\r")
FIND_BAD = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
RG_BAD = ("--pre", "--pre-glob")
# sed の書き込み（w・W）と実行（e）のコマンド
SED_SCRIPT_BAD = re.compile(r"(^|[;}\s\d/])[wWe](\s|$)|[/;]\s*[wWe]\b")
ALLOWED_TEXT = ("許されているのは、読み取りのコマンド（%s）を | でつないだもの、配信サーバーが指定した検査のコマンド、"
                "作業用コピー・回答ファイル・images/ への書き込みだけ" % "・".join(sorted(READERS)))


def split_pipeline(command):
    """引用符の外の | で区切り、それぞれを argv にする。引用符の外の拒む記号や、二重引用符の中の $ とバッククォート
    （二重引用符の中でも置換・展開される）があれば ValueError。"""
    parts, buf, quote, i = [], "", None, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif quote == '"' and c in "$`":
                raise ValueError("二重引用符の中の %s" % c)
            elif quote == '"' and c == "\\" and i + 1 < len(command):
                buf += c
                i += 1
                c = command[i]
            buf += c
        elif c in "'\"":
            quote = c
            buf += c
        elif c == "\\":
            raise ValueError("\\")
        elif c == "|":
            if command[i + 1:i + 2] == "|":
                raise ValueError("||")
            parts.append(buf)
            buf = ""
        elif c in FORBIDDEN:
            raise ValueError(c.replace("\n", "改行").replace("\r", "改行"))
        else:
            buf += c
        i += 1
    if quote:
        raise ValueError("閉じていない引用符")
    parts.append(buf)
    return [shlex.split(p) for p in parts]


def check_reader(argv):
    name, args = argv[0], argv[1:]
    if "/" in name or name not in READERS:  # パスで指定したもの（images/ に置いた同名のファイルなど）は通さない
        return "「%s」は許されていない" % name
    if name == "find" and any(a in FIND_BAD for a in args):
        return "find の書き込み・実行のオプションは許されていない"
    if name == "rg" and any(a == b or a.startswith(b + "=") for a in args for b in RG_BAD):
        return "rg の --pre は許されていない"
    if name == "sed":
        quiet = any(a in ("--quiet", "--silent") or (a.startswith("-") and not a.startswith("--") and "n" in a[1:])
                    for a in args)
        if not quiet:
            return "sed は -n を付けた表示だけ許されている"
        if any(a.startswith("-i") or a.startswith("--in-place") or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
               for a in args):
            return "sed -i は許されていない"
        if any(SED_SCRIPT_BAD.search(a) for a in args if not a.startswith("-")):
            return "sed の w・W・e は許されていない"
    return None


def check_bash(command, policy):
    """シェルのコマンドを許すなら None、拒むなら理由。"""
    if not isinstance(command, str):
        return "コマンドを読めない"
    try:
        if shlex.split(command) in (policy.get("commands") or []):
            return None
        pipeline = split_pipeline(command)
    except ValueError as e:
        return "「%s」は許されていない（%s）" % (command.strip()[:80], e)
    for argv in pipeline:
        if not argv:
            return "空のコマンド"
        reason = check_reader(argv)
        if reason:
            return reason
    return None


def writable(path, policy):
    path = os.path.normpath(path)
    if path in [os.path.normpath(p) for p in policy.get("write_files") or []]:
        return True
    return any(path.startswith(os.path.normpath(d) + os.sep) for d in policy.get("write_dirs") or [])


def check_patch(patch, cwd, policy):
    """apply_patch のパッチを許すなら None、拒むなら理由。"""
    if not isinstance(patch, str):
        return "パッチを読めない"
    if re.search(r"^\*\*\* Delete File: ", patch, re.M):
        return "ファイルの削除は許されていない"
    paths = re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$", patch, re.M)
    if not paths:
        return "書き込むファイルが無い"
    for p in paths + re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M):
        p = p.strip()
        if ".." in p.split("/") or not writable(os.path.join(cwd or "/", p), policy):
            return "「%s」には書き込めない" % p
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "%s。%s。" % (reason, ALLOWED_TEXT)}},
                     ensure_ascii=False))
    return 0


def main(argv):
    try:
        with open(argv[argv.index("--policy") + 1], encoding="utf-8") as f:
            policy = json.load(f)
        payload = json.load(sys.stdin)
        assert isinstance(policy, dict) and isinstance(payload, dict)
    except (ValueError, OSError, IndexError, AssertionError):
        return deny("ガードの方針か入力を読めない")
    inp = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool = payload.get("tool_name")
    if tool == "Bash":
        reason = check_bash(inp.get("command"), policy)
    elif tool == "apply_patch":
        reason = check_patch(inp.get("command"), payload.get("cwd"), policy)
    else:
        return 0
    return deny(reason) if reason else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # 思わぬ失敗では拒む
        sys.exit(deny("ガードが失敗した"))
