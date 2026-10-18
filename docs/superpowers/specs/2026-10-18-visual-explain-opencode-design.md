# visual-explain の設計（OpenCode 対応）

## 目的

visual-explain を、Claude Code・Codex CLI に加えて OpenCode（`opencode`）でも使えるようにする。OpenCode でも、資料作り・配信・ページからの質問に加えて、受け箱（エージェントの質問にページで答える仕組み）まで動かす。

[2026-10-12 の設計](2026-10-12-visual-explain-design.md)の延長で、エージェントの違いはアダプタ（`scripts/agents.py`）と入り口（hook か plugin か）に閉じ込める。

### 決めたこと

| 項目 | 決定 |
| --- | --- |
| 狙い | Claude Code・Codex と同じ使い勝手で OpenCode でも使えるようにする |
| 対応範囲 | 受け箱まで（資料作り・配信・ページの質問・受け箱） |
| 確かめた版 | OpenCode（`opencode` コマンド。plugin API は V2、`opencode run --format json`） |
| 質問の差し込み方 | OpenCode には hook が無いので、plugin で `question` ツールの実行を差し替える（`ctx.tool.transform` → `editor.update("question", …)`） |
| 登録の仕方 | 設定フォルダの `plugins/` に、スキルの plugin を読み込む 1 行のファイルを置く（`plugins` 配列は触らない） |
| 資料作り・回答 | 子の `opencode run`。親の設定を読み込ませない（`--standalone`） |
| 子の権限 | `OPENCODE_CONFIG_CONTENT` の子エージェント（`visual-explain-child`）と権限の規則で、既定を全部拒否にして必要なものだけ許す |
| 作り方 | 既存の受け箱（`serve.py`・`ask_hook.py`）をそのまま使い、差分だけを足す |

### 対象外

- OpenCode 以外の新しいエージェントへの対応
- OpenCode の TUI の見た目（待っている間の表示など）の作り込み
- 子の opencode での MCP の利用（質問したセッションと同じ MCP を子で使う）

## 1. 配置と導入

```text
skills/visual-explain/
├── SKILL.md            # 「Claude Code・Codex CLI・OpenCode のどれでも使える」と書く
├── setup.sh            # hook（Claude Code・Codex）と plugin（OpenCode）を登録する
├── teardown.sh         # 登録した hook と plugin を外す
├── opencode/
│   └── plugin.js       # OpenCode の plugin 本体（question ツールを差し替える）
├── scripts/
│   ├── serve.py              # 配信・質問欄・受け箱（エージェントの呼び出しはアダプタを通す）
│   ├── ask_hook.py           # Claude Code・Codex の hook と、OpenCode の plugin の入り口
│   ├── agents.py             # Claude / Codex / OpenCode のアダプタ
│   ├── opencode_plugin.py    # OpenCode の設定フォルダへの登録・解除（setup.sh / teardown.sh から呼ぶ）
│   └── codex_guard.py        # 子の codex を絞る hook
└── tests/
    └── fixtures/fake_opencode.py   # opencode run の代役
```

### 導入

```bash
npx skills add fukata/skills -g -a claude-code -a codex -a opencode -s visual-explain
bash ~/.agents/skills/visual-explain/setup.sh     # 対象は自動判定。--claude / --codex / --opencode で絞る
```

`setup.sh` の OpenCode 部分:

- OpenCode の設定フォルダ（`OPENCODE_CONFIG_DIR`、無ければ `${XDG_CONFIG_HOME:-$HOME/.config}/opencode`）があるか、`opencode` コマンドが PATH にあれば対象にする。
- `<設定フォルダ>/plugins/visual-explain.js` に、`<スキル>/opencode/plugin.js` を再輸出する 1 行を書く。

  ```js
  // visual-explain の受け箱を OpenCode で使えるようにする登録ファイル。setup.sh が置く（外すときは teardown.sh）。
  export { default } from "<スキル>/opencode/plugin.js"
  ```

- 設定ファイルの `plugins` の配列は書かない（**実測**: 配列にディレクトリや素のファイルパスを書いても読み込まれなかった。`<設定フォルダ>/plugins/*.js` は自動で読み込まれる）。
- 既にあるほかの plugin には触れない。同じ名前の別のファイル（目印 `<スキル>/opencode/plugin.js` を含まない `visual-explain.js`）があれば、書き換えずに失敗する。
- 何度実行しても同じ結果にする（自分のものは置き換える）。`.tmp` に書いて `os.replace` で置き換える。
- 案件ファイルがシンボリックリンクならリンク先を書き換え、権限を保つ（Claude Code の `settings.json` と同じ扱い）。
- 「OpenCode を起動し直すと効く」と案内する。

