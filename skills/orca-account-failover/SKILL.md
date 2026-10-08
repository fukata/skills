---
name: orca-account-failover
description: Orca の端末で動く Claude Code セッションが利用上限（usage limit）で止まったとき、Orca に登録した別の Claude アカウントへ切り替えて同じ端末で自動再開する hook。導入・解除・一時停止・状態確認・ログ調査に使う。トリガー - 「orca-account-failover を入れて／外して／止めて」「上限で止まったセッションを自動で再開したい」「【自動再開】というメッセージの出どころは」「failover のログを見て」など。
license: MIT
---

# orca-account-failover

Claude Code の `StopFailure` hook として動く。Orca の端末で動く対話セッションが利用上限で止まると、次の順で動く。

1. 止まった理由が利用上限であることを文面で確かめる（一時的な 429 では何もしない）
2. 必要なら、Orca のアクティブな Claude アカウントを次のアカウントへ切り替える
3. 止まった `claude` を終了させ、同じ端末に `claude <元の起動オプション> --resume <session-id>` を打ち直す

再開後のセッションには、ユーザー発言として次の文面が届く。この文面を見かけたら、出どころはこの hook である。

> 【自動再開】利用上限で中断したため、利用可能な Claude アカウントでセッションを再開しました（orca-account-failover）。中断した作業を続けてください。

以下の `<skill>` は、この `SKILL.md` があるディレクトリを指す。

## 前提

- Linux（`/proc` と `setsid` を使う）
- Orca デスクトップアプリが起動していて、切替可能な Claude 認証先が 2 つ以上ある（`orca account list` の管理アカウントと、保存済みの System Default）
- Claude Code のネイティブ版（プロセス名が `claude`）
- `jq`（登録・解除のスクリプトが使う）

Orca 1.4.220 / Claude Code 2.1.292 で確認している。

## 導入

```bash
orca account list          # 管理アカウントの一覧（System Default はこの一覧に含まれない）
bash <skill>/setup.sh      # ~/.claude/settings.json の hooks.StopFailure（matcher: rate_limit）に登録する
```

`setup.sh` は何度実行しても同じ結果になる。書き換える前の `settings.json` は、同じ場所の
`settings.json.orca-account-failover.bak` に残る。

登録されるのは、`setup.sh` を呼び出したときのパスである。**Claude Code から見えるパス
（`~/.claude/skills/orca-account-failover/setup.sh`）で実行する。** `npx skills` は更新時に実体の置き場所を
変えることがあるが（`~/.claude/skills/` 直下のコピー → `~/.agents/skills/` の実体 + シンボリックリンク）、
このパスは変わらないので、更新のたびに登録し直す必要は無い。

新しく起動するセッションから有効になる。

## 解除

```bash
bash <skill>/teardown.sh   # 登録を取り除く
npx skills remove orca-account-failover
```

先に `teardown.sh` を実行する。スキルの削除だけだと、無くなったスクリプトを指す登録が `settings.json` に残る。

## 一時的に止める

```bash
touch ~/.local/state/orca-account-failover/disabled   # 止める
rm ~/.local/state/orca-account-failover/disabled      # 戻す
```

## 状態とログ

状態ディレクトリは `~/.local/state/orca-account-failover/`（`XDG_STATE_HOME` があればその下）。初回の発火で作られる。

| ファイル | 中身 |
| --- | --- |
| `failover.log` | 1 行 1 件の JSON。下の表を参照 |
| `state.json` | 把握しているアクティブなアカウント、上限中のアカウントと解除時刻、セッションごとの再開回数 |
| `resume/<session-id>.args` | 再開時に `claude` へ渡す引数（NUL 区切り） |

`failover.log` の `event`:

| event | 意味 |
| --- | --- |
| `decision` | 判定結果。`action` は `switch`（切り替えて再開）/ `resume`（再開だけ）/ `give-up`（何もしない） |
| `switched` | アカウントを切り替えた |
| `resumed` | 同じ端末で `claude --resume` が起動した |
| `skip` | 対象外として何もしなかった。`reason` に理由 |
| `error` | 失敗した。`message` に内容。切替に失敗した場合はセッションを止めない |

`skip` の主な `reason`: `not-usage-limit`（利用上限ではない）、`print-mode`（`claude -p`）、
`claude-not-in-this-terminal`（止める対象が、再開コマンドを打つ端末のプロセスではない）。

`give-up` の `reason`: `no-available-account`（切替先がすべて上限中）、`resume-loop`（同じセッションの再開が続いた）。

## 試す（dry-run）

