# visual-explain

仕様・変更内容・仕組み・調整案を、図とスクリーンショットを中心にした HTML 資料にまとめて配信するスキル。Claude Code・Codex CLI・OpenCode（opencode）で使えます。

- 資料は、事前知識の無い人が読む前提で書き、文章だけでは掴みにくい内容（流れ・日程・分岐・前後の違い）を図にします。
- 質問への回答でも、複数の事象の関係を問う質問や長くなる回答には、文章の回答に資料を添えます。
- 配信したページからは追加の質問ができ、配信しているマシンのエージェントが答えます。説明不足なら本文も直します。
- エージェントの質問（Claude Code の AskUserQuestion、Codex の request_user_input、OpenCode の question）にも、資料のページで答えられます。
- 説明動画を頼むと、図やスクショに要点のテロップ・全文字幕を添えた MP4 と再生ページを作ります。日本語の音声解説も付けられます。動く機能の説明では、冒頭に実際の操作の録画で、機能の全体が分かるデモを入れます。

## 導入

```bash
npx skills add fukata/skills -g -a claude-code -a codex -a opencode -s visual-explain
bash ~/.agents/skills/visual-explain/setup.sh
```

- `setup.sh` は、Claude Code の `~/.claude/settings.json` と Codex の `~/.codex/hooks.json` に hook を、OpenCode の設定フォルダ（`~/.config/opencode/`）の `plugins/` に受け箱の plugin を登録します（設定フォルダや `opencode` コマンドがあるものを対象にします。`--claude` / `--codex` / `--opencode` で絞れます）。hook は、セッションの開始時に受け箱の配信サーバーを起動し、エージェントの質問をページに出します。
- **Codex は、hook を信頼するまで動かしません。** `setup.sh` の後に Codex を開き、`/hooks` で visual-explain の 2 つの hook を信頼してください。
- **OpenCode は、起動し直すと効きます。** 登録は `~/.config/opencode/plugins/visual-explain.js`（スキルの中の `opencode/plugin.js` を読み込む 1 行）を置くだけです。OpenCode を起動し直すと、`question` ツールの質問に資料のページで答えられるようになります。スキルを更新しても置き直しは要りません。
- plugin 版の visual-explain を入れている場合は、先に外してください（`/plugin uninstall visual-explain@<marketplace>`）。両方あると hook が二重に動き、受け箱の配信サーバーがポートを取り合います。`setup.sh` は plugin 版が有効なら止まります。

外すとき:

```bash
bash ~/.agents/skills/visual-explain/teardown.sh   # OpenCode は起動し直すと効きます
python3 ~/.agents/skills/visual-explain/scripts/serve.py ask-config --stop   # 受け箱の配信サーバーも止める
npx skills remove -g visual-explain
```

## 依存

- **Python 3**（必須）。配信サーバー・検査スクリプト・hook は標準ライブラリだけで動きます。
- **ヘッドレス Chrome**（任意）。検査スクリプトが、図の文字のはみ出し・狭い幅・印刷での欠けを測ります。配信したページの「PDF をダウンロード」も、これで PDF を作ります。無ければその検査だけ飛ばします（`npx playwright install chromium-headless-shell` などで入ります）。chrome-headless-shell、通常の Chrome・Chromium の順に探し、起動してページを取り出せた最初のものを使います（`--chrome` か環境変数 `VISUAL_EXPLAIN_CHROME` で指定もできます）。
- **Tailscale**（任意）。別の端末から資料を開くときの経路の一つです。無ければ SSH ポート転送で開けます。
- **FFmpeg と日本語フォント**（動画作成時）。動画ではヘッドレス Chrome も必須です。録画の場面を使うときは ffprobe（FFmpeg に同梱）も使います。日本語の合成音声には **ローカルの VOICEVOX エンジン**、または **Open JTalk・UTF-8 辞書・HTS 音声モデル**を使います。通常の HTML 資料の作成にはこれらの動画用依存は不要です。

## 使い方

次のような依頼で自動的に使われます。

