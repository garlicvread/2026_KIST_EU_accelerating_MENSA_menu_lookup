import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

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

test('notice translations preserve exact source association and order in each language', () => {
  assert.equal(typeof ui.translatedNotices, 'function');
  const notices = ['Milch und Laktose', 'Senf'];
  const cache = { notices: { 'Milch und Laktose': { en: 'Milk and lactose', ko: '우유 및 유당' }, Senf: { en: 'Mustard', ko: '겨자' } } };
  assert.deepEqual(ui.translatedNotices(notices, cache, 'ko'), [
    { original: 'Milch und Laktose', text: '우유 및 유당', translated: true },
    { original: 'Senf', text: '겨자', translated: true }
  ]);
  assert.equal(ui.translatedNotices(notices, cache, 'en')[0].text, 'Milk and lactose');
  assert.deepEqual(ui.translatedNotices(notices, cache, 'de'), notices.map(original => ({ original, text: original, translated: false })));
  assert.deepEqual(notices, ['Milch und Laktose', 'Senf']);
});

test('missing, malformed or inexact notice translations retain every original warning', () => {
  assert.equal(typeof ui.translatedNotices, 'function');
  const notices = ['Kann Spuren von Senf enthalten', 'Senf'];
  for (const cache of [null, {}, { notices: [] }, { notices: { Senf: { en: '', ko: '겨자' } } }, { notices: { Senf: { en: 5, ko: '겨자' } } }]) {
    assert.deepEqual(ui.translatedNotices(notices, cache, 'en'), notices.map(original => ({ original, text: original, translated: false })));
  }
  const cache = { notices: { Senf: { en: 'Mustard', ko: '겨자' } } };
  assert.equal(ui.translatedNotices(notices, cache, 'ko')[0].text, notices[0]);
  assert.equal(ui.translatedNotices(notices, cache, 'ko')[1].text, '겨자');
  assert.deepEqual(ui.translatedNotices([], cache, 'ko'), []);
  const inherited = { notices: Object.create({ Senf: { en: 'Wrong', ko: '잘못됨' } }) };
  assert.equal(ui.translatedNotices(['Senf'], inherited, 'ko')[0].translated, false);
});

// Exercise the real browser entry point with a small DOM adapter and local snapshots.
// Only expose load()'s existing promise so the test can await startup deterministically.
async function browserHarness({ missingHeading = false, snapshot, cache, language = 'ko' } = {}) {
  const html = await readFile(new URL('../site/index.html', import.meta.url), 'utf8');
  const menu = snapshot ?? JSON.parse(await readFile(new URL('../site/data/menu.json', import.meta.url), 'utf8'));
  const translations = cache ?? JSON.parse(await readFile(new URL('../site/data/translations.json', import.meta.url), 'utf8'));
  class Node {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this.attributes = {}; this.events = {}; this.dataset = {}; this.style = { setProperty() {} }; this.hidden = false; this.textContent = ''; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, listener) { this.events[name] = listener; }
  }
  const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, new Node()]));
  const heading = nodes.get('menu-heading');
  if (missingHeading) nodes.delete('menu-heading');
  const context = {
    document: { getElementById: id => nodes.get(id) ?? null, createElement: tag => new Node(tag), querySelectorAll: () => [], addEventListener() {}, documentElement: {} },
    navigator: { languages: [language] },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: async path => ({ ok: true, json: async () => path.includes('translations') ? translations : menu }),
    AbortController, setTimeout, clearTimeout, Intl, console: { error() {} }
  };
  return {
    nodes, restoreHeading: () => nodes.set('menu-heading', heading),
    start: () => runInNewContext(source.replace(/^export /gm, '').replace('  load();\n}', '  return load();\n}'), context)
  };
}

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

