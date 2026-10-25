'use strict';
// 判定ロジック。Orca にも端末にもファイルにも触らないので、素の node でテストできる（tests/decide.test.js）。

// Orca で管理アカウントを選んでいない状態（エージェント自身のログインを使う）を表すキー
const SYSTEM_DEFAULT_KEY = 'system-default';

// 解除時刻を読めなかったときに、そのアカウントを上限中として扱う時間
const FALLBACK_LIMIT_MS = 60 * 60 * 1000;
// 文面の解除時刻は分に丸められているので、少しだけ遅らせて解けた扱いにする。
// ただし Claude Code 自身の自動継続（表示時刻の 44〜109 秒後。過去ログ 9 件）より前に解けた扱いにすること。
// 自動継続の再試行が別のアカウントの上限に当たって hook が起きたとき、解けたはずのアカウントを
// まだ上限中と見なすと、切替を見送ってしまう
const RESET_MARGIN_MS = 30 * 1000;
// 古い制限記録を試し直す最短間隔（ホスト全体）
const PROBE_INTERVAL_MS = 30 * 60 * 1000;
// 同じセッションの再開回数を数える時間幅
const RESUME_WINDOW_MS = 30 * 60 * 1000;
// これより先の解除時刻は読み違いとして扱う（週次の上限でも 7 日以内に解ける）
const MAX_RESET_AHEAD_MS = 8 * 24 * 60 * 60 * 1000;
const LIMITED_RETENTION_MS = 24 * 60 * 60 * 1000;
const SESSION_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// 実物（~/.claude/projects のセッションログ）:
//   You've hit your session limit · resets 6:50pm (Asia/Tokyo)
//   You've hit your weekly limit · resets 12am (Asia/Tokyo)
//   You've hit your weekly limit · resets Oct 6, 12am (Asia/Tokyo)
const LIMIT_PATTERN = /You['’]ve hit your (.+?) limit/i;
const RESET_PATTERN = /resets (?:([A-Z][a-z]{2}) (\d{1,2}), )?(\d{1,2})(?::(\d{2}))?\s?(am|pm) \(([^)]+)\)/;

// 再開後のセッションへ引き継ぐ起動オプション。ここに無いもの（起動時のプロンプト、--resume などの再開先指定、
// 未知のオプション）は捨てる。値を取るかどうか分からないオプションを残すと、その値をプロンプトと読み違えるため
const CARRY_OVER_FLAGS = new Set(['--dangerously-skip-permissions', '--allow-dangerously-skip-permissions', '--verbose']);
const CARRY_OVER_OPTIONS = new Set(['--model', '--permission-mode', '--effort', '--settings', '--agent', '--fallback-model']);

/**
 * 利用上限に達したときのエラー文面を読む
 * @param {unknown} text
 * @returns {{kind: string, reset: object|null}|null} 利用上限の文面でなければ null
 */
function parseLimitMessage(text) {
  if (typeof text !== 'string') {
    return null;
  }
  const limit = LIMIT_PATTERN.exec(text);
  if (!limit) {
    return null;
  }
  return { kind: limit[1].toLowerCase(), reset: parseReset(text) };
}

function parseReset(text) {
  const match = RESET_PATTERN.exec(text);
  if (!match) {
    return null;
  }
  const [, monthName, day, hour12, minute, meridiem, timeZone] = match;
  const month = monthName ? MONTHS.indexOf(monthName) : null;
  if (month === -1) {
    return null;
  }
  return {
    month,
    day: day ? Number(day) : null,
    hour: (Number(hour12) % 12) + (meridiem === 'pm' ? 12 : 0),
    minute: minute ? Number(minute) : 0,
    timeZone
  };
}

function isValidTimeZone(timeZone) {
  try {
    new Intl.DateTimeFormat('en-US', { timeZone });
    return true;
  } catch {
    return false;
  }
}

