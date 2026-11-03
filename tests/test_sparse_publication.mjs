import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { validateMenu, validateTranslations } from '../dist/frontend/contracts.js';
import { priceText, translatedMeal, translatedNotices } from '../dist/frontend/display.js';

const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/menu-data/${name}.json`, import.meta.url), 'utf8'));

test('a sparse valid publication displays translated items and German gaps with verified prices', () => {
  const menu = validateMenu(fixture('menu'));
  const complete = fixture('translations');
  const meals = menu.days.flatMap(day => day.meals);
  const translated = meals[0];
  const missing = meals.find(meal => meal.translation_key !== translated.translation_key);
  assert.ok(missing, 'Fixture must contain a distinct untranslated dish');
  const cache = validateTranslations({ schema_version: 1,
    entries: { [translated.translation_key]: complete.entries[translated.translation_key] }, notices: {} });
  const before = structuredClone({ menu, cache });
  for (const language of ['en', 'ko']) {
    assert.equal(translatedMeal(translated, cache, language).translated, true);
    assert.deepEqual(translatedMeal(missing, cache, language), {
      name: missing.name_de, components: missing.components.map(component => component.name_de), translated: false,
    });
    const labels = [...missing.notices, ...missing.components.flatMap(component => component.notices), 'Neue Quellenwarnung'];
    assert.deepEqual(translatedNotices(labels, cache, language),
      labels.map(original => ({ original, text: original, translated: false })));
    assert.ok(priceText(missing, 'student', language)?.includes('€'));
  }
  assert.deepEqual({ menu, cache }, before);
});
