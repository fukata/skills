# Orca Client → Serveでの図解

## 表示経路

```text
Serve側の対象worktree
  └─ visual-explain-<topic>/
       ├─ explanation.html     主な閲覧用: 操作説明・図・インライン画像
       ├─ images/             実際に確認したスクショ
       ├─ diagrams/            必要な場合だけ保存するMermaid原稿
       └─ qa/                 ページからの質問と回答（配信サーバーが作る。qa.md）
           ↓ 経路A: プライベートネットワーク経由またはSSHポート転送のHTTP配信（viewing.md。ページから質問できる）
           ↓ 経路B: Orcaのワークスペース用ファイル接続（ページの質問欄は出ない）
Client側のブラウザで開く（A: URLを入力、B: ファイル一覧からプレビュー）
           ↓
図とスクショを確認 → 必要ならAskUserQuestionで回答（質問文とプレビューに資料のURL）
```

スキルはClaudeが実行される環境で読み込まれている必要がある。Serve側でClaudeを起動するなら、そのClaudeのプラグイン環境にインストールする。Client側だけへのインストールがServe側にも反映されるとは仮定しない。

## 1. ホストとworktreeを特定する

- ユーザーが指定した接続先とworktreeを優先する。CLIから分かるのは接続したホストの状態であり、Clientの画面やバージョンまで確認済みとはしない。
- `orca-cli`スキルがあれば読み、CLI実行ファイルの解決とバージョンに合うガイドの取得に従う。Linuxで裸の `orca` を実行しない（GNOMEのスクリーンリーダーと衝突する）。
- スキルがなくてもCLIが利用できる場合、`ORCA_CLI_COMMAND`、開発環境の `orca-dev`、Linuxの `orca-ide`、その他環境の `orca` の順に環境に合う実行ファイルを選ぶ。選んだ実行ファイルの `skills get orca-cli` と必要な `--help` を読む。CLIが使えなければファイル作成と手動での閲覧案内で進める。
- 以下の `ORCA` は解決済み実行ファイルのプレースホルダー。文字通り実行しない。`worktree current --json` または `worktree list --json` で対象を確認する。取得した完全なworktree IDを使い、別ホストの同名パスを推測で指定しない。

## 2. クライアントから取得できる場所へ保存する

- 既存の成果物保存規約があれば従い、なければ対象worktree直下の新しい `visual-explain-<topic>/` に保存する。`/tmp` やホーム直下を最終的な資料置き場にしない。
- スクショが別の場所に出力されたら、そのファイルがどのホストにあるか確認する。Serve側で読める画像は `images/` にコピーする。Client側のパスしか返ってこない場合は利用可能なファイル転送機能で取得し、取得できなければ未取得と伝える。
- 画像への参照は `<img src="images/screen-01-list.png" alt="画面1: 一覧">` のような相対パスにする。シンボリックリンクでworktree外の画像を参照しない。実ファイルがworktree内に収まることを確認する。
- スキルの `assets/explanation.html` を基に、説明・図・スクショ・比較表を静的HTMLへまとめる。CSSは内包し、画像は相対参照する。外部CDNは不要で、静的ファイルのまま開いても読めるようにする。経路AのHTTP配信は、Clientへ届けるためだけに使う。
- Mermaidは必要なら `diagrams/` に原稿を保存し、HTMLには描画済みSVG/PNGを載せる。レンダラーがなければ単純な図は静的SVG/HTMLで表現し、複雑な図にはテキストの代替を添える。Mermaid原稿をHTMLへ貼るだけでは図にならない。
- HTMLで画像が表示されなければ参照先とホストを確認し、同じworktreeの画像ファイルを単独で開く。HTML自体が閲覧できなければ `explanation.md` とテキスト図を代替として用意する。Markdown指定の依頼では最初からMarkdownにする。

## 3. Clientで開く

[viewing.md](viewing.md) の経路の選び方に従い、ClientからServeへHTTPで届くなら経路A、届かなければ経路Bを使う。どちらも、資料を開く操作が受理されたこととClientで描画されたことは区別する。

### `tab create` を人が読む用途に使わない

`ORCA tab create --url <URL>` で作ったタブは、`file://` でも `http://` でもServe側のブラウザで描画され、画面がClientへ転送される。文字やSVGの線が荒く、スクロールや範囲選択も遅い（Orca 1.4.220で確認）。`tab create` に描画場所を指定するオプションは無い。自動操作や読み込み確認だけに使い、人が読むタブはユーザーにClient側で開いてもらう。

