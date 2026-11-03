import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createDataClient } from '../dist/frontend/data_client.js';
import { translatedMeal, translatedNotices, priceText } from '../dist/frontend/display.js';
import * as contracts from '../dist/frontend/contracts.js';

const releaseId = 'a'.repeat(64);
const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/menu-data/${name}.json`, import.meta.url), 'utf8'));
const response = value => ({ ok: true, json: async () => value });

for (const [label, damage] of [
  ['missing language', (cache, key) => { delete cache.entries[key].ko; }],
  ['byte tokens in a translated name', (cache, key) => { cache.entries[key].ko.name = '고<0xEB><0xA0><0x9D>'; }],
  ['byte tokens in a translated component', (cache, key) => { cache.entries[key].en.components[0] = '<0xEB>'; }],
  ['component count mismatch', (cache, key) => { cache.entries[key].ko.components.pop(); }],
]) {
  test(`data client excludes an entry with ${label} while keeping healthy siblings`, async () => {
    const menu = fixture('menu'), cache = fixture('translations');
    const meals = menu.days.flatMap(day => day.meals);
    const bad = meals[0], healthy = meals.find(meal => meal.translation_key !== bad.translation_key);
    assert.ok(healthy);
    damage(cache, bad.translation_key);
    const before = structuredClone({ menu, cache });
    const client = createDataClient(async path => response(path.endsWith('/menu.json') ? menu : cache));
    const loadedMenu = await client.loadMenu(releaseId);
    const recovered = await client.loadTranslations(releaseId);
    assert.equal(Object.hasOwn(recovered.entries, bad.translation_key), false);
    assert.deepEqual(recovered.entries[healthy.translation_key].en, cache.entries[healthy.translation_key].en);
    for (const language of ['en', 'ko']) {
      assert.deepEqual(translatedMeal(bad, recovered, language), {
        name: bad.name_de, components: bad.components.map(component => component.name_de), translated: false,
      });
      assert.equal(translatedMeal(healthy, recovered, language).translated, true);
    }
    assert.deepEqual(loadedMenu, menu);
    assert.ok(priceText(loadedMenu.days[0].meals[0], 'student', 'en')?.includes('€'));
    assert.deepEqual({ menu, cache }, before);
    assert.throws(() => contracts.validateTranslations(cache), Error, 'Strict validation still rejects the damaged entry');
  });
}

for (const [label, value] of [
  ['missing language', { en: 'Incomplete notice' }],
  ['byte tokens', { en: 'Notice', ko: '<0xEB><0xA0><0x9D>' }],
]) {
  test(`data client excludes a notice with ${label} while keeping healthy translations`, async () => {
    const cache = fixture('translations');
    const healthyNotice = Object.keys(cache.notices)[0];
    cache.notices['Neue Quellenwarnung'] = value;
    const before = structuredClone(cache);
    const recovered = await createDataClient(async () => response(cache)).loadTranslations(releaseId);
    assert.deepEqual(Object.keys(recovered.entries), Object.keys(cache.entries));
    assert.deepEqual(recovered.notices[healthyNotice], cache.notices[healthyNotice]);
    assert.deepEqual(translatedNotices(['Neue Quellenwarnung'], recovered, 'ko'),
      [{ original: 'Neue Quellenwarnung', text: 'Neue Quellenwarnung', translated: false }]);
    assert.deepEqual(cache, before);
  });
}

test('data client retains strict translation container validation', async () => {
  for (const invalid of [null, [], {}, { schema_version: 2, entries: {} },
    { schema_version: 1, entries: [] }, { schema_version: 1, entries: {}, notices: [] },
    { schema_version: 1, entries: {}, unexpected: true }]) {
    await assert.rejects(() => createDataClient(async () => response(invalid)).loadTranslations(releaseId), Error);
  }
});

test('recovery accepts different valid translated phrasings and returns isolated display data', async () => {
  for (const name of ['Vegetable dish', 'A dish with vegetables']) {
    const cache = fixture('translations');
    const key = Object.keys(cache.entries)[0];
    cache.entries[key].en.name = name;
    const recovered = await createDataClient(async () => response(cache)).loadTranslations(releaseId);
    assert.equal(recovered.entries[key].en.name, name);
    assert.deepEqual(contracts.recoverTranslations(cache), recovered);
    recovered.entries[key].en.components.push('Change the display copy');
    assert.notDeepEqual(recovered.entries[key].en.components, cache.entries[key].en.components);
  }
});
