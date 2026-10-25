---
name: orca-account-failover
description: Orca の端末で動く Claude Code セッションが利用上限（usage limit）で止まったとき、Orca に登録した別の Claude アカウントへ切り替えて同じ端末で自動再開する hook。導入・解除・一時停止・状態確認・ログ調査に使う。トリガー - 「orca-account-failover を入れて／外して／止めて」「上限で止まったセッションを自動で再開したい」「【自動再開】というメッセージの出どころは」「failover のログを見て」など。
license: MIT
---

# orca-account-failover

Claude Code の `StopFailure` hook として動く。Orca の端末で動く対話セッションが利用上限で止まると、次の順で動く。

1. 止まった理由が利用上限であることを文面で確かめる（一時的な 429 では何もしない）
2. 止まった `claude` が使っていた設定フォルダ（`CLAUDE_CONFIG_DIR`）から上限に達したアカウントを特定し、
   上限でない別のアカウントを選ぶ。Orca の選択もそのアカウントに変える
3. 止まった `claude` を終了させ、同じ端末に、選んだアカウントの設定フォルダで
   `claude <元の起動オプション> --resume <session-id>` を打ち直す

再開後のセッションには、ユーザー発言として次の文面が届く。この文面を見かけたら、出どころはこの hook である。

> 【自動再開】利用上限で中断したため、利用可能な Claude アカウントでセッションを再開しました（orca-account-failover）。中断した作業を続けてください。

以下の `<skill>` は、この `SKILL.md` があるディレクトリを指す。

## 前提

- Linux（`/proc` と `setsid` を使う）
- Orca デスクトップアプリが起動していて、切替可能な Claude 認証先が 2 つ以上ある（`orca account list` の管理アカウントと、保存済みの System Default）
- Orca が、管理アカウントごとの設定フォルダ（`~/.config/orca/claude-profiles/<アカウントID>/home`）を作り、
  端末に `CLAUDE_CONFIG_DIR` と `ORCA_CLAUDE_PROFILE_POINTER` を渡す版であること。渡さない Orca では何もしない
- Claude Code のネイティブ版（プロセス名が `claude`）
- Python 3（登録・解除のスクリプトが使う。標準ライブラリだけで動く）

Orca 1.4.240 / Claude Code 2.1.310 で確認している。

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
| `state.json` | 上限中のアカウントと解除時刻、セッションごとの再開回数 |
| `resume/<session-id>.args` | 再開時に `claude` へ渡す引数（NUL 区切り）。設定フォルダは `resume.sh` の 2 番目の引数で渡す |

`failover.log` の `event`:

| event | 意味 |
| --- | --- |
| `decision` | 判定結果。`action` は `switch`（別のアカウントで再開）/ `give-up`（何もしない）。`failed` は上限に達したアカウント、`active` は判定時の Orca の選択、`target` は切替先 |
| `switched` | Orca の選択を切替先に変えた |
| `resumed` | 同じ端末で `claude --resume` が起動した。`to` は使ったアカウント |
| `skip` | 対象外として何もしなかった。`reason` に理由 |
| `error` | 失敗した。`message` に内容。Orca の選択を変えられなかった場合は、記録して再開を続ける |

`skip` の主な `reason`: `not-usage-limit`（利用上限ではない）、`print-mode`（`claude -p`）、
`claude-not-in-this-terminal`（止める対象が、再開コマンドを打つ端末のプロセスではない）、
`no-claude-profiles`（止まった `claude` に Orca の `ORCA_CLAUDE_PROFILE_POINTER` が無い）、
`unknown-config-dir`（止まった `claude` の `CLAUDE_CONFIG_DIR` が、Orca のアカウントのフォルダでも System Default でもない）。

`decision.reason: stale-limit-probe` は古い制限記録への再試行。
`candidates` に各候補の識別ハッシュ・制限検出時刻（`at`）・解除予定（`until`）・制限種別（`kind`、新しい記録のみ）・選択／除外理由を残す。
除外理由は `failed-account`（止まったアカウント）、`needs-sign-in`（Orca が再ログインを求めている）、
`no-credentials`（アカウントのフォルダに認証情報が無い）、`cached-limit`（上限中の記録がある）。
`state.json` の `identities` は認証先の識別情報、`probe` はホスト全体の最終試行先・時刻・試行中フラグ。認証トークンは保存しない。

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
復帰では、`CLAUDE_CONFIG_DIR` を外して（利用者が自分で指定していたフォルダがあれば、Orca が渡す
`ORCA_CLAUDE_USER_CONFIG_DIR` に戻して）再開し、Orca の `accounts.selectClaude` に `accountId: null` を渡す。

