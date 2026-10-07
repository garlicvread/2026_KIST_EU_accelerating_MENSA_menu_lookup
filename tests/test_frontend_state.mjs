import assert from 'node:assert/strict';
import test from 'node:test';
import { validateMenu } from '../dist/frontend/contracts.js';
const stateURL = new URL('../dist/frontend/state.js', import.meta.url);
const selectorsURL = new URL('../dist/frontend/selectors.js', import.meta.url);
const states = await import(stateURL.href);
const selectors = await import(selectorsURL.href);
const hash = 'a'.repeat(64);
function menu(dates = ['2026-12-31', '2027-01-01', '2027-01-04', '2027-02-01']) {
  return validateMenu({ schema_version: 1, source: { url: 'https://www.stw-saarland.de/gastro/mensa-saarbruecken/', fetched_at: '2026-12-30T12:00:00Z', sha256: hash },
    coverage: { start: dates[0], end: dates.at(-1) }, days: dates.map(date => ({ date, meals: [{ id: `${date}-${hash}`, translation_key: hash,
      category: 'Menü 1', location: 'A', name_de: 'Suppe', components: [], notices: [], prices: null, price_status: 'source_pending',
      price_source: { date, category: 'Menü 1', name: 'Suppe', raw: null } }] })) });
}
const cache = { schema_version: 1, entries: {}, notices: {} };
function frozen(value) { if (value && typeof value === 'object') { Object.values(value).forEach(frozen); Object.freeze(value); } return value; }
function ready(value = menu()) { return states.transition(states.createInitialState(), { type: 'menu-ready', menu: value, today: '2027-01-01' }); }

test('initial state and ready menu choose only a published date', () => {
  const initial = states.createInitialState();
  assert.deepEqual(initial, { language: 'en', group: 'student', view: 'day', menu: { status: 'idle' }, translations: { status: 'idle' }, selectedDate: null, expandedIds: [] });
  const supplied = states.createInitialState({ language: 'ko', group: 'guest' }); assert.equal(supplied.language, 'ko'); assert.equal(supplied.group, 'guest');
  frozen(initial); const value = frozen(menu());
  const selected = states.transition(initial, { type: 'menu-ready', menu: value, today: '2027-01-02' });
  assert.equal(selected.selectedDate, '2027-01-04'); assert.deepEqual(initial.expandedIds, []);
  assert.equal(states.transition(selected, { type: 'today', today: '2028-01-01' }).selectedDate, '2027-02-01');
});

test('preference, cache and view changes retain selection and open details; new menus prune only absent IDs', () => {
  const value = frozen(menu()); const id = value.days[0].meals[0].id;
  let state = states.transition(ready(value), { type: 'details', id, open: true });
  const original = frozen(state);
  for (const event of [{ type: 'language', language: 'ko' }, { type: 'group', group: 'staff' }, { type: 'view', view: 'week' }, { type: 'translations-ready', cache }]) {
    state = states.transition(state, event); assert.equal(state.selectedDate, original.selectedDate); assert.deepEqual(state.expandedIds, [id]);
  }
  assert.equal(states.transition(state, { type: 'details', id, open: true }), state);
  const retained = states.transition(state, { type: 'menu-ready', menu: menu(['2026-12-31', '2027-01-01']), today: '2027-01-01' });
  assert.equal(retained.selectedDate, '2027-01-01'); assert.deepEqual(retained.expandedIds, [id]);
  const removed = states.transition(retained, { type: 'menu-ready', menu: menu(['2027-01-04']), today: '2027-01-02' });
  assert.equal(removed.selectedDate, '2027-01-04'); assert.deepEqual(removed.expandedIds, []);
  assert.deepEqual(original.expandedIds, [id]); assert.equal(original.language, 'en');
  const closed = states.transition(retained, { type: 'details', id, open: false }); assert.deepEqual(closed.expandedIds, []);
});

