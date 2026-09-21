const SOURCE_URL = 'https://www.stw-saarland.de/gastro/mensa-saarbruecken/';
const LANGUAGES = ['ko', 'en', 'de'];
const GROUPS = ['student', 'staff', 'guest'];
const LOCALES = { ko: 'ko-KR', en: 'en-GB', de: 'de-DE' };
const COPY = {
  en: {
    skip: 'Skip to menus', language: 'Language', eyebrow: 'YOUR CAMPUS LUNCH, AT A GLANCE', title: 'What’s for lunch?', heroNote: 'A good lunch. A little less deciding.', view: 'Menu view', dayView: 'By day', weekView: 'The whole week', schedule: 'Updated every Monday', navigation: 'Menu dates', previousWeek: 'Previous available week', nextWeek: 'Next available week', today: 'Today', chooseDate: 'Choose a date', allDates: 'All published dates', priceGroup: 'Prices for', student: 'Students', staff: 'Staff', guest: 'Guests', loading: 'Getting the menu ready…', footerTitle: 'A little clarity for lunchtime.', source: 'Original menu ↗', disclaimer: 'An independent menu viewer. Meals and prices may change; check the original menu for the latest details.', translationDisclaimer: 'Translations are a guide. Ingredient and allergen notices stay in the original German.', thisWeek: 'This week', nextWeekLabel: 'Next week', publishedWeek: 'Published week', weekTitle: 'The week at a glance', todayMenu: 'On the menu today', futureMenu: 'A little planning ahead', pastMenu: 'Past menu', dishes: 'dishes', details: 'Sides · ingredients / allergens', components: 'Sides & components', notices: 'Original ingredient & allergen notices', noNotices: 'No notices supplied in this source entry.', original: 'German original', fallback: 'Original German · translation unavailable', checkPrice: 'Check price ↗', checkPriceAria: 'Check the price on the original menu', vegan: 'Vegan', vegetarian: 'Vegetarian', empty: 'No meals are published for this date.', pastNotice: 'You are viewing a past menu. It is not today’s offering.', expiredNotice: 'All available menus are in the past. Check the original menu for current information.', staleNotice: 'This snapshot is more than a week old. Check the original menu for updates.', translationsFailed: 'Translations could not be loaded. The German menu is still available.', coverage: 'Published dates', updated: 'Last successful update', berlin: 'Berlin time', errorTitle: 'The menu is taking a break.', errorBody: 'We couldn’t load a valid menu right now. Try again, or visit the original menu.', retry: 'Try again', openDay: 'View day', todayUnavailable: 'Today is not published; showing the next available menu.', latestPast: 'Showing the latest available past menu.'
  },
  ko: {
    skip: '메뉴로 바로 이동', language: '언어', eyebrow: '캠퍼스의 점심을 한눈에', title: '점심, 뭐 먹지?', heroNote: '맛있는 점심. 조금 더 가벼운 고민.', view: '메뉴 보기 방식', dayView: '하루씩 보기', weekView: '한 주 보기', schedule: '매주 월요일 갱신', navigation: '메뉴 날짜', previousWeek: '이전 식단 주간', nextWeek: '다음 식단 주간', today: '오늘', chooseDate: '날짜 선택', allDates: '공개된 모든 날짜', priceGroup: '가격 기준', student: '학생', staff: '교직원', guest: '방문객', loading: '메뉴를 불러오는 중…', footerTitle: '점심 고민을 조금 더 가볍게.', source: '원본 식단표 ↗', disclaimer: '비공식 메뉴 안내. 메뉴와 가격은 변경될 수 있으며, 최신 정보는 원본 식단표에서 확인.', translationDisclaimer: '번역은 이해를 돕기 위한 안내. 식재료·알레르기 정보는 독일어 원문을 유지.', thisWeek: '이번 주', nextWeekLabel: '다음 주', publishedWeek: '공개된 주간 식단', weekTitle: '한 주 한눈에', todayMenu: '오늘의 점심', futureMenu: '미리 보는 점심', pastMenu: '지난 식단', dishes: '개 메뉴', details: '구성 · 식재료 / 알레르기', components: '구성 및 곁들임', notices: '식재료·알레르기 원문 정보', noNotices: '해당 원본 항목에 별도 표시 정보 없음.', original: '독일어 원문', fallback: '독일어 원문 · 번역 준비 중', checkPrice: '가격 확인 ↗', checkPriceAria: '원본 식단표에서 가격 확인', vegan: '비건', vegetarian: '채식', empty: '이 날짜에 공개된 메뉴가 없음.', pastNotice: '지난 날짜의 식단이며 오늘 제공되는 메뉴가 아님.', expiredNotice: '공개된 메뉴가 모두 지난 날짜의 식단. 최신 메뉴는 원본 식단표에서 확인.', staleNotice: '마지막 수집 후 일주일 이상 경과. 원본 식단표에서 최신 정보 확인.', translationsFailed: '번역을 불러오지 못해 독일어 원문으로 표시.', coverage: '공개된 식단', updated: '마지막 정상 갱신', berlin: '베를린 시간', errorTitle: '메뉴를 잠시 불러올 수 없음', errorBody: '유효한 식단을 불러오지 못함. 다시 시도하거나 원본 식단표에서 확인.', retry: '다시 시도', openDay: '하루 보기', todayUnavailable: '오늘 식단이 없어 다음 공개 날짜로 이동.', latestPast: '가장 최근에 공개된 지난 식단으로 이동.'
  },
  de: {
    skip: 'Zum Speiseplan', language: 'Sprache', eyebrow: 'DEIN CAMPUS-MITTAGESSEN AUF EINEN BLICK', title: 'Was gibt’s zu Mittag?', heroNote: 'Gutes Essen. Ein bisschen weniger Grübeln.', view: 'Ansicht', dayView: 'Tagesansicht', weekView: 'Wochenansicht', schedule: 'Jeden Montag aktualisiert', navigation: 'Menüdaten', previousWeek: 'Vorherige verfügbare Woche', nextWeek: 'Nächste verfügbare Woche', today: 'Heute', chooseDate: 'Datum wählen', allDates: 'Alle veröffentlichten Tage', priceGroup: 'Preise für', student: 'Studierende', staff: 'Bedienstete', guest: 'Gäste', loading: 'Der Speiseplan wird geladen…', footerTitle: 'Mehr Überblick für die Mittagspause.', source: 'Original-Speiseplan ↗', disclaimer: 'Unabhängige Menüansicht. Speisen und Preise können sich ändern. Aktuelle Angaben stehen im Original-Speiseplan.', translationDisclaimer: 'Übersetzungen dienen der Orientierung. Zutaten- und Allergenhinweise bleiben im deutschen Original.', thisWeek: 'Diese Woche', nextWeekLabel: 'Nächste Woche', publishedWeek: 'Veröffentlichte Woche', weekTitle: 'Die Woche im Überblick', todayMenu: 'Heute auf dem Speiseplan', futureMenu: 'Vorfreude auf die Mittagspause', pastMenu: 'Vergangener Speiseplan', dishes: 'Gerichte', details: 'Beilagen · Zutaten / Allergene', components: 'Beilagen & Bestandteile', notices: 'Originalhinweise zu Zutaten & Allergenen', noNotices: 'Keine Hinweise in diesem Quelleintrag.', original: 'Deutsches Original', fallback: 'Deutsches Original · Übersetzung nicht verfügbar', checkPrice: 'Preis prüfen ↗', checkPriceAria: 'Preis im Original-Speiseplan prüfen', vegan: 'Vegan', vegetarian: 'Vegetarisch', empty: 'Für diesen Tag sind keine Gerichte veröffentlicht.', pastNotice: 'Dieser Speiseplan liegt in der Vergangenheit und gilt nicht für heute.', expiredNotice: 'Alle verfügbaren Speisepläne liegen in der Vergangenheit. Aktuelle Angaben stehen im Original-Speiseplan.', staleNotice: 'Dieser Stand ist über eine Woche alt. Bitte den Original-Speiseplan prüfen.', translationsFailed: 'Übersetzungen konnten nicht geladen werden. Das deutsche Original ist weiterhin verfügbar.', coverage: 'Veröffentlichter Zeitraum', updated: 'Letzte erfolgreiche Aktualisierung', berlin: 'Berliner Zeit', errorTitle: 'Der Speiseplan macht kurz Pause.', errorBody: 'Der Speiseplan konnte gerade nicht geladen werden. Bitte erneut versuchen oder den Original-Speiseplan öffnen.', retry: 'Erneut versuchen', openDay: 'Tag ansehen', todayUnavailable: 'Heute ist kein Menü veröffentlicht; der nächste verfügbare Tag wird angezeigt.', latestPast: 'Der zuletzt veröffentlichte vergangene Speiseplan wird angezeigt.'
  }
};

