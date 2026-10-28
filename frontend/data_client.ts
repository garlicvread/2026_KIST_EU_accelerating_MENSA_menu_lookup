import { validateMenu, validateReleaseManifest, validateTranslations } from './contracts.js';
import type { MenuSnapshot, ReleaseManifest, TranslationCache } from './contracts.js';

export interface JsonResponse { readonly ok: boolean; json(): Promise<unknown> }
export type FetchPort = (path: string) => Promise<JsonResponse>;
export interface DataClient {
  loadManifest(): Promise<ReleaseManifest>;
  loadMenu(releaseId: string): Promise<MenuSnapshot>;
  loadTranslations(releaseId: string): Promise<TranslationCache>;
}
export interface DataClientOptions { readonly driveFileId?: string }
interface DriveBundle { readonly menu: unknown; readonly translations: unknown }

/** createDataClient는 Drive 공개 묶음 또는 현재 문서 기준의 JSON 파일을 읽습니다. Drive 모드에서는 loadManifest가 받은 묶음을 게시본 ID별로 보관하고 두 분기를 각각 검사하므로 번역 오류가 독일어 메뉴를 차단하지 않습니다. HTTP 요청 옵션과 통신 정책은 fetchPort 호출자가 선택합니다. */
export function createDataClient(fetchPort: FetchPort, options: DataClientOptions = {}): DataClient {
  const fileId = options.driveFileId;
  if (fileId !== undefined && !/^[A-Za-z0-9_-]{10,200}$/u.test(fileId)) throw new Error('Invalid Drive file ID');
  async function read(path: string): Promise<unknown> {
    const response = await fetchPort(path);
    if (!response.ok) throw new Error(`HTTP request failed: ${path}`);
    return response.json();
  }
  if (fileId !== undefined) {
    const bundles = new Map<string, DriveBundle>();
    function selected(releaseId: string): DriveBundle {
      validateReleaseManifest({ schema_version: 1, release_id: releaseId });
      const bundle = bundles.get(releaseId);
      if (bundle === undefined) throw new Error('Drive release has not been loaded');
      return bundle;
    }
    return {
      async loadManifest() {
        const input = await read(`https://drive.usercontent.google.com/download?id=${fileId}&export=download&authuser=0&confirm=t`);
        if (typeof input !== 'object' || input === null || Array.isArray(input)) throw new Error('Invalid Drive bundle');
        const bundle = input as Record<string, unknown>;
        const fields = ['schema_version', 'release_id', 'menu', 'translations'];
        if (Object.keys(bundle).length !== fields.length || fields.some(key => !Object.hasOwn(bundle, key))) throw new Error('Invalid Drive bundle fields');
        const manifest = validateReleaseManifest({ schema_version: bundle.schema_version, release_id: bundle.release_id });
        // 같은 ID의 첫 묶음을 유지하여 다른 load 호출이 이미 선택한 분기를 바꾸지 않습니다.
        if (!bundles.has(manifest.release_id)) bundles.set(manifest.release_id, { menu: bundle.menu, translations: bundle.translations });
        return manifest;
      },
      async loadMenu(releaseId) { return validateMenu(selected(releaseId).menu); },
      async loadTranslations(releaseId) { return validateTranslations(selected(releaseId).translations); },
    };
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
