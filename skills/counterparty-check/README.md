# counterparty-check

業務委託案件の発注元や取引候補の会社について、関わる前に知っておくべき懸念がないかを公開情報から調べるスキルです。

- モラル面: 訴訟・行政処分・炎上、事業そのものの倫理面、代表者の経歴
- 財務面: 決算の数字、資金調達、債務超過や未払いの報告
- 国税庁の法人番号公表サイトで、登記の有無・法人番号・所在地・変更履歴も確かめます
- 会社ごとに「懸念なし・要確認・避けるのが無難・情報不足」のどれかを、出典の URL と一緒に返します

## 導入

```bash
npx skills add fukata/skills -g -a claude-code -s counterparty-check
```

hook は使いません。外すとき:

```bash
npx skills remove -g counterparty-check
```

## 依存

- Web検索とページの取得ができるエージェント（Claude Code では WebSearch と WebFetch）
- [agent-browser](https://github.com/vercel-labs/agent-browser)（法人番号公表サイトの検索に使います。無ければ、ほかのブラウザ操作の道具で代わりにするか、検索できなかったことを報告に書きます）

## 使い方

会社名を渡して呼び出します。所在地や事業内容を添えると、似た名前の別会社と取り違えにくくなります。

```text
/counterparty-check 株式会社A
/counterparty-check 株式会社A 株式会社B 株式会社C
/counterparty-check 株式会社A（恵比寿、チケット販売）
```

- 結果は会話に報告します。ファイルには保存しません。
- 調べるのは会社・運営サービス・代表者と役員の公的な活動・報道・開示資料・口コミサイトまでです。一般社員個人の SNS は調べません。
- 申出・提訴・告訴は一方の主張として扱い、どの段階かを報告に書きます。

手順と判断の基準は [SKILL.md](SKILL.md) にあります。

## テスト

```bash
bash skills/counterparty-check/tests/run.sh
```

python3 が必須です。`SKILL.md` の形式だけを確かめ、Web には接続しません。

## ライセンス

[MIT](../../LICENSE)