test('date, available-week and Today navigation handles year/month bounds without mutating inputs', () => {
  const value = frozen(menu()); let state = ready(value);
  state = states.transition(state, { type: 'view', view: 'week' });
  assert.equal(states.transition(state, { type: 'week', offset: -1 }), state);
  state = states.transition(state, { type: 'week', offset: 1 }); assert.equal(state.selectedDate, '2027-01-04'); assert.equal(state.view, 'week');
  state = states.transition(state, { type: 'week', offset: 1 }); assert.equal(state.selectedDate, '2027-02-01');
  assert.equal(states.transition(state, { type: 'week', offset: 1 }), state);
  state = states.transition(state, { type: 'date', date: '2026-12-31' }); assert.equal(state.view, 'day');
  assert.equal(states.transition(state, { type: 'date', date: '2026-12-30' }), state);
  state = states.transition(state, { type: 'view', view: 'week' });
  state = states.transition(state, { type: 'today', today: '2027-01-02' }); assert.equal(state.selectedDate, '2027-01-04'); assert.equal(state.view, 'day');
  assert.deepEqual(value.days.map(d => d.date), ['2026-12-31', '2027-01-01', '2027-01-04', '2027-02-01']);
});

test('loading/failure preserves retry context but blocks navigation and unknown detail IDs', () => {
  const value = menu(); let state = ready(value);
  const id = value.days[0].meals[0].id; state = states.transition(state, { type: 'details', id, open: true });
  assert.equal(states.transition(state, { type: 'details', id: 'unknown', open: true }), state);
  const loading = states.transition(state, { type: 'load-start' }); assert.equal(loading.menu.status, 'loading'); assert.equal(loading.translations.status, 'loading');
  assert.equal(loading.selectedDate, state.selectedDate); assert.deepEqual(loading.expandedIds, [id]);
  assert.equal(states.transition(loading, { type: 'load-start' }), loading);
  const failed = states.transition(loading, { type: 'menu-failed', error: 'offline' });
  for (const event of [{ type: 'date', date: '2026-12-31' }, { type: 'week', offset: 1 }, { type: 'today', today: '2027-01-04' }, { type: 'details', id, open: false }]) assert.equal(states.transition(failed, event), failed);
  assert.deepEqual(failed.expandedIds, [id]);
  const retried = states.transition(failed, { type: 'menu-ready', menu: value, today: '2027-01-04' }); assert.equal(retried.selectedDate, state.selectedDate);
  assert.deepEqual(retried.expandedIds, [id]);
});

test('selectors use explicit Berlin date, UTC Monday grouping and strict stale threshold', () => {
  for (const [utc, date] of [['2026-09-20T22:30:00Z','2026-09-21'],['2026-12-20T23:30:00Z','2026-12-21']]) assert.equal(selectors.berlinToday(new Date(utc)), date);
  const defaults = [{ date: '2026-09-21' }, { date: '2026-09-25' }, { date: '2026-09-28' }];
  for (const [today, expected] of [['2026-09-21','2026-09-21'],['2026-09-26','2026-09-28'],['2026-10-01','2026-09-28']]) assert.equal(selectors.chooseDefaultDate(defaults, today), expected);
  assert.equal(selectors.chooseDefaultDate([], '2027-01-01'), null);
  const value = menu(); const shuffled = frozen([...value.days].reverse());
  const weeks = selectors.groupWeeks(shuffled); assert.deepEqual(weeks.map(w => w.key), ['2026-12-28', '2027-01-04', '2027-02-01']);
  assert.deepEqual(weeks.flatMap(w => w.days.map(day => day.date)), value.days.map(day => day.date));
  assert.equal(selectors.selectedWeek(value, '2027-01-01').key, '2026-12-28'); assert.equal(selectors.selectedWeek(value, null), null);
  const options = { selectedDate: null, view: 'day', today: '2027-01-01', now: new Date('2027-01-01T12:00:00Z') };
  assert.deepEqual(selectors.menuFreshness(value, options), { expired: false, past: false, stale: false });
  assert.equal(selectors.menuFreshness(value, { ...options, today: '2027-02-02' }).expired, true);
  assert.equal(selectors.menuFreshness(value, { ...options, selectedDate: '2026-12-31' }).past, true);
  assert.equal(selectors.menuFreshness(value, { ...options, selectedDate: '2026-12-31', view: 'week' }).past, false);
  assert.equal(selectors.menuFreshness(value, { ...options, now: new Date(options.now.getTime() + 1) }).stale, true);
  assert.deepEqual(shuffled.map(d => d.date), [...value.days].reverse().map(d => d.date));
});
