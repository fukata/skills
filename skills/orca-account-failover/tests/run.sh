#!/usr/bin/env bash
# orca-account-failover スキルの一括テスト。node と python3 が必須。Orca と実際の設定には触れない。
#   usage: bash skills/orca-account-failover/tests/run.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail=0

echo "== 判定・設定フォルダとアカウントの対応・System Default の候補追加と切替 RPC"
node --test "$HERE"/*.test.js || fail=1

echo "== resume.sh"
bash "$HERE/resume.test.sh" || fail=1

echo "== setup.sh / teardown.sh"
bash "$HERE/settings.test.sh" || fail=1

[ "$fail" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
