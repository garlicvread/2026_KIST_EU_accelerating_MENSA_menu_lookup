/** 이 모듈은 화면에서 사용할 공개 JSON 데이터를 검사하여 반환합니다. 게시본을 식별하는 표준 해시 계산은 공개 파일을 만드는 게시 코드가 담당하므로 여기서는 수행하지 않습니다. */
export const SOURCE_URL = 'https://www.stw-saarland.de/gastro/mensa-saarbruecken/';
export type Language = 'ko' | 'en' | 'de';
export type PriceGroup = 'student' | 'staff' | 'guest';
export type ViewMode = 'day' | 'week';
export type RequestState<T> =
  | { readonly status: 'idle' }
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly value: T }
  | { readonly status: 'failed'; readonly error: string };
export interface MenuSource { readonly url: typeof SOURCE_URL; readonly fetched_at: string; readonly sha256: string }
export interface Component { readonly name_de: string; readonly notices: readonly string[] }
export interface Prices { readonly student: number; readonly staff: number; readonly guest: number }
interface MealFields {
  readonly id: string;
  readonly translation_key: string;
  readonly category: string;
  readonly location: string;
  readonly name_de: string;
  readonly components: readonly Component[];
  readonly notices: readonly string[];
}
interface PriceSource { readonly date: string; readonly category: string; readonly name: string; readonly scope?: 'counter' }
export type Meal = MealFields & (
  | { readonly price_status: 'verified'; readonly prices: Prices; readonly price_source: PriceSource & { readonly raw: string } }
  | { readonly price_status: 'source_pending'; readonly prices: null; readonly price_source: PriceSource & { readonly raw: null } }
);
export interface MenuDay { readonly date: string; readonly meals: readonly Meal[] }
export interface MenuSnapshot {
  readonly schema_version: 1;
  readonly source: MenuSource;
  readonly coverage: { readonly start: string; readonly end: string };
  readonly days: readonly MenuDay[];
}
export interface TranslationSource { readonly name_de: string; readonly components: readonly string[] }
export interface TranslationText { readonly name: string; readonly components: readonly string[] }
export interface TranslationEntry {
  readonly source: TranslationSource;
  readonly en: TranslationText;
  readonly ko: TranslationText;
}
export interface NoticeTranslation { readonly en: string; readonly ko: string }
export interface TranslationCache {
  readonly schema_version: 1;
  readonly entries: Readonly<Record<string, TranslationEntry>>;
  readonly notices: Readonly<Record<string, NoticeTranslation>>;
}
export interface ReleaseManifest { readonly schema_version: 1; readonly release_id: string }

export function isLanguage(value: unknown): value is Language { return value === 'ko' || value === 'en' || value === 'de'; }
export function isPriceGroup(value: unknown): value is PriceGroup { return value === 'student' || value === 'staff' || value === 'guest'; }
export function isViewMode(value: unknown): value is ViewMode { return value === 'day' || value === 'week'; }

