'use strict';
// 利用上限で止まった Claude Code セッションを、利用可能な別の Claude アカウントで再開する。
//
// hook.sh（Claude Code の StopFailure hook）から、セッションごとに切り離して起動される。流れ:
//   1. 止まった理由が利用上限であることを確かめる（一時的な 429 では何もしない）
//   2. 必要なら Orca のアクティブな Claude アカウントを次のアカウントへ切り替える（lib/decide.js）
//   3. 止まった claude を終了させ、同じ Orca 端末で `claude --resume <session-id>` を打ち直す
//
// Orca の切替はホスト全体の認証情報（~/.claude/.credentials.json）を書き換える。切り替えるのは最初に
// 止まった 1 セッションだけで、残りのセッションは各自が止まった時点でここへ来て、再開だけを行う。
//
// 状態ディレクトリ: $ORCA_ACCOUNT_FAILOVER_STATE_DIR（既定は ~/.local/state/orca-account-failover。hook.sh が決める）
// 止めたいとき: 状態ディレクトリに disabled という名前のファイルを置く（hook.sh が何もせず戻る）。
// 記録: 状態ディレクトリの failover.log（1 行 1 件の JSON）
// 試すとき: ORCA_ACCOUNT_FAILOVER_DRY_RUN=1 で、判定を記録するだけで切替も再開も行わない。

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { SYSTEM_DEFAULT_KEY, parseLimitMessage, limitUntil, carryOverArgs, isPrintMode, decide } = require('./lib/decide.js');
const { createClient, listClaudeAccounts, selectClaudeAccount, runOrcaCli } = require('./lib/orca.js');

const STATE_DIR = process.env.ORCA_ACCOUNT_FAILOVER_STATE_DIR;
const DRY_RUN = process.env.ORCA_ACCOUNT_FAILOVER_DRY_RUN === '1';
const RESUME_SCRIPT = path.join(__dirname, 'resume.sh');

const RESUME_PROMPT =
  '【自動再開】利用上限で中断したため、利用可能な Claude アカウントでセッションを再開しました（orca-account-failover）。中断した作業を続けてください。';

const SESSION_ID_PATTERN = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
// hook の入力に文面が無いときにセッションログから拾う。これより古い記録は今回の失敗のものではない
const TRANSCRIPT_RECORD_MAX_AGE_MS = 2 * 60 * 1000;
const TRANSCRIPT_TAIL_BYTES = 256 * 1024;
const LOCK_TIMEOUT_MS = 2 * 60 * 1000;
const LOCK_STALE_MS = 5 * 60 * 1000;
const LOCK_POLL_MS = 250;
const EXIT_WAIT_MS = 20 * 1000;
const EXIT_POLL_MS = 200;
// claude の終了からシェルがプロンプトを出すまでの待ち
const SHELL_SETTLE_MS = 1500;
const RESUME_CONFIRM_MS = 20 * 1000;
const RESUME_CONFIRM_POLL_MS = 500;

/** 何もせず終える理由。異常ではないので error とは分けて記録する */
class Skip extends Error {}

function log(event, fields = {}) {
  const line = JSON.stringify({ ts: new Date().toISOString(), event, pid: process.pid, ...fields });
  fs.appendFileSync(path.join(STATE_DIR, 'failover.log'), `${line}\n`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseArgs(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    options[argv[i]] = argv[i + 1];
  }
  return { payloadPath: options['--payload'], claudePid: Number(options['--claude-pid']) };
}

function readPayload(payloadPath) {
  const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
  fs.unlinkSync(payloadPath);
  return payload;
}

function lastRateLimitRecord(transcriptPath, now) {
  if (!transcriptPath || !fs.existsSync(transcriptPath)) {
    return null;
  }
  const { size } = fs.statSync(transcriptPath);
  const length = Math.min(size, TRANSCRIPT_TAIL_BYTES);
  const buffer = Buffer.alloc(length);
  const fd = fs.openSync(transcriptPath, 'r');
  try {
    fs.readSync(fd, buffer, 0, length, size - length);
  } finally {
    fs.closeSync(fd);
  }
  const lines = buffer.toString('utf8').split('\n').reverse();
  for (const line of lines) {
    if (!line.includes('"isApiErrorMessage":true')) {
      continue;
    }
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      continue; // 末尾から切り出した先頭の行は途中から始まる
    }
    if (record.error !== 'rate_limit' || now - Date.parse(record.timestamp) > TRANSCRIPT_RECORD_MAX_AGE_MS) {
      return null;
    }
    return { text: record.message?.content?.[0]?.text, apiError: record.apiError };
  }
  return null;
}

