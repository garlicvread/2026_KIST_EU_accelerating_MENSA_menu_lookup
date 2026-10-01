import type { Language, Meal, PriceGroup, TranslationCache } from './contracts.js';
import { LOCALES } from './copy.js';
export interface DisplayMeal { readonly name: string; readonly components: readonly string[]; readonly translated: boolean }
export interface DisplayNotice { readonly original: string; readonly text: string; readonly translated: boolean }
function record(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>; // record는 필드 값이 아직 확인되지 않은 객체만 반환합니다. 아래의 표시 함수는 잘못된 번역을 표시하지 않도록 사용할 값을 각각 검사합니다.
}
function nonempty(value: unknown): value is string { return typeof value === 'string' && !!value.trim(); }
function strings(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const result: string[] = [];
  for (const item of value) { if (!nonempty(item)) return null; result.push(item); }
  return result;
}
export function priceText(meal: Meal, group: PriceGroup, language: Language): string | null {
  const cents = meal.prices?.[group];
  if (meal.price_status !== 'verified' || typeof cents !== 'number' || !Number.isSafeInteger(cents) || cents < 0) return null;
  return new Intl.NumberFormat(LOCALES[language], { style: 'currency', currency: 'EUR' }).format(cents / 100);
}
export function translatedMeal(meal: Meal, cache: TranslationCache | null, language: Language): DisplayMeal {
  const components = meal.components.map(component => component.name_de);
  const original: DisplayMeal = { name: meal.name_de, components, translated: false };
  if (language === 'de') return original;
  const entries = record(record(cache)?.entries);
  if (!entries || !Object.hasOwn(entries, meal.translation_key)) return original;
  const entry = record(entries[meal.translation_key]); const source = record(entry?.source);
  const sourceComponents = strings(source?.components);
  if (source?.name_de !== meal.name_de || !sourceComponents || sourceComponents.length !== components.length || sourceComponents.some((name, index) => name !== components[index])) return original;
  const translation = record(entry?.[language]); const translatedComponents = strings(translation?.components);
  if (!nonempty(translation?.name) || !translatedComponents || translatedComponents.length !== components.length) return original;
  return { name: translation.name, components: translatedComponents, translated: true };
}
export function translatedNotices(notices: readonly string[], cache: TranslationCache | null, language: Language): readonly DisplayNotice[] {
  const dictionary = record(record(cache)?.notices);
  return notices.map(original => {
    const fallback = { original, text: original, translated: false };
    if (language === 'de' || !dictionary || !Object.hasOwn(dictionary, original)) return fallback;
    const entry = record(dictionary[original]);
    if (!entry || Object.keys(entry).length !== 2 || !Object.hasOwn(entry, 'en') || !Object.hasOwn(entry, 'ko') || !nonempty(entry.en) || !nonempty(entry.ko)) return fallback;
    return { original, text: language === 'en' ? entry.en : entry.ko, translated: true };
  });
}