```text
この画面の操作をスクショ付きで説明して。
申請から承認までの状態遷移を図で見せて。
編集方法を2案、図で比較してからどちらにするか聞いて。
注文の確定から発送の手配までどう流れるの？
このブランチで改修したAPIについて認識合わせするための資料を作成して。
落とし所の案を作成して。
この処理の流れを、テロップと音声解説付きの説明動画にして。
```

図を頼んでいない質問でも、複数の事象の関係（順序・依存・分岐・対応・並行して動く仕組み）を問う場合や、回答が長くなる（見出し 3 つ以上、または表 2 つ以上）場合は、文章の回答に資料の URL を添えます。一つの事実で答えられる質問や、関係を問わない一覧では作りません。

資料は既定で `visual-explain-<topic>/explanation.html` に作り、同梱の配信サーバーで配信して URL を案内します（git のリポジトリの中なら、無視される `tmp/` などを優先します）。

## テロップ・音声解説付きの動画

動画の依頼には、図やスクショを場面ごとに並べた MP4 と、再生ページ・字幕・解説原稿を作ります。テロップは短い要点、音声と字幕はその場面の解説です。図と文字の表示領域を分け、音声の長さに字幕と場面を合わせます。

動く機能・画面の説明では、冒頭に機能の全体が分かるデモを、実際の操作の録画で入れます。録画はブラウザ操作のツール（agent-browser の `record` など）で撮り、台本の `video` の場面で使う範囲・早送りの倍率・拡大する領域を指定します。早送りの倍率は映像の上に表示し、解説が録画より長ければ最後の画面を止めて待ちます。録画そのものを撮る機能ではありません。

音声は既定で自動選択します。起動済みの VOICEVOX を使い、なければ Docker イメージを取得・起動します。Docker が使えない、取得・合成に失敗する場合は利用可能な Open JTalk、それも使えなければテロップと解説字幕の無音動画で完成させます。動画全体で同じ方式を使い、選択した方式と切り替え理由を再生ページ・原稿・生成結果に残します。VOICEVOX の話者名は動画にも表示します。API キーは不要です。

Docker 本体や Open JTalk は自動インストールしません。取得済みの Docker イメージは再利用し、自分で作ったコンテナーだけを音声生成後に削除します。既存のエンジンや他のコンテナーには触れません。初回はイメージのダウンロードが発生します。取得と起動の上限は既定300秒（`--voicevox-setup-timeout`）、文ごとの合成の上限は120秒（`--speech-timeout`）です。

同梱の台本で試す例（スキルのディレクトリで実行）:

```bash
python3 scripts/make-video.py assets/video-example/story.json --out /tmp/visual-explain-video
```

無音に固定する場合は `--speech none` を追加します。この場合、VOICEVOX や Docker を調べたり起動したりしません。音声方式を固定する `--speech voicevox`・`--speech open-jtalk` は、使えなければエラーで止まります。

Open JTalk に固定する例（辞書・モデルのパスは Ubuntu の例）:

```bash
python3 scripts/make-video.py assets/video-example/story.json --out /tmp/visual-explain-video-voice \
  --speech open-jtalk \
  --dictionary /var/lib/mecab/dic/open-jtalk/naist-jdic \
  --voice /usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice
```

VOICEVOX のエディターかエンジンが起動していれば、`--speech voicevox --voicevox-speaker 11` で玄野武宏・ノーマルの声を利用できます。接続先は既定で `http://127.0.0.1:50021` です。

字幕は既定で切替式です。`--burn-captions` を付けると MP4 に焼き込みます。`--speech recorded` で文ごとの提供音声も使えます。自動選択でも全ての文に `audio` があれば、その提供音声を優先します。読み仮名や区切りを調整する場合は台本の `reading` に指定し、字幕と分けられます。Open JTalk の発音が異常に長くなる合成は検査し、自動選択では無音動画へ切り替えます。出力先は新しいフォルダを指定し、既存のものは上書きしません。

台本の形式・依存の導入例・確認方法は [references/video.md](references/video.md)。通常の HTML 資料を作るときは動画を自動生成しません。

## ページから追加の質問をする

配信したページの右下の「質問する」から、資料を開いたまま質問できます。

