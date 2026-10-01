import { createApp } from './app.js';
import { createDataClient } from './data_client.js';
import type { FetchPort } from './data_client.js';
import { sourceLink } from './dom.js';

const fetchPort: FetchPort = async path => {
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(path, { signal: controller.signal, cache: 'no-cache' });
    if (!response.ok) { clearTimeout(timeout); return { ok: false, json: async () => { throw new Error('HTTP request failed'); } }; }
    return { ok: true, json: async () => { try { return await response.json(); } finally { clearTimeout(timeout); } } };
  } catch (error) { clearTimeout(timeout); throw error; }
};
function fallback(document: Document): void {
  const content = document.getElementById('menu-content'); if (!content) return;
  const alert = document.createElement('div'); alert.setAttribute('role', 'alert');
  const message = document.createElement('p'); message.textContent = 'Menu unavailable. Please open the original menu.';
  alert.append(message, sourceLink(document, 'Original menu ↗')); content.setAttribute('aria-busy', 'false'); content.replaceChildren(alert);
}
if (typeof document !== 'undefined') {
  try {
    let storage: Storage | undefined; try { storage = localStorage; } catch { /* 브라우저의 비공개 모드에서는 localStorage 접근이 차단될 수 있으므로 앱 시작 코드가 저장소 없이 화면을 구성합니다. */ }
    const languages = typeof navigator !== 'undefined' ? navigator.languages : [];
    const app = createApp({ document, client: createDataClient(fetchPort), clock: () => new Date(), browserLanguages: languages, ...(storage === undefined ? {} : { storage }) });
    void app.load().catch(() => fallback(document));
  } catch { fallback(document); }
}
