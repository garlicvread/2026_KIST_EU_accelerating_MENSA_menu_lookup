import type { Meal, MenuDay, MenuSnapshot, ViewMode } from './contracts.js';

export interface MenuWeek { readonly key: string; readonly days: readonly MenuDay[] }

/** visibleMeals는 한 날짜 안에서 ID를 제외한 검증된 메뉴 필드 전체를 비교해 모든 값이 같은 기록 중 첫 번째 기록만 반환합니다. 중복 표시를 막되 서로 다른 기록을 잘못 합치지 않도록 모든 가격과 가격 원문 출처 정보, translation_key도 비교에 포함합니다. */
export function visibleMeals(day: MenuDay): readonly Meal[] {
  const seen = new Set<string>();
  return day.meals.filter(meal => {
    const key = JSON.stringify([
      meal.category, meal.location, meal.name_de, meal.translation_key,
      meal.components.map(component => [component.name_de, component.notices]), meal.notices,
      meal.price_status, meal.prices === null ? null : [meal.prices.student, meal.prices.staff, meal.prices.guest],
      [meal.price_source.date, meal.price_source.category, meal.price_source.name, meal.price_source.raw, meal.price_source.scope],
    ]);
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
}

export function berlinToday(now: Date): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now);
  function part(type: 'year' | 'month' | 'day'): string {
    const value = parts.find(item => item.type === type)?.value;
    if (value === undefined) throw new Error('Missing Berlin calendar date');
    return value;
  }
  return `${part('year')}-${part('month')}-${part('day')}`;
}

export function chooseDefaultDate(days: readonly Pick<MenuDay, 'date'>[], today: string): string | null {
  const dates = days.map(day => day.date).sort();
  return dates.find(date => date >= today) ?? dates.at(-1) ?? null;
}

function mondayOf(date: string): string {
  const value = new Date(`${date}T12:00:00Z`);
  value.setUTCDate(value.getUTCDate() - (value.getUTCDay() + 6) % 7);
  return value.toISOString().slice(0, 10);
}

export function groupWeeks(days: readonly MenuDay[]): readonly MenuWeek[] {
  const weeks = new Map<string, { key: string; days: MenuDay[] }>();
  for (const day of [...days].sort((a, b) => a.date.localeCompare(b.date))) {
    const key = mondayOf(day.date);
    let week = weeks.get(key);
    if (week === undefined) { week = { key, days: [] }; weeks.set(key, week); }
    week.days.push(day);
  }
  return [...weeks.values()];
}

export function selectedWeek(menu: MenuSnapshot, date: string | null): MenuWeek | null {
  return groupWeeks(menu.days).find(week => week.days.some(day => day.date === date)) ?? null;
}

export function menuFreshness(menu: MenuSnapshot, options: {
  selectedDate: string | null; view: ViewMode; today: string; now: Date;
}): { expired: boolean; past: boolean; stale: boolean } {
  return {
    expired: menu.days.every(day => day.date < options.today),
    past: options.view === 'day' && options.selectedDate !== null && options.selectedDate < options.today,
    stale: options.now.getTime() - Date.parse(menu.source.fetched_at) > 2 * 24 * 60 * 60 * 1000,
  };
}
