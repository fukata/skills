# skills

[`npx skills`](https://github.com/vercel-labs/skills) で入れる Agent Skills。

```bash
npx skills add fukata/skills --list                              # 一覧
npx skills add fukata/skills -g -a claude-code -s <skill 名>     # 入れる
npx skills update -g                                             # 更新
```

## 一覧

| skill | 内容 |
| --- | --- |
| [counterparty-check](skills/counterparty-check/README.md) | 業務委託案件の発注元や取引候補の会社について、訴訟・行政処分・炎上などの懸念と決算・財務の状況を公開情報から調べ、会社ごとに判断を返す |
| [orca-account-failover](skills/orca-account-failover/README.md) | Orca の端末で動く Claude Code セッションが利用上限で止まったとき、別の Claude アカウントへ切り替えて同じ端末で自動再開する hook。インストール後に `setup.sh` で hook を登録する |
| [visual-explain](skills/visual-explain/README.md) | 仕様・変更・仕組みを図解の HTML 資料にして配信し、ページからの質問にエージェントが答える。Claude Code と Codex の質問（AskUserQuestion / request_user_input）にもページで答えられる。インストール後に `setup.sh` で hook を登録する |

## 開発

```bash
bash test.sh                    # すべてのスキルのテスト
bash test.sh visual-explain     # スキルを指定する
```

スキルを足す・直すときの決まりは [AGENTS.md](AGENTS.md) にあります。

## ライセンス

[MIT](LICENSE)
