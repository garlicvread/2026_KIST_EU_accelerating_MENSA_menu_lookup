import { validateMenu, validateReleaseManifest, validateTranslations } from './contracts.js';
import type { MenuSnapshot, ReleaseManifest, TranslationCache } from './contracts.js';

export interface JsonResponse { readonly ok: boolean; readonly status?: number; json(): Promise<unknown> }
export class ReleaseNotFoundError extends Error {}
export type FetchPort = (path: string) => Promise<JsonResponse>;
export interface DataClient {
  loadManifest(): Promise<ReleaseManifest>;
  loadMenu(releaseId: string): Promise<MenuSnapshot>;
  loadTranslations(releaseId: string): Promise<TranslationCache>;
}
/** createDataClient는 같은 사이트의 게시 포인터와 그 ID에 고정된 메뉴·번역을 읽습니다. HTTP 요청 옵션은 fetchPort 호출자가 선택합니다. */
export function createDataClient(fetchPort: FetchPort): DataClient {
  async function read(path: string, release = false): Promise<unknown> {
    const response = await fetchPort(path);
    if (!response.ok) {
      if (release && response.status === 404) throw new ReleaseNotFoundError('HTTP release not found');
      throw new Error('HTTP request failed');
    }
    return response.json();
  }
  function releasePath(releaseId: string, file: 'menu.json' | 'translations.json'): string {
    const manifest = validateReleaseManifest({ schema_version: 1, release_id: releaseId });
    return `data/releases/${manifest.release_id}/${file}`;
  }
  return {
    async loadManifest() { return validateReleaseManifest(await read('data/current.json')); },
    async loadMenu(releaseId) { return validateMenu(await read(releasePath(releaseId, 'menu.json'), true)); },
    async loadTranslations(releaseId) { return validateTranslations(await read(releasePath(releaseId, 'translations.json'), true)); },
  };
}