test('render failures leave loading and allow a successful retry', async () => {
  const browser = await browserHarness({ missingHeading: true });
  await assert.doesNotReject(browser.start());
  const content = browser.nodes.get('menu-content');
  assert.equal(content.attributes['aria-busy'], 'false');
  assert.equal(content.children[0].attributes.role, 'alert');
  assert.equal(browser.nodes.get('navigation').hidden, true);
  assert.equal(browser.nodes.get('menu-toolbar').hidden, true);
  const retry = descendants(content).find(node => node.tagName === 'button');
  assert.equal(retry.textContent, '다시 시도');
  browser.restoreHeading();
  await retry.events.click();
  assert.equal(content.children[0].className, 'meal-grid');
  assert.ok(descendants(content).some(node => node.tagName === 'article'));
  assert.equal(browser.nodes.get('navigation').hidden, false);
});

test('normal browser startup renders meals without an error or loading placeholder', async () => {
  const browser = await browserHarness();
  await browser.start();
  const content = browser.nodes.get('menu-content');
  assert.equal(content.children[0].className, 'meal-grid');
  assert.equal(content.attributes['aria-busy'], 'false');
  assert.ok(descendants(content).some(node => node.tagName === 'article'));
});

function withClass(node, className) {
  return descendants(node).filter(item => item.className?.split(' ').includes(className));
}

function ingredientSnapshot() {
  const snapshot = pricedSnapshot();
  const meal = snapshot.days[0].meals[0];
  meal.translation_key = 'soup';
  meal.components = [
    { name_de: 'Brot', notices: ['Milch und Laktose', 'Weizen'] },
    { name_de: 'Salat', notices: [] },
    { name_de: 'Klare Salatsoße', notices: ['Senf'] }
  ];
  meal.notices = ['Soja'];
  const cache = {
    schema_version: 1,
    entries: { soup: {
      source: { name_de: 'Suppe', components: ['Brot', 'Salat', 'Klare Salatsoße'] },
      ko: { name: '수프', components: ['작은 롤빵', '모둠 잎채소 샐러드', '맑은 샐러드 드레싱'] },
      en: { name: 'Soup', components: ['Bread', 'Mixed leaf salad', 'Clear salad dressing'] }
    } },
    notices: {
      'Milch und Laktose': { ko: '우유 및 유당', en: 'Milk and lactose' },
      Weizen: { ko: '밀', en: 'Wheat' },
      Senf: { ko: '겨자', en: 'Mustard' },
      Soja: { ko: '대두', en: 'Soy' }
    }
  };
  return { snapshot, cache };
}

test('components appear once in source order inside a counted, localized disclosure', async () => {
  const cases = [
    ['ko', '구성품별 정보 · 3', ['작은 롤빵', '모둠 잎채소 샐러드', '맑은 샐러드 드레싱']],
    ['en', 'Component details · 3', ['Bread', 'Mixed leaf salad', 'Clear salad dressing']],
    ['de', 'Bestandteile · 3', ['Brot', 'Salat', 'Klare Salatsoße']]
  ];
  for (const [language, summary, names] of cases) {
    const browser = await browserHarness({ ...ingredientSnapshot(), language });
    await browser.start();
    const content = browser.nodes.get('menu-content');
    const disclosures = withClass(content, 'meal-details');
    assert.equal(disclosures.length, 1);
    const details = disclosures[0];
    assert.equal(details.tagName, 'details');
    assert.equal(details.children[0].tagName, 'summary');
    assert.equal(details.children[0].textContent, summary);
    assert.equal(details.children.length, 2);
    const detail = details.children[1];
    assert.equal(detail.className, 'detail-content');
    assert.equal(detail.children.length, 1);
    const list = detail.children[0];
    assert.equal(list.tagName, 'ul');
    assert.equal(list.className, 'component-list');
    assert.deepEqual(list.children.map(item => withClass(item, 'component-name')[0].textContent), names);
    assert.ok(list.children.every(item => item.tagName === 'li'));
    for (const name of names) assert.equal(descendants(content).filter(item => item.textContent === name).length, 1);
    assert.equal(withClass(content, 'components-preview').length, 0);
  }
});

