# setup.sh / teardown.sh が共有する、Claude Code の settings.json の書き換え。source して使う。

# hook.sh の登録を見分ける目印。スキルの置き場所が変わっても、古い場所を指す登録を同じ目印で拾える
FAILOVER_HOOK_MARKER='orca-account-failover/hook.sh'

# この hook の登録を取り除く jq の定義。rewrite_settings に渡すフィルタから使える
JQ_WITHOUT_FAILOVER='
def without_failover:
  map(.hooks |= map(select((.command // "") | contains($marker) | not)))
  | map(select((.hooks | length) > 0));
'

settings_path() {
  printf '%s/settings.json\n' "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
}

# settings.json に jq のフィルタを適用する。内容が変わるときだけ、元のファイルを
# <settings.json>.orca-account-failover.bak に残して置き換え、settings_changed=1 にする。
#   $1: jq のフィルタ、$2 以降: jq への追加引数
rewrite_settings() {
  local filter="$1"
  shift
  local settings tmp
  settings="$(settings_path)"
  settings_changed=0
  mkdir -p "$(dirname "$settings")"
  [ -f "$settings" ] || printf '{}\n' >"$settings"
  tmp="$(mktemp "$settings.XXXXXX")"
  if ! jq --arg marker "$FAILOVER_HOOK_MARKER" "$@" "$JQ_WITHOUT_FAILOVER $filter" "$settings" >"$tmp"; then
    rm -f "$tmp"
    echo "settings.json を書き換えられません（JSON として読めない可能性があります）: $settings" >&2
    return 1
  fi
  if cmp -s "$tmp" "$settings"; then
    rm -f "$tmp"
    return 0
  fi
  chmod --reference="$settings" "$tmp"
  cp -p "$settings" "$settings.orca-account-failover.bak"
  mv "$tmp" "$settings"
  settings_changed=1
}
