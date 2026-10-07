import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createDataClient } from '../dist/frontend/data_client.js';

const displayURL = new URL('../dist/frontend/display.js', import.meta.url);
const appURL = new URL('../dist/frontend/app.js', import.meta.url);
const ui = await import(displayURL.href);
const apps = await import(appURL.href);


test('prices display the selected group and keep genuinely pending prices blank', () => {
  const meal = { prices: { student: 350, staff: 465, guest: 535 }, price_status: 'verified' };
  assert.match(ui.priceText(meal, 'student', 'en'), /3\.50/);
  assert.match(ui.priceText(meal, 'staff', 'en'), /4\.65/);
  assert.equal(ui.priceText({ ...meal, price_status: 'source_pending' }, 'student', 'en'), null);
});

test('translations match the dish and ordered components; mismatch safely retains German', () => {
  const meal = { translation_key: 'key', name_de: 'Suppe', components: [{ name_de: 'Brot' }] };
  const cache = { entries: { key: { source: { name_de: 'Suppe', components: ['Brot'] }, en: { name: 'Soup', components: ['Bread'] } } } };
  assert.deepEqual(ui.translatedMeal(meal, cache, 'en'), { name: 'Soup', components: ['Bread'], translated: true });
  assert.equal(ui.translatedMeal({ ...meal, name_de: 'Salat' }, cache, 'en').name, 'Salat');
  assert.equal(ui.translatedMeal(meal, null, 'en').name, 'Suppe');
});


function pricedSnapshot() {
  return {
    schema_version: 1,
    source: { url: 'https://www.stw-saarland.de/gastro/mensa-saarbruecken/', fetched_at: '2026-09-21T10:00:00Z', sha256: 'a'.repeat(64) },
    coverage: { start: '2026-09-21', end: '2026-09-21' },
    days: [{ date: '2026-09-21', meals: [{
      id: '2026-09-21-' + 'a'.repeat(64), translation_key: 'b'.repeat(64), location: 'A', name_de: 'Suppe', category: 'Menü 1', components: [], notices: [],
      prices: { student: 350, staff: 465, guest: 535 }, price_status: 'verified',
      price_source: { date: '2026-09-21', category: 'Menü 1', name: 'Suppe', raw: 'S: 3,50 | M: 4,65 | G: 5,35' }
    }] }]
  };
}


// browserHarness는 실제로 컴파일한 화면 구성·메뉴 로딩 조정·JSON 요청 코드를 DOM 테스트 대체물과 함께 실행합니다. 요소 재사용과 입력 초점 유지 동작을 확인하도록 대체물이 요소의 동일성과 초점을 보존합니다.
async function browserHarness({ snapshot, cache, language = 'ko', storage, translationPromise, clock = () => new Date('2026-09-21T10:00:00Z') } = {}) {
  const html = await readFile(new URL('../site/index.html', import.meta.url), 'utf8');
  const menu = snapshot ?? JSON.parse(await readFile(new URL('../site/data/menu.json', import.meta.url), 'utf8'));
  const translations = cache ?? JSON.parse(await readFile(new URL('../site/data/translations.json', import.meta.url), 'utf8'));
  const document = { nodes: new Map(), actions: [], title: '', activeElement: null };
  class Node {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase(); this.nodeType = 1; this.children = []; this.parentNode = null;
      this.attributes = {}; this.events = {}; this.dataset = {}; this.style = { setProperty() {} };
      this.hidden = false; this.disabled = false; this.className = ''; this._text = ''; this.value = ''; this._open = false;
    }
    get childNodes() { return this.children; }
    get firstChild() { return this.children[0] ?? null; }
    get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
    set textContent(value) { this.replaceChildren(); this._text = String(value ?? ''); }
    get open() { return this._open; }
    set open(value) { if (this._open !== value) { this._open = value; queueMicrotask(() => this.emit('toggle')); } }
    contains(node) { return this === node || this.children.some(child => child.contains(node)); }
    removeChild(child) {
      if (child.contains(document.activeElement)) document.activeElement = document.body;
      const index = this.children.indexOf(child); if (index < 0) throw new Error('Not a child');
      this.children.splice(index, 1); child.parentNode = null; return child;
    }
    insertBefore(child, before) {
      if (child === before) return child;
      if (child.parentNode) child.parentNode.removeChild(child);
      const index = before === null ? this.children.length : this.children.indexOf(before);
      if (index < 0) throw new Error('Not a reference child');
      this.children.splice(index, 0, child); child.parentNode = this; return child;
    }
    append(...children) { for (const child of children) this.insertBefore(child, null); }
    replaceChildren(...children) { for (const child of [...this.children]) this.removeChild(child); this._text = ''; this.append(...children); }
    remove() { this.parentNode?.removeChild(this); }
    setAttribute(name, value) {
      this.attributes[name] = String(value);
      if (name === 'value') this.value = String(value);
      if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(name, listener) { this.events[name] = listener; }
    async emit(name) { return this.events[name]?.({ target: this, currentTarget: this }); }
    focus(options) { document.activeElement = this; document.actions.push(['focus', this, options]); }
    scrollIntoView(options) { document.actions.push(['scroll', this, options]); }
  }
  document.createElement = tag => new Node(tag);
  document.documentElement = new Node('html'); document.body = new Node('body'); document.documentElement.append(document.body); document.activeElement = document.body;
  document.getElementById = id => document.nodes.get(id) ?? null;
  document.querySelectorAll = selector => {
    const key = selector.slice(1, -1);
    return descendants(document.body).filter(node => Object.hasOwn(node.attributes, key));
  };
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*)>/gi)) {
    if (['html', 'body', 'meta', 'link', 'script'].includes(match[1])) continue;
    const node = new Node(match[1]);
    for (const attribute of match[2].matchAll(/([\w-]+)="([^"]*)"/g)) node.setAttribute(attribute[1], attribute[2]);
    if (node.attributes.id) document.nodes.set(node.attributes.id, node);
    document.body.append(node);
  }
  const requests = [];
  const client = createDataClient(async path => {
    requests.push(path);
    return { ok: true, json: async () => path === 'data/current.json' ? { schema_version: 1, release_id: 'a'.repeat(64) } : path.endsWith('/translations.json') ? translationPromise ?? translations : menu };
  });
  const options = { document, client, clock, browserLanguages: [language] };
  if (storage) options.storage = storage;
  const app = apps.createApp(options);
  return { document, nodes: document.nodes, app, requests, start: () => app.load() };
}

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}


