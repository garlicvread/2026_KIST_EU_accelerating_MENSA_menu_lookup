import { isLanguage, isPriceGroup, isViewMode } from './contracts.js';
import type { Language } from './contracts.js';
import type { DataClient } from './data_client.js';
import type { AppEvent, AppState } from './state.js';
import { createMenuController } from './controller.js';
import { copyFor } from './copy.js';
import { required } from './dom.js';
import { berlinToday } from './selectors.js';
import { createRenderer } from './render.js';
export interface MenuApp { load(): Promise<void>; getState(): AppState }
export function createApp(options: { document: Document; client: DataClient; clock: () => Date; storage?: Pick<Storage, 'getItem' | 'setItem'>; browserLanguages?: readonly string[] }): MenuApp {
  const document = options.document;
  const languageSelect = required(document, 'language', 'select'), groupSelect = required(document, 'price-group', 'select'), datePicker = required(document, 'date-picker', 'select');
  const previous = required(document, 'previous-week', 'button'), next = required(document, 'next-week', 'button'), todayButton = required(document, 'today-button', 'button');
  const viewButtons = Array.from(document.querySelectorAll('[data-view]'));
  if (viewButtons.some(button => button.tagName !== 'BUTTON' || !isViewMode(button.getAttribute('data-view'))) ||
      !['day', 'week'].every(view => viewButtons.some(button => button.getAttribute('data-view') === view))) throw new Error('Missing or invalid view control');
  function read(key: string): string | null { try { return options.storage?.getItem(`mensa-${key}`) ?? null; } catch { return null; } }
  function write(key: string, value: string): void { try { options.storage?.setItem(`mensa-${key}`, value); } catch { /* write는 저장소 사용이 선택 사항이므로 설정 값을 저장하지 못해도 메뉴 화면의 동작을 계속합니다. */ } }
  let preferred: Language = 'en';
  for (const language of options.browserLanguages ?? []) { const base = language.split('-')[0]?.toLowerCase(); if (isLanguage(base)) { preferred = base; break; } }
  const savedLanguage = read('language'), savedGroup = read('price-group');
  let render: (state: AppState) => boolean = () => false; let rendered = false;
  const controller = createMenuController({ client: options.client, clock: options.clock, language: isLanguage(savedLanguage) ? savedLanguage : preferred,
    group: isPriceGroup(savedGroup) ? savedGroup : 'student', onChange: state => { rendered = render(state); } });
  function navigate(event: AppEvent): void {
    const origin = document.activeElement;
    const before = controller.getState(); controller.dispatch(event); const state = controller.getState();
    if (!rendered || state.menu.status !== 'ready') return;
    let prefix = '';
    if (event.type === 'today' && state.selectedDate !== event.today && state.selectedDate !== null) prefix = state.selectedDate > event.today ? copyFor(state.language).todayUnavailable : copyFor(state.language).latestPast;
    if (state === before && !prefix) return;
    // navigate는 선택 결과를 라이브 영역에 알립니다. 날짜·주간·보기 컨트롤을 계속 조작할 수
    // 있도록 초점이나 화면 위치를 메뉴 본문으로 옮기지 않습니다.
    renderer.announcement(state, prefix);
    // 주간의 하루 보기처럼 원래 컨트롤이 제거된 경우에만, 화면을 움직이지 않고 계속
    // 남아 있는 날짜 선택기로 초점을 돌려 키보드 탐색을 이어갈 수 있게 합니다.
    if (origin && !document.documentElement.contains(origin)) datePicker.focus({ preventScroll: true });
  }
  const renderer = createRenderer({ document, clock: options.clock, retry: () => controller.load(), navigate, details: (id, open) => controller.dispatch({ type: 'details', id, open }) });
  render = renderer.render;
  languageSelect.addEventListener('change', () => {
    if (isLanguage(languageSelect.value)) { write('language', languageSelect.value); controller.dispatch({ type: 'language', language: languageSelect.value }); }
    else languageSelect.value = controller.getState().language;
  });
  groupSelect.addEventListener('change', () => {
    if (isPriceGroup(groupSelect.value)) { write('price-group', groupSelect.value); controller.dispatch({ type: 'group', group: groupSelect.value }); }
    else groupSelect.value = controller.getState().group;
  });
  datePicker.addEventListener('change', () => { navigate({ type: 'date', date: datePicker.value }); datePicker.value = controller.getState().selectedDate ?? ''; });
  previous.addEventListener('click', () => navigate({ type: 'week', offset: -1 })); next.addEventListener('click', () => navigate({ type: 'week', offset: 1 }));
  todayButton.addEventListener('click', () => navigate({ type: 'today', today: berlinToday(options.clock()) }));
  for (const button of viewButtons) button.addEventListener('click', () => { const view = button.getAttribute('data-view'); if (isViewMode(view)) navigate({ type: 'view', view }); });
  render(controller.getState());
  return { load: () => controller.load(), getState: () => controller.getState() };
}
