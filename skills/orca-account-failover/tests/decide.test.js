'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  SYSTEM_DEFAULT_KEY,
  FALLBACK_LIMIT_MS,
  RESET_MARGIN_MS,
  RESUME_WINDOW_MS,
  parseLimitMessage,
  resolveResetTime,
  limitUntil,
  carryOverArgs,
  isPrintMode,
  decide
} = require('../lib/decide.js');

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

// 日本時間（UTC+9、夏時間なし）の壁時計を epoch ms にする
const jst = (year, month, day, hour, minute = 0) => Date.UTC(year, month - 1, day, hour - 9, minute);

test('parseLimitMessage: 時刻だけの解除時刻を読む', () => {
  assert.deepEqual(parseLimitMessage("You've hit your session limit · resets 6:50pm (Asia/Tokyo)"), {
    kind: 'session',
    reset: { month: null, day: null, hour: 18, minute: 50, timeZone: 'Asia/Tokyo' }
  });
});

test('parseLimitMessage: 日付つきの解除時刻を読む', () => {
  assert.deepEqual(parseLimitMessage("You've hit your weekly limit · resets Oct 6, 12am (Asia/Tokyo)"), {
    kind: 'weekly',
    reset: { month: 9, day: 6, hour: 0, minute: 0, timeZone: 'Asia/Tokyo' }
  });
});

test('parseLimitMessage: 12am は 0 時、12pm は 12 時', () => {
  assert.equal(parseLimitMessage("You've hit your weekly limit · resets 12am (Asia/Tokyo)").reset.hour, 0);
  assert.equal(parseLimitMessage("You've hit your weekly limit · resets 12pm (Asia/Tokyo)").reset.hour, 12);
  assert.equal(parseLimitMessage("You've hit your session limit · resets 6am (Asia/Tokyo)").reset.hour, 6);
});

test('parseLimitMessage: 解除時刻が読めなくても上限到達とは判定する', () => {
  assert.deepEqual(parseLimitMessage("You've hit your session limit"), { kind: 'session', reset: null });
  assert.deepEqual(parseLimitMessage("You've hit your weekly limit · resets Foo 6, 12am (Asia/Tokyo)"), {
    kind: 'weekly',
    reset: null
  });
});

test('parseLimitMessage: 曲がったアポストロフィでも読む', () => {
  assert.equal(parseLimitMessage('You’ve hit your session limit · resets 6am (Asia/Tokyo)').kind, 'session');
});

test('parseLimitMessage: 利用上限でないエラーは null', () => {
  assert.equal(parseLimitMessage('API Error: Rate limit reached'), null);
  assert.equal(parseLimitMessage(''), null);
  assert.equal(parseLimitMessage(undefined), null);
});

test('resolveResetTime: 今日まだ来ていない時刻は今日', () => {
  const reset = { month: null, day: null, hour: 6, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(reset, jst(2026, 10, 8, 1)), jst(2026, 10, 8, 6));
});

test('resolveResetTime: 今日すでに過ぎた時刻は翌日', () => {
  const reset = { month: null, day: null, hour: 6, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(reset, jst(2026, 10, 8, 7)), jst(2026, 10, 9, 6));
});

test('resolveResetTime: ちょうど同時刻なら翌日', () => {
  const reset = { month: null, day: null, hour: 6, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(reset, jst(2026, 10, 8, 6)), jst(2026, 10, 9, 6));
});

test('resolveResetTime: 12am は次の 0 時、分も反映する', () => {
  const midnight = { month: null, day: null, hour: 0, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(midnight, jst(2026, 10, 8, 18)), jst(2026, 10, 9, 0));
  const withMinute = { month: null, day: null, hour: 18, minute: 50, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(withMinute, jst(2026, 10, 8, 18)), jst(2026, 10, 8, 18, 50));
});

test('resolveResetTime: 月末をまたぐ', () => {
  const reset = { month: null, day: null, hour: 0, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(reset, jst(2026, 10, 31, 23)), jst(2026, 11, 1, 0));
});

test('resolveResetTime: 日付つきはその日付', () => {
  const reset = { month: 9, day: 6, hour: 0, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(reset, jst(2026, 10, 5, 18)), jst(2026, 10, 6, 0));
});

test('resolveResetTime: 日付つきで年をまたぐ', () => {
  const reset = { month: 0, day: 1, hour: 0, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(reset, jst(2026, 12, 31, 23)), jst(2027, 1, 1, 0));
});