- 回答は、資料を作ったエージェントが、資料を作ったセッションを分岐して書きます（Claude Code は `claude -p --resume <ID> --fork-session`、Codex は `codex exec fork <ID>`、OpenCode は `opencode run --session <ID> --fork`）。配信しているマシンでログインしているアカウントの使用量を使います。2 問目以降は 1 問目のセッションの続きです。
- 説明が足りなければ作業用コピーを直し、検査を通ったら本文と差し替えます。ページは最新版に切り替わり、直した節が目立ちます。直す前の版は `qa/history/` に残ります。
- 回答には、回答の下の「この回答に質問する」か、回答の中の文字を選んで、続けて質問できます。続きの質問は元の質問のすぐ下に並びます。
- 回答を待つ間は途中経過が出て、取り消せます。質問欄は幅を変えたり、別ウィンドウに切り出したりできます。「回答を資料に書き出す」で、質問と回答を資料の末尾の節に残せます。
- 資料の右上の「PDF をダウンロード」で、資料を A4 縦の PDF にして保存できます（配信しているマシンのヘッドレス Chrome で作ります。無ければブラウザの印刷の画面が開きます）。`--no-qa` で配信したページにも出ます。
- 資料とリポジトリの外の情報は、Web とエージェントに設定された MCP のサーバーで調べます（`--local-only` で止めます）。
- エージェントにできること:
  - Claude Code: 読み取り・Web・MCP・作業用コピーと回答と `images/` への書き込み・検査スクリプトだけをツール単位で許します。
  - Codex: 子の codex にだけ付けるガードの hook（`scripts/codex_guard.py`）で、すべてのツールを許可リストで絞ります（読み取りのコマンド・検査・作業用コピーと回答と `images/` への書き込み・MCP）。サンドボックスが動けば、その上で読み取り専用＋資料フォルダだけ書き込める設定で動かします（AppArmor が bwrap を止める Ubuntu 24.04 系などでは、ガードだけになります）。ガードはシェルの文字列で判定するので、Claude Code より弱い仕組みです。
  - OpenCode: 子の opencode を `--standalone` で動かし、`OPENCODE_CONFIG_CONTENT` の子エージェント（`visual-explain-child`）と権限で、既定を全部拒否にして資料の作業用コピーと回答と `images/` への書き込み・検査だけを許します。親の設定（MCP サーバー・plugin など）は読み込ませません。
- 質問には、配信の出力に出る `?k=<合言葉>` 付きの URL で一度開く必要があります。配信者本人だけが使ってください。

詳しくは [references/qa.md](references/qa.md)。

## エージェントの質問に資料のページで答える

受け箱を一度でも開くと、それ以降のエージェントの質問は資料のページで答えを待ちます。ページで選んで送ると、答えが元のセッションに戻ります。受け箱の URL は次で表示します（変わらないのでブックマークできます）。

```bash
python3 ~/.agents/skills/visual-explain/scripts/serve.py ask-config
```

| 項目 | Claude Code | Codex | OpenCode |
| --- | --- | --- | --- |
| 対象の質問 | AskUserQuestion | request_user_input（既定では Plan モードでだけ使われる） | question |
| 複数選択・プレビュー | ある | 無い | 複数選択は `multiple` である。プレビューは無い |
| 答えの返り方 | AskUserQuestion の答えとメモ | hook がツールを拒否し、その理由の文に答えを入れて返す | plugin が `question` の実行を差し替え、`answers`（質問ごとの配列）をツールの結果にして返す |
| 登録 | settings.json の hook | hooks.json の hook（`/hooks` で信頼する） | 設定フォルダの `plugins/visual-explain.js`（OpenCode の起動し直しが要る） |
| セッションの開始時 | 受け箱の URL をターミナルに出す | 何も出さない（モデルの文脈に入るため） | 何も出さない（hook が無いため。受け箱の URL は `ask-config` で見る） |

質問ごとに、質問したセッションを分岐したエージェントが、選ぶための図入りの資料を作ります（数分。`ask-config --build never` で作らない）。資料には、選択肢の比較の前に、なぜその質問が出たか（何の作業の途中か・聞く理由・決まっていること・判断の軸・答えた後）を書きます。複数のセッション・アカウント・エージェントから質問が来ても、答えはそれぞれのセッションに戻ります。設定とデータは `~/.local/state/visual-explain/` に置きます。

