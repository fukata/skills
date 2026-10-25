'use strict';
// Orca のアカウントごとの設定フォルダ（CLAUDE_CONFIG_DIR）と、アカウントの対応。
//
// Orca は管理アカウントごとに <データフォルダ>/claude-profiles/<アカウントID>/home を作り、端末を開くときに
// そのとき選ばれているアカウントのフォルダを CLAUDE_CONFIG_DIR で渡す。この値は端末を閉じるまで変わらない。
// System Default（管理アカウントを選んでいない状態）では CLAUDE_CONFIG_DIR を外す（利用者が自分で指定していた
// フォルダがあれば、Orca はそれを ORCA_CLAUDE_USER_CONFIG_DIR で渡し、System Default ではそれに戻す）。

const fs = require('node:fs');
const path = require('node:path');
const { SYSTEM_DEFAULT_KEY } = require('./decide.js');

const sameDir = (a, b) => Boolean(a) && Boolean(b) && path.resolve(a) === path.resolve(b);

/**
 * 止まった claude が使っていたアカウント
 * @param {object} input
 * @param {string|undefined} input.configDir 止まった claude の CLAUDE_CONFIG_DIR
 * @param {string|undefined} input.userConfigDir 止まった claude の ORCA_CLAUDE_USER_CONFIG_DIR
 * @param {string} input.homeDir
 * @param {string} input.profilesRoot <Orca のデータフォルダ>/claude-profiles
 * @param {string|null} input.systemDefaultAccount listClaudeAccounts の systemDefaultAccount
 * @returns {string|null} Orca の管理外のフォルダなら null
 */
function failedAccountKey({ configDir, userConfigDir, homeDir, profilesRoot, systemDefaultAccount }) {
  if (!configDir || sameDir(configDir, userConfigDir) || sameDir(configDir, path.join(homeDir, '.claude'))) {
    return systemDefaultAccount ?? SYSTEM_DEFAULT_KEY;
  }
  const dir = path.resolve(configDir);
  if (path.basename(dir) === 'home' && sameDir(path.dirname(path.dirname(dir)), profilesRoot)) {
    return path.basename(path.dirname(dir));
  }
  return null;
}

/**
 * 切替先のアカウントで claude を起動するときの CLAUDE_CONFIG_DIR
 * @returns {string|null} アカウントのフォルダ。System Default なら ''（resume.sh が指定を外す）。起動に使えなければ null
 */
function launchHome({ key, profilesRoot, systemDefaultAccount }) {
  if (key === SYSTEM_DEFAULT_KEY) {
    return '';
  }
  const home = path.join(profilesRoot, key, 'home');
  if (fs.existsSync(path.join(home, '.credentials.json'))) {
    return home;
  }
  // Orca も、フォルダにログインが無く System Default と同じ認証先のアカウントは System Default のフォルダで動かす
  return key === systemDefaultAccount ? '' : null;
}

module.exports = { failedAccountKey, launchHome };