function invalid(label: string): never { throw new Error(`Invalid ${label}`); }
function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return invalid(label);
  const prototype: unknown = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return invalid(label);
  // record는 객체가 아닌 값과 배열을 배제하여 필드 값이 아직 확인되지 않은 객체를 반환합니다. 데이터 형식을 확정하려면 이후 함수에서 필드를 추가로 검사해야 합니다.
  return value as Record<string, unknown>;
}
function fields(value: unknown, expected: readonly string[], label: string): Record<string, unknown> {
  const result = record(value, label);
  const keys = Object.keys(result);
  if (keys.length !== expected.length || expected.some(key => !Object.hasOwn(result, key))) return invalid(`${label} fields`);
  return result;
}
function array(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) return invalid(label);
  return value;
}
function text(value: unknown, label: string, multiline = false): string {
  if (typeof value !== 'string' || !value.trim() || (multiline ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u : /[\u0000-\u001f]/u).test(value)) return invalid(label);
  return value;
}
function sourceText(value: unknown, label: string): string {
  const result = text(value, label);
  if (result !== result.trim().replace(/\s+/gu, ' ')) return invalid(label);
  return result;
}
function digest(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.length !== 64 || !/^[0-9a-f]{64}$/u.test(value)) return invalid(label);
  return value;
}
function isoDate(value: unknown, label: string): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/u.test(value)) return invalid(label);
  const parsed = new Date(`${value}T12:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) return invalid(label);
  return value;
}
function timestamp(value: unknown): string {
  if (typeof value !== 'string') return invalid('fetch timestamp');
  const matched = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/u.exec(value);
  if (!matched) return invalid('aware fetch timestamp');
  isoDate(matched[1], 'fetch date');
  if (Number(matched[2]) > 23 || Number(matched[3]) > 59 || Number(matched[4]) > 59 ||
      (matched[5] !== undefined && (Number(matched[6]) > 23 || Number(matched[7]) > 59)) || !Number.isFinite(Date.parse(value))) return invalid('fetch timestamp');
  return value;
}
function notices(value: unknown): string[] { return array(value, 'source notices').map(item => sourceText(item, 'source notice')); }
function component(value: unknown): Component {
  const item = fields(value, ['name_de', 'notices'], 'component');
  return { name_de: sourceText(item.name_de, 'component name'), notices: notices(item.notices) };
}
function meal(value: unknown, day: string, ids: Set<string>): Meal {
  const item = fields(value, ['id', 'translation_key', 'category', 'location', 'name_de', 'components', 'notices', 'prices', 'price_status', 'price_source'], 'meal');
  const id = sourceText(item.id, 'meal ID');
  if (!/^\d{4}-\d{2}-\d{2}-[0-9a-f]{64}(?:-(?:[2-9]|[1-9]\d+))?$/u.test(id) || !id.startsWith(`${day}-`) || ids.has(id)) return invalid('meal identity');
  ids.add(id);
  const category = sourceText(item.category, 'category');
  if (category === 'Information') return invalid('meal category');
  const name = sourceText(item.name_de, 'meal name');
  const common: MealFields = {
    id, translation_key: digest(item.translation_key, 'translation key'), category,
    location: sourceText(item.location, 'location'), name_de: name,
    components: array(item.components, 'components').map(component), notices: notices(item.notices),
  };
  const sourceRecord = record(item.price_source, 'price provenance');
  const shared = Object.hasOwn(sourceRecord, 'scope');
  const provenance = fields(sourceRecord, shared ? ['date', 'category', 'name', 'raw', 'scope'] : ['date', 'category', 'name', 'raw'], 'price provenance');
  const sourceName = sourceText(provenance.name, 'price source name');
  if (provenance.date !== day || provenance.category !== category ||
      (shared ? provenance.scope !== 'counter' : sourceName !== name)) return invalid('price source association');
  const priceSource: PriceSource = { date: day, category, name: sourceName, ...(shared ? { scope: 'counter' as const } : {}) };
  if (!shared && item.price_status === 'source_pending' && item.prices === null && provenance.raw === null) {
    return { ...common, price_status: 'source_pending', prices: null, price_source: { ...priceSource, raw: null } };
  }
  if (item.price_status !== 'verified') return invalid('price status');
  const prices = fields(item.prices, ['student', 'staff', 'guest'], 'prices');
  const raw = text(provenance.raw, 'raw price');
  const matched = /^S:\s*(\d+)[,.](\d{2})\s*\|\s*M:\s*(\d+)[,.](\d{2})\s*\|\s*G:\s*(\d+)[,.](\d{2})$/u.exec(raw);
  if (!matched || matched[0] !== raw) return invalid('raw price');
  function cents(group: PriceGroup, index: number): number {
    const value = prices[group];
    const expected = Number(matched?.[index]) * 100 + Number(matched?.[index + 1]);
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0 || value > 100_000 || value !== expected) return invalid('integer source price');
    return value;
  }
  return { ...common, price_status: 'verified',
    prices: { student: cents('student', 1), staff: cents('staff', 3), guest: cents('guest', 5) },
    price_source: { ...priceSource, raw } };
}

export function validateMenu(input: unknown): MenuSnapshot {
  const item = fields(input, ['schema_version', 'source', 'coverage', 'days'], 'menu');
  if (item.schema_version !== 1) return invalid('menu schema');
  const source = fields(item.source, ['url', 'fetched_at', 'sha256'], 'source');
  if (source.url !== SOURCE_URL) return invalid('source URL');
  const coverage = fields(item.coverage, ['start', 'end'], 'coverage');
  const start = isoDate(coverage.start, 'coverage start');
  const end = isoDate(coverage.end, 'coverage end');
  const ids = new Set<string>();
  let previous = '';
  const days = array(item.days, 'days').map(value => {
    const day = fields(value, ['date', 'meals'], 'day');
    const date = isoDate(day.date, 'day date');
    if (date <= previous) return invalid('ordered unique dates');
    previous = date;
    const meals = array(day.meals, 'meals').map(value => meal(value, date, ids));
    // 공통 가격의 원문과 마지막 메뉴를 확인하여 다른 판매대의 가격이 섞이지 않게 합니다.
    const counters = new Map<string, Meal[]>();
    for (const item of meals) {
      const key = JSON.stringify([item.category, item.location]);
      const group = counters.get(key) ?? []; group.push(item); counters.set(key, group);
    }
    for (const group of counters.values()) {
      const shared = group.filter(item => item.price_source.scope === 'counter');
      if (shared.length === 0) continue;
      const owners = group.filter(item => item.price_status === 'verified' && item.price_source.scope === undefined);
      const owner = owners[0];
      if (owners.length !== 1 || owner !== group.at(-1) || owner?.price_status !== 'verified') return invalid('shared counter price owner');
      for (const item of group) {
        if (item.price_status !== 'verified' || item.prices.student !== owner.prices.student ||
            item.prices.staff !== owner.prices.staff || item.prices.guest !== owner.prices.guest) return invalid('counter prices');
      }
      if (shared.some(item => item.price_source.name !== owner.name_de || item.price_source.raw !== owner.price_source.raw)) return invalid('shared price provenance');
    }
    return { date, meals };
  });
  if (days.length === 0 || days[0]?.date !== start || days[days.length - 1]?.date !== end) return invalid('coverage');
  return { schema_version: 1, source: { url: SOURCE_URL, fetched_at: timestamp(source.fetched_at), sha256: digest(source.sha256, 'source hash') }, coverage: { start, end }, days };
}

function translationSource(value: unknown): TranslationSource {
  const source = fields(value, ['name_de', 'components'], 'translation source');
  return { name_de: text(source.name_de, 'translation source name', true), components: array(source.components, 'source components').map(value => text(value, 'source component', true)) };
}
function translationText(value: unknown, count: number): TranslationText {
  const item = fields(value, ['name', 'components'], 'translation text');
  const components = array(item.components, 'translated components').map(value => text(value, 'translated component', true));
  if (components.length !== count) return invalid('translation component count');
  return { name: text(item.name, 'translated name', true), components };
}
function translationEntry(value: unknown): TranslationEntry {
  const item = record(value, 'translation entry');
  if (!['source', 'en', 'ko'].every(key => Object.hasOwn(item, key))) return invalid('translation entry fields');
  const source = translationSource(item.source);
  return { source, en: translationText(item.en, source.components.length), ko: translationText(item.ko, source.components.length) };
}
function dictionary<T>(value: unknown, parse: (value: unknown) => T, keys: (key: string) => void): Record<string, T> {
  return Object.fromEntries(Object.entries(record(value, 'dictionary')).map(([key, entry]): [string, T] => {
    keys(key);
    return [key, parse(entry)];
  }));
}
function noticeTranslation(value: unknown): NoticeTranslation {
  const item = fields(value, ['en', 'ko'], 'notice translation');
  return { en: text(item.en, 'English notice'), ko: text(item.ko, 'Korean notice') };
}
export function validateTranslations(input: unknown): TranslationCache {
  const item = record(input, 'translation cache');
  if (!Object.hasOwn(item, 'schema_version') || item.schema_version !== 1 || !Object.hasOwn(item, 'entries')) return invalid('translation schema');
  return { schema_version: 1,
    entries: dictionary(item.entries, translationEntry, key => { digest(key, 'translation key'); }),
    notices: Object.hasOwn(item, 'notices') ? dictionary(item.notices, noticeTranslation, key => { text(key, 'notice key'); }) : {},
  };
}
export function validateReleaseManifest(input: unknown): ReleaseManifest {
  const item = fields(input, ['schema_version', 'release_id'], 'release manifest');
  if (item.schema_version !== 1) return invalid('release schema');
  return { schema_version: 1, release_id: digest(item.release_id, 'release ID') };
}