`ORCA_ACCOUNT_FAILOVER_DRY_RUN=1` を付けると、判定を `failover.log` に記録するだけで、切替も停止も再開もしない。
Orca の端末で動く Claude Code のシェルから、hook の入力を流し込んで確かめる。

```bash
printf '%s' '{"session_id":"00000000-0000-4000-8000-000000000000","hook_event_name":"StopFailure","error":"rate_limit","last_assistant_message":"You'"'"'ve hit your session limit · resets 6am (Asia/Tokyo)"}' \
  | ORCA_ACCOUNT_FAILOVER_DRY_RUN=1 bash <skill>/hook.sh
tail -n 1 ~/.local/state/orca-account-failover/failover.log
```

**`ORCA_ACCOUNT_FAILOVER_DRY_RUN=1` を付けずに実行しないこと。** hook は自分の親をたどって `claude` を探すので、
このコマンドを実行しているセッション自身が終了させられる。

## System Default への復帰

管理アカウントに加え、Orca が退避した System Default の認証情報も切替先に含める。
同じメールアドレスでも組織が異なる Team 用・個人用は別候補として扱う。
管理アカウントとメール・組織が両方一致する場合は重複させない。

Linux の退避先は `${XDG_CONFIG_HOME:-$HOME/.config}/orca/claude-runtime-auth/system-default-auth.json`。
Orca のデータディレクトリを変更している場合は、hook の環境変数
`ORCA_ACCOUNT_FAILOVER_ORCA_USER_DATA_DIR` にそのディレクトリを指定する。
退避ファイルがない・読めない・認証情報が不足している場合は候補に含めない。
認証情報は読み取りだけに使い、内容をログへ記録しない。
復帰には Orca の `accounts.selectClaude` に `accountId: null` を渡す。

既存の `system-default` の上限期限と再開回数制限は引き続き適用する。
更新後は次回の hook 発火から反映される。待機中セッションを直ちに起こす処理はない。

## 判定の考え方

実装は `lib/decide.js`、その冒頭のコメントに理由を書いてある。要点だけ挙げる。

- **切替はホスト全体に効く。** Orca は `~/.claude/.credentials.json` を書き換えるので、セッション単位では切り替えられない。
  切り替えるのは最初に止まった 1 セッションで、他のセッションは各自が止まった時点で再開だけを行う。
- **どのアカウントの上限で止まったかは、エラーからは分からない。** 切替より前から動いているプロセスは、
  切替前のアカウントで失敗した可能性がある。そのため「アクティブなアカウントはこれだ」と把握した後に起動した
  プロセスの失敗だけを、そのアカウントの上限として記録する。導入後の最初の 1 回と、Orca の画面で手動切替した
  直後は、切り替えずに再開だけを行う（本当に上限なら、再開直後の失敗で切り替わる）。
- **上限が解ける時刻は文面から読む**（`resets 6:50pm (Asia/Tokyo)` など）。読めなければ 1 時間だけ上限中として扱う。
- **切替先がすべて上限中なら何もしない。** セッションは Claude Code 標準の「解除時刻に自動継続」を待つ。
- **同じセッションの再開は 30 分に（アカウント数 + 2）回まで。**

## 制約

- アカウントの切替に使う `accounts.selectClaude` は、`orca` CLI に無い Orca の内部 RPC である。
  Orca の更新で名前や引数が変わると切替に失敗する（`error` として記録され、セッションは止めない）。
- 対象は Orca の端末で動く対話セッションだけ。`claude -p`、Orca の外のセッション、プロセス名が `claude` でない
  Claude Code（npm 版など）では何もしない。
- 再開後に引き継ぐ起動オプションは `--dangerously-skip-permissions` `--allow-dangerously-skip-permissions`
  `--verbose` `--model` `--permission-mode` `--effort` `--settings` `--agent` `--fallback-model`。
  起動時に渡したプロンプトと、これ以外のオプションは引き継がない。
- 上限で待機している他のセッションを、別のアカウントが解けた時点で起こす仕組みは無い。
  各セッションは、自分に表示された解除時刻に Claude Code の自動継続で動く。
- 利用上限の検出は Claude Code のエラー文面（`You've hit your … limit`）に依存する。文面が変わると何もしなくなる。
- 管理アカウント間の切替・再開は実行ログで確認済み。System Default への復帰は
  Orca 1.4.220 の実装確認とモックによる RPC テストまでで、実アカウントでの通しの動作は未検証。

## テスト

```bash
node --test <skill>/test/*.test.js        # 判定・System Default の候補追加と切替 RPC
bash <skill>/test/settings.test.sh        # 登録・解除（一時ディレクトリで実行し、実際の設定には触れない）
```
