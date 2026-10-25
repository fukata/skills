'use strict';
// 利用上限で止まった Claude Code セッションを、利用可能な別の Claude アカウントで再開する。
//
// hook.sh（Claude Code の StopFailure hook）から、セッションごとに切り離して起動される。流れ:
//   1. 止まった理由が利用上限であることを確かめる（一時的な 429 では何もしない）
//   2. 止まった claude の設定フォルダ（CLAUDE_CONFIG_DIR）から上限に達したアカウントを特定し、
//      上限でない別のアカウントを選ぶ（lib/profiles.js、lib/decide.js）。Orca の選択もそのアカウントに変える
//   3. 止まった claude を終了させ、同じ Orca 端末で、選んだアカウントのフォルダで `claude --resume <session-id>` を打ち直す
//
// Orca は端末ごとに、端末を開いた時点の選択のアカウントのフォルダを CLAUDE_CONFIG_DIR で渡す。Orca の選択を
// 変えても動いている claude のアカウントは変わらず、次に起動する claude だけが変わる。そのため再開では、
// 選んだアカウントのフォルダを resume.sh に明示して起動する。Orca の選択を変えるのは、この後に人が端末で
// 起動し直す claude や新しく開く端末も、上限でないアカウントで動かすため（失敗しても再開は続ける）。
//
// 状態ディレクトリ: $ORCA_ACCOUNT_FAILOVER_STATE_DIR（既定は ~/.local/state/orca-account-failover。hook.sh が決める）
// 止めたいとき: 状態ディレクトリに disabled という名前のファイルを置く（hook.sh が何もせず戻る）。
// 記録: 状態ディレクトリの failover.log（1 行 1 件の JSON）
// 試すとき: ORCA_ACCOUNT_FAILOVER_DRY_RUN=1 で、判定を記録するだけで切替も再開も行わない。

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { SYSTEM_DEFAULT_KEY, parseLimitMessage, limitUntil, carryOverArgs, isPrintMode, decide } = require('./lib/decide.js');
const { createClient, listClaudeAccounts, selectClaudeAccount, runOrcaCli } = require('./lib/orca.js');
const { failedAccountKey, launchHome } = require('./lib/profiles.js');

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
 * 終了させてよい claude プロセスかを確かめ、起動引数と環境変数を返す。
 * 再開コマンドを打ち込む端末（handle）で動いている claude だけを対象にする。別の端末の claude を
 * 止めると、そのセッションは止まったまま、無関係な端末に再開コマンドが打たれる。
 */