test('resolveResetTime: 日付がすでに過去なら null（来年の同日と読まない）', () => {
  const reset = { month: 9, day: 6, hour: 0, minute: 0, timeZone: 'Asia/Tokyo' };
  assert.equal(resolveResetTime(reset, jst(2026, 10, 8, 1)), null);
  assert.equal(resolveResetTime(reset, jst(2026, 10, 6, 0)), null);
});

test('resolveResetTime: 夏時間の切替日でも壁時計どおり', () => {
  // 2026-03-08 02:00 に EST(-5) → EDT(-4)。3/7 20:00 EST から見た「6am」は 3/8 06:00 EDT = 10:00 UTC
  const reset = { month: null, day: null, hour: 6, minute: 0, timeZone: 'America/New_York' };
  assert.equal(resolveResetTime(reset, Date.UTC(2026, 2, 8, 1, 0)), Date.UTC(2026, 2, 8, 10, 0));
});

test('resolveResetTime: 不明なタイムゾーンと reset なしは null', () => {
  assert.equal(resolveResetTime({ month: null, day: null, hour: 6, minute: 0, timeZone: 'Not/AZone' }, jst(2026, 10, 8, 1)), null);
  assert.equal(resolveResetTime(null, jst(2026, 10, 8, 1)), null);
});

test('limitUntil: 解除時刻が読めれば余裕を足して返す', () => {
  const limit = parseLimitMessage("You've hit your session limit · resets 6am (Asia/Tokyo)");
  assert.deepEqual(limitUntil(limit, jst(2026, 10, 8, 1)), { at: jst(2026, 10, 8, 6) + RESET_MARGIN_MS, parsed: true });
});

test('limitUntil: 読めなければ既定の時間だけ上限扱いにする', () => {
  const now = jst(2026, 10, 8, 1);
  assert.deepEqual(limitUntil({ kind: 'session', reset: null }, now), { at: now + FALLBACK_LIMIT_MS, parsed: false });
});

test('carryOverArgs: 起動時のプロンプトを落としてフラグだけ残す', () => {
  assert.deepEqual(carryOverArgs(['--dangerously-skip-permissions', '/develop DEV-10061']), ['--dangerously-skip-permissions']);
  assert.deepEqual(carryOverArgs(['/publish-daily']), []);
  assert.deepEqual(carryOverArgs([]), []);
});

test('carryOverArgs: 値つきオプションは値ごと残す', () => {
  assert.deepEqual(carryOverArgs(['--model', 'opus', 'hello']), ['--model', 'opus']);
  assert.deepEqual(carryOverArgs(['--model=opus', '--verbose']), ['--model=opus', '--verbose']);
  assert.deepEqual(carryOverArgs(['--settings', '{"a":1}', '--permission-mode', 'plan']), [
    '--settings',
    '{"a":1}',
    '--permission-mode',
    'plan'
  ]);
});

test('carryOverArgs: 再開先を決めるオプションと未知のオプションは引き継がない', () => {
  assert.deepEqual(carryOverArgs(['--resume', 'abc', '--model', 'opus']), ['--model', 'opus']);
  assert.deepEqual(carryOverArgs(['-c', '--session-id', 'abc', '--fork-session']), []);
  assert.deepEqual(carryOverArgs(['--unknown=1', '--name', 'x']), []);
});

test('carryOverArgs: 値が欠けた値つきオプションは捨てる', () => {
  assert.deepEqual(carryOverArgs(['--verbose', '--model']), ['--verbose']);
});

test('carryOverArgs: 値つきオプションの次は、フラグに見えても値として 1 回だけ使う', () => {
  assert.deepEqual(carryOverArgs(['--settings', '--verbose']), ['--settings', '--verbose']);
});

test('carryOverArgs: 名前が前方一致するだけのオプションは引き継がない', () => {
  assert.deepEqual(carryOverArgs(['--models', '--effortx']), []);
});

test('isPrintMode: -p / --print を検出する', () => {
  assert.equal(isPrintMode(['-p', 'hi']), true);
  assert.equal(isPrintMode(['--model', 'opus', '--print']), true);
  assert.equal(isPrintMode(['--dangerously-skip-permissions', '/develop X']), false);
});


// --- decide ---

const NOW = jst(2026, 10, 8, 12);
const UNTIL = NOW + 5 * HOUR;

