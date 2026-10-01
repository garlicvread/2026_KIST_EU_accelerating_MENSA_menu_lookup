"""공개 Saarbrücken 식단의 HTML을 받아 날짜별 메뉴 스냅샷으로 변환합니다.

fetch_html은 네트워크 접근을 담당하고, parse_menu는 전달된 문자열만 해석합니다.
파서는 같은 날짜·판매대·이름의 반복 항목도 각각 보존하며, 추출한 구조의 종류별 수와
별도로 센 원본 토큰 수를 비교하여 누락을 검사합니다. 메뉴 형식과 이전 공개 메뉴의 손실 검사는 mensa.menu_contract가
담당하며, 이 모듈은 파일 저장·번역·공개를 수행하지 않습니다.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# 수집 스크립트가 사용하는 계약 함수와 기존 scripts.menu_source 호출자의 가져오기
# 경로를 함께 제공합니다. 스키마·가격·식별자 규칙은 mensa.menu_contract에서 관리합니다.
from mensa.menu_contract import (
    SOURCE_URL, PRICE_PATTERN, validate_menu,
    _require, _text, _digest, _translation_key, _meal_id, _source_date, _iso_date,
    _prices, _fields, _string, _notice_list, _validate_schema, _validate_previous,
)


FETCH_TIMEOUT = 20
FETCH_ATTEMPTS = 3
MAX_SOURCE_BYTES = 5_000_000
VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
TRACKED_CLASSES = frozenset(("meal", "open-feedback", "counter", "component-item",
                             "component-name", "component-notices", "meal-notices", "notices"))
PRICE_MARKER = re.compile(r"\bPreise\s*:", re.IGNORECASE)
GROUP_MARKER = re.compile(r"\b[SMG]\s*:")


def fetch_html(url=SOURCE_URL):
    """허용된 SOURCE_URL에서 UTF-8 HTML 문자열을 가져오며, 실패하면 ValueError를 발생시킵니다.

    요청마다 20초 제한을 적용하고 최대 세 번 시도하며 재시도 사이에는 대기합니다.
    다른 주소로의 응답, HTML 이외의 형식, 빈 본문·과도한 크기·잘못된 UTF-8을 거부하여
    파서가 다른 문서나 손상된 원본을 메뉴로 받아들이지 않도록 합니다.
    """
    _require(url == SOURCE_URL, "Unexpected menu source URL")
    request = Request(url, headers={"User-Agent": "SaarbrueckenMensaMenu/1.0 (+public menu collector)",
                                    "Accept": "text/html", "Accept-Encoding": "identity"})
    failure = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with urlopen(request, timeout=FETCH_TIMEOUT) as response:
                _require(response.geturl() == SOURCE_URL, "Unexpected source redirect")
                _require(response.headers.get_content_type() == "text/html", "Source is not HTML")
                body = response.read(MAX_SOURCE_BYTES + 1)
                _require(0 < len(body) <= MAX_SOURCE_BYTES, "Source is empty or exceeds size limit")
                return body.decode("utf-8", errors="strict")
        except (HTTPError, URLError, OSError, UnicodeError, ValueError) as exc:
            failure = exc
            if attempt + 1 < FETCH_ATTEMPTS:
                time.sleep(attempt + 1)
    raise ValueError(f"Unable to fetch menu after {FETCH_ATTEMPTS} attempts: {failure}") from failure


class _Node:
    """HTML 요소의 속성·부모·자식·닫힘 여부를 보관하는 파서 내부 트리입니다.

    all과 by_class는 자손만 조회하고, text는 자식 순서대로 문자열을 합칩니다.
    원본의 계층과 문구를 유지하며 공백 정규화는 해당 값을 해석하는 단계에서 수행합니다.
    """

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = {key: value if value is not None else "" for key, value in (attrs or [])}
        self.classes = set(self.attrs.get("class", "").split())
        self.parent = parent
        self.children = []
        self.closed = tag in VOID_TAGS

    def all(self):
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.all()

    def text(self):
        return "".join(child.text() if isinstance(child, _Node) else child for child in self.children)

    def by_class(self, name):
        return [node for node in self.all() if name in node.classes]


class _DOM(HTMLParser):
    """HTML 이벤트를 _Node 트리로 만들고 종료 태그 또는 빈 요소의 닫힘 상태를 표시합니다.

    중복 속성은 즉시 거부합니다. 종료 태그가 상위 요소와 맞으면 열린 자손을 스택에서
    제거하되 자손을 닫힌 것으로 표시하지 않아, 이후 메뉴 구조 검사가 잘린 요소를
    발견할 수 있게 합니다. HTML 전체의 문법 검증은 이 클래스의 책임이 아닙니다.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.stack[-1])
        _require(len(node.attrs) == len(attrs), "Duplicate HTML attribute")
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class _RawInventory(HTMLParser):
    """DOM 조회와 별도로 구조 클래스·메타데이터·가격 표시의 등장 횟수를 셉니다.

    구조 클래스와 메타데이터는 문서 전체에서 세고, 가격 표시는 판매대 또는 메뉴 요소
    안에서만 셉니다. 추출 루프가 읽지 못한 요소도 집계하여 종류별 누락을 드러냅니다.
    텍스트 전체의 일치나 모든 종류의 HTML 변경을 판별하는 검사는 아닙니다.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.counts = Counter()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = {key: value if value is not None else "" for key, value in attrs}
        classes = set(attrs.get("class", "").split())
        self.counts.update(classes & TRACKED_CLASSES)
        if any(key in attrs for key in ("data-date", "data-counter", "data-meal")):
            self.counts["metadata"] += 1
        active = bool(self.stack and self.stack[-1][1]) or "counter" in classes or "meal" in classes
        if tag not in VOID_TAGS:
            self.stack.append((tag, active))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            self.counts["price-markers"] += len(PRICE_MARKER.findall(data))
            self.counts["price-groups"] += len(GROUP_MARKER.findall(data))


def _nearest(node, predicate):
    """node 자신을 제외한 조상 중 predicate를 처음 만족하는 요소 또는 None을 반환합니다."""
    parent = node.parent
    while parent is not None:
        if predicate(parent):
            return parent
        parent = parent.parent
    return None


def _one(nodes, message):
    """필수 요소가 정확히 하나일 때만 반환하여 임의의 첫 항목으로 모호함을 숨기지 않습니다."""
    _require(len(nodes) == 1, message)
    return nodes[0]


def _notices(node):
    """안내 요소의 문구를 구분자별로 나누고 공백을 정리한 문자열 목록을 반환합니다."""
    # 원본 사이트는 안내 문구 사이에 NBSP를 연속해서 넣습니다. _notices는 문구 내부의
    # 공백("Milch und Laktose")에 의미가 있으므로 이 공백으로 문구를 나누지 않습니다.
    values = re.split(r"\xa0{2,}|\n+|\s*\|\s*", node.text())
    return [_text(value) for value in values if _text(value)]


def parse_menu(html, fetched_at=None):
    """HTML 문자열에서 검증된 메뉴 사전을 반환하고 잘못된 원본은 ValueError로 거부합니다.

    fetched_at으로 수집 시간을 전달할 수 있으며, 값이 없으면 현재 UTC 시간을 기록합니다.
    반환값에는 원본 문자열의 SHA256과 추출 날짜 범위가 포함됩니다. Information 항목은
    구조 검사에 포함하지만 식사 목록에서 제외합니다. 파일이나 네트워크에 접근하지 않으며,
    이전 메뉴와의 손실 비교는 호출자가 validate_menu에 previous를 전달하여 수행합니다.
    """
    _require(isinstance(html, str) and html.strip(), "Empty HTML source")
    try:
        return _parse_menu(html, fetched_at)
    except (TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
        raise ValueError(f"Invalid menu source: {exc}") from exc


def _parse_menu(html, fetched_at):
    """두 파서의 결과를 대조하며 날짜·판매대·요리·구성품·가격을 메뉴 계약에 맞게 추출합니다."""
    dom, inventory = _DOM(), _RawInventory()
    dom.feed(html)
    dom.close()
    inventory.feed(html)
    inventory.close()
    nodes = list(dom.root.all())
    relevant = [node for node in nodes if node.classes & TRACKED_CLASSES
                or node.attrs.get("id", "").startswith(("day-", "tab-day-"))]
    _require(all(node.closed for node in relevant), "Truncated or malformed menu structure")

    # 탭의 날짜와 패널의 상호 참조가 일대일이어야 메뉴를 그 날짜에 귀속할 수 있습니다.
    # 같은 수의 탭·패널만으로는 빠진 날짜나 서로 바뀐 연결을 배제할 수 없습니다.
    tabs = [node for node in nodes if node.attrs.get("id", "").startswith("tab-day-")]
    panels = [node for node in nodes if node.attrs.get("id", "").startswith("day-")]
    _require(tabs and panels and len(tabs) == len(panels), "Missing day tab/panel coverage")
    tab_map, panel_map = {}, {}
    for panel in panels:
        key = panel.attrs["id"]
        _require(key not in panel_map, "Duplicate day panel")
        panel_map[key] = panel
    for tab in tabs:
        panel_id = tab.attrs.get("aria-controls")
        _require(tab.tag == "a" and panel_id in panel_map and panel_id not in tab_map,
                 "Missing or duplicate day metadata")
        _require(panel_map[panel_id].attrs.get("aria-labelledby") == tab.attrs["id"],
                 "Day metadata does not match panel")
        tab_map[panel_id] = _source_date(_text(tab.text()))
    _require(set(tab_map) == set(panel_map), "Day coverage inventory mismatch")
    _require(len(set(tab_map.values())) == len(tabs), "Duplicate date tabs")

    consumed = set()
    extracted_counts = Counter()

    def consume(node):
        """읽은 원본 요소를 한 번만 집계하고 중첩 구조의 중복 추출을 거부합니다."""
        _require(node not in consumed, "Overlapping or duplicate menu structures")
        _require(node.closed, "Unclosed source data element")
        consumed.add(node)
        extracted_counts.update(node.classes & TRACKED_CLASSES)
        if any(key in node.attrs for key in ("data-date", "data-counter", "data-meal")):
            extracted_counts["metadata"] += 1

    days = []
    identity_occurrences = Counter()
    for panel_id, panel in panel_map.items():
        day = tab_map[panel_id]
        records = []
        counters = panel.by_class("counter")
        _require(counters, "Day has no menu counters")
        for counter in counters:
            # 자손 조회만으로는 중첩된 다른 판매대의 항목도 포함될 수 있으므로 가장 가까운
            # 소유 요소를 확인합니다. 메뉴와 구성품에도 같은 소유 관계 검사를 적용합니다.
            _require(_nearest(counter, lambda n: n in panels) is panel, "Orphan counter")
            consume(counter)
            heading = _one([node for node in counter.children if isinstance(node, _Node) and node.tag == "h3"],
                           "Missing or duplicate counter heading")
            category = _text(heading.text())
            _require(category, "Empty counter name")
            locations = [node for node in counter.children if isinstance(node, _Node) and node.tag == "p"]
            _require(len(locations) == 1 or (category == "Information" and not locations),
                     "Missing or duplicate counter location")
            location = _text(locations[0].text()) if locations else ""
            _require(location or category == "Information", "Empty counter location")
            meals = counter.by_class("meal")
            _require(meals, "Counter has no source meals")
            for source_meal in meals:
                _require(_nearest(source_meal, lambda n: "counter" in n.classes) is counter,
                         "Orphan or nested meal")
                consume(source_meal)
                link = _one(source_meal.by_class("open-feedback"), "Missing or duplicate meal metadata")
                _require(link.tag == "a", "Meal metadata is not on a link")
                consume(link)
                name = _text(link.attrs.get("data-meal", ""))
                _require(name and _text(link.text()) == name, "Missing or mismatched source meal name")
                _require(_text(link.attrs.get("data-counter", "")) == category, "Mismatched category metadata")
                _require(_source_date(link.attrs.get("data-date")) == day, "Mixed dates in day panel")

                components = []
                for component in source_meal.by_class("component-item"):
                    _require(_nearest(component, lambda n: "meal" in n.classes) is source_meal,
                             "Orphan component")
                    consume(component)
                    component_name = _one(component.by_class("component-name"), "Missing or duplicate component name")
                    consume(component_name)
                    name_de = _text(component_name.text())
                    _require(name_de, "Empty component name")
                    component_notices = []
                    for notice in component.by_class("component-notices"):
                        consume(notice)
                        component_notices.extend(_notices(notice))
                    components.append({"name_de": name_de, "notices": component_notices})
                notices = []
                for notice in source_meal.by_class("meal-notices"):
                    consume(notice)
                    notices.extend(_notices(notice))

                price_nodes = [node for node in source_meal.all() if node.tag == "strong"
                               and PRICE_MARKER.search(_text(node.text()))]
                _require(len(price_nodes) <= 1, "Duplicate price block")
                raw = None
                prices = None
                # 가격 표시가 없으면 미확인 상태를 보존합니다. 표시가 있으면 S/M/G 전체를
                # _prices가 검증하므로 일부 금액만 채우거나 누락 가격을 추측하지 않습니다.
                if price_nodes:
                    label = price_nodes[0]
                    block = label.parent
                    _require(_text(label.text()).lower() == "preise:" and block.tag == "p" and block.closed,
                             "Malformed price marker")
                    _require(_nearest(block, lambda n: "meal" in n.classes) is source_meal, "Orphan price block")
                    block_text = _text(block.text())
                    _require(block_text.lower().startswith("preise:"), "Price marker is not block prefix")
                    raw = block_text[len("Preise:"):].strip()
                    prices = _prices(raw)
                    extracted_counts["price-markers"] += 1
                    extracted_counts["price-groups"] += 3
                if category != "Information":
                    # 동일한 요리가 여러 번 등장해도 원본 항목을 합치지 않습니다. 등장 순서로
                    # 고유 ID를 만들고, 이름·구성품이 같은 항목은 번역 키를 공유하게 합니다.
                    identity_occurrences[(day, category, name)] += 1
                    records.append({
                        "id": _meal_id(day, category, name, identity_occurrences[(day, category, name)]),
                        "translation_key": _translation_key(name, components),
                        "category": category, "location": location, "name_de": name,
                        "components": components, "notices": notices, "prices": prices,
                        "price_status": "verified" if prices is not None else "source_pending",
                        "price_source": {"date": day, "category": category, "name": name, "raw": raw}})
        days.append({"date": day, "meals": records})

    # 읽은 구조의 종류별 수가 독립 집계와 달라지면 부분 결과를 거부합니다. Information도
    # 집계하므로 식사 목록에서 제외한 항목이 원본 누락처럼 취급되지 않습니다. 이 대조는
    # 추적 대상 토큰의 범위를 검사하며, 원본 바이트의 동일성이나 모든 변경을 보장하지 않습니다.
    _require(extracted_counts == inventory.counts,
             f"Source inventory mismatch: raw={dict(inventory.counts)}, extracted={dict(extracted_counts)}")
    days.sort(key=lambda item: item["date"])
    result = {
        "schema_version": 1,
        "source": {"url": SOURCE_URL,
                   "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                   "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest()},
        "coverage": {"start": days[0]["date"], "end": days[-1]["date"]}, "days": days}
    validate_menu(result)
    return result
