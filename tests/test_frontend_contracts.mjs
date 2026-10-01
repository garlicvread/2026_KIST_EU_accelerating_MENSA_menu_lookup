import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import test from 'node:test';

const moduleURL = new URL('../dist/frontend/contracts.js', import.meta.url);
const contracts = existsSync(moduleURL) ? await import(moduleURL.href) : null;
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
  assert.ok(contracts, 'Compile the browser contract implementation before validating snapshots');
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

test('menu rejects malformed schema, source and identity boundaries', () => {
  rejected(contracts.validateMenu, menu, [
    ['schema boolean', m => { m.schema_version = true; }],
    ['zero days', m => { m.days = []; }],
    ['invalid timestamp', m => { m.source.fetched_at = 'bad timestamp'; }],
    ['unknown top field', m => { m.debug = true; }],
    ['timezone absent', m => { m.source.fetched_at = '2026-09-28T08:00:00'; }],
    ['timestamp trailing newline', m => { m.source.fetched_at = '2026-09-28T08:00:00Z\n'; }],
    ['impossible timestamp date', m => { m.source.fetched_at = '2026-02-30T08:00:00Z'; }],
    ['wrong source', m => { m.source.url = 'https://example.invalid/'; }],
    ['uppercase hash', m => { m.source.sha256 = 'A'.repeat(64); }],
    ['hash trailing newline', m => { m.source.sha256 = digest + '\n'; }],
    ['id wrong day', m => { m.days[0].meals[0].id = `2026-09-29-${digest}`; }],
    ['unsafe identity', m => { m.days[0].meals[0].id = '../menu'; }],
    ['key shape', m => { m.days[0].meals[0].translation_key = '__proto__'; }],
    ['duplicate id', m => { m.days[0].meals.push(structuredClone(m.days[0].meals[0])); }],
    ['category information', m => { m.days[0].meals[0].category = 'Information'; }],
    ['component shape', m => { m.days[0].meals[0].components[0].extra = 1; }],
    ['notice control', m => { m.days[0].meals[0].notices = ['bad\u0000notice']; }],
  ]);
  const emptyDay = menu(); emptyDay.days[0].meals = [];
  assert.deepEqual(contracts.validateMenu(emptyDay), emptyDay);
  const long = menu(); long.days[0].meals[0].name_de = 'x'.repeat(5000);
  long.days[0].meals[0].price_source.name = long.days[0].meals[0].name_de;
  assert.equal(contracts.validateMenu(long).days[0].meals[0].name_de.length, 5000);
  for (const input of [null, [], 1, 'menu']) assert.throws(() => contracts.validateMenu(input), Error);
});

test('menu enforces ordered coverage and correlated price provenance', () => {
  rejected(contracts.validateMenu, menu, [
    ['bad calendar date', m => { m.days[0].date = '2026-02-30'; }],
    ['coverage end mismatch', m => { m.coverage.end = '2026-09-29'; }],
    ['coverage start mismatch', m => { m.coverage.start = '2026-09-29'; }],
    ['duplicate date', m => { m.days.push(structuredClone(m.days[0])); }],
    ['descending dates', m => { m.days.unshift({ date: '2026-09-29', meals: [] }); m.coverage.end = '2026-09-29'; }],
    ['integer cents', m => { m.days[0].meals[0].prices.student = 100.5; }],
    ['pending with raw', m => { m.days[0].meals[0].prices = null; m.days[0].meals[0].price_status = 'source_pending'; }],
  ]);
  for (const [field, value] of [['date', '2026-09-29'], ['category', 'Menü 2'], ['name', 'Salat']]) {
    const corrupted = menu(); corrupted.days[0].meals[0].price_source[field] = value;
    assert.throws(() => contracts.validateMenu(corrupted), Error, `wrong price source ${field}`);
  }
  for (const group of ['student', 'staff', 'guest']) {
    const corrupted = menu(); corrupted.days[0].meals[0].prices[group] += 100;
    assert.throws(() => contracts.validateMenu(corrupted), Error, `changed ${group}`);
  }
  for (const raw of [null, 'S: 1,00 | M: 2,00', 'S: 1,00 | M: 2,00 | G: 3,00 | S: 1,00', 'S: 1,0 | M: 2,00 | G: 3,00', 'S: 0,00 | M: 2,00 | G: 3,00']) {
    const corrupted = menu(); corrupted.days[0].meals[0].price_source.raw = raw;
    assert.throws(() => contracts.validateMenu(corrupted), Error, `malformed raw ${raw}`);
  }
  const decimalPoint = menu(); decimalPoint.days[0].meals[0].prices = { student: 350, staff: 465, guest: 535 };
  decimalPoint.days[0].meals[0].price_source.raw = 'S: 3.50 | M: 4.65 | G: 5.35';
  assert.deepEqual(contracts.validateMenu(decimalPoint), decimalPoint);
  const withAmount = menu(); withAmount.days[0].meals[0].price_status = 'source_pending'; withAmount.days[0].meals[0].price_source.raw = null;
  assert.throws(() => contracts.validateMenu(withAmount), Error);
  const value = menu(); const meal = value.days[0].meals[0];
  meal.prices = null; meal.price_status = 'source_pending'; meal.price_source.raw = null;
  assert.deepEqual(contracts.validateMenu(value), value);
});

