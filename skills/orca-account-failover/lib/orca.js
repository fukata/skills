'use strict';
// Orca の runtime とのやり取り。worker.js は Orca の Electron を ELECTRON_RUN_AS_NODE=1 で起動して動かす
// （orca CLI と同じ起動方法）ので、CLI の場所は process.execPath から辿れる。

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { SYSTEM_DEFAULT_KEY } = require('./decide.js');
const { spawnSync } = require('node:child_process');

const CLI_TIMEOUT_MS = 30_000;

function resolveCliDir() {
  if (process.env.ORCA_CLI_DIR) {
    return process.env.ORCA_CLI_DIR;
  }
  const execDir = path.dirname(process.execPath);
  const candidates = [
    // Linux / Windows: <app>/resources/app.asar.unpacked
    path.join(execDir, 'resources', 'app.asar.unpacked', 'out', 'cli'),
    // macOS: <app>.app/Contents/MacOS/<bin> と <app>.app/Contents/Resources
    path.join(execDir, '..', 'Resources', 'app.asar.unpacked', 'out', 'cli')
  ];
  const found = candidates.find((dir) => fs.existsSync(path.join(dir, 'runtime-client.js')));
  if (!found) {
    throw new Error(`Orca CLI directory not found near ${process.execPath} (set ORCA_CLI_DIR)`);
  }
  return found;
}

function createClient() {
  const { RuntimeClient } = require(path.join(resolveCliDir(), 'runtime-client.js'));
  return new RuntimeClient();
}

function readSystemDefault(snapshotPath) {
  try {
    const snapshot = JSON.parse(fs.readFileSync(snapshotPath, 'utf8'));
    const credentials = JSON.parse(snapshot.credentialsJson);
    const identity = snapshot.configOauthAccount;
    if (!credentials?.claudeAiOauth?.accessToken || !identity?.organizationUuid || !identity?.emailAddress) {
      return null;
    }
    return { email: identity.emailAddress, organizationUuid: identity.organizationUuid };
  } catch {
    // 未保存・破損・認証情報なしの場合は復帰先にしない。認証情報をログへ出さない。
    return null;
  }
}

function systemDefaultSnapshotPath() {
  const userData = process.env.ORCA_ACCOUNT_FAILOVER_ORCA_USER_DATA_DIR
    || path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), 'orca');
  return path.join(userData, 'claude-runtime-auth', 'system-default-auth.json');
}

/**
 * @returns {Promise<{accountIds: string[], activeId: string|null, emails: Object<string, string>}>}
 */
async function listClaudeAccounts(client, { snapshotPath = systemDefaultSnapshotPath() } = {}) {
  // refreshUsage: false — 使用量は判定に使わない。取得側がレート制限に掛かっていると待たされるだけになる
  const { result } = await client.call('accounts.list', { refreshUsage: false });
  const claude = result?.claude;
  if (!claude || !Array.isArray(claude.accounts)) {
    throw new Error(`accounts.list returned unexpected shape: ${JSON.stringify(Object.keys(result ?? {}))}`);
  }
  // 返ってくる順は updatedAt の降順で、再認証のたびに入れ替わる。回す順を固定するため登録順に並べる
  const accounts = [...claude.accounts].sort((a, b) => a.createdAt - b.createdAt);
  const systemDefault = readSystemDefault(snapshotPath);
  const accountIds = accounts.map((account) => account.id);
  const emails = Object.fromEntries(accounts.map((account) => [account.id, account.email]));
  // 同じメールでも Team と個人の組織が異なれば別の利用枠。組織まで一致するときだけ重複を除く。
  if (systemDefault && !accounts.some((account) =>
    account.email === systemDefault.email && account.organizationUuid === systemDefault.organizationUuid)) {
    accountIds.push(SYSTEM_DEFAULT_KEY);
    emails[SYSTEM_DEFAULT_KEY] = `${systemDefault.email} (system default)`;
  }
  return {
    accountIds,
    activeId: claude.activeAccountId ?? null,
    emails
  };
}

/**
 * アクティブな Claude アカウントを切り替える。
 * accounts.selectClaude は orca CLI に無い内部 RPC（Orca 1.4.220 で確認。モバイルアプリのアカウント切替が使う）。
 * Orca の更新で名前や引数が変わると、ここが失敗する。
 */
async function selectClaudeAccount(client, accountId) {
  const selectedId = accountId === SYSTEM_DEFAULT_KEY ? null : accountId;
  await client.call('accounts.selectClaude', { accountId: selectedId });
  // 戻り値の形に頼らず、一覧を取り直して切り替わったことを確かめる
  const { activeId } = await listClaudeAccounts(client);
  if (activeId !== selectedId) {
    throw new Error(`accounts.selectClaude did not take effect (active=${activeId}, expected=${accountId})`);
  }
}

/**
 * orca CLI を実行する
 * @returns {{ok: boolean, stdout: string, stderr: string}}
 */
function runOrcaCli(args) {
  const result = spawnSync(process.execPath, [path.join(resolveCliDir(), 'index.js'), ...args], {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    encoding: 'utf8',
    timeout: CLI_TIMEOUT_MS
  });
  return {
    ok: !result.error && result.status === 0,
    stdout: result.stdout ?? '',
    stderr: result.error ? String(result.error.message) : (result.stderr ?? '')
  };
}

module.exports = { createClient, listClaudeAccounts, selectClaudeAccount, runOrcaCli };