`teardown.sh` は、自分が置いた登録ファイルだけを外す（同じ名前でも目印が無ければ外さない）。

### データの置き場

- 受け箱の設定・ログ・質問ごとの資料は、Claude Code・Codex と同じ `${XDG_STATE_HOME:-~/.local/state}/visual-explain` を使う。
- OpenCode の会話の記録は、`XDG_DATA_HOME`（既定 `~/.local/share`）の `opencode/opencode.db`（SQLite。`session_v2` と `session_message`）から読む。設定フォルダ（`OPENCODE_CONFIG_DIR`）とは別。

## 2. アダプタ（`scripts/agents.py` の `OpenCodeAgent`）

| 項目 | OpenCode |
| --- | --- |
| 1問目（作成セッションを分岐） | `opencode run --standalone --format json --agent visual-explain-child --session <id> --fork` |
| 2問目以降（続き） | 同じ `--session <id>`（`--fork` なし） |
| 新しいセッション | 同じ（`--session` なし。ID は `sessionID` のイベントから受け取る） |
| 出力 | `--format json` の 1 行 1 イベント（`text`/`tool_use`/`error`）。`tool_use` は `part.tool`・`part.state.status`・`part.state.input` |
| 権限 | `run_env` の `OPENCODE_CONFIG_CONTENT`（下の 4 節） |
| Web | `webfetch`・`websearch` を許す（`--local-only` では許さない） |
| MCP | 子では使わない（`list_mcp` は `[]`。`external_label` は Web の検索と取得） |
| 自分のセッション ID | `OPENCODE_SESSION_ID` |
| アカウント | `OPENCODE_CONFIG_DIR`（既定 `~/.config/opencode`） |
| セッションの有無・ブランチ・最初の依頼 | `XDG_DATA_HOME/opencode/*.db` の `session_v2`（`title`・`directory`）と `session_message`（`type='user'` の `data.text`）。DB が 1 つも無ければ「分岐できるもの」として扱い、分岐に失敗したら新しいセッションでやり直す |
| 子プロセスから外す環境変数 | `OPENCODE`・`OPENCODE_SESSION_ID`・`OPENCODE_TERMINAL`・`AI_AGENT` |
| 標準入力 | 指示文を渡す（`opencode run` は位置引数が無ければ標準入力から読む） |

### 途中経過

`OpenCodeReader` が、opencode のツール名を Claude・Codex と同じ形に直してから、共通の `step_label` に渡す。
`read`→`Read`、`grep`→`Grep`、`glob`→`Glob`、`write`→`Write`、`edit`/`patch`→`Edit`、`shell`→`Bash`、`webfetch`→`WebFetch`、`websearch`→`WebSearch`、`skill`→`Skill`、`task`/`subagent`→`Task`。
入出力のキー（`read`/`write`/`edit` の `path`、`shell` の `command`、`grep`/`glob` の `pattern`）は、`step_label` が読むキーと同じなので変換しない（**実測**で確認）。
同じツール呼び出しは `part.id`（`call_…`）と `part.partID`（`prt_…`）の組と `status` で重複を除く。`error` のイベントは終了コードが 0 でも出るので失敗の判定に使う。

### どのエージェントで答えるか

- `serve.py origin` は `OPENCODE_SESSION_ID` でも OpenCode と判定し、`qa/origin.json` に `"agent": "opencode"` とアカウントを書く。
- `default_agent()` の探索は `claude` → `codex` → `opencode` の順（前の 2 つがあれば挙動は変わらない）。

## 3. OpenCode の受け箱

### 流れ

1. OpenCode のモデルが `question` ツールを呼ぶ。plugin が `tool.execute` を差し替えているので、元の実行の前に plugin が動く。
2. plugin は質問・セッション・作業ディレクトリを `python3 -I <スキル>/scripts/ask_hook.py --agent opencode` に標準入力の JSON で渡し、終わるまで待つ。

   ```json
   {"session_id": "ses_…", "cwd": "/path/to/repo", "questions": [{"header": "…", "question": "…", "options": [{"label": "…", "description": "…"}], "multiple": false}]}
   ```

   作業ディレクトリは `ctx.session.get({sessionID})` の `location.directory` から取る。
3. `ask_hook.py` の `opencode_main` が、質問を既存の受け箱の形（`multiSelect`）に直して配信サーバーに登録し、受け箱か質問の資料のページで答えを待つ（Claude Code・Codex と共通の `register_ask`・`page_ask`）。
4. ページで答えが来たら、質問ごとの配列にして返す。

   ```json
   {"answers": [["青"], ["その他: 検証用のメモ"]]}
   ```

   plugin はこれを `question` ツールと同じ形の結果（`{output: {answers}, content: "User has answered your questions: …", metadata: {answers}}`）にしてモデルに返す。
