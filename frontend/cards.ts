import type { Language, Meal, MenuSnapshot, TranslationCache } from './contracts.js';
import type { AppState } from './state.js';
import { copyFor } from './copy.js';
import { priceText, translatedMeal, translatedNotices } from './display.js';
import { element, setText, sourceLink, syncChildren } from './dom.js';

function noticeOwner(document: Document, className: string) {
  const list = element(document, 'ul', `notice-list ${className}`);
  const rows: { item: HTMLLIElement; text: HTMLSpanElement; original: HTMLSpanElement; fallback: HTMLSpanElement }[] = [];
  return { list, update(notices: readonly string[], cache: TranslationCache | null, language: Language) {
    const children = translatedNotices(notices, cache, language).map((notice, index) => {
      let row = rows[index];
      if (!row) { row = { item: element(document, 'li', 'notice-pair'), text: element(document, 'span', 'notice-translation'), original: element(document, 'span', 'notice-original'), fallback: element(document, 'span', 'notice-fallback') }; rows[index] = row; }
      setText(row.text, notice.text); row.text.setAttribute('lang', notice.translated ? language : 'de');
      setText(row.original, notice.original); row.original.setAttribute('lang', 'de');
      setText(row.fallback, copyFor(language).fallback); row.fallback.setAttribute('lang', language);
      syncChildren(row.item, notice.translated ? [row.text, row.original] : language === 'de' ? [row.text] : [row.text, row.fallback]);
      return row.item;
    });
    syncChildren(list, children); rows.length = children.length;
  } };
}
function componentOwner(document: Document) {
  const group = element(document, 'li', 'component-group'); const heading = element(document, 'div', 'component-heading');
  const name = element(document, 'span', 'component-name'); const original = element(document, 'span', 'component-original');
  const notices = element(document, 'div', 'component-notice-group'); const label = element(document, 'span', 'detail-label');
  const warnings = noticeOwner(document, 'component-notices'); syncChildren(notices, [label, warnings.list]);
  return { group, heading, name, original, notices, label, warnings };
}
function cardOwner(document: Document, id: string, onDetails: (id: string, open: boolean) => void) {
  const article = element(document, 'article', 'meal-card'); const top = element(document, 'div', 'card-top');
  const counter = element(document, 'p', 'counter'); const location = element(document, 'span', 'counter-location');
  const category = element(document, 'span'); counter.setAttribute('lang', 'de');
  const tag = element(document, 'span', 'meal-tag'); const titleRow = element(document, 'div', 'title-row');
  let title = element(document, 'h3', 'meal-title'); const price = element(document, 'span', 'price'); const priceLink = sourceLink(document, '', 'price-source-link');
  const originalTitle = element(document, 'p', 'original-title'); originalTitle.setAttribute('lang', 'de');
  const fallback = element(document, 'span', 'original-label');
  const mealNotices = element(document, 'div', 'meal-notice-group'); const warnings = noticeOwner(document, 'meal-notices'); syncChildren(mealNotices, [warnings.list]);
  const details = element(document, 'details', 'meal-details'); const summary = element(document, 'summary');
  const body = element(document, 'div', 'detail-content'); const components = element(document, 'ul', 'component-list');
  syncChildren(body, [components]); syncChildren(details, [summary, body]);
  details.addEventListener('toggle', () => onDetails(id, details.open));
  const rows: ReturnType<typeof componentOwner>[] = [];
  return { article, update(meal: Meal, state: AppState, titleTag: 'h3' | 'h4') {
    const language = state.language; const copy = copyFor(language); const cache = state.translations.status === 'ready' ? state.translations.value : null;
    const translated = translatedMeal(meal, cache, language);
    setText(category, meal.category); setText(location, ` · ${meal.location}`); syncChildren(counter, [category, location]);
    const labels = meal.notices.map(notice => notice.trim().toLowerCase());
    const diet = labels.includes('vegan') ? copy.vegan : labels.some(notice => notice === 'vegetarisch' || notice === 'vegetarian') ? copy.vegetarian : null;
    if (diet !== null) setText(tag, diet); syncChildren(top, diet === null ? [counter] : [counter, tag]);
    if (title.tagName !== titleTag.toUpperCase()) title = element(document, titleTag, 'meal-title');
    setText(title, translated.name); title.setAttribute('lang', translated.translated ? language : 'de');
    const amount = priceText(meal, state.group, language);
    if (amount !== null) setText(price, amount); setText(priceLink, copy.checkPrice); priceLink.setAttribute('aria-label', copy.checkPriceAria);
    syncChildren(titleRow, [title, amount === null ? priceLink : price]);
    setText(originalTitle, meal.name_de); setText(fallback, copy.fallback); fallback.setAttribute('lang', language);
    warnings.update(meal.notices, cache, language); mealNotices.setAttribute('aria-label', copy.notices);
    setText(summary, `${copy.componentDetails} · ${meal.components.length}`);
    const componentNodes = meal.components.map((component, index) => {
      let row = rows[index]; if (!row) { row = componentOwner(document); rows[index] = row; }
      setText(row.name, translated.components[index] ?? component.name_de); row.name.setAttribute('lang', translated.translated ? language : 'de');
      setText(row.original, component.name_de); row.original.setAttribute('lang', 'de');
      syncChildren(row.heading, translated.translated ? [row.name, row.original] : [row.name]);
      setText(row.label, copy.notices); row.warnings.update(component.notices, cache, language);
      syncChildren(row.group, component.notices.length ? [row.heading, row.notices] : [row.heading]); return row.group;
    });
    syncChildren(components, componentNodes); rows.length = componentNodes.length;
    details.open = state.expandedIds.includes(id);
    const children: HTMLElement[] = [top, titleRow];
    if (translated.translated) children.push(originalTitle); else if (language !== 'de') children.push(fallback);
    if (meal.notices.length) children.push(mealNotices); if (meal.components.length) children.push(details);
    syncChildren(article, children); return article;
  } };
}
export function createCards(document: Document, onDetails: (id: string, open: boolean) => void) {
  const cards = new Map<string, ReturnType<typeof cardOwner>>();
  return {
    render(meal: Meal, state: AppState, titleTag: 'h3' | 'h4'): HTMLElement {
      let card = cards.get(meal.id); if (!card) { card = cardOwner(document, meal.id, onDetails); cards.set(meal.id, card); }
      return card.update(meal, state, titleTag);
    },
    prune(menu: MenuSnapshot): void {
      const ids = new Set(menu.days.flatMap(day => day.meals.map(meal => meal.id)));
      for (const id of cards.keys()) if (!ids.has(id)) cards.delete(id);
    },
  };
}
