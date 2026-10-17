# テロップ・字幕・音声解説付きの説明動画

`<skill>` は、このスキルの `SKILL.md` があるディレクトリを指す。

図やスクリーンショットを場面ごとに並べる動画を、`<skill>/scripts/make-video.py` で作る。画面操作の録画・生成映像・自動スクロールを行う機能ではない。出力は 1280 × 720、24 フレーム毎秒の MP4（映像 H.264、音声付きなら AAC）。

## 依存

- Python 3（標準ライブラリだけ）。
- ヘッドレス Chrome。既存の資料検査と同じ順番で探す。`--chrome` または `VISUAL_EXPLAIN_CHROME` でも指定できる。
- FFmpeg（`libx264`。字幕を焼き込むときは `ass` フィルターも必要）。`--ffmpeg` で実行ファイルを指定できる。
- 日本語を表示できるフォント。例: Noto Sans CJK JP。
- 日本語の合成音声を使うときだけ、Open JTalk・UTF-8 辞書・HTS 音声モデル。音声モデルは再配布せず、利用するモデルのライセンスに従う。API キーは不要。
- より自然な日本語の声を試す場合は、ローカルの VOICEVOX エンジンにも対応する。HTTP はループバックアドレスだけに接続し、外部のサービスやプロキシへ台本を送らない。API キーは不要。

Docker 本体や Open JTalk は自動インストールしない。Docker が利用できる場合、動画の自動選択で必要になった VOICEVOX イメージだけを取得する。Ubuntu でその他の依存を用意するコマンドの例:

```bash
sudo apt-get install ffmpeg fonts-noto-cjk open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001
npx playwright install chromium-headless-shell
```

## 台本

説明したい問いを決め、問いに答える図を作ってから台本を JSON にする。画像・音声の相対パスは台本ファイルのあるディレクトリを基準にする。URL は指定できない。

```json
{
  "title": "変更の保存方法",
  "scenes": [
    {
      "title": "保存した後の確認",
      "image": "images/save.svg",
      "telop": "保存すると一覧へ戻る",
      "narration": [
        {"text": "変更を保存すると、一覧画面へ戻ります。"},
        {"text": "変更した内容が、一覧に反映されていることを確認します。"}
      ]
    }
  ]
}
```

- `title` は動画全体の名前。各場面の `title` は見出し、`image` は PNG・JPEG・WebP・SVG、`telop` は短い要点。
- `narration` は文ごとの配列。`text` が解説の原稿と字幕になる。一文が字幕の 2 行に収まらなければ文を分ける。字幕は図と要点の下に表示する。
- 任意の `reading` に、読み上げ用の表記を指定できる。読点や読み仮名の調整に使い、字幕の `text` は変えない。説明内容は変えない。
- 音声を使うときは、一文ごとに WAV・MP3 などのファイルを `audio` で指定してもよい。自動選択で全ての文に `audio` があれば、提供音声を優先し合成環境を準備しない。一部だけ `audio` がある場合は、音声を黙って捨てずエラーにする。提供音声と合成を意図して混在させる場合は `--speech voicevox` または `--speech open-jtalk` を明示する。`--speech recorded` では全ての文に `audio` が必要。
- 任意の `seconds` は、場面に指定すると最小表示秒数、各文に指定すると無音動画の字幕表示秒数。音声付きでは音声ファイルの実際の長さを使い、指定秒数で読み上げを切らない。
- 音声付きの場面は、最初に 0.35 秒、文の後に 0.2 秒、場面の最後に 0.55 秒の余白を置く。最小表示秒数を超えても、読み終わるまで同じ場面を表示する。

## 作成

音声を自動選択して作成する（`--speech auto` が既定）:

```text
python3 <skill>/scripts/make-video.py <台本.json> --out <新しい出力フォルダ>
```

次の順で試す。

1. 起動済みのローカルの VOICEVOX。話者の存在を確認し、全場面の音声を生成する。
2. 接続できなければ、この端末の Docker を確認する。`voicevox/voicevox_engine:cpu-latest` が無いときだけ取得し、自分専用のコンテナーをループバックの空きポートで起動する。
3. Docker が無い、デーモンに接続できない、取得・起動・音声合成に失敗した場合は Open JTalk を試す。実行ファイルと一般的な辞書・音声モデルの場所を探す。`--open-jtalk`・`--dictionary`・`--voice` でも指定できる。
4. Open JTalk の環境が無い、合成や発音検査に失敗した場合は、テロップと解説の全文字幕を付けた無音動画で完成させる。

一つの方式で全場面の音声を用意してから動画と結合する。途中で失敗した音声は捨て、次の方式で全場面を用意し直す。台本・画像・提供音声・動画出力の不備は、音声方式の切り替えで隠さずエラーにする。

Docker による取得・起動は `--voicevox-setup-timeout`（既定300秒）、文ごとの合成は `--speech-timeout`（既定120秒）を上限にする。`--docker` で実行ファイルを指定できる。遠隔の Docker は、この端末のループバックから利用できないため自動起動しない。

取得したイメージは次回も使う。自分が作った印のあるコンテナーだけを音声生成後や失敗時に削除し、既存のエンジンや他のコンテナーは停止しない。後片付けに失敗した場合は対象を生成結果に残す。

無音に固定し、音声環境の確認・取得・起動を行わない:

```text
python3 <skill>/scripts/make-video.py <台本.json> --out <新しい出力フォルダ> --speech none
```