test('translations validate display source, bilingual components and exact notice mappings', () => {
  rejected(contracts.validateTranslations, cache, [
    ['schema boolean', c => { c.schema_version = true; }],
    ['entries array', c => { c.entries = []; }],
    ['unsafe key', c => { c.entries = { '../data': c.entries[digest] }; }],
    ['missing source', c => { delete c.entries[digest].source; }],
    ['source added field', c => { c.entries[digest].source.price = 1; }],
    ['empty source text', c => { c.entries[digest].source.name_de = ''; }],
    ['missing Korean', c => { delete c.entries[digest].ko; }],
    ['added display field', c => { c.entries[digest].en.price = 1; }],
    ['component count', c => { c.entries[digest].ko.components = []; }],
    ['control text', c => { c.entries[digest].en.name = 'bad\u0001'; }],
    ['notice added language', c => { c.notices.Weizen.de = 'Weizen'; }],
    ['notice empty', c => { c.notices.Weizen.ko = ' '; }],
    ['inherited notices', c => { c.notices = Object.create({ Weizen: { en: 'Wheat', ko: '밀' } }); }],
  ]);
  const legacy = cache(); delete legacy.notices;
  assert.deepEqual(contracts.validateTranslations(legacy).notices, {});
  assert.deepEqual(contracts.validateTranslations({ schema_version: 1, entries: {} }), { schema_version: 1, entries: {}, notices: {} });
  const own = cache(); own.notices = JSON.parse('{"__proto__":{"en":"Label","ko":"표시"}}');
  const result = contracts.validateTranslations(own);
  assert.equal(Object.hasOwn(result.notices, '__proto__'), true);
  assert.deepEqual(result.notices.__proto__, { en: 'Label', ko: '표시' });
});

test('release pointers reject arbitrary paths and unknown fields', () => {
  const pointer = { schema_version: 1, release_id: digest };
  assert.deepEqual(contracts.validateReleaseManifest(pointer), pointer);
  for (const value of [null, [], { ...pointer, schema_version: true }, { ...pointer, release_id: '../menu' },
    { ...pointer, release_id: 'A'.repeat(64) }, { ...pointer, release_id: digest + '\n' }, { ...pointer, path: 'menu.json' }]) {
    assert.throws(() => contracts.validateReleaseManifest(value), Error);
  }
});

test('unknown preferences are narrowed only to supported choices', () => {
  for (const value of ['ko', 'en', 'de']) assert.equal(contracts.isLanguage(value), true);
  for (const value of ['student', 'staff', 'guest']) assert.equal(contracts.isPriceGroup(value), true);
  for (const value of ['day', 'week']) assert.equal(contracts.isViewMode(value), true);
  for (const value of [null, undefined, {}, [], 1, true, 'constructor', 'DE', '']) {
    assert.equal(contracts.isLanguage(value), false);
    assert.equal(contracts.isPriceGroup(value), false);
    assert.equal(contracts.isViewMode(value), false);
  }
});
