#!/usr/bin/env bash
# company-check スキルの一括テスト。python3 が必須。SKILL.md の形式だけを確かめ、Web には接続しない。
#   usage: bash skills/company-check/tests/run.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail=0

echo "== SKILL.md の形式"
python3 "$HERE/test_skill_md.py" || fail=1

[ "$fail" = 0 ] && echo "ALL PASSED" || { echo "SOME FAILED"; exit 1; }
