import type { Language, MenuSnapshot, PriceGroup, RequestState, TranslationCache, ViewMode } from './contracts.js';
import { chooseDefaultDate, groupWeeks } from './selectors.js';

export interface AppState {
  readonly language: Language;
  readonly group: PriceGroup;
  readonly view: ViewMode;
  readonly menu: RequestState<MenuSnapshot>;
  readonly translations: RequestState<TranslationCache>;
  readonly selectedDate: string | null;
  readonly expandedIds: readonly string[];
}
export type AppEvent =
  | { readonly type: 'load-start' }
  | { readonly type: 'menu-ready'; readonly menu: MenuSnapshot; readonly today: string }
  | { readonly type: 'menu-failed'; readonly error: string }
  | { readonly type: 'translations-ready'; readonly cache: TranslationCache }
  | { readonly type: 'translations-failed'; readonly error: string }
  | { readonly type: 'language'; readonly language: Language }
  | { readonly type: 'group'; readonly group: PriceGroup }
  | { readonly type: 'view'; readonly view: ViewMode }
  | { readonly type: 'date'; readonly date: string }
  | { readonly type: 'week'; readonly offset: -1 | 1 }
  | { readonly type: 'today'; readonly today: string }
  | { readonly type: 'details'; readonly id: string; readonly open: boolean };

export function createInitialState(options: { language?: Language; group?: PriceGroup } = {}): AppState {
  return { language: options.language ?? 'en', group: options.group ?? 'student', view: 'day',
    menu: { status: 'idle' }, translations: { status: 'idle' }, selectedDate: null, expandedIds: [] };
}
function mealIds(menu: MenuSnapshot): Set<string> {
  return new Set(menu.days.flatMap(day => day.meals.map(meal => meal.id)));
}
function selectDate(state: AppState, date: string | null, view: ViewMode): AppState {
  return state.selectedDate === date && state.view === view ? state : { ...state, selectedDate: date, view };
}

/** transition은 AppEvent로 확인된 이벤트로 화면 데이터를 갱신합니다. createApp은 이 함수에 이벤트를 전달하기 전에 입력 요소의 값을 검사하여 허용된 값으로 좁힙니다. */
export function transition(state: AppState, event: AppEvent): AppState {
  switch (event.type) {
    case 'load-start':
      return state.menu.status === 'loading' && state.translations.status === 'loading' ? state :
        { ...state, menu: { status: 'loading' }, translations: { status: 'loading' } };
    case 'menu-ready': {
      const selectedDate = event.menu.days.some(day => day.date === state.selectedDate) ? state.selectedDate : chooseDefaultDate(event.menu.days, event.today);
      const ids = mealIds(event.menu);
      const retained = state.expandedIds.filter(id => ids.has(id));
      const expandedIds = retained.length === state.expandedIds.length ? state.expandedIds : retained;
      if (state.menu.status === 'ready' && state.menu.value === event.menu && selectedDate === state.selectedDate && expandedIds === state.expandedIds) return state;
      return { ...state, menu: { status: 'ready', value: event.menu }, selectedDate, expandedIds };
    }
    case 'menu-failed':
      return state.menu.status === 'failed' && state.menu.error === event.error ? state : { ...state, menu: { status: 'failed', error: event.error } };
    case 'translations-ready':
      return state.translations.status === 'ready' && state.translations.value === event.cache ? state : { ...state, translations: { status: 'ready', value: event.cache } };
    case 'translations-failed':
      return state.translations.status === 'failed' && state.translations.error === event.error ? state : { ...state, translations: { status: 'failed', error: event.error } };
    case 'language': return state.language === event.language ? state : { ...state, language: event.language };
    case 'group': return state.group === event.group ? state : { ...state, group: event.group };
    case 'view': return state.view === event.view ? state : { ...state, view: event.view };
    case 'date':
      if (state.menu.status !== 'ready' || !state.menu.value.days.some(day => day.date === event.date)) return state;
      return selectDate(state, event.date, 'day');
    case 'week': {
      if (state.menu.status !== 'ready') return state;
      const weeks = groupWeeks(state.menu.value.days);
      const index = weeks.findIndex(week => week.days.some(day => day.date === state.selectedDate));
      if (index === -1) return state;
      const date = weeks[index + event.offset]?.days[0]?.date;
      return date === undefined ? state : selectDate(state, date, state.view);
    }
    case 'today':
      return state.menu.status !== 'ready' ? state : selectDate(state, chooseDefaultDate(state.menu.value.days, event.today), 'day');
    case 'details': {
      if (state.menu.status !== 'ready' || !mealIds(state.menu.value).has(event.id)) return state;
      if (state.expandedIds.includes(event.id) === event.open) return state;
      return { ...state, expandedIds: event.open ? [...state.expandedIds, event.id] : state.expandedIds.filter(id => id !== event.id) };
    }
  }
}
