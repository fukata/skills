#!/usr/bin/env bash
# Claude Code の settings.json（hooks.StopFailure / matcher: rate_limit）に hook.sh を登録する。
# 何度実行しても同じ結果になる。スキルの置き場所が変わった後に実行し直すと、登録先のパスが付け替わる。
set -euo pipefail

# 登録するのは、このスクリプトを呼び出したときのパス（シンボリックリンクを解決しない）。
# npx skills は更新時に実体の置き場所を変えることがあり（~/.claude/skills/ 直下のコピー →
# ~/.agents/skills/ の実体 + シンボリックリンク）、Claude Code から見えるパスのほうが変わらない
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/settings.sh
source "$dir/lib/settings.sh"

if ! command -v jq >/dev/null 2>&1; then
  echo "jq が必要です" >&2
  exit 1
fi
case "$dir" in
  *\'*)
    echo "置き場所のパスに ' が含まれているため登録できません: $dir" >&2
    exit 1
    ;;
esac

command="bash '$dir/hook.sh'"
case "$command" in
  *"$FAILOVER_HOOK_MARKER"*) ;;
  *)
    # 解除や付け替えでは、登録をこの目印で見分ける。目印を含まないパスで登録すると取り除けなくなる
    echo "スキルのディレクトリ名が orca-account-failover ではないため登録できません: $dir" >&2
    exit 1
    ;;
esac
# shellcheck disable=SC2016
rewrite_settings '
  .hooks.StopFailure =
    [{matcher: "rate_limit", hooks: [{type: "command", command: $command, timeout: 10}]}]
    + ((.hooks.StopFailure // []) | without_failover)
' --arg command "$command"

if [ "$settings_changed" = 1 ]; then
  echo "登録しました: $command"
  echo "  $(settings_path)（変更前の内容は同じ場所の .orca-account-failover.bak）"
else
  echo "登録済みです: $command"
fi
