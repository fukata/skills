# visual-explain 公開版の設計

## 目的

zaicodev/claude-code-plugins（private）の plugin「visual-explain」を、fukata/skills（public）から `npx skills` で誰でも入れられるスキルにする。Claude Code に加えて Codex CLI でも、資料作り・配信・ページからの質問・受け箱（エージェントの質問にページで答える仕組み）まで動くようにする。

### 決めたこと

| 項目 | 決定 |
| --- | --- |
| 狙い | 社外でも使える公開版にする。Claude Code 以外のエージェントでも使えるようにする |
| zaicodev 版との関係 | 両方を別々に保守する。公開版は zaicodev 版 0.18.1（commit `c7c7c99`）から分岐し、以後の変更は必要に応じて手で移す |
| 対応範囲 | 受け箱まですべて |
| 受け箱まで動かすエージェント | Claude Code、Codex CLI（0.161.0 で確認）。ほかのエージェントでも資料作り・検査・配信は動く |
| 言語 | 日本語のまま（資料の本文は従来どおりユーザーの言語で書く） |
| zaicodev 版との併用 | しない。名前は `visual-explain` のまま。setup.sh が zaicodev 版を見つけたら警告して止める |
| Codex で答えるときの権限 | サンドボックスが動けばサンドボックス、動かなければ子の codex だけに付ける PreToolUse hook で絞る |
| 作り方 | エージェントの違いをアダプタ（`scripts/agents.py`）に閉じ込める（案A） |

### 対象外

- Claude Code・Codex 以外のエージェントのページからの質問・受け箱
- Codex の Default モードで `request_user_input` を使えるようにする機能フラグの変更
- UI の多言語化
- zaicodev 版の evals の移植（plugin 形式が前提で、社内の会話記録を含む）

## 1. 配置と導入

```text
skills/visual-explain/
├── SKILL.md            # name / description / license: MIT
├── setup.sh            # hook を登録する（Claude Code と Codex）
├── teardown.sh         # 登録した hook を外す
├── assets/             # 雛形・質問欄・受け箱・プロンプトの雛形（zaicodev 版から移植）
├── references/         # viewing / qa / ask / orca / examples（同上）
├── scripts/
│   ├── serve.py              # 配信・質問欄・受け箱（同上。エージェントの呼び出しをアダプタへ移す）
│   ├── ask_hook.py           # 受け箱の hook（同上。Codex の入出力を足す）
│   ├── check-explanation.py  # 資料の検査（同上）
│   ├── agents.py             # 新規: Claude / Codex のアダプタ
│   └── codex_guard.py        # 新規: サンドボックスが動かないときに子の codex を絞る hook
└── tests/              # zaicodev 版の tests を移植し、Codex 分を足す
```

- zaicodev 版の `.claude-plugin/plugin.json`、`hooks/hooks.json`、`evals/`、`__pycache__` は持ってこない。
- SKILL.md の frontmatter は `name`・`description`・`license` だけにする（`user-invocable`・`argument-hint` は Claude Code 固有なので外す）。description は 1,024 文字以内（Codex は一覧で 1,024 文字に切り詰める）。
- SKILL.md・references の中のパスは `${CLAUDE_PLUGIN_ROOT}/skills/visual-explain/…` をやめ、「`<skill>` は この SKILL.md があるディレクトリ」と書く（orca-account-failover と同じ）。Codex は SKILL.md 内の相対パスを SKILL.md 基準で解決し、Claude Code はスキルを読み込むときにベースディレクトリを示す。

### 導入

```bash
npx skills add fukata/skills -g -a claude-code -a codex -s visual-explain
bash ~/.agents/skills/visual-explain/setup.sh     # 対象は自動判定。--claude / --codex で絞る
```

`setup.sh`:

- 呼び出されたパス（`<skill>`）をそのまま hook のコマンドに書く（orca-account-failover と同じ）。
- Claude Code（`~/.claude` か `CLAUDE_CONFIG_DIR` があるとき）: `settings.json` に次を足す。
  - `SessionStart`: `python3 -I <skill>/scripts/ask_hook.py --agent claude --session-start`（timeout 30）
  - `PreToolUse`（matcher `AskUserQuestion`）: `python3 -I <skill>/scripts/ask_hook.py --agent claude`（timeout 7200、statusMessage「資料のページで答えを待っています（受け箱から開けます）」）