export function berlinToday(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function validDate(date) {
  if (typeof date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const value = new Date(`${date}T12:00:00Z`);
  return !Number.isNaN(value.getTime()) && value.toISOString().slice(0, 10) === date;
}

function mondayOf(date) {
  const value = new Date(`${date}T12:00:00Z`);
  value.setUTCDate(value.getUTCDate() - (value.getUTCDay() + 6) % 7);
  return value.toISOString().slice(0, 10);
}

export function chooseDefaultDate(days, today = berlinToday()) {
  const dates = days.map(day => day.date).sort();
  return dates.find(date => date >= today) ?? dates.at(-1) ?? null;
}

export function groupWeeks(days) {
  const weeks = new Map();
  [...days].sort((a, b) => a.date.localeCompare(b.date)).forEach(day => {
    const key = mondayOf(day.date);
    if (!weeks.has(key)) weeks.set(key, { key, days: [] });
    weeks.get(key).days.push(day);
  });
  return [...weeks.values()];
}

export function menuFreshness(menu, { selectedDate, view, today = berlinToday(), now = new Date() }) {
  return {
    expired: menu.days.every(day => day.date < today),
    past: view === 'day' && selectedDate < today,
    stale: now.getTime() - Date.parse(menu.source.fetched_at) > 7 * 24 * 60 * 60 * 1000
  };
}

export function priceText(meal, group, language) {
  const cents = meal.prices?.[group];
  if (meal.price_status !== 'verified' || !Number.isSafeInteger(cents) || cents < 0) return null;
  return new Intl.NumberFormat(LOCALES[language] ?? LOCALES.en, { style: 'currency', currency: 'EUR' }).format(cents / 100);
}

export function translatedMeal(meal, cache, language) {
  const originalComponents = meal.components.map(component => component.name_de);
  const original = { name: meal.name_de, components: originalComponents, translated: false };
  if (language === 'de') return original;
  const entry = cache?.entries?.[meal.translation_key];
  const translation = entry?.[language];
  if (entry?.source?.name_de !== meal.name_de || JSON.stringify(entry?.source?.components) !== JSON.stringify(originalComponents)) return original;
  if (!translation || typeof translation.name !== 'string' || !translation.name.trim() || !Array.isArray(translation.components) || translation.components.length !== originalComponents.length || !translation.components.every(item => typeof item === 'string' && item.trim())) return original;
  return { name: translation.name, components: translation.components, translated: true };
}

function validateMealPrices(meal, date) {
  const provenance = meal.price_source;
  if (provenance?.date !== date || provenance?.category !== meal.category || provenance?.name !== meal.name_de) throw new Error('Price source does not identify this meal');
  if (meal.price_status === 'source_pending') {
    if (meal.prices !== null || provenance.raw !== null) throw new Error('Pending price contains an amount');
    return;
  }
  if (meal.price_status !== 'verified' || !meal.prices || typeof meal.prices !== 'object' || Object.keys(meal.prices).length !== GROUPS.length || typeof provenance.raw !== 'string') throw new Error('Invalid verified price');
  const matched = /^S:\s*(\d+)[,.](\d{2})\s*\|\s*M:\s*(\d+)[,.](\d{2})\s*\|\s*G:\s*(\d+)[,.](\d{2})$/.exec(provenance.raw);
  if (!matched || matched[0] !== provenance.raw) throw new Error('Malformed source price block');
  GROUPS.forEach((group, index) => {
    const cents = meal.prices[group];
    const sourceCents = Number(matched[index * 2 + 1]) * 100 + Number(matched[index * 2 + 2]);
    if (!Number.isSafeInteger(cents) || cents <= 0 || cents > 100000 || cents !== sourceCents) throw new Error('Price differs from its source amount');
  });
}

export function validateMenu(menu) {
  if (menu?.schema_version !== 1 || !Array.isArray(menu.days) || !menu.days.length || !Number.isFinite(Date.parse(menu.source?.fetched_at)) || !validDate(menu.coverage?.start) || !validDate(menu.coverage?.end)) throw new Error('Invalid menu snapshot');
  const dates = new Set();
  for (const day of menu.days) {
    if (!validDate(day.date) || dates.has(day.date) || !Array.isArray(day.meals)) throw new Error('Invalid menu day');
    dates.add(day.date);
    for (const meal of day.meals) {
      if (typeof meal.name_de !== 'string' || !meal.name_de.trim() || typeof meal.category !== 'string' || !Array.isArray(meal.components) || !meal.components.every(component => typeof component.name_de === 'string' && Array.isArray(component.notices) && component.notices.every(item => typeof item === 'string')) || !Array.isArray(meal.notices) || !meal.notices.every(item => typeof item === 'string')) throw new Error('Invalid menu item');
      validateMealPrices(meal, day.date);
    }
  }
  const sortedDates = [...dates].sort();
  if (menu.coverage.start !== sortedDates[0] || menu.coverage.end !== sortedDates.at(-1)) throw new Error('Invalid menu coverage');
  return menu;
}

function startApp() {
  const $ = id => document.getElementById(id);
  const readPreference = key => { try { return localStorage.getItem(`mensa-${key}`); } catch { return null; } };
  const writePreference = (key, value) => { try { localStorage.setItem(`mensa-${key}`, value); } catch { /* Private browsing may disable storage. */ } };
  const browserLanguages = navigator.languages ?? [navigator.language];
  const preferred = browserLanguages.map(language => language?.split('-')[0]).find(language => LANGUAGES.includes(language)) ?? 'en';
  const savedLanguage = readPreference('language');
  const savedGroup = readPreference('price-group');
  const state = { language: LANGUAGES.includes(savedLanguage) ? savedLanguage : preferred, group: GROUPS.includes(savedGroup) ? savedGroup : 'student', view: 'day', selectedDate: null, weekIndex: 0, menu: null, translations: null, translationsFailed: false, error: false, loading: true };
  const t = key => COPY[state.language][key];
  const element = (tag, className, text, attributes = {}) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    return node;
  };
  const sourceLink = (text, className = 'source-link') => element('a', className, text, { href: SOURCE_URL, target: '_blank', rel: 'noopener noreferrer' });
  const dateLabel = (date, options = { month: 'short', day: 'numeric' }) => new Intl.DateTimeFormat(LOCALES[state.language], { ...options, timeZone: 'Europe/Berlin' }).format(new Date(`${date}T12:00:00Z`));
  const rangeLabel = days => `${dateLabel(days[0].date)} – ${dateLabel(days.at(-1).date, { month: 'short', day: 'numeric', year: 'numeric' })}`;
  const weeks = () => groupWeeks(state.menu.days);

  function applyLanguage() {
    document.documentElement.lang = state.language;
    document.title = `${t('title')} · Mensa Saarbrücken`;
    document.querySelectorAll('[data-i18n]').forEach(node => { node.textContent = t(node.dataset.i18n); });
    document.querySelectorAll('[data-i18n-aria]').forEach(node => { node.setAttribute('aria-label', t(node.dataset.i18nAria)); });
    $('language').value = state.language;
    $('price-group').value = state.group;
  }

  function mealCard(meal, titleTag = 'h3') {
    const translated = translatedMeal(meal, state.translations, state.language);
    const article = element('article', 'meal-card');
    const top = element('div', 'card-top');
    const counter = element('p', 'counter', meal.category, { lang: 'de' });
    if (meal.location) counter.append(element('span', 'counter-location', ` · ${meal.location}`));
    top.append(counter);
    const originalNotices = meal.notices.map(notice => notice.trim().toLowerCase());
    if (originalNotices.includes('vegan')) top.append(element('span', 'meal-tag', t('vegan')));
    else if (originalNotices.includes('vegetarisch') || originalNotices.includes('vegetarian')) top.append(element('span', 'meal-tag', t('vegetarian')));
    const titleRow = element('div', 'title-row');
    const title = element(titleTag, 'meal-title', translated.name, { lang: translated.translated ? state.language : 'de' });
    titleRow.append(title);
    const price = priceText(meal, state.group, state.language);
    if (price !== null) titleRow.append(element('span', 'price', price));
    else {
      const link = sourceLink(t('checkPrice'), 'price-source-link');
      link.setAttribute('aria-label', t('checkPriceAria'));
      titleRow.append(link);
    }
    article.append(top, titleRow);
    if (translated.translated) article.append(element('p', 'original-title', meal.name_de, { lang: 'de' }));
    else if (state.language !== 'de') article.append(element('span', 'original-label', t('fallback')));
    if (translated.components.length) article.append(element('p', 'components-preview', translated.components.join(' · '), { lang: translated.translated ? state.language : 'de' }));
    const details = element('details', 'meal-details');
    details.append(element('summary', '', t('details')));
    const detail = element('div', 'detail-content');
    if (meal.components.length) {
      detail.append(element('span', 'detail-label', t('components')));
      const list = element('ul', 'component-list');
      meal.components.forEach((component, index) => {
        const item = element('li', '', translated.components[index], { lang: translated.translated ? state.language : 'de' });
        if (translated.translated) item.append(element('span', 'component-original', component.name_de, { lang: 'de' }));
        if (component.notices.length) item.append(element('span', 'component-notices', component.notices.join(' · '), { lang: 'de' }));
        list.append(item);
      });
      detail.append(list);
    }
    detail.append(element('span', 'detail-label', t('notices')));
    detail.append(element('p', 'meal-notices', meal.notices.length ? meal.notices.join(' · ') : t('noNotices'), meal.notices.length ? { lang: 'de' } : {}));
    details.append(detail);
    article.append(details);
    return article;
  }

  function fillMeals(container, day, titleTag = 'h3') {
    if (day.meals.length) day.meals.forEach(meal => container.append(mealCard(meal, titleTag)));
    else container.append(element('p', 'empty-state', t('empty')));
  }

  function renderNavigation(weekList, today) {
    const week = weekList[state.weekIndex];
    const currentMonday = mondayOf(today);
    const nextMonday = new Date(`${currentMonday}T12:00:00Z`);
    nextMonday.setUTCDate(nextMonday.getUTCDate() + 7);
    $('week-label').textContent = week.key === currentMonday ? t('thisWeek') : week.key === nextMonday.toISOString().slice(0, 10) ? t('nextWeekLabel') : t('publishedWeek');
    $('week-range').textContent = rangeLabel(week.days);
    $('previous-week').disabled = state.weekIndex === 0;
    $('next-week').disabled = state.weekIndex === weekList.length - 1;
    $('day-strip').style.setProperty('--day-count', Math.min(5, week.days.length));
    $('day-strip').hidden = state.view === 'week';
    $('day-strip').replaceChildren(...week.days.map(day => {
      const button = element('button', 'day-button', null, { type: 'button', 'data-date': day.date, 'aria-pressed': day.date === state.selectedDate, 'aria-label': dateLabel(day.date, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' }) });
      button.append(element('span', 'weekday', dateLabel(day.date, { weekday: 'short' })), element('span', 'day-number', Number(day.date.slice(-2))));
      if (day.date === today) button.append(element('span', 'today-dot', null, { 'aria-hidden': 'true' }));
      return button;
    }));
    $('date-picker').replaceChildren(...[...state.menu.days].sort((a, b) => a.date.localeCompare(b.date)).map(day => element('option', '', dateLabel(day.date, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' }), { value: day.date })));
    $('date-picker').value = state.selectedDate;
  }

  function render(announce = false) {
    applyLanguage();
    document.querySelectorAll('[data-view]').forEach(button => { button.setAttribute('aria-pressed', String(button.dataset.view === state.view)); });
    if (state.loading) return;
    if (state.error) {
      $('menu-content').replaceChildren();
      const error = element('div', 'error-state', null, { role: 'alert' });
      error.append(element('h2', '', t('errorTitle')), element('p', '', t('errorBody')));
      const retry = element('button', 'retry-button', t('retry'), { type: 'button' });
      retry.addEventListener('click', load);
      error.append(retry, sourceLink(t('source')));
      $('menu-content').append(error);
      return;
    }
    const today = berlinToday();
    const weekList = weeks();
    const week = weekList[state.weekIndex];
    const day = state.menu.days.find(item => item.date === state.selectedDate);
    renderNavigation(weekList, today);
    $('navigation').hidden = false;
    $('menu-toolbar').hidden = false;
    $('selection-kicker').textContent = state.view === 'week' ? `${week.days.reduce((sum, item) => sum + item.meals.length, 0)} ${t('dishes')}` : day.date < today ? t('pastMenu') : day.date === today ? t('todayMenu') : t('futureMenu');
    $('menu-heading').textContent = state.view === 'week' ? t('weekTitle') : dateLabel(day.date, { weekday: 'long', month: 'short', day: 'numeric' });
    const freshness = menuFreshness(state.menu, { selectedDate: day.date, view: state.view, today });
    $('freshness-note').hidden = !(freshness.expired || freshness.past || freshness.stale);
    $('freshness-note').textContent = freshness.expired ? t('expiredNotice') : freshness.past ? t('pastNotice') : freshness.stale ? t('staleNotice') : '';
    $('translation-note').hidden = !state.translationsFailed || state.language === 'de';
    $('translation-note').textContent = t('translationsFailed');
    $('menu-content').replaceChildren();
    if (state.view === 'day') {
      const grid = element('div', 'meal-grid');
      fillMeals(grid, day);
      $('menu-content').append(grid);
    } else {
      const grid = element('div', 'weekly-grid');
      week.days.forEach(item => {
        const section = element('section', 'week-day');
        const header = element('div', 'week-day-header');
        const heading = element('h3');
        const button = element('button', 'week-day-button', dateLabel(item.date, { weekday: 'long', month: 'short', day: 'numeric' }), { type: 'button', 'data-date': item.date, 'aria-label': `${dateLabel(item.date, { weekday: 'long', month: 'long', day: 'numeric' })} · ${t('openDay')}` });
        button.append(element('span', '', '↗', { 'aria-hidden': 'true' }));
        heading.append(button);
        header.append(heading);
        if (item.date < today || item.date === today) header.append(element('span', 'week-day-status', item.date < today ? t('pastMenu') : t('today')));
        const meals = element('div', 'week-meals');
        fillMeals(meals, item, 'h4');
        section.append(header, meals);
        grid.append(section);
      });
      $('menu-content').append(grid);
    }
    $('coverage').textContent = `${t('coverage')}: ${rangeLabel([{ date: state.menu.coverage.start }, { date: state.menu.coverage.end }])}`;
    const fetched = new Intl.DateTimeFormat(LOCALES[state.language], { timeZone: 'Europe/Berlin', year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(state.menu.source.fetched_at));
    $('updated').textContent = `${t('updated')}: ${fetched} · ${t('berlin')}`;
    if (announce) $('live-status').textContent = `${$('menu-heading').textContent}. ${t(state.group)}.`;
  }

  function selectDate(date, focus = false) {
    state.selectedDate = date;
    state.weekIndex = weeks().findIndex(week => week.days.some(day => day.date === date));
    state.view = 'day';
    render(true);
    if (focus) document.querySelector(`.day-button[data-date="${date}"]`)?.focus({ preventScroll: true });
  }

  async function fetchJson(path) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, { signal: controller.signal, cache: 'no-cache' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally { clearTimeout(timeout); }
  }

  async function load() {
    state.loading = true;
    state.error = false;
    $('navigation').hidden = true;
    $('menu-toolbar').hidden = true;
    $('freshness-note').hidden = true;
    $('translation-note').hidden = true;
    $('menu-content').setAttribute('aria-busy', 'true');
    const loading = element('div', 'loading-state', null, { role: 'status' });
    loading.append(element('span', 'loading-dot', null, { 'aria-hidden': 'true' }), element('p', '', t('loading'), { 'data-i18n': 'loading' }));
    $('menu-content').replaceChildren(loading);
    const [menuResult, translationResult] = await Promise.allSettled([fetchJson('data/menu.json'), fetchJson('data/translations.json')]);
    try {
      if (menuResult.status !== 'fulfilled') throw menuResult.reason;
      state.menu = validateMenu(menuResult.value);
      const cache = translationResult.status === 'fulfilled' ? translationResult.value : null;
      state.translationsFailed = cache?.schema_version !== 1 || !cache.entries || typeof cache.entries !== 'object' || Array.isArray(cache.entries);
      state.translations = state.translationsFailed ? null : cache;
      state.selectedDate = chooseDefaultDate(state.menu.days);
      state.weekIndex = weeks().findIndex(week => week.days.some(day => day.date === state.selectedDate));
    } catch { state.error = true; }
    state.loading = false;
    $('menu-content').setAttribute('aria-busy', 'false');
    render();
  }

  $('language').addEventListener('change', event => {
    state.language = event.target.value;
    writePreference('language', state.language);
    render(true);
  });
  $('price-group').addEventListener('change', event => {
    state.group = event.target.value;
    writePreference('price-group', state.group);
    render(true);
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || state.loading || state.error) return;
    if (button.dataset.view) { state.view = button.dataset.view; render(true); }
    else if (button.dataset.date) selectDate(button.dataset.date, true);
  });
  $('date-picker').addEventListener('change', event => selectDate(event.target.value));
  for (const [id, delta] of [['previous-week', -1], ['next-week', 1]]) {
    $(id).addEventListener('click', () => {
      const weekList = weeks();
      state.weekIndex = Math.max(0, Math.min(weekList.length - 1, state.weekIndex + delta));
      state.selectedDate = weekList[state.weekIndex].days[0].date;
      render(true);
    });
  }
  $('today-button').addEventListener('click', () => {
    const today = berlinToday();
    const date = chooseDefaultDate(state.menu.days, today);
    selectDate(date);
    if (date !== today) $('live-status').textContent = date > today ? t('todayUnavailable') : t('latestPast');
  });
  applyLanguage();
  load();
}

if (typeof document !== 'undefined') startApp();