function readUsageLimit(payload, now) {
  const fromHook = parseLimitMessage(payload.last_assistant_message);
  if (fromHook) {
    return fromHook;
  }
  const record = lastRateLimitRecord(payload.transcript_path, now);
  if (!record) {
    return null;
  }
  return parseLimitMessage(record.text) ?? (record.apiError === 'usage_limit_reached' ? { kind: 'unknown', reset: null } : null);
}

function readProcFile(pid, name) {
  try {
    return fs.readFileSync(`/proc/${pid}/${name}`, 'utf8');
  } catch {
    return null;
  }
}

/**
 * 終了させてよい claude プロセスかを確かめ、起動引数と起動時刻を返す。
 * 再開コマンドを打ち込む端末（handle）で動いている claude だけを対象にする。別の端末の claude を
 * 止めると、そのセッションは止まったまま、無関係な端末に再開コマンドが打たれる。
 */
function inspectClaudeProcess(pid, handle, now) {
  if (!Number.isInteger(pid) || pid <= 1) {
    throw new Skip('claude-pid-unknown');
  }
  if (readProcFile(pid, 'comm')?.trim() !== 'claude') {
    throw new Skip('not-a-claude-process');
  }
  const environ = readProcFile(pid, 'environ')?.split('\0') ?? [];
  if (!environ.includes(`ORCA_TERMINAL_HANDLE=${handle}`)) {
    throw new Skip('claude-not-in-this-terminal');
  }
  const args = (readProcFile(pid, 'cmdline') ?? '').split('\0').slice(1);
  if (args.at(-1) === '') {
    args.pop();
  }
  const elapsedMs = processElapsedMs(pid);
  if (elapsedMs === null) {
    throw new Skip('claude-start-time-unknown');
  }
  return { pid, args, startedAt: now - elapsedMs };
}

/**
 * プロセスが起動してからの経過時間。ps の etimes は秒に切り捨てるため、切替直後に再開したプロセスの
 * 前後関係を取り違える。/proc の起動時刻（起動からの tick 数）で求める
 */
function processElapsedMs(pid) {
  const stat = readProcFile(pid, 'stat');
  const uptimeSeconds = Number(fs.readFileSync('/proc/uptime', 'utf8').split(' ')[0]);
  const ticksPerSecond = Number(spawnSync('getconf', ['CLK_TCK'], { encoding: 'utf8' }).stdout);
  if (!stat || !Number.isFinite(uptimeSeconds) || !(ticksPerSecond > 0)) {
    return null;
  }
  // comm は括弧や空白を含みうるので、最後の ')' より後ろを数える。starttime は全体の 22 番目
  const startTicks = Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[19]);
  return Number.isFinite(startTicks) ? (uptimeSeconds - startTicks / ticksPerSecond) * 1000 : null;
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

async function withLock(task) {
  const lockDir = path.join(STATE_DIR, 'lock');
  const ownerFile = path.join(lockDir, 'owner');
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  for (;;) {
    try {
      fs.mkdirSync(lockDir);
      break;
    } catch (err) {
      if (err.code !== 'EEXIST') {
        throw err;
      }
    }
    if (isLockStale(lockDir, ownerFile)) {
      fs.rmSync(lockDir, { recursive: true, force: true });
      continue;
    }
    if (Date.now() >= deadline) {
      throw new Error('lock timeout');
    }
    await sleep(LOCK_POLL_MS);
  }
  fs.writeFileSync(ownerFile, String(process.pid));
  try {
    return await task();
  } finally {
    fs.rmSync(lockDir, { recursive: true, force: true });
  }
}

function isLockStale(lockDir, ownerFile) {
  let owner = NaN;
  let ageMs = 0;
  try {
    ageMs = Date.now() - fs.statSync(lockDir).mtimeMs;
    owner = Number(fs.readFileSync(ownerFile, 'utf8'));
  } catch {
    // 取得直後で owner がまだ書かれていない、または相手が解放した直後
  }
  if (Number.isInteger(owner) && owner > 0) {
    return !isAlive(owner);
  }
  return ageMs > LOCK_STALE_MS;
}

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'state.json'), 'utf8'));
  } catch (err) {
    if (err.code === 'ENOENT') {
      return {};
    }
    throw err;
  }
}