- Codex（`~/.codex` か `CODEX_HOME` があるとき）: `hooks.json` に次を足す。
  - `SessionStart`: `python3 -I <skill>/scripts/ask_hook.py --agent codex --session-start`（timeout 30）
  - `PreToolUse`（matcher `request_user_input`）: `python3 -I <skill>/scripts/ask_hook.py --agent codex`（timeout 7200、同じ statusMessage）
  - Codex は信頼していない hook を黙って無視するので、最後に「Codex を開いて `/hooks` でこの hook を信頼する」と案内する。`config.toml` の `trusted_hash` は書かない。hook のコマンド文字列は固定にし、スキルを更新しても信頼し直しにならないようにする。
- 何度実行しても重複しない（同じコマンドの項目があれば置き換える）。`jq` を使わず python3 で JSON を読み書きする（スキル全体の依存を python3 に揃える）。
- zaicodev 版の plugin（`visual-explain@zaicodev`）が `settings.json` の `enabledPlugins` で有効なら、外すよう案内して止める（受け箱の hook が二重に動くため）。
- `teardown.sh` は、コマンドに `<skill>/scripts/ask_hook.py` を含む項目だけを外す。

### データの置き場

- `${VISUAL_EXPLAIN_HOME:-${XDG_STATE_HOME:-~/.local/state}/visual-explain}`。zaicodev 版の `~/.claude/visual-explain/` から移す（Codex だけの利用者には `~/.claude` が無い）。
- 受け箱の設定（`ask.json`）・合言葉・質問ごとの資料（`asks/`）・ログ・質問欄の幅（`viewer.json`）を置く。Claude と Codex のセッションは同じ受け箱・同じ配信サーバー（既定ポート 8770）を使う。
- zaicodev 版の `VISUAL_EXPLAIN_ASK_HOME` も読む（テストと既存の上書きのため）。

### README

- ルートの README の一覧に visual-explain を足す。
- `skills/visual-explain/README.md` に、導入・使い方・依存・制約・分岐元（zaicodev 版 0.18.1、`c7c7c99`）を書く。zaicodev の marketplace の手順は書かない。

## 2. アダプタ（`scripts/agents.py`）

`serve.py` と `ask_hook.py` は、エージェントの名前で分岐せずアダプタを呼ぶ。`ClaudeAgent` と `CodexAgent` が次を受け持つ。

| 項目 | Claude | Codex |
| --- | --- | --- |
| 1問目（作成セッションを分岐） | `claude -p --resume <id> --fork-session` | `codex exec fork <id> -` |
| 2問目以降（続き） | `--resume <id>` | `codex exec resume <id> -` |
| 新しいセッション | `--session-id <uuid>` | `codex exec -`（ID は `thread.started.thread_id`） |
| 出力 | `--output-format stream-json --verbose`、最後の `result` | `--json`、最後の `agent_message` と `turn.completed` / `turn.failed` |
| 権限 | `--permission-mode dontAsk --allowedTools …` | 4節 |
| Web | `WebSearch`・`WebFetch` を許す | `-c web_search="live"`（`--local-only` では `"disabled"`） |
| MCP | `claude mcp list` の名前を `mcp__<名前>` で許す | `codex mcp list --json` の名前ごとに `-c mcp_servers.<名前>.default_tools_approval_mode="approve"`（`--local-only` では `enabled=false`） |
| 自分のセッション ID | `CLAUDE_CODE_SESSION_ID` | `CODEX_THREAD_ID` |
| アカウント | `CLAUDE_CONFIG_DIR`（既定 `~/.claude`） | `CODEX_HOME`（既定 `~/.codex`） |
| セッションの有無・ブランチ・最初の依頼 | `projects/*/<id>.jsonl` | `state_5.sqlite` の `threads`（`id, cwd, git_branch, first_user_message`）。無ければ `sessions/**/rollout-*-<id>.jsonl` の `session_meta` と最初の UserMessage |
| 子プロセスから外す環境変数 | `CLAUDECODE`・`CLAUDE_CODE_SESSION_ID` など（現行の `PARENT_ENV`） | `CODEX_THREAD_ID`・`CODEX_SESSION_ID` など |
| 標準入力 | プロンプトを渡す | プロンプトを渡す（`-` を付けて標準入力から読ませる） |

### 途中経過

