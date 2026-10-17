# orca-account-failover

Orca の端末で動く Claude Code のセッションが利用上限（usage limit）で止まったとき、Orca に登録した別の Claude アカウントへ切り替えて、同じ端末でセッションを自動で再開する hook です。

- 止まった理由が利用上限のときだけ動きます（一時的な 429 では何もしません）。
- 切替先がすべて上限中でも、30分以上前の制限記録はホスト全体で30分に1回まで切り替えて試し直します。判定は hook 発火時に行い、定期タイマーではありません。
- 同じメールでも組織を区別し、認証先が変わった場合は以前の組織の制限記録を外します。
- 同じセッションの再開が続いた場合は打ち切ります。
- 判定と結果は `~/.local/state/orca-account-failover/failover.log` に残ります。

## 導入

```bash
npx skills add fukata/skills -g -a claude-code -s orca-account-failover
bash ~/.claude/skills/orca-account-failover/setup.sh
```

- `setup.sh` は、Claude Code の `~/.claude/settings.json` の `hooks.StopFailure`（matcher: `rate_limit`）に hook を登録します。新しく起動するセッションから有効になります。
- 書き換える前の `settings.json` は、同じ場所の `settings.json.orca-account-failover.bak` に残ります。

外すとき:

```bash
bash ~/.claude/skills/orca-account-failover/teardown.sh
npx skills remove -g orca-account-failover
```

## 依存

- Linux（`/proc` と `setsid` を使います）
- Orca デスクトップアプリ。切替可能な Claude の認証先が 2 つ以上要ります
- Claude Code のネイティブ版
- Python 3（登録・解除のスクリプトが使います。標準ライブラリだけで動きます）

Orca 1.4.220 / Claude Code 2.1.292 で確認しています。

## 使い方

導入後は自動で動きます。一時的な停止、状態とログの読み方、dry-run、制約は [SKILL.md](SKILL.md) にあります。

## テスト

```bash
bash skills/orca-account-failover/tests/run.sh
```

node と python3 が必須です。Orca と実際の設定には触れません。

## ライセンス

[MIT](../../LICENSE)
