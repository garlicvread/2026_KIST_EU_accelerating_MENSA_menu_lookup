import type { Language, MenuSnapshot, PriceGroup, TranslationCache } from './contracts.js';
import type { DataClient } from './data_client.js';
import { berlinToday } from './selectors.js';
import { createInitialState, transition } from './state.js';
import type { AppEvent, AppState } from './state.js';

export interface MenuController {
  getState(): AppState;
  dispatch(event: AppEvent): void;
  load(): Promise<void>;
}
function errorText(error: unknown): string { return error instanceof Error ? error.message : String(error); }

/** createMenuController는 메뉴와 번역에 같은 게시본 ID를 사용하도록 load 호출마다 선택 파일을 한 번 읽습니다. 메뉴와 번역은 각각 불러오며, 생성 시에는 외부 작업을 실행하지 않습니다. */
export function createMenuController(options: {
  client: DataClient; clock: () => Date; onChange: (state: AppState) => void;
  language?: Language; group?: PriceGroup;
}): MenuController {
  let state = createInitialState(options);
  let sequence = 0;
  function dispatch(event: AppEvent): void {
    const next = transition(state, event);
    if (next !== state) { state = next; options.onChange(state); }
  }
  function emit(current: number, event: AppEvent): void { if (current === sequence) dispatch(event); }
  async function menuBranch(current: number, id: string): Promise<void> {
    let menu: MenuSnapshot;
    let today: string;
    try {
      menu = await options.client.loadMenu(id);
      if (current !== sequence) return;
      today = berlinToday(options.clock());
    } catch (error) {
      emit(current, { type: 'menu-failed', error: errorText(error) });
      return;
    }
    emit(current, { type: 'menu-ready', menu, today });
  }
  async function translationBranch(current: number, id: string): Promise<void> {
    let cache: TranslationCache;
    try { cache = await options.client.loadTranslations(id); }
    catch (error) { emit(current, { type: 'translations-failed', error: errorText(error) }); return; }
    emit(current, { type: 'translations-ready', cache });
  }
  async function load(): Promise<void> {
    const current = ++sequence;
    dispatch({ type: 'load-start' });
    if (current !== sequence) return;
    let id: string;
    try { id = (await options.client.loadManifest()).release_id; }
    catch (error) {
      emit(current, { type: 'menu-failed', error: errorText(error) });
      emit(current, { type: 'translations-failed', error: errorText(error) });
      return;
    }
    if (current !== sequence) return;
    await Promise.all([menuBranch(current, id), translationBranch(current, id)]);
  }
  return { getState: () => state, dispatch, load };
}