Open JTalk に固定する（辞書とモデルの場所は環境による）:

```text
python3 <skill>/scripts/make-video.py <台本.json> --out <新しい出力フォルダ> \
  --speech open-jtalk \
  --dictionary /var/lib/mecab/dic/open-jtalk/naist-jdic \
  --voice /usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice
```

- Open JTalk の実行ファイルは `--open-jtalk`、読み上げ速度は `--speed`（既定 1.05）で指定する。
- 提供音声だけを使うときは `--speech recorded`。読み上げの代わりに、各文の `audio` を使う。
- 字幕を MP4 に焼き込むときは `--burn-captions` を追加する。必要なら `--font "Noto Sans CJK JP"` を指定する。この形式では字幕を消せない。
- 既定は MP4 内の切替式字幕と、再生ページの WebVTT 字幕。対応するプレイヤーで表示を切り替えられる。焼き込み時は同じ字幕を二重に表示しない。
- `--out` は新しいフォルダを指定する。既存フォルダは上書きしない。失敗したときは途中の動画を完成品として残さない。
- `--speech voicevox`・`--speech open-jtalk`・`--speech recorded` は方式の固定指定。失敗時は別方式へ自動で切り替えずエラーにする。
- Open JTalk は文によって発音が不自然に引き延ばされる場合がある。連続した音素が極端に長くなる合成は検査で止め、自動選択では無音動画へ切り替える。`reading` に読点を入れて区切る、短く言い換える、音声を切り替えることで対処し、実際の音声も確認する。

## VOICEVOX の声を使う

自動選択では、必要に応じて Docker で準備する。起動済みのローカルのエディターまたはエンジンだけを使い、他の方式へ切り替えない場合は次のように指定する。

```text
python3 <skill>/scripts/make-video.py <台本.json> --out <新しい出力フォルダ> \
  --speech voicevox --voicevox-url http://127.0.0.1:50021 --voicevox-speaker 11
```

`--voicevox-speaker` はエンジンの `/speakers` で得られるスタイル ID。既定の 11 は玄野武宏・ノーマル。話者の存在を確かめてから合成し、実際の話者名を動画・再生ページ・原稿に記す。話速は `--speed` で調整できる。

Docker を使う起動例（必要な場合にだけ実行し、使い終わったら自分が作ったコンテナーだけを停止する）:

```bash
docker run --rm --name visual-explain-voicevox -p 127.0.0.1:50021:50021 voicevox/voicevox_engine:cpu-latest
```

音声の利用条件は [VOICEVOX の利用規約](https://voicevox.hiroshiba.jp/term/) と各話者の規約で確認する。クレジットは動画にも表示されるため、MP4 単体で渡す場合も残る。

同梱の例を使って試す:

```text
python3 <skill>/scripts/make-video.py <skill>/assets/video-example/story.json \
  --out <新しい出力フォルダ>
```

この例は、テロップと解説の役割・音声との同期・成果物の渡し方を説明する。保存操作は架空の例と明示している。

## 出力と配信

```text
<出力フォルダ>/
├── explanation.html    # 再生・保存・場面ごとの原稿
├── explanation.mp4
├── captions.vtt        # ブラウザ用の字幕
├── captions.srt        # 動画プレイヤー用の字幕
├── transcript.txt      # テロップと解説原稿
├── timeline.json       # 場面・字幕の時刻と、選んだ音声方式・切り替え理由
└── frames/             # テロップ入りの場面画像
```

個別の音声や作業用ファイルは完成フォルダに残さない。台本と元の図は、再編集のため別に保管する。生成物をリポジトリで配る必要はなく、指定された出力先や一時フォルダを使う。

再生ページの「音声の選択結果」、解説原稿、`timeline.json` の `requested_speech`・`speech`・`speech_events` で、実際に使った方式と切り替え理由を確認できる。無音では文ごとの読める時間を確保し、解説のない場面もテロップの長さに合わせて表示する。

`explanation.html` を資料の検査スクリプトで検査し、[viewing.md](viewing.md) と [qa.md](qa.md) に従って同梱のサーバーで配信する。MP4 は途中の位置から再生するためのバイト範囲要求にも対応している。ファイルで渡すときは出力フォルダごと渡す。動画を渡す依頼を、外部のサービスへの公開やアップロードの許可として扱わない。

確認すること:

1. `ffprobe` などで映像・音声のトラックと全体の長さを確認する。
2. `timeline.json` の各文の時刻が、その場面の開始と終了に収まることを確認する。音声付きなら実際の音声の長さを使っていることを見る。
3. 動画から場面を切り出し、図・テロップ・字幕が読めるか確認する。切替式字幕はプレイヤーで有効にして確認する。
4. 配信したページで再生・字幕・シークを確認する。作成側での確認と、ユーザー端末での表示確認は分けて報告する。
5. 発話内容と発音を試聴や利用可能なローカルの文字起こしで確かめる。長さが合うことと、自然に話せることは別に確認する。機械的な検査や文字起こしだけで、試聴済みとは報告しない。

使用するツールの仕様: [FFmpeg の字幕描画](https://ffmpeg.org/ffmpeg-filters.html#ass)、[Open JTalk](https://open-jtalk.sourceforge.net/)、[VOICEVOX エンジン](https://github.com/VOICEVOX/voicevox_engine)、[Docker のコンテナー作成](https://docs.docker.com/reference/cli/docker/container/create/)。