- Codex のイベントは、アダプタが Claude と同じ「ツール名＋入力」の形に直してから、現行の `step_label` に渡す。
  - `item.started` / `item.completed` の `command_execution{command}` → `Bash{command}`
  - `file_change{changes[{path}]}` → 変わったファイルごとに `Edit{file_path}`
  - `web_search{query}` → `WebSearch{query}`
  - `mcp_tool_call{server, tool}` → `mcp__<server>__<tool>`
- Codex は読み取りもシェル経由なので、`step_label` の `Bash` に、資料・作業用コピー・回答のパスを含むコマンドを「資料を読んでいます」等と出す分岐を足す。

### どのエージェントで答えるか

- `serve.py origin` は、`CLAUDE_CODE_SESSION_ID` と `CODEX_THREAD_ID` のどちらがあるかでエージェントを決め（`--agent` で上書き）、`qa/origin.json` に `"agent"` と、アカウント（`config_dir`）を書く。
- 1問目は origin のエージェントで分岐し、`qa/session.json` に `"agent"` も記録して2問目以降はそれで続ける。
- origin が無い・分岐に失敗したときの新しいセッションは、配信の `--agent claude|codex|auto` で決める。`auto` は PATH にあるもの、両方あれば Claude。
- 受け箱の質問は、hook の `--agent` で決まる。資料作りは、質問したセッションのエージェントとアカウントで動かす。
- 現行の `--claude <パス>` は残し、`--codex <パス>` を足す。

### プロンプトの雛形

- `answer-prompt.md` と `build-prompt.md` の Claude 固有の言葉を差し込み口にする。
  - `{{agent_name}}`: 「Claude Code」/「Codex」
  - `{{write_hint}}`: 「Write ツールで書く」/「apply_patch で書く」
  - `{{ask_tool}}`: 「AskUserQuestion」/「request_user_input」
- ページの UI の「Claude Code がもう答えを待っていない」などは、質問したエージェントの名前を差し込む（受け箱の質問は `agent`、質問欄は回答しているエージェント）。

### アダプタの外に置くもの

配信・合言葉・CSP・本文の差し替え・検査・書き出し・受け箱の画面は、現行のコードを共通で使う。

## 3. Codex の受け箱

### 流れ

1. Codex のモデルが `request_user_input` を呼ぶと、PreToolUse の hook がツールのモード判定より前に動く。
2. アダプタが質問を共通の形に直す。Codex の `{id, header, question, options[{label, description}]}` → `{question, header, options[{label, description}], multiSelect: false}`、`id` は答えの対応付けに残す。
3. 配信サーバーに登録し、受け箱か質問の資料のページで答えを待つ（Claude 版と共通）。
4. ページで答えが来たら、`deny` の理由の文として返す（Codex にはツールの結果を差し替える手段が無い）。

   ```json
   {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
     "permissionDecisionReason": "ユーザーは資料のページでこの質問に答えた。ツールは実行していないが、これが回答である。同じ質問をし直さず、この回答で作業を続けること。資料: <URL（合言葉なし）>\n{\"answers\":{\"<id>\":{\"answers\":[\"<選んだラベル または その他に書いた文>\",\"user_note: <メモ>\"]}}}"}}
   ```

   モデルには `Tool call blocked by PreToolUse hook: <理由>. Tool: request_user_input` として届く。メモが無ければ `user_note` は付けない。
5. 「ターミナルで答える」・待つ時間切れ・受け箱を誰も開いていない（`open` のとき）は、何も出さずに終わる。Codex のいつもの選択画面になる。

### 資料作り

- 質問したセッションを `codex exec fork <session_id>` で分岐する。アカウントは hook のプロセスの `CODEX_HOME`。
- 元のセッションは質問の途中なので、分岐できるかは実装時に確かめる。失敗したら、現行どおり新しいセッションで 1 回だけやり直す（質問・作業ディレクトリを渡す）。

### SessionStart

- Codex の SessionStart の hook の標準出力はモデルの文脈に入るので、配信サーバーの起動だけ行い、何も出力しない。受け箱の URL は `serve.py ask-config` で見る。
- Claude は現行どおり `systemMessage` で受け箱の URL をターミナルに出す。

### 答えた後の知らせ

- Codex の hook の出力で `systemMessage`（利用者にだけ見える文）が使えるなら、資料の URL を出す。使えなければ出さない。実装時に確かめる。

### 制約（README に書く）

