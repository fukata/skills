# このリポジトリの決まり

`npx skills` で配る Agent Skills を置くリポジトリ。人とエージェント（Claude Code・Codex）のどちらも、スキルを足す・直すときはこの決まりに従う。

## 構成

```text
skills/<名前>/        1 フォルダ = 1 スキル。スキル同士で呼び合わない（共通の部品を置くフォルダは無い）
├── SKILL.md          エージェントが読む説明書
├── README.md         人が読む紹介（導入・依存・使い方・テスト）
├── setup.sh          hook を登録する（hook を使うスキルだけ）
├── teardown.sh       登録した hook を外す（同上）
├── …                 動く部分（言語と分け方はスキルごとに決める）
└── tests/run.sh      そのスキルのテストをすべて実行する入口
docs/superpowers/specs/   設計メモ（<日付>-<スキル名>-design.md）
test.sh               すべてのスキルの tests/run.sh を実行する
```

## スキルを足すとき

1. `skills/<名前>/` を作る。フォルダ名は `SKILL.md` の `name` と同じにする。
2. `SKILL.md`・`README.md`・`tests/run.sh` を置く。
3. ルートの `README.md` の一覧に 1 行足す。リンク先はスキルの `README.md` にする。

## SKILL.md

- frontmatter は `name`・`description`・`license: MIT` だけにする。特定のエージェントだけの項目は書かない。
- `description` は 1,024 文字以内にする（Codex は一覧で 1,024 文字に切り詰める）。
- スキルの中のファイルは「`<skill>` は、この `SKILL.md` があるディレクトリ」と断ってから、`<skill>/setup.sh` のように書く。

## setup.sh と teardown.sh

- 登録するコマンドには、`setup.sh` が呼び出されたパスをそのまま書く。シンボリックリンクを解決しない（`npx skills` は更新時に実体の置き場所を変えることがある）。
- 何度実行しても同じ結果にする。自分の登録は、コマンドに含まれる目印（`<名前>/hook.sh` など）で見分け、既にあれば置き換える。
- 既存の設定とほかの hook は変えない。設定を JSON として読めないとき・形が想定と違うときは、書き換えずに失敗する。
- 設定ファイルがシンボリックリンクならリンク先を書き換え、ファイルの権限を保つ。
- 設定の JSON は python3（標準ライブラリだけ）で読み書きする。`jq` などの追加の依存を足さない。
- `teardown.sh` は自分の登録だけを外す。README には、スキルを消す前に `teardown.sh` を実行すると書く。

## 状態とログ

- `${XDG_STATE_HOME:-~/.local/state}/<名前>/` に置く。スキルのフォルダ・リポジトリ・`~/.claude` の下には書かない。

## テスト

- `skills/<名前>/tests/run.sh` で、そのスキルのテストがすべて動くようにする。失敗したら 0 以外で終わる。
- 実際の設定・アカウント・外部のサービスに触れない（設定フォルダは一時ディレクトリに向け、エージェントの CLI は代役を使う）。
- 変更したら `bash test.sh` を通す。GitHub Actions（`.github/workflows/test.yml`）も同じコマンドを実行する。ブラウザを使うテストは、CI では chrome-headless-shell で動かす。

## 文章とコミット

- 説明書・コメント・コミットメッセージは日本語で書く。
- コミットメッセージは `feat:`・`fix:`・`change:`・`update:` のどれかで始める。
