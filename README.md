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
| [orca-account-failover](skills/orca-account-failover/SKILL.md) | Orca の端末で動く Claude Code セッションが利用上限で止まったとき、別の Claude アカウントへ切り替えて同じ端末で自動再開する hook。インストール後に `setup.sh` で hook を登録する |
| [visual-explain](skills/visual-explain/README.md) | 仕様・変更・仕組みを図解の HTML 資料にして配信し、ページからの質問にエージェントが答える。Claude Code と Codex の質問（AskUserQuestion / request_user_input）にもページで答えられる。インストール後に `setup.sh` で hook を登録する |

## ライセンス

[MIT](LICENSE)
