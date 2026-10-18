#!/usr/bin/env python3
"""setup.sh / teardown.sh から呼ぶ。OpenCode の設定フォルダに、受け箱の plugin を読み込む登録ファイルを置く（外す）。

  python3 -I opencode_plugin.py add|remove --dir <OpenCode の設定フォルダ> --skill <スキルのフォルダ>

OpenCode は、設定フォルダの plugins/ にある .js を起動時に読み込む（既定は ~/.config/opencode/plugins/）。
setup.sh は <設定フォルダ>/plugins/visual-explain.js に、<スキル>/opencode/plugin.js を読み込む 1 行を書く。
中身はスキルの中の plugin.js を読み込むだけなので、スキルを更新しても置き直さなくてよい。
ほかの plugin には触れない。同じ名前の別のものが既にあれば、書き換えずに失敗する。
"""
import argparse
import json
import os
import sys

NAME = "visual-explain.js"
MARK = "/visual-explain/opencode/plugin.js"  # 自分が置いたファイルかを見分ける目印


def plugin_path(dir):
    return os.path.join(dir, "plugins", NAME)


def ours(path):
    try:
        with open(path, encoding="utf-8") as f:
            return MARK in f.read()
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="OpenCode に受け箱の plugin を登録する")
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--dir", required=True, help="OpenCode の設定フォルダ")
    ap.add_argument("--skill", required=True, help="visual-explain のスキルのフォルダ")
    args = ap.parse_args()
    path = plugin_path(args.dir)
    if args.action == "remove":
        if os.path.isfile(path) and ours(path):  # 自分の登録だけを外す
            os.remove(path)
        return 0
    source = os.path.join(args.skill, "opencode", "plugin.js")
    if not os.path.isfile(source):
        print("opencode_plugin.py: %s が無い（スキルのフォルダが違う？）" % source, file=sys.stderr)
        return 1
    if os.path.lexists(path) and not ours(path):
        print("opencode_plugin.py: %s に別のファイルが在るので、書き換えなかった。どけてから実行し直す" % path, file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 読み込む先は、setup.sh が呼ばれたときのスキルのパスをそのまま書く（シンボリックリンクを解決しない）
    text = ("// visual-explain の受け箱を OpenCode で使えるようにする登録ファイル。setup.sh が置く（外すときは teardown.sh）。\n"
            "export { default } from %s\n" % json.dumps(source))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
