import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
const clientURL = new URL('../dist/frontend/data_client.js', import.meta.url);
const controllerURL = new URL('../dist/frontend/controller.js', import.meta.url);
const clients = await import(clientURL.href);
const controllers = await import(controllerURL.href);
const A = 'a'.repeat(64), B = 'b'.repeat(64);
const fixture = name => JSON.parse(readFileSync(new URL(`../site/data/${name}.json`, import.meta.url), 'utf8'));
const response = value => ({ ok: true, json: async () => value });
function menu(id = A) { const value = fixture('menu'); value.source.sha256 = id; return value; }
function cache(name = 'Translated A') {
  const value = fixture('translations'); const key = Object.keys(value.entries)[0];
  value.entries[key].en.name = name; return value;
}
function translatedName(state) { const entries = state.translations.value.entries; return entries[Object.keys(entries)[0]].en.name; }
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const clock = () => new Date('2026-09-28T12:00:00Z');


test('one manifest pins the concurrent pair and menu becomes ready before translations', async () => {
  const translation = deferred(), menuReady = deferred(); let pointer = A;
  const paths = [];
  const client = clients.createDataClient(async path => {
    paths.push(path);
    if (path === 'data/current.json') return response({ schema_version: 1, release_id: pointer });
    if (path.endsWith('/menu.json')) return response(menu(A));
    return { ok: true, json: () => translation.promise };
  });
  const controller = controllers.createMenuController({ client, clock, onChange: state => {
    if (state.menu.status === 'ready') menuReady.resolve(state);
  } });
  const loading = controller.load();
  const ready = await menuReady.promise;
  assert.equal(ready.menu.value.source.sha256, A); assert.equal(ready.translations.status, 'loading');
  pointer = B;
  translation.resolve(cache()); await loading;
  assert.equal(controller.getState().menu.value.source.sha256, A);
  assert.equal(translatedName(controller.getState()), 'Translated A');
  assert.equal(paths.filter(p => p === 'data/current.json').length, 1);
  assert.deepEqual(paths.slice(1), [`data/releases/${A}/menu.json`, `data/releases/${A}/translations.json`]);
});

test('ordinary pointer and pair failures keep independent loading states', async () => {
  for (const failure of ['pointer', 'menu', 'translations']) {
    const paths = [];
    const controller = controllers.createMenuController({ clock, onChange: () => {}, client: clients.createDataClient(async path => {
      paths.push(path);
      if (path === 'data/current.json') return response(failure === 'pointer' ? { schema_version: 1, release_id: '../bad' } : { schema_version: 1, release_id: A });
      if (path.endsWith('/menu.json')) return failure === 'menu' ? { ok: false, json: async () => { throw new Error('must not parse failed HTTP'); } } : response(menu());
      return failure === 'translations' ? { ok: true, json: async () => { throw new Error('invalid translation JSON'); } } : response(cache());
    }) });
    await controller.load(); const state = controller.getState();
    if (failure === 'pointer') {
      assert.equal(state.menu.status, 'failed'); assert.equal(state.translations.status, 'failed'); assert.deepEqual(paths, ['data/current.json']);
    } else if (failure === 'menu') {
      assert.equal(state.menu.status, 'failed'); assert.match(state.menu.error, /HTTP/); assert.equal(state.translations.status, 'ready');
    } else {
      assert.equal(state.menu.status, 'ready'); assert.equal(state.translations.status, 'failed'); assert.match(state.translations.error, /invalid translation JSON/);
      assert.equal(state.menu.value.days[0].meals[0].name_de, menu().days[0].meals[0].name_de);
    }
  }
});

test('older load finishing last cannot replace either current branch or report an obsolete failure', async () => {
  for (const oldFails of [false, true]) {
    const oldMenu = deferred(), oldCache = deferred(), oldStarted = deferred(); let count = 0;
    const controller = controllers.createMenuController({ clock, onChange: () => {}, client: clients.createDataClient(async path => {
      if (path === 'data/current.json') return response({ schema_version: 1, release_id: ++count === 1 ? A : B });
      if (path.includes(A)) {
        if (path.endsWith('/translations.json')) oldStarted.resolve();
        return { ok: true, json: () => path.endsWith('/menu.json') ? oldMenu.promise : oldCache.promise };
      }
      return response(path.endsWith('/menu.json') ? menu(B) : cache('Translated B'));
    }) });
    const first = controller.load(); await oldStarted.promise;
    await controller.load(); const current = controller.getState();
    assert.equal(current.menu.value.source.sha256, B); assert.equal(translatedName(current), 'Translated B');
    if (oldFails) { oldMenu.reject(new Error('obsolete menu')); oldCache.reject(new Error('obsolete cache')); }
    else { oldMenu.resolve(menu(A)); oldCache.resolve(cache('Translated A')); }
    await first; assert.equal(controller.getState(), current);
  }
});