function wallClock(epoch, timeZone) {
  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric'
  });
  const parts = {};
  for (const { type, value } of formatter.formatToParts(new Date(epoch))) {
    parts[type] = Number(value);
  }
  return { year: parts.year, month: parts.month - 1, day: parts.day, hour: parts.hour, minute: parts.minute, second: parts.second };
}

function timeZoneOffsetMs(epoch, timeZone) {
  const wall = wallClock(epoch, timeZone);
  return Date.UTC(wall.year, wall.month, wall.day, wall.hour, wall.minute, wall.second) - (epoch - (epoch % 1000));
}

function wallClockToEpoch({ year, month, day, hour, minute }, timeZone) {
  const asUtc = Date.UTC(year, month, day, hour, minute);
  const guess = asUtc - timeZoneOffsetMs(asUtc, timeZone);
  // 夏時間の切替をまたぐと 1 回目の時差がずれるので、求めた時刻の時差で引き直す
  return asUtc - timeZoneOffsetMs(guess, timeZone);
}

/**
 * 文面の解除時刻を、now より後の時刻（epoch ms）にする
 * @returns {number|null} 読めない・ありえない時刻なら null
 */
function resolveResetTime(reset, now) {
  if (!reset || !isValidTimeZone(reset.timeZone)) {
    return null;
  }
  const today = wallClock(now, reset.timeZone);
  const at = (year, month, day) => wallClockToEpoch({ year, month, day, hour: reset.hour, minute: reset.minute }, reset.timeZone);
  if (reset.month === null) {
    const todayAt = at(today.year, today.month, today.day);
    return todayAt > now ? todayAt : at(today.year, today.month, today.day + 1);
  }
  const thisYear = at(today.year, reset.month, reset.day);
  const candidate = thisYear > now ? thisYear : at(today.year + 1, reset.month, reset.day);
  return candidate - now <= MAX_RESET_AHEAD_MS ? candidate : null;
}

/**
 * そのアカウントをいつまで上限中として扱うか
 * @returns {{at: number, parsed: boolean}}
 */
function limitUntil(limit, now) {
  const reset = resolveResetTime(limit.reset, now);
  return reset === null ? { at: now + FALLBACK_LIMIT_MS, parsed: false } : { at: reset + RESET_MARGIN_MS, parsed: true };
}

/**
 * @param {string[]} args claude の起動引数（argv[0] を除く）
 * @returns {string[]} 再開後のセッションへ引き継ぐ引数
 */
function carryOverArgs(args) {
  const kept = [];
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (CARRY_OVER_FLAGS.has(arg)) {
      kept.push(arg);
    } else if (CARRY_OVER_OPTIONS.has(arg)) {
      if (i + 1 < args.length) {
        kept.push(arg, args[++i]);
      }
    } else if (arg.includes('=') && CARRY_OVER_OPTIONS.has(arg.slice(0, arg.indexOf('=')))) {
      kept.push(arg);
    }
  }
  return kept;
}

function isPrintMode(args) {
  return args.includes('-p') || args.includes('--print');
}

function normalizeState(state, now) {
  const limited = {};
  for (const [key, entry] of Object.entries(state?.limited ?? {})) {
    if (entry.until > now - LIMITED_RETENTION_MS) {
      limited[key] = { ...entry };
    }
  }
  const sessions = {};
  for (const [id, entry] of Object.entries(state?.sessions ?? {})) {
    if (entry.lastAt > now - SESSION_RETENTION_MS) {
      sessions[id] = { ...entry };
    }
  }
  // 以前の版が持っていた Orca の選択の記録（active）は使わないので引き継がない
  return { limited, sessions, identities: { ...state?.identities }, probe: state?.probe ? { ...state.probe } : null };
}

function isLimited(state, key, now) {
  const entry = state.limited[key];
  return entry !== undefined && entry.until > now;
}

// 試している最中のアカウントは、記録上は上限中でも切替先にしてよい（使えるかを確かめている）
function isAvailable(state, key, now) {
  return !isLimited(state, key, now) || (state.probe?.key === key && state.probe.pending);
}

