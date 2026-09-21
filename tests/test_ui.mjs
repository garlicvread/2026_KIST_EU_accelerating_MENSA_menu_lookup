import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../site/app.js', import.meta.url), 'utf8');
const ui = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('Berlin date crosses midnight independently of the visitor timezone', () => {
  assert.equal(ui.berlinToday(new Date('2026-09-20T22:30:00Z')), '2026-09-21');
  assert.equal(ui.berlinToday(new Date('2026-12-20T23:30:00Z')), '2026-12-21');
});

test('default date chooses today, then next available, then newest past menu', () => {
  const days = [{ date: '2026-09-21' }, { date: '2026-09-25' }, { date: '2026-09-28' }];
  assert.equal(ui.chooseDefaultDate(days, '2026-09-21'), '2026-09-21');
  assert.equal(ui.chooseDefaultDate(days, '2026-09-26'), '2026-09-28');
  assert.equal(ui.chooseDefaultDate(days, '2026-10-01'), '2026-09-28');
  assert.equal(ui.chooseDefaultDate([], '2026-09-21'), null);
});

test('week navigation retains every supplied date across month and year boundaries', () => {
  const days = ['2026-12-28', '2027-01-01', '2027-01-04'].map(date => ({ date }));
  const weeks = ui.groupWeeks(days);
  assert.deepEqual(weeks.map(week => week.key), ['2026-12-28', '2027-01-04']);
  assert.deepEqual(weeks.flatMap(week => week.days.map(day => day.date)), days.map(day => day.date));
});

test('freshness distinguishes past selections, wholly expired coverage, and old snapshots', () => {
  const menu = { source: { fetched_at: '2026-09-21T10:00:00Z' }, days: [{ date: '2026-09-21' }, { date: '2026-10-02' }] };
  const options = { selectedDate: '2026-09-21', view: 'day', today: '2026-09-22', now: new Date('2026-09-22T10:00:00Z') };
  assert.deepEqual(ui.menuFreshness(menu, options), { expired: false, past: true, stale: false });
  assert.equal(ui.menuFreshness(menu, { ...options, view: 'week' }).past, false);
  assert.equal(ui.menuFreshness(menu, { ...options, today: '2026-10-03' }).expired, true);
  assert.equal(ui.menuFreshness(menu, { ...options, now: new Date('2026-09-28T10:00:01Z') }).stale, true);
  assert.equal(ui.menuFreshness(menu, { ...options, now: new Date('2026-09-28T10:00:00Z') }).stale, false);
});

test('prices require verified integer cents and never substitute another price group', () => {
  const meal = { prices: { student: 350, staff: 465, guest: 535 }, price_status: 'verified' };
  assert.match(ui.priceText(meal, 'student', 'en'), /3\.50/);
  assert.match(ui.priceText(meal, 'staff', 'en'), /4\.65/);
  assert.equal(ui.priceText({ ...meal, price_status: 'source_pending' }, 'student', 'en'), null);
  assert.equal(ui.priceText({ ...meal, prices: { staff: 465 } }, 'student', 'en'), null);
  assert.equal(ui.priceText({ ...meal, prices: { student: 3.5 } }, 'student', 'en'), null);
});

test('translations match the dish and ordered components; mismatch safely retains German', () => {
  const meal = { translation_key: 'key', name_de: 'Suppe', components: [{ name_de: 'Brot' }] };
  const cache = { entries: { key: { source: { name_de: 'Suppe', components: ['Brot'] }, en: { name: 'Soup', components: ['Bread'] } } } };
  assert.deepEqual(ui.translatedMeal(meal, cache, 'en'), { name: 'Soup', components: ['Bread'], translated: true });
  assert.deepEqual(ui.translatedMeal(meal, cache, 'ko'), { name: 'Suppe', components: ['Brot'], translated: false });
  assert.equal(ui.translatedMeal({ ...meal, name_de: 'Salat' }, cache, 'en').name, 'Salat');
  assert.equal(ui.translatedMeal(meal, { entries: { key: { ...cache.entries.key, en: { name: 'Soup', components: [] } } } }, 'en').translated, false);
  assert.equal(ui.translatedMeal(meal, null, 'en').name, 'Suppe');
});