function withClass(node, className) {
  return descendants(node).filter(item => item.className?.split(' ').includes(className));
}

test('identical source occurrences render once per day in every language and both views', async () => {
  const fixture = ingredientSnapshot();
  const first = fixture.snapshot.days[0].meals[0];
  fixture.snapshot.days[0].meals.push({ ...structuredClone(first), id: first.id + '-2' });
  const next = structuredClone(fixture.snapshot.days[0]);
  next.date = '2026-09-22';
  for (const meal of next.meals) {
    meal.id = meal.id.replace('2026-09-21', next.date); meal.price_source.date = next.date;
  }
  fixture.snapshot.days.push(next); fixture.snapshot.coverage.end = next.date;
  const original = JSON.stringify(fixture.snapshot);
  const browser = await browserHarness(fixture); await browser.start();
  const content = browser.nodes.get('menu-content');
  const article = withClass(content, 'meal-card')[0];
  const details = withClass(article, 'meal-details')[0];
  details.open = true; await details.emit('toggle'); details.children[0].focus();
  for (const [language, title] of [['ko', '수프'], ['en', 'Soup'], ['de', 'Suppe']]) {
    browser.nodes.get('language').value = language; await browser.nodes.get('language').emit('change');
    assert.equal(withClass(content, 'meal-card').length, 1);
    assert.equal(withClass(content, 'meal-card')[0], article);
    assert.equal(withClass(content, 'meal-title')[0].textContent, title);
    assert.equal(details.open, true); assert.equal(browser.document.activeElement, details.children[0]);
    const week = browser.document.querySelectorAll('[data-view]').find(node => node.dataset.view === 'week');
    await week.emit('click');
    assert.equal(withClass(content, 'meal-card').length, 2, 'the same dish on another day stays visible');
    const day = browser.document.querySelectorAll('[data-view]').find(node => node.dataset.view === 'day');
    await day.emit('click'); details.children[0].focus();
  }
  assert.deepEqual(browser.app.getState().menu.value.days.map(day => day.meals.length), [2, 2]);
  assert.equal(JSON.stringify(fixture.snapshot), original, 'display selection must not delete source records');
});