// 呼び出すのは止まったアカウントを上限と記録した後なので、そのアカウント自身は候補から外れる
function nextAvailableAccount(accountIds, failedKey, state, now) {
  const start = accountIds.indexOf(failedKey) + 1;
  const rotated = [...accountIds.slice(start), ...accountIds.slice(0, start)];
  return rotated.find((id) => id !== failedKey && isAvailable(state, id, now)) ?? null;
}

function recordResume(state, sessionId, now) {
  const previous = state.sessions[sessionId];
  if (!previous || now - previous.firstAt >= RESUME_WINDOW_MS) {
    state.sessions[sessionId] = { firstAt: now, lastAt: now, count: 1 };
  } else {
    state.sessions[sessionId] = { firstAt: previous.firstAt, lastAt: now, count: previous.count + 1 };
  }
}

/**
 * 利用上限で止まったセッションをどう扱うかを決める
 *
 * Orca は端末ごとに、アカウントの設定フォルダ（CLAUDE_CONFIG_DIR）を決めて claude に渡す。止まった claude が
 * どのフォルダで動いていたかで、上限に達したアカウント（failedKey）は確定する。Orca の選択中のアカウントは、
 * 次に起動する claude のアカウントでしかなく、動いている claude とは食い違うことがあるので判断に使わない。
 *
 * @param {object} input
 * @param {number} input.now
 * @param {string[]} input.accountIds 切替候補（再ログインが要るものは除いておく。復帰可能なら SYSTEM_DEFAULT_KEY も含む）。この順に回す
 * @param {string} input.failedKey 止まった claude が使っていたアカウント
 * @param {object} input.state 前回までの記録（書き換えない）
 * @param {string} input.sessionId
 * @param {number} input.limitUntil 今回の上限が解ける時刻
 * @returns {{action: 'switch'|'give-up', target?: string, reason: string, state: object}}
 */
function decide({ now, accountIds, failedKey, identities = {}, state, sessionId, limitUntil: until, limitKind }) {
  const next = normalizeState(state, now);

  // 旧形式の記録は組織を特定できないので保持し、古い記録の再試行で確認する。
  for (const [key, identity] of Object.entries(identities)) {
    if (next.identities[key] && next.identities[key] !== identity) {
      delete next.limited[key];
      if (next.probe?.key === key) next.probe.pending = false;
    }
    next.identities[key] = identity;
  }

  const session = next.sessions[sessionId];
  const maxResumes = accountIds.length + 2;
  if (session && session.count >= maxResumes && now - session.firstAt < RESUME_WINDOW_MS) {
    return { action: 'give-up', reason: 'resume-loop', state: next };
  }

  next.limited[failedKey] = { at: now, until, ...(limitKind ? { kind: limitKind } : {}) };
  if (next.probe?.key === failedKey) next.probe.pending = false;

  let target = nextAvailableAccount(accountIds, failedKey, next, now);
  let probing = false;
  if (target === null && (!next.probe || now - next.probe.at >= PROBE_INTERVAL_MS)) {
    // 全候補が上限中のときだけ、確認が最も古い別アカウントを一つ試す。
    target = accountIds.filter((key) => key !== failedKey &&
      Number.isFinite(next.limited[key]?.at) && now - next.limited[key].at >= PROBE_INTERVAL_MS)
      .sort((a, b) => next.limited[a].at - next.limited[b].at)[0] ?? null;
    probing = target !== null;
  }
  if (target === null) {
    return { action: 'give-up', reason: 'no-available-account', state: next };
  }
  if (probing) next.probe = { key: target, at: now, pending: true };
  recordResume(next, sessionId, now);
  return { action: 'switch', target, reason: probing ? 'stale-limit-probe' : 'limited', state: next };
}

module.exports = {
  SYSTEM_DEFAULT_KEY,
  FALLBACK_LIMIT_MS,
  RESET_MARGIN_MS,
  RESUME_WINDOW_MS,
  PROBE_INTERVAL_MS,
  parseLimitMessage,
  resolveResetTime,
  limitUntil,
  carryOverArgs,
  isPrintMode,
  decide
};
