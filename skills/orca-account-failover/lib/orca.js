'use strict';
// Orca の runtime とのやり取り。worker.js は Orca の Electron を ELECTRON_RUN_AS_NODE=1 で起動して動かす
// （orca CLI と同じ起動方法）ので、CLI の場所は process.execPath から辿れる。

const fs = require('node:fs');
const path = require('node:path');
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

/**
 * @returns {Promise<{accountIds: string[], activeId: string|null, emails: Object<string, string>}>}
 */
async function listClaudeAccounts(client) {
  // refreshUsage: false — 使用量は判定に使わない。取得側がレート制限に掛かっていると待たされるだけになる
  const { result } = await client.call('accounts.list', { refreshUsage: false });
  const claude = result?.claude;
  if (!claude || !Array.isArray(claude.accounts)) {
    throw new Error(`accounts.list returned unexpected shape: ${JSON.stringify(Object.keys(result ?? {}))}`);
  }
  // 返ってくる順は updatedAt の降順で、再認証のたびに入れ替わる。回す順を固定するため登録順に並べる
  const accounts = [...claude.accounts].sort((a, b) => a.createdAt - b.createdAt);
  return {
    accountIds: accounts.map((account) => account.id),
    activeId: claude.activeAccountId ?? null,
    emails: Object.fromEntries(accounts.map((account) => [account.id, account.email]))
  };
}

/**
 * アクティブな Claude アカウントを切り替える。
 * accounts.selectClaude は orca CLI に無い内部 RPC（Orca 1.4.220 で確認。モバイルアプリのアカウント切替が使う）。
 * Orca の更新で名前や引数が変わると、ここが失敗する。
 */
async function selectClaudeAccount(client, accountId) {
  await client.call('accounts.selectClaude', { accountId });
  // 戻り値の形に頼らず、一覧を取り直して切り替わったことを確かめる
  const { activeId } = await listClaudeAccounts(client);
  if (activeId !== accountId) {
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
