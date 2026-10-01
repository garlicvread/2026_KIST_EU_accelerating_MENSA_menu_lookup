import type { Language, MenuDay } from './contracts.js';
import type { AppEvent, AppState } from './state.js';
import { BRAND_NAME, copyFor, isCopyKey, LOCALES } from './copy.js';
import { createCards } from './cards.js';
import { element, required, setText, sourceLink, syncChildren } from './dom.js';
import { berlinToday, groupWeeks, menuFreshness, selectedWeek, visibleMeals } from './selectors.js';

export function dateLabel(date: string, language: Language, options: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' }): string {
  return new Intl.DateTimeFormat(LOCALES[language], { ...options, timeZone: 'Europe/Berlin' }).format(new Date(`${date}T12:00:00Z`));
}
export function rangeLabel(days: readonly MenuDay[], language: Language): string {
  const first = days[0], last = days.at(-1); if (!first || !last) throw new Error('Missing published week');
  // 같은 주의 월·연도를 반복하지 않아 좁은 화면에서도 날짜 범위를 한 줄로 읽을 수 있습니다.
  // 연도가 바뀌는 주에는 양쪽 연도를 표시하여 서로 다른 해의 날짜를 구별합니다.
  const options: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', timeZone: 'Europe/Berlin' };
  if (first.date.slice(0, 4) !== last.date.slice(0, 4)) options.year = 'numeric';
  return new Intl.DateTimeFormat(LOCALES[language], options).formatRange(new Date(`${first.date}T12:00:00Z`), new Date(`${last.date}T12:00:00Z`));
}
export function createRenderer(options: { document: Document; clock: () => Date; retry: () => Promise<void>; navigate: (event: AppEvent) => void; details: (id: string, open: boolean) => void }) {
  const document = options.document;
  const cards = createCards(document, options.details);
  const daily = element(document, 'div', 'meal-grid'), weekly = element(document, 'div', 'weekly-grid');
  const empty = element(document, 'p', 'empty-state');
  const loading = element(document, 'div', 'loading-state'); loading.setAttribute('role', 'status');
  const loadingText = element(document, 'p'); const dot = element(document, 'span', 'loading-dot'); dot.setAttribute('aria-hidden', 'true'); syncChildren(loading, [dot, loadingText]);
  const error = element(document, 'div', 'error-state'); error.setAttribute('role', 'alert');
  const errorTitle = element(document, 'h2'), errorBody = element(document, 'p');
  const retry = element(document, 'button', 'retry-button'); retry.setAttribute('type', 'button'); retry.addEventListener('click', () => options.retry());
  const errorSource = sourceLink(document, ''); syncChildren(error, [errorTitle, errorBody, retry, errorSource]);
  const dates = new Map<string, { button: HTMLButtonElement; weekday: HTMLSpanElement; number: HTMLSpanElement; today: HTMLSpanElement }>();
  const dateOptions = new Map<string, HTMLOptionElement>();
  const sections = new Map<string, { section: HTMLElement; header: HTMLDivElement; heading: HTMLHeadingElement; button: HTMLButtonElement; label: HTMLSpanElement; status: HTMLSpanElement; meals: HTMLDivElement; empty: HTMLParagraphElement }>();

  function applyLanguage(state: AppState): void {
    const copy = copyFor(state.language); document.documentElement.lang = state.language; document.title = `${BRAND_NAME} ${copy.brandSubtitle} · Saarbrücken`;
    required(document, 'brand-link', 'a').setAttribute('aria-label', document.title);
    for (const node of document.querySelectorAll('[data-i18n]')) { const key = node.getAttribute('data-i18n'); if (isCopyKey(key) && node.textContent !== copy[key]) node.textContent = copy[key]; }
    for (const node of document.querySelectorAll('[data-i18n-aria]')) { const key = node.getAttribute('data-i18n-aria'); if (isCopyKey(key)) node.setAttribute('aria-label', copy[key]); }
    required(document, 'language', 'select').value = state.language; required(document, 'price-group', 'select').value = state.group;
    for (const node of document.querySelectorAll('[data-view]')) node.setAttribute('aria-pressed', String(node.getAttribute('data-view') === state.view));
  }
  function hideNavigation(): void {
    required(document, 'navigation', 'section').hidden = true; required(document, 'menu-toolbar', 'div').hidden = true;
    required(document, 'freshness-note', 'div').hidden = true; required(document, 'translation-note', 'div').hidden = true;
  }
  function renderError(state: AppState): void {
    hideNavigation(); const copy = copyFor(state.language); const content = required(document, 'menu-content', 'div');
    content.setAttribute('aria-busy', 'false'); setText(errorTitle, copy.errorTitle); setText(errorBody, copy.errorBody); setText(retry, copy.retry); setText(errorSource, copy.source); syncChildren(content, [error]);
  }
  function navigation(state: AppState, today: string): void {
    if (state.menu.status !== 'ready') return;
    const menu = state.menu.value, copy = copyFor(state.language); const weekList = groupWeeks(menu.days), week = selectedWeek(menu, state.selectedDate);
    if (!week) throw new Error('Missing selected published week');
    const index = weekList.findIndex(item => item.key === week.key);
    const currentMonday = groupWeeks([{ date: today, meals: [] }])[0]?.key;
    const nextMonday = new Date(`${currentMonday}T12:00:00Z`); nextMonday.setUTCDate(nextMonday.getUTCDate() + 7);
    setText(required(document, 'week-label', 'p'), week.key === currentMonday ? copy.thisWeek : week.key === nextMonday.toISOString().slice(0, 10) ? copy.nextWeekLabel : copy.publishedWeek);
    setText(required(document, 'week-range', 'p'), rangeLabel(week.days, state.language));
    required(document, 'previous-week', 'button').disabled = index === 0; required(document, 'next-week', 'button').disabled = index === weekList.length - 1;
    const strip = required(document, 'day-strip', 'div'); strip.style.setProperty('--day-count', String(Math.min(5, week.days.length))); strip.hidden = state.view === 'week';
    const buttons = week.days.map(day => {
      let entry = dates.get(day.date);
      if (!entry) {
        const button = element(document, 'button', 'day-button'); button.setAttribute('type', 'button'); button.setAttribute('data-date', day.date);
        button.addEventListener('click', () => options.navigate({ type: 'date', date: day.date }));
        entry = { button, weekday: element(document, 'span', 'weekday'), number: element(document, 'span', 'day-number'), today: element(document, 'span', 'today-dot') };
        entry.today.setAttribute('aria-hidden', 'true'); dates.set(day.date, entry);
      }
      setText(entry.weekday, dateLabel(day.date, state.language, { weekday: 'short' })); setText(entry.number, String(Number(day.date.slice(-2))));
      entry.button.setAttribute('aria-pressed', String(day.date === state.selectedDate)); entry.button.setAttribute('aria-label', dateLabel(day.date, state.language, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' }));
      syncChildren(entry.button, day.date === today ? [entry.weekday, entry.number, entry.today] : [entry.weekday, entry.number]); return entry.button;
    });
    syncChildren(strip, buttons);
    const picker = required(document, 'date-picker', 'select');
    syncChildren(picker, menu.days.map(day => {
      let option = dateOptions.get(day.date); if (!option) { option = element(document, 'option'); option.value = day.date; dateOptions.set(day.date, option); }
      setText(option, dateLabel(day.date, state.language, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' })); return option;
    })); picker.value = state.selectedDate ?? '';
    const published = new Set(menu.days.map(day => day.date));
    for (const key of dates.keys()) if (!published.has(key)) dates.delete(key);
    for (const key of dateOptions.keys()) if (!published.has(key)) dateOptions.delete(key);
  }
  function weekSection(day: MenuDay, state: AppState, today: string): HTMLElement {
    let entry = sections.get(day.date);
    if (!entry) {
      const section = element(document, 'section', 'week-day'), header = element(document, 'div', 'week-day-header'), heading = element(document, 'h3');
      const button = element(document, 'button', 'week-day-button'); button.setAttribute('type', 'button'); button.setAttribute('data-date', day.date);
      button.addEventListener('click', () => options.navigate({ type: 'date', date: day.date }));
      const label = element(document, 'span'), arrow = element(document, 'span', '', '↗'); arrow.setAttribute('aria-hidden', 'true'); syncChildren(button, [label, arrow]); syncChildren(heading, [button]);
      entry = { section, header, heading, button, label, status: element(document, 'span', 'week-day-status'), meals: element(document, 'div', 'week-meals'), empty: element(document, 'p', 'empty-state') };
      syncChildren(section, [header, entry.meals]); sections.set(day.date, entry);
    }
    const copy = copyFor(state.language); setText(entry.label, dateLabel(day.date, state.language, { weekday: 'long', month: 'short', day: 'numeric' }));
    entry.button.setAttribute('aria-label', `${dateLabel(day.date, state.language, { weekday: 'long', month: 'long', day: 'numeric' })} · ${copy.openDay}`);
    setText(entry.status, day.date < today ? copy.pastMenu : copy.today); syncChildren(entry.header, day.date <= today ? [entry.heading, entry.status] : [entry.heading]);
    const meals = visibleMeals(day);
    setText(entry.empty, copy.empty); syncChildren(entry.meals, meals.length ? meals.map(meal => cards.render(meal, state, 'h4')) : [entry.empty]); return entry.section;
  }
  function announcement(state: AppState, prefix = ''): void {
    if (state.menu.status !== 'ready' || state.selectedDate === null) return;
    const week = selectedWeek(state.menu.value, state.selectedDate); const copy = copyFor(state.language);
    const selected = state.view === 'week' && week ? rangeLabel(week.days, state.language) : dateLabel(state.selectedDate, state.language, { weekday: 'long', month: 'short', day: 'numeric' });
    setText(required(document, 'live-status', 'p'), `${prefix ? prefix + ' ' : ''}${selected}. ${copy[state.group]}.`);
  }
  function render(state: AppState): boolean {
    try {
      applyLanguage(state); const content = required(document, 'menu-content', 'div');
      if (state.menu.status === 'failed') { renderError(state); return false; }
      if (state.menu.status !== 'ready') { hideNavigation(); content.setAttribute('aria-busy', 'true'); setText(loadingText, copyFor(state.language).loading); syncChildren(content, [loading]); return false; }
      const menu = state.menu.value, copy = copyFor(state.language); const now = options.clock(), today = berlinToday(now);
      const heading = required(document, 'menu-heading', 'h2'), day = menu.days.find(item => item.date === state.selectedDate), week = selectedWeek(menu, state.selectedDate);
      if (!day || !week) throw new Error('Missing selected published menu');
      navigation(state, today); required(document, 'navigation', 'section').hidden = false; required(document, 'menu-toolbar', 'div').hidden = false;
      setText(heading, state.view === 'week' ? copy.weekTitle : dateLabel(day.date, state.language, { weekday: 'long', month: 'short', day: 'numeric' }));
      const fresh = menuFreshness(menu, { selectedDate: state.selectedDate, view: state.view, today, now }); const note = required(document, 'freshness-note', 'div');
      note.hidden = !(fresh.expired || fresh.past || fresh.stale); setText(note, fresh.expired ? copy.expiredNotice : fresh.past ? copy.pastNotice : fresh.stale ? copy.staleNotice : '');
      const translationNote = required(document, 'translation-note', 'div'); translationNote.hidden = state.translations.status !== 'failed' || state.language === 'de'; setText(translationNote, copy.translationsFailed);
      cards.prune(menu); setText(empty, copy.empty);
      if (state.view === 'day') { const meals = visibleMeals(day); syncChildren(daily, meals.length ? meals.map(meal => cards.render(meal, state, 'h3')) : [empty]); syncChildren(content, [daily]); }
      else { syncChildren(weekly, week.days.map(item => weekSection(item, state, today))); syncChildren(content, [weekly]); }
      for (const key of sections.keys()) if (!menu.days.some(item => item.date === key)) sections.delete(key);
      content.setAttribute('aria-busy', 'false'); announcement(state); return true;
    } catch { renderError(state); return false; }
  }
  return { render, announcement };
}
