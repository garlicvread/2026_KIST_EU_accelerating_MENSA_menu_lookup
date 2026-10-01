import { SOURCE_URL } from './contracts.js';
export function required<K extends keyof HTMLElementTagNameMap>(document: Document, id: string, tag: K): HTMLElementTagNameMap[K] {
  const node = document.getElementById(id);
  if (!node || node.nodeType !== 1 || node.tagName !== tag.toUpperCase() || typeof node.setAttribute !== 'function' || typeof node.addEventListener !== 'function' ||
      (tag === 'select' && (!('value' in node) || typeof node.value !== 'string'))) throw new Error(`Missing or invalid ${id}`);
  // required는 반환할 요소의 HTML 태그를 확인하기 위해 위에서 실제 태그, 메서드, 값을 검사합니다.
  return node as HTMLElementTagNameMap[K];
}
export function element<K extends keyof HTMLElementTagNameMap>(document: Document, tag: K, className = '', text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node;
}
export function setText(node: HTMLElement, text: string): void { if (node.textContent !== text) node.textContent = text; }
/** syncChildren은 필요한 위치에 이미 있는 변경 없는 자식 요소를 그대로 두어 해당 요소의 입력 초점과 상세 정보 펼침 상태를 유지합니다. 필요 없는 자식은 제거하고 나머지는 삽입하거나 위치를 옮깁니다. */
export function syncChildren(parent: HTMLElement, children: readonly HTMLElement[]): void {
  const wanted = new Set<Node>(children);
  for (const child of Array.from(parent.childNodes)) if (!wanted.has(child)) parent.removeChild(child);
  children.forEach((child, index) => { if (parent.childNodes[index] !== child) parent.insertBefore(child, parent.childNodes[index] ?? null); });
}
export function sourceLink(document: Document, text: string, className = 'source-link'): HTMLAnchorElement {
  const link = element(document, 'a', className, text); link.setAttribute('href', SOURCE_URL); link.setAttribute('target', '_blank'); link.setAttribute('rel', 'noopener noreferrer'); return link;
}