5. ページに出せないとき（`off` の設定・子の中で動いている・質問の形が読めない・受け箱を誰も開いていない・「ターミナルで答える」・待つ時間切れ）は、`ask_hook.py` が `{"fallback": true}` を返す。plugin は元の `question` ツールの実行をそのまま呼び、いつもどおりターミナルの質問画面を出す。

### 権限の確認

plugin は、ページに出す前に元の `question` ツールと同じ権限の確認をする。

```js
await ctx.permission.assert({action: "question", resources: ["*"], sessionID: context.sessionID,
  agent: context.agent, source: {type: "tool", messageID: context.messageID, id: context.id}})
```

断られたら元の `question` ツールに任せる（いつもどおり断られる）。

### 中断

`context.signal` が abort したら（ターミナルで質問を取り消した）、plugin は子の `ask_hook.py` に SIGTERM を送る。`ask_hook.py` は待つのをやめ、質問を締め切って終わる。ページからは答えを送れなくなる（「OpenCode がもう答えを待っていない」と出る）。

### SessionStart

- OpenCode には SessionStart の hook が無いので、何も出さない。受け箱の URL は `serve.py ask-config` で見る。
- 配信サーバーは、最初の質問のときに `ask_hook.py` が起動する（動いていれば何もしない）。
- 利用者は、`ask-config` の URL で受け箱を一度開く。開けば以降はページで答えを待つ（`auto` のときの「受け箱を一度でも開いたら常に」に載る）。

### ページに出す選択肢

`serve.py` の `answer_ask` は、質問ごとに選ばれたラベル（選択肢のラベルと「その他」に書いた文）を `selections` として記録し、`ask_view` に載せる。`ask_hook.py` の `opencode_answers` がこれを使って `answers` の配列を作る（「その他」に書いた文は `user_note: …` を付けて足す）。

## 4. 子の opencode の権限

子の `opencode run` は次のように動かす。

- `--standalone` を付け、`OPENCODE_CONFIG_CONTENT` で子だけの設定を渡す（親の設定ファイル・plugin・MCP を読み込ませない）。
- 設定では `visual-explain-child` エージェントを定義し、`permissions` を許可リストにする。

  ```json
  {"agents": {"visual-explain-child": {"description": "…", "system": "…",
    "permissions": [{"action": "*", "resource": "*", "effect": "deny"},
                    {"action": "read", "resource": "*", "effect": "allow"},
                    {"action": "glob", "resource": "*", "effect": "allow"},
                    {"action": "grep", "resource": "*", "effect": "allow"},
                    {"action": "external_directory", "resource": "*", "effect": "allow"},
                    {"action": "edit", "resource": "…", "effect": "allow"},
                    {"action": "shell", "resource": "…", "effect": "allow"},
                    {"action": "webfetch", "resource": "*", "effect": "allow"},
                    {"action": "websearch", "resource": "*", "effect": "allow"}]}}}
  ```

- 規則は action・resource・effect の 3 つ組で、最後に一致したものが効く（`*` は `/` も含む）。既定で全部拒否し、後ろに書いたものだけを許す。
- `edit` で書けるのは、資料の作業用コピー・回答ファイル・`images/`（`path_rules`。子の作業ディレクトリから見た相対の形と絶対の形の両方を許す。作業ディレクトリの外は `external_directory` も要る）。
- `shell` で動かせるのは、配信サーバーが組み立てた検査コマンド（`python3 -I <スキル>/scripts/check-explanation.py …`）だけ（`shell_rules`。区切りの無い文字列なので、素の形と引用した形を `*` 付きで許す）。
- 子には `VISUAL_EXPLAIN_ASK_CHILD=1` を付ける（子で受け箱の plugin が動かないようにする。plugin はこの変数があると何もしない）。
- 子では MCP を使わない（サーバーごとのツール名を、権限に使える形では取れないため）。外部の情報源は Web の検索と取得だけ。

## 5. テスト

### 自動テスト（`bash skills/visual-explain/tests/run.sh`）