function input(overrides = {}) {
  return {
    now: NOW,
    accountIds: ['a', 'b'],
    failedKey: 'a',
    state: {},
    sessionId: 's1',
    limitUntil: UNTIL,
    ...overrides
  };
}

test('decide: 止まったプロセスが使っていたアカウントを上限として記録し、次のアカウントへ切り替える', () => {
  const result = decide(input());
  assert.equal(result.action, 'switch');
  assert.equal(result.reason, 'limited');
  assert.equal(result.target, 'b');
  assert.deepEqual(result.state.limited.a, { at: NOW, until: UNTIL });
  assert.equal(result.state.sessions.s1.count, 1);
});

test('decide: 初回でも、止まったプロセスのアカウントで判断して切り替える', () => {
  // 以前は「Orca の選択を把握する前のプロセス」として再開だけしていた。止まったアカウントは確定しているので切り替える
  const result = decide(input({ state: {} }));
  assert.equal(result.target, 'b');
});

test('decide: 以前の版の state にある Orca の選択の記録は使わずに捨てる', () => {
  const state = { active: { key: 'b', since: NOW - HOUR }, limited: {}, sessions: {} };
  const result = decide(input({ state }));
  assert.equal(result.target, 'b');
  assert.equal(result.state.active, undefined);
});

test('decide: 制限の種類があれば記録に残す', () => {
  assert.deepEqual(decide(input({ limitKind: 'weekly' })).state.limited.a, { at: NOW, until: UNTIL, kind: 'weekly' });
});

test('decide: 切替先がすべて最近確認した上限なら何もしない（止まったアカウントの上限は記録する）', () => {
  const state = { limited: { b: { at: NOW - MINUTE, until: NOW + HOUR } } };
  const result = decide(input({ state }));
  assert.equal(result.action, 'give-up');
  assert.equal(result.reason, 'no-available-account');
  assert.deepEqual(result.state.limited.a, { at: NOW, until: UNTIL });
  assert.equal(result.state.sessions.s1, undefined);
});

test('decide: 切替先の上限が解けていれば切り替える（解除時刻ちょうどは解けた扱い）', () => {
  const state = { limited: { b: { at: NOW - 6 * HOUR, until: NOW } } };
  assert.equal(decide(input({ state })).target, 'b');
});

test('decide: System Default の上限は専用のキーで記録し、管理アカウントへ切り替える', () => {
  const result = decide(input({ accountIds: ['a', SYSTEM_DEFAULT_KEY], failedKey: SYSTEM_DEFAULT_KEY }));
  assert.equal(result.target, 'a');
  assert.deepEqual(result.state.limited[SYSTEM_DEFAULT_KEY], { at: NOW, until: UNTIL });
});

test('decide: 切替先は一覧で止まったアカウントの次。末尾なら先頭へ戻り、上限中は飛ばす', () => {
  const three = { accountIds: ['a', 'b', 'c'] };
  assert.equal(decide(input({ ...three, failedKey: 'b' })).target, 'c');
  assert.equal(decide(input({ ...three, failedKey: 'c' })).target, 'a');
  assert.equal(decide(input({ ...three, state: { limited: { b: { at: NOW, until: NOW + HOUR } } } })).target, 'c');
});

test('decide: 候補に無いアカウント（再ログインが要るなど）で止まったら、一覧の先頭から選ぶ', () => {
  assert.equal(decide(input({ accountIds: ['b', 'c'], failedKey: 'a' })).target, 'b');
});

test('decide: アカウントが 1 つだけなら切替先が無い', () => {
  const result = decide(input({ accountIds: ['a'] }));
  assert.equal(result.action, 'give-up');
  assert.equal(result.reason, 'no-available-account');
});

test('decide: 同じセッションの再開が短時間に続いたら打ち切る', () => {
  // アカウント 2 つなら 4 回まで
  const looping = (count, firstAt) => ({ sessions: { s1: { firstAt, lastAt: NOW - MINUTE, count } } });
  const stopped = decide(input({ state: looping(4, NOW - 10 * MINUTE) }));
  assert.equal(stopped.action, 'give-up');
  assert.equal(stopped.reason, 'resume-loop');
  assert.deepEqual(stopped.state.limited, {});

  const allowed = decide(input({ state: looping(3, NOW - 10 * MINUTE) }));
  assert.equal(allowed.action, 'switch');
  assert.equal(allowed.state.sessions.s1.count, 4);
  assert.equal(allowed.state.sessions.s1.firstAt, NOW - 10 * MINUTE);
});

