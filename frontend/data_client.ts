import { validateMenu, validateReleaseManifest, validateTranslations } from './contracts.js';
import type { MenuSnapshot, ReleaseManifest, TranslationCache } from './contracts.js';

export interface JsonResponse { readonly ok: boolean; json(): Promise<unknown> }
export type FetchPort = (path: string) => Promise<JsonResponse>;
export interface DataClient {
  loadManifest(): Promise<ReleaseManifest>;
  loadMenu(releaseId: string): Promise<MenuSnapshot>;
  loadTranslations(releaseId: string): Promise<TranslationCache>;
}

/** createDataClient는 현재 문서를 기준으로 JSON 파일 경로를 구성합니다. HTTP 요청 옵션과 통신 정책을 호출자가 선택할 수 있도록 실제 요청은 전달받은 fetchPort 함수에 맡깁니다. */
export function createDataClient(fetchPort: FetchPort): DataClient {
  async function read(path: string): Promise<unknown> {
    const response = await fetchPort(path);
    if (!response.ok) throw new Error(`HTTP request failed: ${path}`);
    return response.json();
  }
  function releasePath(releaseId: string, file: 'menu.json' | 'translations.json'): string {
    const manifest = validateReleaseManifest({ schema_version: 1, release_id: releaseId });
    return `data/releases/${manifest.release_id}/${file}`;
  }
  return {
    async loadManifest() { return validateReleaseManifest(await read('data/current.json')); },
    async loadMenu(releaseId) { return validateMenu(await read(releasePath(releaseId, 'menu.json'))); },
    async loadTranslations(releaseId) { return validateTranslations(await read(releasePath(releaseId, 'translations.json'))); },
  };
}