function inspectClaudeProcess(pid, handle) {
  if (!Number.isInteger(pid) || pid <= 1) {
    throw new Skip('claude-pid-unknown');
  }
  if (readProcFile(pid, 'comm')?.trim() !== 'claude') {
    throw new Skip('not-a-claude-process');
  }
  const env = {};
  for (const entry of readProcFile(pid, 'environ')?.split('\0') ?? []) {
    const separator = entry.indexOf('=');
    if (separator > 0) {
      env[entry.slice(0, separator)] = entry.slice(separator + 1);
    }
  }
  if (env.ORCA_TERMINAL_HANDLE !== handle) {
    throw new Skip('claude-not-in-this-terminal');
  }
  const args = (readProcFile(pid, 'cmdline') ?? '').split('\0').slice(1);
  if (args.at(-1) === '') {
    args.pop();
  }
  return { pid, args, env };
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

/**
 * @param {string} home 再開する claude の CLAUDE_CONFIG_DIR。System Default なら ''
 */
async function restartSession({ claude, handle, sessionId, home, context }) {
  const resumeDir = path.join(STATE_DIR, 'resume');
  fs.mkdirSync(resumeDir, { recursive: true });
  const argsFile = path.join(resumeDir, `${sessionId}.args`);
  // resume.sh が mapfile -d '' で読む。引数をシェルの文字列として端末へ流さないための受け渡し
  const resumeArgs = [...carryOverArgs(claude.args), '--resume', sessionId, RESUME_PROMPT];
  fs.writeFileSync(argsFile, resumeArgs.map((arg) => `${arg}\0`).join(''));

  // ロック待ちの間に元のプロセスが終わり、PID が別のプロセスへ再利用されていないかを確かめ直す
  inspectClaudeProcess(claude.pid, handle);
  process.kill(claude.pid, 'SIGTERM');
  if (!(await waitFor(() => !isAlive(claude.pid), EXIT_WAIT_MS, EXIT_POLL_MS))) {
    throw new Error(`claude (pid ${claude.pid}) did not exit after SIGTERM`);
  }
  await sleep(SHELL_SETTLE_MS);

  const command = `bash ${shellQuote(RESUME_SCRIPT)} ${shellQuote(argsFile)} ${shellQuote(home)}`;
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
    const claude = inspectClaudeProcess(claudePid, handle);
    if (isPrintMode(claude.args)) {
      throw new Skip('print-mode');
    }
    // Orca が端末に渡す、選択中のフォルダの場所を書いたファイル（<データフォルダ>/claude-profiles/selected-host）。
    // 無ければ、アカウントごとの設定フォルダを使わない Orca か、Orca の管理外で起動した claude
    const pointer = claude.env.ORCA_CLAUDE_PROFILE_POINTER;
    if (!pointer || !path.isAbsolute(pointer)) {
      throw new Skip('no-claude-profiles');
    }
    const profilesRoot = path.dirname(pointer);
    const until = limitUntil(limit, now);

    const client = createClient();
    const decision = await withLock(async () => {
      const accounts = await listClaudeAccounts(client);
      const failedKey = failedAccountKey({
        configDir: claude.env.CLAUDE_CONFIG_DIR,
        userConfigDir: claude.env.ORCA_CLAUDE_USER_CONFIG_DIR,
        homeDir: os.homedir(),
        profilesRoot,
        systemDefaultAccount: accounts.systemDefaultAccount
      });
      if (failedKey === null) {
        // 利用者が自分で指定したフォルダなど。どのアカウントの上限か分からない
        throw new Skip('unknown-config-dir');
      }
      const homes = Object.fromEntries(accounts.accountIds.map((key) =>
        [key, launchHome({ key, profilesRoot, systemDefaultAccount: accounts.systemDefaultAccount })]));
      const excluded = (key) => accounts.needsSignIn.includes(key) ? 'needs-sign-in' : homes[key] === null ? 'no-credentials' : null;
      const decided = decide({
        now: Date.now(),
        identities: accounts.identities,
        limitKind: limit.kind,
        accountIds: accounts.accountIds.filter((key) => !excluded(key)),
        failedKey,
        state: loadState(),
        sessionId,
        limitUntil: until.at
      });
      const activeKey = accounts.activeId ?? SYSTEM_DEFAULT_KEY;
      log('decision', {
        ...context,
        action: decided.action,
        reason: decided.reason,
        limit: limit.kind,
        limitUntil: new Date(until.at).toISOString(),
        resetParsed: until.parsed,
        failed: accounts.emails[failedKey] ?? failedKey,
        active: accounts.emails[activeKey] ?? 'system default',
        target: decided.target ? accounts.emails[decided.target] : undefined,
        probe: decided.state.probe,
        candidates: accounts.accountIds.map((key) => ({
          key, identity: accounts.identities[key],
          limited: decided.state.limited[key] ?? null,
          reason: key === decided.target ? decided.reason :
            key === failedKey ? 'failed-account' :
              excluded(key) ?? (decided.state.limited[key]?.until > Date.now() ? 'cached-limit' : 'available')
        }))
      });
      if (DRY_RUN) {
        return decided;
      }
      saveState(decided.state);
      if (decided.action === 'switch' && decided.target !== activeKey) {
        try {
          await selectClaudeAccount(client, decided.target);
          log('switched', { ...context, to: accounts.emails[decided.target] });
        } catch (err) {
          // 再開は選んだフォルダで行うので、Orca の選択を変えられなくても続ける
          log('error', { ...context, message: `accounts.selectClaude failed (resuming anyway): ${err?.message ?? err}` });
        }
      }
      return { ...decided, home: homes[decided.target], to: accounts.emails[decided.target] };
    });

    if (DRY_RUN || decision.action === 'give-up') {
      return;
    }
    await restartSession({ claude, handle, sessionId, home: decision.home, context: { ...context, to: decision.to } });
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
