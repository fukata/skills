'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { listClaudeAccounts, selectClaudeAccount } = require('../lib/orca.js');
const { SYSTEM_DEFAULT_KEY, decide } = require('../lib/decide.js');

function fixture(t, snapshot) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'failover-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const snapshotPath = path.join(dir, 'snapshot.json');
  if (snapshot !== undefined) fs.writeFileSync(snapshotPath, snapshot);
  const calls = [];
  const client = { async call(method, params) {
    calls.push({ method, params });
    return { result: { claude: { activeAccountId: 'team', accounts: [
      { id: 'team', email: 'user@example.test', organizationUuid: 'team-org', createdAt: 2 },
      { id: 'other', email: 'other@example.test', organizationUuid: 'other-org', createdAt: 1 }
    ] } } };
  } };
  return { client, snapshotPath, calls };
}
const snapshot = (organizationUuid = 'personal-org') => JSON.stringify({
  credentialsJson: JSON.stringify({ claudeAiOauth: { accessToken: 'test-only' } }),
  configOauthAccount: { emailAddress: 'user@example.test', organizationUuid }
});

test('同じメールの個人組織を追加し、管理アカウントが上限中なら復帰先にする', async (t) => {
  const f = fixture(t, snapshot());
  const accounts = await listClaudeAccounts(f.client, f);
  assert.deepEqual(accounts.accountIds, ['other', 'team', SYSTEM_DEFAULT_KEY]);
  assert.match(accounts.emails[SYSTEM_DEFAULT_KEY], /system default/);
  const now = 10000;
  const state = { active: { key: 'team', since: 1 }, limited: { other: { until: 20000 } } };
  const result = decide({ ...accounts, state, now, processStartedAt: 2, sessionId: 's', limitUntil: 20000 });
  assert.equal(result.action, 'switch');
  assert.equal(result.target, SYSTEM_DEFAULT_KEY);
  const exhausted = decide({ ...accounts, activeId: null, state: result.state,
    now: now + 2, processStartedAt: now + 1, sessionId: 's', limitUntil: 20000 });
  assert.equal(exhausted.reason, 'no-available-account');
  const recovered = decide({ ...accounts, state: { ...state,
    limited: { other: { until: 20000 }, [SYSTEM_DEFAULT_KEY]: { until: now } } },
    now, processStartedAt: 2, sessionId: 's', limitUntil: 20000 });
  assert.equal(recovered.target, SYSTEM_DEFAULT_KEY);
});

test('同じメール・組織の管理アカウントと重複させない', async (t) => {
  const f = fixture(t, snapshot('team-org'));
  assert.deepEqual((await listClaudeAccounts(f.client, f)).accountIds, ['other', 'team']);
});

for (const [name, value] of [['未保存', undefined], ['破損', '{'], ['空', '{}'],
  ['トークンなし', JSON.stringify({ credentialsJson: '{}', configOauthAccount: { emailAddress: 'x', organizationUuid: 'x' } })]]) {
  test(`${name}の認証情報では System Default を追加しない`, async (t) => {
    const f = fixture(t, value);
    assert.deepEqual((await listClaudeAccounts(f.client, f)).accountIds, ['other', 'team']);
  });
}

for (const target of [SYSTEM_DEFAULT_KEY, 'team']) {
  test(`${target} の切替は RPC と切替後の一覧で確認する`, async () => {
    const selectedId = target === SYSTEM_DEFAULT_KEY ? null : target;
    const calls = [];
    const client = { async call(method, params) {
      calls.push({ method, params });
      return { result: { claude: { activeAccountId: selectedId, accounts: [] } } };
    } };
    await selectClaudeAccount(client, target);
    assert.deepEqual(calls[0], { method: 'accounts.selectClaude', params: { accountId: selectedId } });
    assert.equal(calls[1].method, 'accounts.list');
  });
}

test('System Default への切替が反映されなければ失敗する', async (t) => {
  const f = fixture(t);
  await assert.rejects(selectClaudeAccount(f.client, SYSTEM_DEFAULT_KEY), /did not take effect/);
});