- Codex の `request_user_input` は既定では Plan モードでだけ使える。Default モードでモデルが呼ぶかは確かめていない。機能フラグは変えない。
- Codex の質問には複数選択とプレビューが無い。
- Codex は hook を `/hooks` で信頼するまで動かさない。

### 最初に確かめること（だめなら Codex の受け箱は諦めて報告する）

- `deny` の理由で届けた答えを、モデルが回答として受け取り、質問し直さずに作業を続けるか。本物の Codex の Plan モードで試し、だめなら文面を直す。

## 4. Codex がページの質問に答えるときの権限

Claude 版の「読み取り・Web・MCP・作業用コピーと回答と `images/` への書き込み・検査スクリプトだけ」に近づける。

### サンドボックスが動くとき

- 配信の開始時に一度、Codex のサンドボックスでコマンドが動くかを試す（`codex sandbox` で `true` を動かす。正確な書式は実装時に `codex sandbox --help` で決める）。結果は配信の間だけ覚える。
- 動けば、読み取り専用を基にし、資料フォルダ（作業用コピー・回答・`images/`）だけ書き込める permission profile で動かす。

  ```text
  -c 'permissions.ve.extends=":read-only"' -c 'permissions.ve.filesystem={"<資料フォルダ>"="write"}' -c default_permissions="ve"
  ```

- 検査スクリプトはサンドボックスの中で動く。ヘッドレス Chrome が動かなければ検査スクリプトは Chrome の検査だけを飛ばす（現行どおり）。本文の差し替え前の検査は配信サーバーが外で行う（現行どおり）。

### サンドボックスが動かないとき

- `-s danger-full-access` で動かし、子の codex だけに PreToolUse の hook（`scripts/codex_guard.py`）を `-c hooks.PreToolUse=[…]` で付ける。`-c` で付けた hook は信頼されていないので `--dangerously-bypass-hook-trust` を付ける（利用者が信頼していない自分の hook も子では動く。README の安全の節に書く）。
- 許すもの（それ以外は `deny` し、理由に「許されているもの」を書く）:
  - シェル（`Bash`）: 次の読み取りのコマンドを `|` でつないだものだけ。`cat` `head` `tail` `wc` `nl` `ls` `tree` `stat` `file` `grep` `rg` `jq`、`sed -n`（`-i` と、スクリプトに `w`・`W`・`e` を含むものは除く）、`find`（`-exec` `-execdir` `-ok` `-okdir` `-delete` `-fprint*` `-fls` は除く）。リダイレクト（`>` `>>` `<(` `>(`）、`;` `&&` `||` `&`、`$(` とバッククォート、改行は拒む。git は許さない（`git log --output=` で書き込めるため。Claude 版と同じ）。
  - 検査スクリプト: 配信サーバーが組み立てた検査のコマンドと完全に一致するもの。
  - ファイルの書き込み（`Write`/`Edit`、Codex の `apply_patch`）: 作業用コピー・回答ファイル・`images/` の下だけ。
  - MCP: 許す（`--local-only` ではサーバーを無効にしてある）。
- hook に渡る `tool_input` の形（シェルのコマンドが文字列か配列か、`apply_patch` のパスの取り出し方）は実装時に Codex 0.161.0 の実物で確かめる。
- シェルの文字列で判定するので、ツール単位で許す Claude 版より弱い。README と `references/qa.md` の安全の節に書く。
- 子の codex には `VISUAL_EXPLAIN_ASK_CHILD=1` を付け、利用者の受け箱の hook が子で動かないようにする（現行どおり）。

## 5. zaico 由来の除去と一般化

- 例を一般の題材に置き換える。`references/examples.md` の「在庫クラウド」「在庫編集／一覧／詳細」「注文の確定から在庫の引当」、SKILL.md の「組織の中の拠点、拠点の中のユーザー」（→「組織の中の部署、部署の中のユーザー」）、README の「注文の確定から在庫の引当までどう流れるの？」。
- `check-explanation.py` とテストのチケット番号の例 `PLG26-33` を `ABC-123` にする（検査の正規表現は変えない）。テストの文字列・MCP 名の例（「在庫の一覧に絞り込みを足して」「mackerel」）も一般のものにする。
- zaicodev の private リポジトリの設計文書への参照（`references/qa.md` の「他のプラグインで使うとき」）は、この spec への参照に変える。
- AskUserQuestion だけを前提にした書き方は「AskUserQuestion（Codex では request_user_input）」にする。Claude Docs・Artifact・docs スキルより優先する指示は Claude 向けだが害は無いので残す。
- Orca・Tailscale の手順は任意の機能として残す（無い環境では従来どおり何もしない）。
- 移植後に `zaico`・`zaicodev`・`拠点`・`在庫`・社内のチケット番号の形・社内 URL がスキルのディレクトリに残っていないことを grep で確かめる（README の分岐元の記載を除く）。

