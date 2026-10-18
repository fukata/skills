// visual-explain の受け箱（エージェントの質問に資料のページで答える仕組み）を OpenCode でも使えるようにする plugin。
//
// OpenCode には Claude Code・Codex のような hook が無いので、質問のツール（question）の実行を差し替える。
// 質問を <スキル>/scripts/ask_hook.py（--agent opencode）に JSON で渡して待ち、ページで選ばれた答えを
// OpenCode と同じ形（answers と content）のツールの結果にする。ページに出せないとき（使わない設定・受け箱を
// 開いている人がいない・「ターミナルで答える」・待つ時間切れ）は、ask_hook.py が {"fallback": true} を返すので、
// いつもどおりターミナルの質問画面を出す。
//
// 登録は setup.sh（OpenCode の設定フォルダの plugins/ に、このファイルを読み込む 1 行を置く）、解除は teardown.sh。
// 配信サーバーが動かす子（資料作り。VISUAL_EXPLAIN_ASK_CHILD=1）の中では何もしない。
import { spawn } from "node:child_process"
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const PLUGIN = fileURLToPath(import.meta.url)

// この plugin は <スキル>/opencode/plugin.js に在る。ask_hook.py の親をたどってスキルのフォルダを求める
// （OpenCode がリンクを解決したパスで読み込む場合に備え、実体のパスでも探す）
function skillDir() {
  const dirs = [path.dirname(PLUGIN)]
  try {
    const real = path.dirname(fs.realpathSync(PLUGIN))
    if (!dirs.includes(real)) dirs.push(real)
  } catch {
    // 実体のパスが取れなくても、読み込まれたパスで探す
  }
  for (const dir of dirs) {
    const skill = path.resolve(dir, "..")
    if (fs.existsSync(path.join(skill, "scripts", "ask_hook.py"))) return skill
  }
  return path.resolve(path.dirname(PLUGIN), "..")
}

const HOOK = path.join(skillDir(), "scripts", "ask_hook.py")

// OpenCode の question ツールが返す文と同じ形（モデルが読む答え）
function contentText(questions, answers) {
  const parts = questions.map((q, i) => {
    const picked = answers[i]?.length ? answers[i].join(", ") : "Unanswered"
    return `"${q.question}"="${picked}"`
  })
  return `User has answered your questions: ${parts.join(", ")}. You can now continue with the user's answers in mind.`
}

// 質問を ask_hook.py に渡し、ページで選ばれた答え（質問ごとの配列）か、ターミナルに任せる印（null）を返す
function runHook(questions, sessionID, cwd, signal) {
  return new Promise((resolve, reject) => {
    let child
    try {
      child = spawn("python3", ["-I", HOOK, "--agent", "opencode"], { stdio: ["pipe", "pipe", "pipe"] })
    } catch {
      return resolve(null)
    }
    let out = ""
    let err = ""
    let aborted = false
    if (signal && typeof signal.addEventListener === "function") {
      const onAbort = () => {
        aborted = true
        try {
          child.kill() // ターミナルで中断された。ask_hook.py が締め切って終わる
        } catch {
          // もう終わっている
        }
      }
      if (signal.aborted) onAbort()
      else signal.addEventListener("abort", onAbort, { once: true })
    }
    child.on("error", () => resolve(null)) // python3 が無い等。いつもどおりターミナルに任せる
    child.stdout.on("data", (chunk) => {
      out += chunk
    })
    child.stderr.on("data", (chunk) => {
      err = (err + chunk).slice(-2000)
    })
    child.on("close", () => {
      if (aborted) return reject(new Error("The user dismissed this question"))
      const lines = out
        .trim()
        .split("\n")
        .filter((line) => line.trim())
      let parsed = null
      try {
        parsed = JSON.parse(lines[lines.length - 1] ?? "")
      } catch {
        parsed = null
      }
      if (!parsed || parsed.fallback === true || !Array.isArray(parsed.answers)) {
        if (err.trim()) console.error("visual-explain: ask_hook.py: %s", err.trim())
        return resolve(null)
      }
      resolve(parsed.answers)
    })
    child.stdin.on("error", () => {
      // 標準入力を読む前に終わった（ask_hook.py が失敗した）。close で始末する
    })
    child.stdin.end(JSON.stringify({ session_id: sessionID, cwd, questions }))
  })
}

// question ツールの実行。ページに出せればその答え、出せなければ null（元の実行に任せる）
async function askOnPage(ctx, input, context) {
  if (process.env.VISUAL_EXPLAIN_ASK_CHILD || !fs.existsSync(HOOK)) return null
  let cwd = ""
  try {
    const session = await ctx.session.get({ sessionID: context.sessionID })
    cwd = session?.location?.directory ?? ""
  } catch {
    return null
  }
  if (!cwd || !path.isAbsolute(cwd)) return null
  return runHook(input.questions ?? [], context.sessionID, cwd, context.signal)
}

export default {
  id: "visual-explain",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.update("question", (tool) => {
        const ask = tool.execute
        tool.execute = async (input, context) => {
          const answers = await askOnPage(ctx, input, context)
          if (!answers) return ask(input, context)
          // 質問のツールの権限は、元の実行（question ツール）が確かめている。差し替えたときも同じように確かめ、
          // 断られたら元の実行に任せる（いつもどおり断られる）
          try {
            await ctx.permission.assert({
              action: "question",
              resources: ["*"],
              sessionID: context.sessionID,
              agent: context.agent,
              source: { type: "tool", messageID: context.messageID, id: context.id },
            })
          } catch {
            return ask(input, context)
          }
          const questions = input.questions ?? []
          return { output: { answers }, content: contentText(questions, answers), metadata: { answers } }
        }
      })
    })
  },
}