test('meal warnings stay visible immediately below the title or fallback, outside component disclosure', async () => {
  const labels = { ko: '식재료 · 알레르기 · 첨가물', en: 'Ingredients · allergens · additives', de: 'Zutaten · Allergene · Zusatzstoffe' };
  for (const language of ['ko', 'en', 'de']) {
    for (const missingTranslations of [false, true]) {
      const fixture = ingredientSnapshot();
      if (missingTranslations) fixture.cache.entries = {};
      const browser = await browserHarness({ ...fixture, language });
      await browser.start();
      const content = browser.nodes.get('menu-content');
      const article = withClass(content, 'meal-card')[0];
      const notices = withClass(article, 'meal-notice-group');
      assert.equal(notices.length, 1);
      assert.equal(notices[0].tagName, 'div');
      assert.equal(notices[0].attributes['aria-label'], labels[language]);
      assert.equal(notices[0].children.length, 1);
      assert.equal(notices[0].children[0].className, 'notice-list meal-notices');
      const index = article.children.indexOf(notices[0]);
      assert.ok(index > 0, 'meal warnings must be a direct child of the card');
      assert.equal(article.children[index - 1].className, language === 'de' ? 'title-row' : missingTranslations ? 'original-label' : 'original-title');
      assert.equal(article.children[index + 1].className, 'meal-details');
      assert.equal(withClass(withClass(article, 'meal-details')[0], 'meal-notice-group').length, 0);
    }
  }
});

test('selected-language names and warnings retain their exact German originals in the same group', async () => {
  const cases = [
    ['ko', '수프', ['작은 롤빵', '모둠 잎채소 샐러드', '맑은 샐러드 드레싱'], [['우유 및 유당', '밀'], [], ['겨자']], '대두'],
    ['en', 'Soup', ['Bread', 'Mixed leaf salad', 'Clear salad dressing'], [['Milk and lactose', 'Wheat'], [], ['Mustard']], 'Soy'],
    ['de', 'Suppe', ['Brot', 'Salat', 'Klare Salatsoße'], [['Milch und Laktose', 'Weizen'], [], ['Senf']], 'Soja']
  ];
  for (const [language, name, componentNames, componentWarnings, mealWarning] of cases) {
    const browser = await browserHarness({ ...ingredientSnapshot(), language });
    await browser.start();
    const content = browser.nodes.get('menu-content');
    const title = withClass(content, 'meal-title')[0];
    assert.equal(title.textContent, name);
    assert.equal(title.attributes.lang, language);
    assert.deepEqual(withClass(content, 'original-title').map(item => [item.textContent, item.attributes.lang]), language === 'de' ? [] : [['Suppe', 'de']]);
    const groups = withClass(content, 'component-group');
    assert.equal(groups.length, 3);
    assert.deepEqual(groups.map(group => withClass(group, 'component-name')[0].textContent), componentNames);
    assert.deepEqual(groups.map(group => withClass(group, 'notice-translation').map(item => item.textContent)), componentWarnings);
    for (const group of groups) {
      assert.equal(group.children[0].className, 'component-heading');
      assert.equal(withClass(group, 'component-name')[0].attributes.lang, language);
    }
    assert.deepEqual(withClass(content, 'component-original').map(item => item.textContent), language === 'de' ? [] : ['Brot', 'Salat', 'Klare Salatsoße']);
    assert.equal(withClass(groups[1], 'component-notice-group').length, 0);
    const mealNotices = withClass(content, 'meal-notice-group')[0];
    assert.deepEqual(withClass(mealNotices, 'notice-translation').map(item => item.textContent), [mealWarning]);
    assert.equal(withClass(mealNotices, 'component-group').length, 0);
    assert.deepEqual(withClass(content, 'notice-original').map(item => item.textContent), language === 'de' ? [] : ['Soja', 'Milch und Laktose', 'Weizen', 'Senf']);
    assert.ok(withClass(content, 'notice-translation').every(item => item.attributes.lang === language));
    assert.ok([...withClass(content, 'component-original'), ...withClass(content, 'notice-original')].every(item => item.attributes.lang === 'de'));
    assert.ok(withClass(content, 'notice-pair').every(item => item.children[0].className === 'notice-translation' && (language === 'de' ? item.children.length === 1 : item.children[1].className === 'notice-original')));
    assert.equal(withClass(content, 'notice-fallback').length, 0);
    for (const original of ['Suppe', 'Brot', 'Salat', 'Klare Salatsoße', 'Soja', 'Milch und Laktose', 'Weizen', 'Senf']) {
      assert.equal(descendants(content).filter(item => item.textContent === original).length, 1);
    }
  }
});

