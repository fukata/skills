#!/usr/bin/env python3
"""配信サーバーが動かす codex exec を絞る PreToolUse の hook（すべてのツールに付ける）。

  python3 -I codex_guard.py --policy <方針の JSON>
  方針: {"write_files": [...], "write_dirs": [...], "commands": [[argv], ...], "mcp": true|false}

許すのは次だけで、ほかのツール（コードモード・サブエージェント・ブラウザなど）はすべて拒む（許可リスト）。
  - シェル（Bash）: 読み取りのコマンドを | でつないだものと、方針の commands と完全に一致するもの
  - ファイルの書き込み（apply_patch）: write_files と、write_dirs の下だけ
  - MCP のツール（mcp が true のとき）と、書き込み・実行をしない補助のツール（計画・画像を見る・ツールの検索）
許すときは何も出さずに終わり、拒むときは deny と理由を出す。思わぬ失敗では拒む（閉じる側に倒す）。
シェルの文字列で判定するので、ツール単位で許す Claude Code より弱い。引用符の外は安全な文字だけにし、bash でも
zsh でも同じに読めるものだけを通す。
"""
import json
import os
import re
import shlex
import sys

# 書き込み・実行のオプションを持つもの（tree -o、file -C、sort -o、awk の system など）は入れない
READERS = {"cat", "head", "tail", "wc", "nl", "ls", "stat", "grep", "rg", "jq", "sed", "find"}
# 書き込み・実行をしない補助のツール
SAFE_TOOLS = {"update_plan", "view_image", "tool_search", "list_mcp_resources", "list_mcp_resource_templates",
              "read_mcp_resource", "get_context_remaining", "current_time"}
# 引用符の外で許す記号（英数字と日本語などの文字のほか）。| はパイプとして別に扱う
SAFE_PUNCT = set("_./,:@%+=-*?~ \t")
FIND_BAD = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
RG_BAD = ("--pre", "--pre-glob", "--hostname-bin")
# sed は「sed -n <範囲>p <ファイル>...」の形だけ許す（行番号・$・/正規表現/ の範囲を表示するだけ）
SED_SCRIPT = re.compile(r"^(?:(?:\d+|\$)(?:,(?:\d+|\$|\+\d+))?|/[^/\n]*/)p$")
ALLOWED_TEXT = ("許されているのは、読み取りのコマンド（%s。sed は「sed -n 1,80p ファイル」の形だけ）を | でつないだもの、"
                "配信サーバーが指定した検査のコマンド、作業用コピー・回答ファイル・images/ への書き込み、MCP のツールだけ"
                % "・".join(sorted(READERS)))


def split_pipeline(command):
    """引用符の外の | で区切り、それぞれを argv にする。引用符の外に安全でない文字があるとき、二重引用符の中に $・
    バッククォート・バックスラッシュ（二重引用符の中でも置換・展開・エスケープされる）があるときは ValueError。"""
    parts, buf, quote = [], "", None
    for i, c in enumerate(command):
        if quote:
            if c == quote:
                quote = None
            elif quote == '"' and c in "$`\\":
                raise ValueError("二重引用符の中の %s" % c)
            buf += c
        elif c in "'\"":
            quote = c
            buf += c
        elif c == "|":
            if command[i + 1:i + 2] == "|" or (i and command[i - 1] == "|"):
                raise ValueError("||")
            parts.append(buf)
            buf = ""
        elif c.isalnum() or c in SAFE_PUNCT:
            buf += c
        else:
            raise ValueError("改行" if c in "\r\n" else c)
    if quote:
        raise ValueError("閉じていない引用符")
    parts.append(buf)
    return [shlex.split(p) for p in parts]


def check_sed(args):
    if len(args) < 3 or args[0] != "-n" or not SED_SCRIPT.match(args[1]) or any(a.startswith("-") for a in args[2:]):
        return "sed は「sed -n 1,80p ファイル」の形（-n・範囲p・ファイル）だけ許されている"
    return None


def check_reader(argv):
    name, args = argv[0], argv[1:]
    if "/" in name or name not in READERS:  # パスで指定したもの（images/ に置いた同名のファイルなど）は通さない
        return "「%s」は許されていない" % name
    if name == "find" and any(a in FIND_BAD for a in args):
        return "find の書き込み・実行のオプションは許されていない"
    if name == "rg" and any(a == b or a.startswith(b + "=") for a in args for b in RG_BAD):
        return "rg の %s は許されていない" % "・".join(RG_BAD)
    if name == "sed":
        return check_sed(args)
    return None


def check_bash(command, policy):
    """シェルのコマンドを許すなら None、拒むなら理由。"""
    if not isinstance(command, str):
        return "コマンドを読めない"
    if "\n" in command or "\r" in command:
        return "改行を含むコマンドは許されていない"
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
    """書いてよい場所か。シンボリックリンクをたどった先で比べる（images/ の中のリンクから外へ書かせない）。"""
    path = os.path.realpath(path)
    if path in [os.path.realpath(p) for p in policy.get("write_files") or []]:
        return True
    return any(path.startswith(os.path.realpath(d) + os.sep) for d in policy.get("write_dirs") or [])


def check_patch(patch, cwd, policy):
    """apply_patch のパッチを許すなら None、拒むなら理由。見出しは、Codex と同じく行の前後の空白を除いて読む。"""
    if not isinstance(patch, str):
        return "パッチを読めない"
    paths = []
    for line in patch.splitlines():
        t = line.strip()
        if not t.startswith("***") or t in ("*** Begin Patch", "*** End Patch", "*** End of File"):
            continue
        m = re.match(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$", t)
        if not m:
            return "読めない見出し「%s」" % t[:80]
        if m.group(1) == "Delete File":
            return "ファイルの削除は許されていない"
        paths.append(m.group(2).strip())
    if not paths:
        return "書き込むファイルが無い"
    for p in paths:
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
    tool = str(payload.get("tool_name") or "")
    if tool == "Bash":
        reason = check_bash(inp.get("command"), policy)
    elif tool == "apply_patch":
        reason = check_patch(inp.get("command"), payload.get("cwd"), policy)
    elif tool.startswith("mcp__"):
        reason = None if policy.get("mcp") else "この配信では MCP のツールを使わない（--local-only）"
    elif tool in SAFE_TOOLS:
        reason = None
    else:
        reason = "ツール「%s」は許されていない" % tool
    return deny(reason) if reason else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # 思わぬ失敗では拒む
        sys.exit(deny("ガードが失敗した"))