描画場所は、タブで `ORCA eval --page <browserPageId> --expression '...'` を実行し、`navigator.hardwareConcurrency` と `screen.width`・`screen.height` を見て判別する。CPU数がServeの `nproc` と一致すればServe側の描画である。ユーザーがClient側で開いたタブも `tab list` に現れる。

### 経路A: HTTP配信をClient側のOrcaで開く

配信の手順・アドレスの選び方・止めるタイミングは [viewing.md](viewing.md) に従う。Tailscaleなら `tailscale ip -4` がServeのアドレスになる。配信サーバーで配信したページからは追加の質問ができ、Serve側の Claude Code が答える（[qa.md](qa.md)）。URLは、Client側のOrcaで新しいブラウザタブ（または普段のブラウザ）を開いて入力してもらう。Client側で開いたタブはClientで描画され、スクロールや範囲選択も手元で処理される（Orca 1.4.220、Tailscale経由で確認）。

### 経路B: Orcaのファイル接続でプレビュー

CLIが対応していれば、対象を指定してHTMLファイルを開く要求を送る（実際のオプションは `file open --help` で確認する）。

```text
ORCA file open visual-explain-<topic>/explanation.html --worktree id:<完全なworktree ID> --json
```

成功応答はファイルを開く要求が受理された証拠であり、Clientでの描画完了の証拠ではない。ServeからのUI操作が非対応・Client未接続等で失敗した場合は、同じ要求を繰り返さず手動の手順を伝える。Serve側にOrcaのウィンドウが無い構成（`ORCA status --json` の `app.desktopWindowStatus` が `openable`）では、`file open` は `renderer_unavailable` で失敗した（Orca 1.4.220で確認）。

ユーザーへの案内には、接続先、worktree名、worktree相対パスを含める:

> Orca Clientで接続先の「対象worktree」を開き、ファイル一覧から `visual-explain-<topic>/explanation.html` を **Open in Orca Browser** または **Open Preview to the Side** で開いてください。スクショは同じフォルダの `images/` にあります。

`file open` はHTMLプレビューへの切替を保証しない。ソース表示になった場合は、同じworktreeで `explanation.html` を開き、**Open in Orca Browser** または **Open Preview to the Side** を案内する。paired-runtimeのHTMLプレビューはworktree内のファイルが対象で、相対アセットをワークスペースのファイル接続経由で読み込む。追加フォルダへのアクセス確認が出たらユーザーが判断する。こちらで許可を迂回しない。

経路BはOrcaがファイルを直接読むので、ページの質問欄は出ない。ページから質問したい場合は経路Aを使う。

経路Aと経路Bを使う限り、手動のフォルダ転送や公開URLは原則不要。サーバーの絶対パス、`file://`、サーバー側の `localhost` をClientで開けるURLとして提示しない（Clientから届くプライベートネットワークのアドレスと、SSHポート転送でClient側に用意した `localhost` は除く）。`orca-preview://` の内部URLも手作業で生成しない。表示できないことを理由に自動でArtifactsへ公開しない。

## 4. 閲覧確認と質問

次の確認段階を区別して報告する:

1. **作成確認**: Serve側に資料・画像があり、参照先が実在する。
2. **表示要求**: 経路AのURLを案内した、CLIがファイルを開く要求を受理した、または手動の閲覧手順を案内した。
3. **Client表示確認**: 実際のClient画面を確認できた、またはユーザーから図と画像が見えたと回答があった。

Server上での画像閲覧、HTMLの取得成功、CLIの成功だけで3を満たしたと報告しない。Client画面の確認手段がなければ「Client表示は未確認」と短く記す。

意思決定が必要な場合は先に資料の場所と図を案内し、その後にAskUserQuestionで「案A・図1」等を参照する。質問文と各プレビューに資料のURL（経路Bならworktree相対パス）を入れる（[viewing.md](viewing.md) の3）。質問欄にスクショやMermaidが直接描画されるとは仮定せず、プレビューにも各案の違いを書く。閲覧できないという返答があったら表示経路を修正し、図を見られないまま選択を強制しない。

## 仕様の根拠

- [Orca: HTML, Mermaid, PDF & image viewers](https://www.onorca.dev/docs/editing/viewers): paired-runtimeのHTML、相対アセット、MarkdownのMermaid、画像ビューア。
- [Orca: Rich markdown editor](https://www.onorca.dev/docs/editing/markdown): Markdownの編集・プレビュー。
- CLIコマンドはインストールされている版の `skills get orca-cli` と `file open --help` を優先する。デスクトップClient以外のWeb・モバイル表示は同等と仮定せず、個別に確認する。
