# skills

[`npx skills`](https://github.com/vercel-labs/skills) で入れる Agent Skills。

```bash
npx skills add fukata/skills --list                              # 一覧
npx skills add fukata/skills -g -a claude-code -s <skill 名>     # 入れる
npx skills add fukata/skills -g -a claude-code -a codex -a opencode -s <skill 名>   # Codex・OpenCode にも入れる
npx skills update -g                                             # 更新
```

## 一覧

| skill | 内容 |
| --- | --- |
| [counterparty-check](skills/counterparty-check/README.md) | 業務委託案件の発注元や取引候補になる会社・個人について、訴訟・行政処分・炎上などの懸念、会社の決算・財務の状況、個人の経歴の裏付けを公開情報から調べ、相手ごとに判断を返す |
| [orca-account-failover](skills/orca-account-failover/README.md) | Orca の端末で動く Claude Code セッションが利用上限で止まったとき、別の Claude アカウントへ切り替えて同じ端末で自動再開する hook。インストール後に `setup.sh` で hook を登録する |
| [visual-explain](skills/visual-explain/README.md) | 仕様・変更・仕組みを図解の HTML 資料にして配信し、ページからの質問にエージェントが答える。テロップ・字幕・音声解説付きの動画も作れる。Claude Code・Codex・OpenCode の質問（AskUserQuestion / request_user_input / question）にもページで答えられる。インストール後に `setup.sh` で hook（OpenCode は plugin）を登録する |

## 開発

```bash
bash test.sh                    # すべてのスキルのテスト
bash test.sh visual-explain     # スキルを指定する
```

スキルを足す・直すときの決まりは [AGENTS.md](AGENTS.md) にあります。

## ライセンス

[MIT](LICENSE)