test('bad or empty menu snapshots fail visibly instead of rendering an empty success', () => {
  const snapshot = { schema_version: 1, source: { fetched_at: '2026-09-21T10:00:00Z' }, coverage: { start: '2026-09-21', end: '2026-09-21' }, days: [{ date: '2026-09-21', meals: [] }] };
  assert.equal(ui.validateMenu(snapshot), snapshot);
  assert.throws(() => ui.validateMenu({ ...snapshot, days: [] }));
  assert.throws(() => ui.validateMenu({ ...snapshot, days: [{ date: '2026-02-30', meals: [] }] }));
  assert.throws(() => ui.validateMenu({ ...snapshot, source: { fetched_at: 'bad timestamp' } }));
  assert.throws(() => ui.validateMenu({ ...snapshot, days: [...snapshot.days, ...snapshot.days] }));
  assert.throws(() => ui.validateMenu({ ...snapshot, coverage: { start: '2026-09-22', end: '2026-09-30' } }));
});

function pricedSnapshot() {
  return {
    schema_version: 1,
    source: { fetched_at: '2026-09-21T10:00:00Z' },
    coverage: { start: '2026-09-21', end: '2026-09-21' },
    days: [{ date: '2026-09-21', meals: [{
      name_de: 'Suppe', category: 'Menü 1', components: [], notices: [],
      prices: { student: 350, staff: 465, guest: 535 }, price_status: 'verified',
      price_source: { date: '2026-09-21', category: 'Menü 1', name: 'Suppe', raw: 'S: 3,50 | M: 4,65 | G: 5,35' }
    }] }]
  };
}

test('price provenance must identify the exact date, category, and dish', () => {
  const valid = pricedSnapshot();
  assert.equal(ui.validateMenu(valid), valid);
  for (const [field, value] of [['date', '2026-09-22'], ['category', 'Menü 2'], ['name', 'Salat']]) {
    const corrupted = pricedSnapshot();
    corrupted.days[0].meals[0].price_source[field] = value;
    assert.throws(() => ui.validateMenu(corrupted), `must reject wrong source ${field}`);
  }
});

test('verified integer cents must match every amount in the complete raw S/M/G block', () => {
  for (const group of ['student', 'staff', 'guest']) {
    const corrupted = pricedSnapshot();
    corrupted.days[0].meals[0].prices[group] += 100;
    assert.throws(() => ui.validateMenu(corrupted), `must reject changed ${group} price`);
  }
  for (const raw of [null, 'S: 3,50 | M: 4,65', 'S: 3,50 | M: 4,65 | G: 5,35 | S: 3,50', 'S: 3,5 | M: 4,65 | G: 5,35', 'S: 0,00 | M: 4,65 | G: 5,35']) {
    const corrupted = pricedSnapshot();
    corrupted.days[0].meals[0].price_source.raw = raw;
    assert.throws(() => ui.validateMenu(corrupted), `must reject malformed raw block ${raw}`);
  }
  const decimalPoint = pricedSnapshot();
  decimalPoint.days[0].meals[0].price_source.raw = 'S: 3.50 | M: 4.65 | G: 5.35';
  assert.equal(ui.validateMenu(decimalPoint), decimalPoint);
});

test('pending source prices must contain neither displayed prices nor a raw price block', () => {
  const pending = pricedSnapshot();
  const meal = pending.days[0].meals[0];
  meal.price_status = 'source_pending';
  meal.prices = null;
  meal.price_source.raw = null;
  assert.equal(ui.validateMenu(pending), pending);
  meal.prices = { student: 350, staff: 465, guest: 535 };
  assert.throws(() => ui.validateMenu(pending));
  meal.prices = null;
  meal.price_source.raw = 'S: 3,50 | M: 4,65 | G: 5,35';
  assert.throws(() => ui.validateMenu(pending));
});