## 6. テスト

### 自動テスト（`bash skills/visual-explain/tests/run.sh`）

- zaicodev 版の `tests/run.sh`・`tests/test_serve.py`・fixtures を移植し、パスを新しい配置に直す。
- 足すもの:
  - `tests/fixtures/fake_codex.py`: `codex exec --json [fork|resume] …`、`codex mcp list --json`、`codex sandbox` の代役（`fake_claude.py` と同じ作り）。
  - `agents.py`: 両エージェントのコマンドの組み立て、Codex のイベントの正規化、セッション情報（`state_5.sqlite` と rollout）、origin のエージェント判定。
  - 質問欄の回答を Codex（代役）で行う流れ: 分岐 → 続き → 失敗時の新しいセッション、途中経過、取り消し、サンドボックスの有無による引数の違い。
  - `ask_hook.py --agent codex`: 入力の正規化、登録、`deny` の出力の形、SessionStart で何も出力しないこと。
  - `codex_guard.py`: 許すコマンド・拒むコマンドの表、書き込み先の判定。
  - `setup.sh` / `teardown.sh`: 一時的な HOME で、Claude の `settings.json` と Codex の `hooks.json` への登録・重複しないこと・既存の項目を壊さないこと・外せること・zaicodev 版の検出。

### 手で確かめること（このマシン）

1. `npx skills add` で入れた Claude Code で、AskUserQuestion をページで答えられる。資料を作って配信し、ページの質問に `claude -p` が答える。
2. Codex の Plan モードで `request_user_input` をページで答え、モデルが質問し直さずに続ける。
3. Codex で作った資料のページの質問に、`codex exec fork` が答える。このマシンはサンドボックスが動かないので、`codex_guard.py` の経路を通る。拒むべきコマンド（書き込み・git）が拒まれることも見る。

## 確かめた結果（2026-10-12、このマシン。Codex CLI 0.161.0、Claude Code 2.1.296）

- Codex の `deny` の理由で届けた答え: `codex exec --enable default_mode_request_user_input` に受け箱の hook を付けて確かめた。モデルは「青、検証用のメモ」と答えを受け取り、質問し直さずに終えた（質問の登録は 1 回だけ）。機能フラグを付けないと、Default モードのモデルは request_user_input を呼ばず「Default モードのため使用できません」と返した（Plan モードでだけ使われる、の裏付け）。
- 質問の途中のセッションの分岐: `codex exec fork` で選ぶための資料を作れた（`session: fork`、検査を通って本文を差し替え）。
- ページの質問に Codex で答える: 資料を作った Codex のセッションを分岐して答え（`fork`）、2 問目は続き（`resume`）で答えた。このマシンはサンドボックスが動かない（`codex sandbox -- true` が bwrap のエラーで exit 1）ので、ガードの経路を通った。
- ガード: 本物の Codex で、`git log -1`・`echo test > /tmp/…`（リダイレクト）・リポジトリのファイルへの apply_patch・許した場所の外への相対パスの apply_patch を拒み、`images/` の下への apply_patch は通した。apply_patch の `tool_input.command` のパスは絶対パスで届いた。
- Claude Code: 本物の `claude -p` でページの質問に答えた（新しいセッション）。
- `codex sandbox` での試し方: `codex sandbox -- true` の終了コードで判定する。

## 未確認（実装時に確かめる）

- Codex の hook の出力の `systemMessage` が利用者に表示されるか（3節。exec では確かめられない。TUI で確かめる）
- Codex の TUI での一連の流れ: `/hooks` での信頼、Plan モードでモデルが request_user_input を呼ぶこと、待っている間の statusMessage の表示
- サンドボックスが動く環境での permission profile（このマシンでは動かないため、コマンドの組み立てだけをテストで確かめた）
- hook を 30 分以上待たせたときの Codex の振る舞い（timeout 7200 を指定する）