test('decide: 再開の回数は時間が空けば数え直す', () => {
  const state = { sessions: { s1: { firstAt: NOW - RESUME_WINDOW_MS, lastAt: NOW - RESUME_WINDOW_MS, count: 4 } } };
  const result = decide(input({ state }));
  assert.equal(result.action, 'switch');
  assert.deepEqual(result.state.sessions.s1, { firstAt: NOW, lastAt: NOW, count: 1 });
});

test('decide: 渡された state を書き換えない', () => {
  const state = { limited: { b: { at: NOW - 6 * HOUR, until: NOW - HOUR } }, sessions: {} };
  const snapshot = JSON.parse(JSON.stringify(state));
  decide(input({ state }));
  assert.deepEqual(state, snapshot);
});

test('decide: 古い記録は捨てる', () => {
  const state = {
    limited: {
      old: { at: NOW - 3 * DAY, until: NOW - DAY },
      recent: { at: NOW - 2 * HOUR, until: NOW - HOUR }
    },
    sessions: {
      old: { firstAt: NOW - 8 * DAY, lastAt: NOW - 7 * DAY, count: 1 },
      recent: { firstAt: NOW - DAY, lastAt: NOW - DAY, count: 1 }
    }
  };
  const result = decide(input({ state }));
  assert.deepEqual(Object.keys(result.state.limited).sort(), ['a', 'recent']);
  assert.deepEqual(Object.keys(result.state.sessions).sort(), ['recent', 's1']);
});

test('上限が解けた後: 自動継続の再試行が別アカウントの上限に当たったら、解けたアカウントへ切り替える', () => {
  // a は 6:00 に解ける。b は 9:00 まで上限中。b で動いていたセッションが待った後に再試行して、b の上限に当たる
  const hitAt = jst(2026, 10, 8, 1);
  const aUntil = limitUntil(parseLimitMessage("You've hit your session limit · resets 6am (Asia/Tokyo)"), hitAt).at;
  const state = { limited: { a: { at: hitAt, until: aUntil }, b: { at: hitAt, until: jst(2026, 10, 8, 9) } } };
  // Claude Code の自動継続は、最短で表示時刻の 44 秒後に再試行する
  const retryAt = jst(2026, 10, 8, 6) + 44 * 1000;
  const result = decide(input({ now: retryAt, failedKey: 'b', state, limitUntil: jst(2026, 10, 8, 9) }));
  assert.equal(result.action, 'switch');
  assert.equal(result.target, 'a');
});

// --- 全アカウントが上限中のまま失敗し続ける場合 ---

// 再開のたびに同じセッションがすぐ失敗する状況を、何もしなくなるまで回す
function runUntilGiveUp({ accountIds, key, state, startAt = NOW, limitFor = 5 * HOUR }) {
  let now = startAt;
  const actions = [];
  for (let i = 0; i < 50; i++) {
    const result = decide({ now, accountIds, failedKey: key, state, sessionId: 's1', limitUntil: now + limitFor });
    actions.push(result.action);
    state = result.state;
    if (result.action === 'give-up') {
      break;
    }
    key = result.target;
    now += 30 * 1000;
  }
  return { actions, state, key, now };
}

test('全アカウント上限: 各アカウントを 1 回ずつ試したら止まる', () => {
  assert.deepEqual(runUntilGiveUp({ accountIds: ['a', 'b'], key: 'a', state: {} }).actions, ['switch', 'give-up']);
  assert.deepEqual(runUntilGiveUp({ accountIds: ['a', 'b', 'c'], key: 'a', state: {} }).actions, ['switch', 'switch', 'give-up']);
});

test('全アカウント上限: 止まった後は、再試行間隔内は何度呼ばれても再開しない', () => {
  const stopped = runUntilGiveUp({ accountIds: ['a', 'b'], key: 'a', state: {} });
  let { state } = stopped;
  // 止まったセッションと、別のアカウントで動いていた別のセッションが、1 分おきに 29 分間失敗し続ける
  for (let minute = 1; minute <= 29; minute++) {
    const now = stopped.now + minute * MINUTE;
    for (const [sessionId, failedKey] of [['s1', stopped.key], ['s2', 'a']]) {
      const result = decide({ now, accountIds: ['a', 'b'], failedKey, state, sessionId, limitUntil: NOW + 5 * HOUR });
      assert.equal(result.action, 'give-up', `${sessionId} at +${minute}m`);
      state = result.state;
    }
  }
});