test('meals without components omit the disclosure while keeping any meal warnings visible', async () => {
  for (const language of ['ko', 'en', 'de']) {
    for (const notices of [[], ['Soja']]) {
      const fixture = ingredientSnapshot();
      fixture.snapshot.days[0].meals[0].components = [];
      fixture.snapshot.days[0].meals[0].notices = notices;
      const browser = await browserHarness({ ...fixture, language });
      await browser.start();
      const content = browser.nodes.get('menu-content');
      assert.equal(withClass(content, 'meal-details').length, 0);
      assert.equal(withClass(content, 'component-list').length, 0);
      assert.equal(withClass(content, 'meal-notice-group').length, notices.length);
      assert.equal(withClass(content, 'notice-pair').length, notices.length);
    }
  }
});

test('empty meal-level warnings omit only that section and retain component warnings', async () => {
  const fixture = ingredientSnapshot();
  fixture.snapshot.days[0].meals[0].notices = [];
  const browser = await browserHarness(fixture);
  await browser.start();
  const content = browser.nodes.get('menu-content');
  assert.equal(withClass(content, 'meal-notice-group').length, 0);
  assert.equal(withClass(content, 'meal-notices').length, 0);
  assert.equal(withClass(content, 'notice-pair').length, 3);
  assert.ok(!descendants(content).some(item => item.textContent === '해당 원본 항목에 별도 표시 정보 없음.'));
});

test('missing translations retain every German name and warning exactly once with correct language tags', async () => {
  const fallback = { ko: '독일어 원문 · 번역 준비 중', en: 'Original German · translation unavailable' };
  for (const language of ['ko', 'en', 'de']) {
    const fixture = ingredientSnapshot();
    fixture.cache = { schema_version: 1, entries: {}, notices: {} };
    const browser = await browserHarness({ ...fixture, language });
    await browser.start();
    const content = browser.nodes.get('menu-content');
    assert.equal(withClass(content, 'meal-title')[0].textContent, 'Suppe');
    assert.equal(withClass(content, 'meal-title')[0].attributes.lang, 'de');
    assert.equal(withClass(content, 'original-title').length, 0);
    assert.deepEqual(withClass(content, 'original-label').map(item => item.textContent), language === 'de' ? [] : [fallback[language]]);
    const groups = withClass(content, 'component-group');
    assert.equal(groups.length, 3);
    assert.deepEqual(groups.map(group => withClass(group, 'component-name')[0].textContent), ['Brot', 'Salat', 'Klare Salatsoße']);
    assert.ok(groups.every(group => withClass(group, 'component-name')[0].attributes.lang === 'de'));
    assert.equal(withClass(content, 'component-original').length, 0);
    assert.deepEqual(withClass(content, 'notice-translation').map(item => item.textContent), ['Soja', 'Milch und Laktose', 'Weizen', 'Senf']);
    assert.ok(withClass(content, 'notice-translation').every(item => item.attributes.lang === 'de'));
    assert.equal(withClass(content, 'notice-original').length, 0);
    assert.equal(withClass(content, 'notice-fallback').length, language === 'de' ? 0 : 4);
    assert.ok(withClass(content, 'notice-fallback').every(item => item.textContent === fallback[language] && item.attributes.lang === language));
    for (const original of ['Suppe', 'Brot', 'Salat', 'Klare Salatsoße', 'Soja', 'Milch und Laktose', 'Weizen', 'Senf']) {
      assert.equal(descendants(content).filter(item => item.textContent === original).length, 1);
    }
  }
});