function saveState(state) {
  const target = path.join(STATE_DIR, 'state.json');
  const temp = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(state, null, 2)}\n`);
  fs.renameSync(temp, target);
}

function shellQuote(value) {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

async function waitFor(condition, timeoutMs, pollMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (condition()) {
      return true;
    }
    await sleep(pollMs);
  }
  return condition();
}

function isResumed(sessionId) {
  const found = spawnSync('pgrep', ['-f', `claude .*--resume ${sessionId}`], { encoding: 'utf8' });
  return found.status === 0;
}

async function restartSession({ claude, handle, sessionId, context }) {
  const resumeDir = path.join(STATE_DIR, 'resume');
  fs.mkdirSync(resumeDir, { recursive: true });
  const argsFile = path.join(resumeDir, `${sessionId}.args`);
  // resume.sh が mapfile -d '' で読む。引数をシェルの文字列として端末へ流さないための受け渡し
  const resumeArgs = [...carryOverArgs(claude.args), '--resume', sessionId, RESUME_PROMPT];
  fs.writeFileSync(argsFile, resumeArgs.map((arg) => `${arg}\0`).join(''));

  // ロック待ちの間に元のプロセスが終わり、PID が別のプロセスへ再利用されていないかを確かめ直す
  inspectClaudeProcess(claude.pid, handle, Date.now());
  process.kill(claude.pid, 'SIGTERM');
  if (!(await waitFor(() => !isAlive(claude.pid), EXIT_WAIT_MS, EXIT_POLL_MS))) {
    throw new Error(`claude (pid ${claude.pid}) did not exit after SIGTERM`);
  }
  await sleep(SHELL_SETTLE_MS);

  const command = `bash ${shellQuote(RESUME_SCRIPT)} ${shellQuote(argsFile)}`;
  const sent = runOrcaCli(['terminal', 'send', '--terminal', handle, '--text', command, '--enter', '--json']);
  if (!sent.ok) {
    throw new Error(`orca terminal send failed: ${(sent.stderr || sent.stdout).trim().slice(0, 500)}`);
  }
  const resumed = await waitFor(() => isResumed(sessionId), RESUME_CONFIRM_MS, RESUME_CONFIRM_POLL_MS);
  log(resumed ? 'resumed' : 'error', { ...context, ...(resumed ? {} : { message: 'resume command sent but claude did not start' }) });
}

async function main() {
  const { payloadPath, claudePid } = parseArgs(process.argv.slice(2));
  const payload = readPayload(payloadPath);
  const sessionId = payload.session_id;
  const context = { session: sessionId, dryRun: DRY_RUN };
  try {
    const now = Date.now();
    if (payload.hook_event_name !== 'StopFailure' || payload.error !== 'rate_limit') {
      throw new Skip('not-rate-limit');
    }
    if (typeof sessionId !== 'string' || !SESSION_ID_PATTERN.test(sessionId)) {
      throw new Skip('invalid-session-id');
    }
    const limit = readUsageLimit(payload, now);
    if (!limit) {
      throw new Skip('not-usage-limit');
    }
    const handle = process.env.ORCA_TERMINAL_HANDLE;
    if (!handle) {
      throw new Skip('not-in-orca-terminal');
    }
    const claude = inspectClaudeProcess(claudePid, handle, now);
    if (isPrintMode(claude.args)) {
      throw new Skip('print-mode');
    }
    const until = limitUntil(limit, now);

    const client = createClient();
    const decision = await withLock(async () => {
      const accounts = await listClaudeAccounts(client);
      const decided = decide({
        now,
        accountIds: accounts.accountIds,
        activeId: accounts.activeId,
        state: loadState(),
        sessionId,
        processStartedAt: claude.startedAt,
        limitUntil: until.at
      });
      log('decision', {
        ...context,
        action: decided.action,
        reason: decided.reason,
        limit: limit.kind,
        limitUntil: new Date(until.at).toISOString(),
        resetParsed: until.parsed,
        active: accounts.emails[accounts.activeId ?? SYSTEM_DEFAULT_KEY] ?? 'system default',
        target: decided.target ? accounts.emails[decided.target] : undefined
      });
      if (DRY_RUN) {
        return decided;
      }
      if (decided.action === 'switch') {
        await selectClaudeAccount(client, decided.target);
        log('switched', { ...context, to: accounts.emails[decided.target] });
      }
      saveState(decided.state);
      return decided;
    });

    if (DRY_RUN || decision.action === 'give-up') {
      return;
    }
    await restartSession({ claude, handle, sessionId, context });
  } catch (err) {
    if (err instanceof Skip) {
      log('skip', { ...context, reason: err.message });
      return;
    }
    log('error', { ...context, message: err?.message ?? String(err) });
    process.exitCode = 1;
  }
}

if (!STATE_DIR) {
  console.error('ORCA_ACCOUNT_FAILOVER_STATE_DIR is not set (worker.js is started by hook.sh)');
  process.exit(2);
}
main()
  .catch((err) => {
    log('error', { message: err?.message ?? String(err) });
    process.exitCode = 1;
  })
  // RuntimeClient が接続を持ち続けることがあるので、終わったら明示的に抜ける
  .finally(() => process.exit());