アカウントのメールと組織の組合せをハッシュで識別し、認証先が変わった場合は以前の制限記録を外す。
トークン更新だけでは識別情報は変わらない。旧形式の記録は保持し、下記の限定的な再試行で確認する。
更新後は次回の hook 発火から反映される。待機中セッションを直ちに起こす処理はない。

## 判定の考え方

実装は `lib/decide.js`、その冒頭のコメントに理由を書いてある。要点だけ挙げる。

- **アカウントは端末ごとに決まる。** Orca は端末を開くとき、そのとき選ばれているアカウントのフォルダを
  `CLAUDE_CONFIG_DIR` で渡し、この値は端末を閉じるまで変わらない。Orca の選択を変えても、動いている `claude` の
  アカウントは変わらない。端末で人が `claude` と打つと、Orca の rcfile の `claude` 関数が選択中のフォルダに差し替える。
- **上限に達したアカウントは、止まった `claude` の `CLAUDE_CONFIG_DIR` で特定する**（`lib/profiles.js`）。
  Orca の選択中のアカウントは、動いている `claude` のアカウントと食い違うことがあるので判断に使わない。
- **再開は、選んだアカウントのフォルダを `resume.sh` に渡して起動する。** 端末に打つ `bash resume.sh` には
  `claude` 関数が引き継がれないので、`resume.sh` が関数と同じ差し替えをする。
- **Orca の選択も切替先に変える。** この後に人が端末で起動し直す `claude` や、新しく開く端末も、上限でない
  アカウントで動かすため。人が選んだ選択も上書きする。変えられなくても、記録して再開は続ける。
- **再ログインが要るアカウントと、フォルダに認証情報が無いアカウントは切替先にしない。** Orca は前者の選択を断り、
  どちらもそのフォルダで再開すると認証で失敗する。
- **上限が解ける時刻は文面から読む**（`resets 6:50pm (Asia/Tokyo)` など）。読めなければ 1 時間だけ上限中として扱う。
- **切替先がすべて上限中なら、古い記録を限定的に試し直す。** 最後の制限検出から30分以上経過した別アカウントのうち、最も古いものを一つ選ぶ。
  ホスト全体で30分に1回までとし、既存の排他ロック内で判定・予約する。切替 RPC が失敗しても次の試行まで待つ。
  再開しただけでは利用可能と確定せず、制限記録を保持したまま `probe.pending` にする。
  試している間に止まった別のセッションも、そのアカウントへ移す。そのアカウントで止まったら記録を更新する。
  条件を満たす候補がなければ Claude Code 標準の自動継続を待つ。使用量 API の照会は行わない。
- **同じセッションの再開は 30 分に（アカウント数 + 2）回まで。**

## 制約

- Orca の選択を変える `accounts.selectClaude` は、`orca` CLI に無い Orca の内部 RPC である。
  Orca の更新で名前や引数が変わると選択の変更に失敗する（`error` として記録し、再開は続ける）。
- アカウントのフォルダの場所（`claude-profiles/<アカウントID>/home`）と、`claude` 関数の差し替えの規則は
  Orca 1.4.240 の実装に合わせている。Orca が変えると、アカウントを特定できず（`unknown-config-dir`）何もしなくなる。
- 対象は Orca の端末で動く対話セッションだけ。`claude -p`、Orca の外のセッション、プロセス名が `claude` でない
  Claude Code（npm 版など）では何もしない。
- 再開後に引き継ぐ起動オプションは `--dangerously-skip-permissions` `--allow-dangerously-skip-permissions`
  `--verbose` `--model` `--permission-mode` `--effort` `--settings` `--agent` `--fallback-model`。
  起動時に渡したプロンプトと、これ以外のオプションは引き継がない。
- 再試行の判定は hook 発火時だけ行う。30分ごとに動くタイマーは無く、上限で待機している他のセッションを、別のアカウントが解けた時点で起こす仕組みも無い。
  各セッションは、自分に表示された解除時刻に Claude Code の自動継続で動く。
- 利用上限の検出は Claude Code のエラー文面（`You've hit your … limit`）に依存する。文面が変わると何もしなくなる。
- 上限に達したアカウントの特定と切替先の判定は、実際の Orca のアカウント一覧と代役の `claude` プロセスを使った
  dry-run で確認した。端末での切替・再開の通しの動作は、Orca 1.4.240 ではまだ確認していない。

## テスト

```bash
bash <skill>/tests/run.sh                 # 下の 3 つをまとめて実行する
node --test <skill>/tests/*.test.js       # 判定・設定フォルダとアカウントの対応・System Default の候補追加と切替 RPC
bash <skill>/tests/resume.test.sh         # 選んだ設定フォルダで claude を起動し直す（claude は代役）
bash <skill>/tests/settings.test.sh       # 登録・解除（一時ディレクトリで実行し、実際の設定には触れない）
```