- `tests/fixtures/fake_opencode.py`: `opencode run --standalone --format json [--session <id> [--fork]] …` の代役。実際の JSONL（`text`・`tool_use`・`error`）を出し、呼ばれた引数を `FAKE_CLAUDE_LOG` に残す。`mcp list` は失敗させる。
- `tests/test_agents.py` の `OpenCodeAgentTest`: コマンドの組み立て（`--standalone`・`--fork`）、子の設定（deny-all → 許可）、`path_rules`（絶対と相対）、`shell_rules`、`run_env`、`OpenCodeReader` の変換・重複除去・`error`、SQLite からの `session_exists`/`session_info`、DB が無いとき、`list_mcp`、引き継がない環境変数、`OPENCODE_SESSION_ID` での判定。
- `tests/test_serve.py` の `OpenCodeAnswerTest`: 質問欄の回答を OpenCode（代役）で行う流れ（分岐 → 続き、分岐に失敗したら新しいセッション、失敗の報告、途中経過、子の設定と `--local-only`、記録が消えたセッションは新しいセッション、`--agent opencode` の選択、opencode が無いときのフォールバック、`serve.py origin` が opencode を記録すること）。
- `tests/test_serve.py` の `OpenCodeHookTest`: `ask_hook.py --agent opencode` の入り口。ページで答えると `{"answers": [[...]]}` を返すこと（このとき `fallback` を出さないこと。「ターミナルで答える」・`off`・子の中・質問の形が読めないときは `{"fallback": true}` を返し、何も登録しないこと）。
- `tests/setup.test.sh`: 一時的な HOME で、`<設定フォルダ>/plugins/visual-explain.js` の登録・2 回実行しても 1 つ・ほかの plugin を残す・同じ名前の別のファイルは書き換えずに止める・`--claude`/`--codex` で対象を絞る・OpenCode の設定フォルダも `opencode` も無ければ登録しない・呼び出されたパスをそのまま書く（リンクでも解決しない）・`teardown.sh` が自分の登録だけを外す。

### 手で確かめること（このマシン）

1. `setup.sh --opencode` で登録し、OpenCode を起動し直す。`question` ツールの質問がページに出て、ページで選んだ答えで作業が続く。
2. OpenCode で作った資料のページの質問に、`opencode run --session <id> --fork` が答える。子の権限で、資料のファイルへの書き込みと検査だけが通り、ほかが拒まれることを見る。
3. `teardown.sh --opencode` で外し、OpenCode を起動し直すと、いつもどおりターミナルの質問画面に戻る。

## 確かめた結果（2026-10-18、このマシン）

- plugin の読み込み: `<設定フォルダ>/plugins/*.js` は起動時に読み込まれる。ローダーファイル方式（`export { default } from "<絶対パス>"`）で、代役の `python3` が呼ばれ、`{"answers": [["Morning"]]}` がモデルに届いた。`package.json` は無くても動く。
- 設定ファイルの `plugins` の配列: スキルの `opencode` ディレクトリや素のファイルパスを書いても読み込まれなかった（採用しない）。
- `opencode run --format json` のイベント: `tool_use` は `part.tool`・`part.id`（`call_…`）・`part.partID`（`prt_…`）・`part.state.status`・`part.state.input`。`text` は `part.text`。`error` は `{"type": "error", "sessionID": …, "error": {"type": …, "message": …}}`。ツールの入力は `read`/`write`/`edit` が `path`、`edit` は `oldString`/`newString`、`shell` は `command`/`workdir`、`grep`/`glob` は `pattern`（`grep` は `path` も）。
- `ctx.session.get({sessionID})` の `location.directory` が親の作業ディレクトリになる。
- `--session <id> --fork` で質問の途中のセッションを分岐でき、`sessionID` のイベントで新しい ID を受け取れる。
- `--standalone` と `OPENCODE_CONFIG_CONTENT` で、子だけのエージェントと権限を渡せる（親の設定を読み込ませない）。権限は action・resource・effect の 3 つ組で、`*` は `/` も含む。子の `edit` の resource は、作業ディレクトリの中なら相対、外なら絶対で届く。
- `opencode mcp list --standalone` はフラグに対応しないので、子の MCP は使わない方針にした。
- OpenCode のセッション ID は `ses_…`（`_` を含む）なので、セッション ID の形の検査を `^[A-Za-z0-9_-]{1,100}$` に広げた。

## 未確認（実装時に確かめる）

- 本物の OpenCode の TUI での一連の流れ: 受け箱を開いてから質問がページに出ること、待っている間の見え方、`question` ツールを差し替えたときの取り消し。
- 子の opencode が、権限の規則どおりに、許した検査コマンドだけを動かし、ほかを拒むこと（本物の `opencode run` で確認する）。
- 本物の OpenCode での資料作りの分岐（`--session <id> --fork`）と、質問欄の回答の続き（`--session <id>`）。