詳しくは [references/ask.md](references/ask.md)。

## 資料の検査

`scripts/check-explanation.py` が、作った資料を機械的に検査します。スキルは資料を渡す前にこれを実行し、エラーを 0 にします。

```bash
python3 scripts/check-explanation.py <出力フォルダ>/explanation.html \
  [--repo <説明したリポジトリ>] [--ignore-ref <別リポジトリのパスの接頭辞>] \
  [--inventory <構成要素の一覧>] [--no-browser] [--print-width <px>] [--json]
```

| 種別 | 内容 |
| --- | --- |
| エラー（exit 1） | ページ内リンクの飛び先・相対パスのファイル・img の alt が無い、雛形の文が残っている、資格情報らしき文字列、コード参照（`path:line`）のファイルが無い・行数を超える、本文に Markdown の記法が残っている、図の文字が枠や viewBox からはみ出す、画像を読み込めない、狭い幅（390px）や印刷（A4縦、幅700px）で横にはみ出す・切れる |
| 警告（exit 0） | 識別子と並べた行番号のずれ、本文の識別子がリポジトリに無い、外部の資源、生の Mermaid 原稿、svg の title・width や figure の figcaption が無い、図の文字の重なり・余白不足、印刷で図の文字が 9px 未満、閉じた details が印刷されない |
| 読み手の警告（exit 0） | 前提の節が無い、略語・チケット番号・クラス名の意味が書かれていない、図のある節に要点が無い、会話を指す言葉がある |
| 羅列・網羅の警告（exit 0） | 1 つの箱に文字が多すぎる、図の無い節に表の行と箇条書きが多い、図の無い段落に日付や「→」が多い、`--inventory` の要素が資料に出てこない |

説明の正しさ、図が仕組みを表しているか、具体例が規則と合うかは検査しません。

## 出力

```text
visual-explain-<topic>/
├── explanation.html
├── images/
├── diagrams/              # 必要に応じて Mermaid 原稿
└── qa/                    # ページからの質問と回答（配信サーバーが作る。コミットしない）
    ├── origin.json        # 資料を作ったセッション（エージェント・ID・アカウント）
    ├── session.json       # 質問に答えているセッション
    ├── qa.json            # 質問・状態・回答
    ├── guard-<番号>.json  # Codex のガードの方針（サンドボックスが動かないとき）
    ├── answers/
    └── history/
```

## 資料を開く

| 資料を見る端末 | 開き方 |
| --- | --- |
| エージェントと同じマシン（WSL・devcontainer の中を含む） | `127.0.0.1` で配信した `http://localhost:<port>/` を開く |
| 別の端末で、Tailscale・社内 LAN・VPN 等で届く | そのアドレスだけで待ち受ける配信の URL を開く |
| 別の端末で、SSH で接続している | `127.0.0.1` で配信し、SSH ポート転送で開く |
| Orca Client → Serve で、上が使えない | Orca のファイル一覧からプレビューで開く |

詳しくは [references/viewing.md](references/viewing.md)・[references/orca.md](references/orca.md)。

## テスト

```bash
bash skills/visual-explain/tests/run.sh
```

python3 と git が必須です。claude・codex・opencode は代役（`tests/fixtures/fake_claude.py`・`fake_codex.py`・`fake_opencode.py`）を使います。受け箱の登録（`tests/setup.test.sh`）は一時的な HOME で行い、OpenCode の設定フォルダは `OPENCODE_CONFIG_DIR` を一時フォルダに向けます。ヘッドレス Chrome があれば、ブラウザでの検査とページの表示も確かめます。FFmpeg・ffprobe もあれば実際に短い動画を出力し、音声・字幕・場面の同期と、録画の場面の早送り・最後の画面の保持を検査します。読み上げには代役を使い、外部サービスには接続しません。ブラウザを使うテストは図の文字幅を測るので、日本語フォントが入っていないと失敗します。

## ライセンス

[MIT](../../LICENSE)
