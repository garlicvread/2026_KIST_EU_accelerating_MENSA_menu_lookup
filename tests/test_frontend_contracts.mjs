import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const moduleURL = new URL('../dist/frontend/contracts.js', import.meta.url);
const contracts = await import(moduleURL.href);
const fixture = name => JSON.parse(readFileSync(new URL(`../site/data/${name}.json`, import.meta.url), 'utf8'));
const digest = 'a'.repeat(64);
function menu() {
  return { schema_version: 1,
    source: { url: 'https://www.stw-saarland.de/gastro/mensa-saarbruecken/', fetched_at: '2026-09-28T08:00:00+02:00', sha256: digest },
    coverage: { start: '2026-09-28', end: '2026-09-28' },
    days: [{ date: '2026-09-28', meals: [{
      id: `2026-09-28-${digest}`, translation_key: digest, category: 'Menü 1', location: 'A', name_de: 'Suppe',
      components: [{ name_de: 'Brot', notices: ['Weizen'] }], notices: [],
      prices: { student: 100, staff: 200, guest: 300 }, price_status: 'verified',
      price_source: { date: '2026-09-28', category: 'Menü 1', name: 'Suppe', raw: 'S: 1,00 | M: 2,00 | G: 3,00' },
    }] }] };
}
function cache() {
  return { schema_version: 1, entries: { [digest]: {
    source: { name_de: 'Suppe', components: ['Brot'] },
    en: { name: 'Soup', components: ['Bread'] }, ko: { name: '수프', components: ['빵'] },
    origin: 'model', model: 'ignored display metadata',
  } }, notices: { Weizen: { en: 'Wheat', ko: '밀' } } };
}
function rejected(validator, factory, mutations) {
  for (const [label, mutate] of mutations) {
    const value = factory(); mutate(value);
    assert.throws(() => validator(value), Error, label);
  }
}

test('valid published snapshots produce isolated display values without changing inputs', () => {
  for (const value of [fixture('menu'), menu()]) {
    const before = structuredClone(value);
    const result = contracts.validateMenu(value);
    assert.deepEqual(result, value);
    assert.deepEqual(value, before);
    result.days[0].meals[0].name_de = 'Changed display copy';
    assert.deepEqual(value, before);
  }
  for (const value of [fixture('translations'), cache()]) {
    const before = structuredClone(value);
    const result = contracts.validateTranslations(value);
    const [key, entry] = Object.entries(value.entries)[0];
    assert.deepEqual(result.entries[key].source, entry.source);
    assert.deepEqual(result.entries[key].en, entry.en);
    assert.deepEqual(result.entries[key].ko, entry.ko);
    assert.deepEqual(result.notices, value.notices ?? {});
    result.entries[key].en.name = 'Changed display copy';
    assert.deepEqual(value, before);
  }
});


test('menu enforces ordered coverage and correlated price provenance', () => {
  rejected(contracts.validateMenu, menu, [
    ['coverage end mismatch', m => { m.coverage.end = '2026-09-29'; }],
    ['integer cents', m => { m.days[0].meals[0].prices.student = 100.5; }],
    ['pending with raw', m => { m.days[0].meals[0].prices = null; m.days[0].meals[0].price_status = 'source_pending'; }],
  ]);
  const corrupted = menu(); corrupted.days[0].meals[0].prices.staff += 100;
  assert.throws(() => contracts.validateMenu(corrupted), Error);
  const value = menu(); const meal = value.days[0].meals[0];
  meal.prices = null; meal.price_status = 'source_pending'; meal.price_source.raw = null;
  assert.deepEqual(contracts.validateMenu(value), value);
});

test('shared counter prices retain the source owner and reject cross-counter associations', () => {
  function sharedMenu() {
    const value = menu();
    const owner = value.days[0].meals[0];
    const dish = structuredClone(owner);
    dish.id = `2026-09-28-${'b'.repeat(64)}`;
    dish.name_de = 'Käsespätzle';
    dish.price_source.scope = 'counter';
    value.days[0].meals.unshift(dish);
    return value;
  }
  const value = sharedMenu();
  assert.deepEqual(contracts.validateMenu(value), value);
  rejected(contracts.validateMenu, sharedMenu, [
    ['other counter', m => { m.days[0].meals[0].location = 'Elsewhere'; }],
    ['changed common price', m => {
      m.days[0].meals[0].prices.student = 400;
      m.days[0].meals[0].price_source.raw = 'S: 4,00 | M: 2,00 | G: 3,00';
    }],
  ]);
});

test('translations validate display source, bilingual components and exact notice mappings', () => {
  rejected(contracts.validateTranslations, cache, [
    ['missing Korean', c => { delete c.entries[digest].ko; }],
    ['component count', c => { c.entries[digest].ko.components = []; }],
    ['notice empty', c => { c.notices.Weizen.ko = ' '; }],
  ]);
  const legacy = cache(); delete legacy.notices;
  assert.deepEqual(contracts.validateTranslations(legacy).notices, {});
  assert.deepEqual(contracts.validateTranslations({ schema_version: 1, entries: {} }), { schema_version: 1, entries: {}, notices: {} });
});