test('全アカウント上限: 解除時刻を読めず 1 時間ごとに試し直す場合でも、再開は 30 分に 1 回まで', () => {
  // 5 分おきに 12 時間、同じセッションが失敗し続ける（実際にはアカウントは一度も回復しない）
  let run = runUntilGiveUp({ accountIds: ['a', 'b'], key: 'a', state: {}, limitFor: FALLBACK_LIMIT_MS });
  let restarts = run.actions.filter((action) => action !== 'give-up').length;
  const end = NOW + 12 * HOUR;
  while (run.now + 5 * MINUTE <= end) {
    run = runUntilGiveUp({ accountIds: ['a', 'b'], key: run.key, state: run.state, startAt: run.now + 5 * MINUTE, limitFor: FALLBACK_LIMIT_MS });
    restarts += run.actions.filter((action) => action !== 'give-up').length;
  }
  assert.ok(restarts <= 25, `restarts=${restarts}`);
});

test('古い週間制限: System Default を試すが成功と決めつけず記録を残す', () => {
  const accountIds = ['a', SYSTEM_DEFAULT_KEY];
  const state = { limited: { [SYSTEM_DEFAULT_KEY]: { at: NOW - DAY, until: NOW + DAY } } };
  const result = decide(input({ accountIds, state }));
  assert.equal(result.reason, 'stale-limit-probe');
  assert.equal(result.target, SYSTEM_DEFAULT_KEY);
  assert.deepEqual(result.state.probe, { key: SYSTEM_DEFAULT_KEY, at: NOW, pending: true });
  assert.deepEqual(result.state.limited[SYSTEM_DEFAULT_KEY], state.limited[SYSTEM_DEFAULT_KEY]);
  // 試している間に a で動いていた別のセッションが止まったら、試している System Default へ移す
  const other = decide(input({ accountIds, state: result.state, sessionId: 'other', now: NOW + MINUTE }));
  assert.equal(other.action, 'switch');
  assert.equal(other.target, SYSTEM_DEFAULT_KEY);
  // System Default で再開したセッションも上限なら、記録を更新して止まる
  const failed = decide(input({ accountIds, failedKey: SYSTEM_DEFAULT_KEY, state: other.state,
    now: NOW + 2 * MINUTE, limitKind: 'weekly' }));
  assert.equal(failed.action, 'give-up');
  assert.equal(failed.state.probe.pending, false);
  assert.equal(failed.state.limited[SYSTEM_DEFAULT_KEY].at, NOW + 2 * MINUTE);
  assert.equal(failed.state.limited[SYSTEM_DEFAULT_KEY].kind, 'weekly');
});

test('古い制限: ホスト全体で別セッションも30分待ち、最も古い候補を一つ試す', () => {
  const state = { limited: { b: { at: NOW - HOUR, until: UNTIL }, c: { at: NOW - DAY, until: UNTIL } } };
  const first = decide(input({ accountIds: ['a', 'b', 'c'], state }));
  assert.equal(first.target, 'c');
  // 試した c で再開したセッションがまた止まる
  const retry = (minutes) => decide(input({ accountIds: ['a', 'b', 'c'], failedKey: 'c',
    state: first.state, sessionId: 'another', now: NOW + minutes * MINUTE }));
  assert.equal(retry(29).action, 'give-up');
  assert.equal(retry(30).target, 'b');
});

test('認証先の変更: 同じキーでも組織が変われば以前の制限を適用しない', () => {
  const state = { identities: { b: 'old-org' }, limited: { b: { at: NOW, until: UNTIL } } };
  const result = decide(input({ state, identities: { b: 'new-org' } }));
  assert.equal(result.target, 'b');
  assert.equal(result.state.limited.b, undefined);
  assert.equal(state.identities.b, 'old-org');
  assert.equal(decide(input({ state, identities: { b: 'old-org' } })).action, 'give-up');
});

test('通常の候補があれば古い制限の再試行より優先する', () => {
  const state = { limited: { b: { at: NOW - DAY, until: UNTIL } } };
  const result = decide(input({ accountIds: ['a', 'b', 'c'], state }));
  assert.equal(result.target, 'c');
  assert.equal(result.state.probe, null);
});