test('same titles retain distinct locations, components, warnings and prices', async () => {
  const fixture = ingredientSnapshot(); const first = fixture.snapshot.days[0].meals[0];
  const variations = [
    meal => { meal.location = 'B'; },
    meal => { meal.components[0].name_de = 'Reis'; },
    meal => { meal.notices = ['Vegan']; },
    meal => { meal.prices.guest = 600; meal.price_source.raw = 'S: 3,50 | M: 4,65 | G: 6,00'; }
  ];
  for (const [index, change] of variations.entries()) {
    const meal = structuredClone(first); meal.id = first.id + '-' + (index + 2); change(meal);
    fixture.snapshot.days[0].meals.push(meal);
  }
  const browser = await browserHarness(fixture); await browser.start();
  for (const view of ['day', 'week']) {
    await browser.document.querySelectorAll('[data-view]').find(node => node.dataset.view === view).emit('click');
    assert.equal(withClass(browser.nodes.get('menu-content'), 'meal-card').length, 5);
  }
});

function ingredientSnapshot() {
  const snapshot = pricedSnapshot();
  const meal = snapshot.days[0].meals[0];
  meal.translation_key = 'b'.repeat(64);
  meal.components = [
    { name_de: 'Brot', notices: ['Milch und Laktose', 'Weizen'] },
    { name_de: 'Salat', notices: [] },
    { name_de: 'Klare Salatsoße', notices: ['Senf'] }
  ];
  meal.notices = ['Soja'];
  const cache = {
    schema_version: 1,
    entries: { ['b'.repeat(64)]: {
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
    assert.ok(groups.every(group => withClass(group, 'component-name')[0].attributes.lang === language));
    assert.deepEqual(withClass(content, 'component-original').map(item => item.textContent), language === 'de' ? [] : ['Brot', 'Salat', 'Klare Salatsoße']);
    assert.equal(withClass(groups[1], 'component-notice-group').length, 0);
    const mealNotices = withClass(content, 'meal-notice-group')[0];
    assert.deepEqual(withClass(mealNotices, 'notice-translation').map(item => item.textContent), [mealWarning]);
    assert.equal(withClass(mealNotices, 'component-group').length, 0);
    assert.deepEqual(withClass(content, 'notice-original').map(item => item.textContent), language === 'de' ? [] : ['Soja', 'Milch und Laktose', 'Weizen', 'Senf']);
    assert.ok(withClass(content, 'notice-translation').every(item => item.attributes.lang === language));
    assert.ok([...withClass(content, 'component-original'), ...withClass(content, 'notice-original')].every(item => item.attributes.lang === 'de'));
    assert.equal(withClass(content, 'notice-fallback').length, 0);
    for (const original of ['Suppe', 'Brot', 'Salat', 'Klare Salatsoße', 'Soja', 'Milch und Laktose', 'Weizen', 'Senf']) {
      assert.equal(descendants(content).filter(item => item.children.length === 0 && item.textContent === original).length, 1);
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
      assert.equal(descendants(content).filter(item => item.children.length === 0 && item.textContent === original).length, 1);
    }
  }
});

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function until(predicate) { for (let turn = 0; turn < 50 && !predicate(); turn++) await Promise.resolve(); assert.ok(predicate(), 'Expected asynchronous app state'); }

test('open focused details survive group language and late cache updates without navigation jumps', async () => {
  const late = deferred(); const fixture = ingredientSnapshot();
  const browser = await browserHarness({ ...fixture, language: 'en', translationPromise: late.promise });
  assert.deepEqual(browser.requests, []);
  const load = browser.start(); await until(() => browser.app.getState().menu.status === 'ready');
  const content = browser.nodes.get('menu-content'); const article = withClass(content, 'meal-card')[0];
  const details = withClass(article, 'meal-details')[0], summary = details.children[0];
  details.open = true; await details.emit('toggle'); summary.focus(); const actions = browser.document.actions.length;
  const group = browser.nodes.get('price-group'); group.value = 'staff'; await group.emit('change');
  const language = browser.nodes.get('language'); language.value = 'ko'; await language.emit('change');
  assert.equal(withClass(content, 'meal-card')[0], article); assert.equal(withClass(article, 'meal-details')[0], details);
  assert.equal(details.children[0], summary); assert.equal(details.open, true); assert.equal(browser.document.activeElement, summary);
  assert.match(withClass(article, 'price')[0].textContent, /4[.,]65/); assert.equal(browser.document.actions.length, actions);
  late.resolve(fixture.cache); await load;
  assert.equal(withClass(article, 'meal-title')[0].textContent, '수프'); assert.equal(details.children[0], summary);
  assert.equal(browser.document.activeElement, summary); assert.equal(details.open, true); assert.equal(browser.document.actions.length, actions);
  const dateButton = withClass(browser.nodes.get('day-strip'), 'day-button')[0]; dateButton.focus();
  group.value = 'guest'; await group.emit('change'); assert.equal(withClass(browser.nodes.get('day-strip'), 'day-button')[0], dateButton);
  assert.equal(browser.document.activeElement, dateButton);
  const navigationCache = deferred(); const second = await browserHarness({ ...ingredientSnapshot(), translationPromise: navigationCache.promise });
  const secondLoad = second.start(); await until(() => second.app.getState().menu.status === 'ready');
  const focusedDate = withClass(second.nodes.get('day-strip'), 'day-button')[0]; focusedDate.focus();
  navigationCache.resolve(fixture.cache); await secondLoad;
  assert.equal(withClass(second.nodes.get('day-strip'), 'day-button')[0], focusedDate); assert.equal(second.document.activeElement, focusedDate);
});

test('failed delayed translations retain immediately usable German menu and a localized note', async () => {
  const late = deferred(); const browser = await browserHarness({ ...ingredientSnapshot(), translationPromise: late.promise });
  const load = browser.start(); await until(() => browser.app.getState().menu.status === 'ready');
  assert.equal(browser.nodes.get('menu-content').attributes['aria-busy'], 'false');
  assert.equal(withClass(browser.nodes.get('menu-content'), 'meal-title')[0].textContent, 'Suppe');
  late.reject(new Error('translations unavailable')); await load;
  assert.equal(browser.nodes.get('translation-note').hidden, false); assert.match(browser.nodes.get('translation-note').textContent, /독일어/);
  assert.equal(browser.nodes.get('navigation').hidden, false);
});

test('actual available navigation and preferences announce selected dates and preserve navigation focus without scrolling', async () => {
  const { rangeLabel } = await import('../dist/frontend/render.js');
  assert.equal(rangeLabel([{date:'2026-10-05'},{date:'2026-10-09'}], 'ko'), '10월 5일~9일');
  const yearCrossing = rangeLabel([{date:'2026-12-31'},{date:'2027-01-01'}], 'ko');
  assert.match(yearCrossing, /2026/); assert.match(yearCrossing, /2027/);

  for (const [now, prefix] of [
    ['2026-09-20T10:00:00Z', 'Today is not published; showing the next available menu.'],
    ['2026-09-22T10:00:00Z', 'Showing the latest available past menu.']
  ]) {
    const fallback = await browserHarness({ ...ingredientSnapshot(), language: 'en', clock: () => new Date(now) });
    await fallback.start();
    const state = fallback.app.getState(), actions = fallback.document.actions.length;
    const content = fallback.nodes.get('menu-content'), cards = [...content.children];
    assert.equal(state.selectedDate, '2026-09-21');
    await fallback.nodes.get('today-button').emit('click');
    assert.equal(fallback.app.getState(), state, 'Today fallback must not change an already selected day');
    assert.equal(fallback.document.actions.length, actions, 'Today fallback must not move focus or scroll');
    assert.deepEqual(content.children, cards, 'Today fallback must not rerender the menu');
    const announcement = fallback.nodes.get('live-status').textContent;
    assert.ok(announcement.startsWith(prefix), 'Today must explain the unchanged fallback selection');
    assert.match(announcement, /21 Sept/); assert.match(announcement, /Students/);
    await fallback.nodes.get('previous-week').emit('click');
    assert.equal(fallback.nodes.get('live-status').textContent, announcement, 'Other no-op navigation must not announce');
    assert.equal(fallback.app.getState(), state); assert.equal(fallback.document.actions.length, actions);
  }
  const writes = []; const browser = await browserHarness({ language: 'fr', storage: { getItem: key => key === 'mensa-language' ? 'en' : 'guest', setItem: (...entry) => writes.push(entry) } });
  await browser.start(); assert.equal(browser.app.getState().language, 'en'); assert.equal(browser.app.getState().group, 'guest');
  const dates = browser.app.getState().menu.value.days.map(day => day.date);
  assert.deepEqual(browser.nodes.get('date-picker').children.map(option => option.value), dates);
  const selected = browser.app.getState().selectedDate; const initialActions = browser.document.actions.length;
  await browser.nodes.get('previous-week').emit('click'); assert.equal(browser.document.actions.length, initialActions);
  const picker = browser.nodes.get('date-picker'); picker.value = '1999-01-01'; await picker.emit('change'); assert.equal(browser.app.getState().selectedDate, selected);
  picker.focus(); const pickerActions = browser.document.actions.length;
  picker.value = dates.at(-1); await picker.emit('change'); assert.equal(browser.app.getState().selectedDate, dates.at(-1));
  assert.equal(browser.document.activeElement, picker); assert.equal(browser.document.actions.length, pickerActions, 'Date selection must not move focus or scroll');
  const pickerAnnouncement = browser.nodes.get('live-status').textContent; assert.match(pickerAnnouncement, /Guests/);
  const dateButton = withClass(browser.nodes.get('day-strip'), 'day-button').find(node => node.dataset.date !== browser.app.getState().selectedDate);
  assert.ok(dateButton); dateButton.focus(); const dateActions = browser.document.actions.length; await dateButton.emit('click');
  assert.equal(browser.app.getState().selectedDate, dateButton.dataset.date); assert.equal(browser.document.activeElement, dateButton);
  assert.equal(browser.document.actions.length, dateActions, 'Day buttons must not move focus or scroll');
  assert.notEqual(browser.nodes.get('live-status').textContent, pickerAnnouncement); assert.match(browser.nodes.get('live-status').textContent, /Guests/);
  const view = browser.document.querySelectorAll('[data-view]').find(node => node.dataset.view === 'week'); view.focus(); const viewActions = browser.document.actions.length; await view.emit('click');
  assert.equal(browser.app.getState().view, 'week'); assert.equal(view.attributes['aria-pressed'], 'true');
  assert.equal(browser.document.activeElement, view); assert.equal(browser.document.actions.length, viewActions, 'View changes must not move focus or scroll');
  assert.ok(browser.nodes.get('live-status').textContent.includes(browser.nodes.get('week-range').textContent), 'weekly announcement includes the selected published date range');
  const openDay = withClass(browser.nodes.get('menu-content'), 'week-day-button').find(node => node.dataset.date !== browser.app.getState().selectedDate);
  assert.ok(openDay); openDay.focus(); const openDayActions = browser.document.actions.length; await openDay.emit('click');
  assert.equal(browser.app.getState().selectedDate, openDay.dataset.date); assert.equal(browser.app.getState().view, 'day');
  assert.equal(browser.document.documentElement.contains(openDay), false, 'Opening the day removes its weekly button');
  assert.ok(browser.document.activeElement === picker, 'A removed weekly button must return focus to the persistent date picker');
  const fallbackActions = browser.document.actions.slice(openDayActions);
  assert.deepEqual(fallbackActions.map(([kind]) => kind), ['focus'], 'Removed-control recovery must focus once without scrolling');
  assert.ok(fallbackActions[0][1] === picker); assert.deepEqual(fallbackActions[0][2], { preventScroll: true });
  assert.match(browser.nodes.get('live-status').textContent, /Guests/);
  const today = browser.nodes.get('today-button'); today.focus(); const todayActions = browser.document.actions.length;
  await today.emit('click'); assert.equal(browser.app.getState().selectedDate, dates[0]); assert.equal(browser.app.getState().view, 'day');
  assert.equal(browser.document.activeElement, today); assert.equal(browser.document.actions.length, todayActions, 'Today selection must not move focus or scroll');
  assert.match(browser.nodes.get('live-status').textContent, /next available/);
  const secondWeek = dates.find(date => date >= '2026-10-05'); const next = browser.nodes.get('next-week'); next.focus(); const weekActions = browser.document.actions.length; await next.emit('click');
  assert.equal(browser.app.getState().selectedDate, secondWeek);
  assert.equal(browser.document.activeElement, next); assert.equal(browser.document.actions.length, weekActions, 'Week navigation must not move focus or scroll');
  const bounded = browser.document.actions.length; await browser.nodes.get('next-week').emit('click'); assert.equal(browser.document.actions.length, bounded);
  const before = browser.document.actions.length;
  const language = browser.nodes.get('language'); language.value = 'invalid'; await language.emit('change');
  const group = browser.nodes.get('price-group'); group.value = 'invalid'; await group.emit('change'); assert.deepEqual(writes, []);
  language.value = 'de'; await language.emit('change'); group.value = 'student'; await group.emit('change');
  assert.deepEqual(writes, [['mensa-language','de'],['mensa-price-group','student']]); assert.equal(browser.document.actions.length, before);
  await view.emit('click');
  language.value = 'en'; await language.emit('change'); group.value = 'staff'; await group.emit('change');
  assert.ok(browser.nodes.get('live-status').textContent.includes(browser.nodes.get('week-range').textContent)); assert.match(browser.nodes.get('live-status').textContent, /Staff/);
  const disabledStorage = await browserHarness({ language: 'fr', storage: { getItem() { throw new Error('private'); }, setItem() { throw new Error('private'); } } });
  await disabledStorage.start(); assert.equal(disabledStorage.app.getState().language, 'en');
  disabledStorage.nodes.get('language').value = 'ko'; await disabledStorage.nodes.get('language').emit('change'); assert.equal(disabledStorage.app.getState().language, 'ko');
});
