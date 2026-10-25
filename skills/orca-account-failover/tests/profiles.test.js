'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { failedAccountKey, launchHome } = require('../lib/profiles.js');
const { SYSTEM_DEFAULT_KEY } = require('../lib/decide.js');

// Orca のデータフォルダを一時ディレクトリに作る。credentials に挙げたアカウントだけ認証情報を置く
function fixture(t, { credentials = [] } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'failover-profiles-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const profilesRoot = path.join(dir, 'claude-profiles');
  for (const id of ['team', 'other']) {
    fs.mkdirSync(path.join(profilesRoot, id, 'home'), { recursive: true });
  }
  for (const id of credentials) {
    fs.writeFileSync(path.join(profilesRoot, id, 'home', '.credentials.json'), '{}');
  }
  return { profilesRoot, homeDir: path.join(dir, 'user') };
}

test('failedAccountKey: Orca のアカウントのフォルダなら、そのアカウント', (t) => {
  const f = fixture(t);
  const key = (configDir) => failedAccountKey({ ...f, configDir, systemDefaultAccount: SYSTEM_DEFAULT_KEY });
  assert.equal(key(path.join(f.profilesRoot, 'team', 'home')), 'team');
  assert.equal(key(`${path.join(f.profilesRoot, 'team', 'home')}/`), 'team');
});

test('failedAccountKey: フォルダの指定が無い・利用者のフォルダ・~/.claude なら System Default', (t) => {
  const f = fixture(t);
  const userConfigDir = path.join(f.homeDir, 'my-claude');
  const key = (configDir) => failedAccountKey({ ...f, configDir, userConfigDir, systemDefaultAccount: SYSTEM_DEFAULT_KEY });
  assert.equal(key(undefined), SYSTEM_DEFAULT_KEY);
  assert.equal(key(''), SYSTEM_DEFAULT_KEY);
  assert.equal(key(userConfigDir), SYSTEM_DEFAULT_KEY);
  assert.equal(key(path.join(f.homeDir, '.claude')), SYSTEM_DEFAULT_KEY);
});

test('failedAccountKey: System Default が管理アカウントと同じ認証先なら、その管理アカウント', (t) => {
  const f = fixture(t);
  assert.equal(failedAccountKey({ ...f, configDir: undefined, systemDefaultAccount: 'team' }), 'team');
  // System Default を候補にできない（退避した認証情報が読めない）ときも、記録は System Default のキーで残す
  assert.equal(failedAccountKey({ ...f, configDir: undefined, systemDefaultAccount: null }), SYSTEM_DEFAULT_KEY);
});

test('failedAccountKey: Orca の管理外のフォルダなら分からない（null）', (t) => {
  const f = fixture(t);
  const key = (configDir) => failedAccountKey({ ...f, configDir, systemDefaultAccount: SYSTEM_DEFAULT_KEY });
  assert.equal(key(path.join(f.homeDir, 'elsewhere')), null);
  assert.equal(key(path.join(f.profilesRoot, 'team')), null);
  assert.equal(key(path.join(f.profilesRoot, 'team', 'home', 'nested')), null);
});

test('launchHome: 認証情報のある管理アカウントは、そのフォルダで起動する', (t) => {
  const f = fixture(t, { credentials: ['team'] });
  assert.equal(launchHome({ ...f, key: 'team', systemDefaultAccount: SYSTEM_DEFAULT_KEY }), path.join(f.profilesRoot, 'team', 'home'));
});

test('launchHome: System Default は空文字（設定フォルダの指定を外して起動する）', (t) => {
  const f = fixture(t);
  assert.equal(launchHome({ ...f, key: SYSTEM_DEFAULT_KEY, systemDefaultAccount: SYSTEM_DEFAULT_KEY }), '');
});

test('launchHome: フォルダに認証情報が無い管理アカウントは、System Default と同じ認証先なら System Default で起動する', (t) => {
  // Orca も、フォルダにログインが無く System Default と同じアカウントなら System Default のフォルダを使う
  const f = fixture(t);
  assert.equal(launchHome({ ...f, key: 'team', systemDefaultAccount: 'team' }), '');
});

test('launchHome: フォルダに認証情報が無い管理アカウントは起動に使えない（null）', (t) => {
  const f = fixture(t, { credentials: ['team'] });
  assert.equal(launchHome({ ...f, key: 'other', systemDefaultAccount: SYSTEM_DEFAULT_KEY }), null);
});
