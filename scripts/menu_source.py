"""Collect the public Saarbrücken menu without guessing missing source data.

The DOM extraction and a separate token inventory must agree before a snapshot
can be returned. This intentionally fails closed when the source shape changes.
"""

from collections import Counter
from datetime import date, datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SOURCE_URL = "https://www.stw-saarland.de/gastro/mensa-saarbruecken/"
FETCH_TIMEOUT = 20
FETCH_ATTEMPTS = 3
MAX_SOURCE_BYTES = 5_000_000
VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
TRACKED_CLASSES = frozenset(("meal", "open-feedback", "counter", "component-item",
                             "component-name", "component-notices", "meal-notices", "notices"))
PRICE_MARKER = re.compile(r"\bPreise\s*:", re.IGNORECASE)
GROUP_MARKER = re.compile(r"\b[SMG]\s*:")
PRICE_PATTERN = re.compile(r"S:\s*(\d+)[,.](\d{2})\s*\|\s*M:\s*(\d+)[,.](\d{2})\s*\|\s*G:\s*(\d+)[,.](\d{2})")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return " ".join(value.split())


def _digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _translation_key(name, components):
    return _digest({"name_de": name, "components": [item["name_de"] for item in components]})


def _meal_id(day, category, name, occurrence=1):
    base = day + "-" + _digest({"date": day, "category": category, "name": name})
    return base if occurrence == 1 else f"{base}-{occurrence}"


def _source_date(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value),
             "Missing or invalid source day metadata")
    return datetime.strptime(value, "%d.%m.%Y").date().isoformat()


def _iso_date(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "Invalid ISO date")
    date.fromisoformat(value)
    return value


def _prices(raw):
    _require(isinstance(raw, str), "Price block is not text")
    matched = PRICE_PATTERN.fullmatch(raw)
    _require(matched is not None, "Malformed, partial, or duplicate S/M/G price block")
    groups = matched.groups()
    amounts = [int(groups[i]) * 100 + int(groups[i + 1]) for i in range(0, 6, 2)]
    _require(all(0 < value <= 100_000 for value in amounts), "Price outside valid range")
    return dict(zip(("student", "staff", "guest"), amounts))


def fetch_html(url=SOURCE_URL):
    """Fetch UTF-8 source with at most three 20-second network attempts."""
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
    """Count raw tokens, independently of DOM selectors and extraction loops."""

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
    parent = node.parent
    while parent is not None:
        if predicate(parent):
            return parent
        parent = parent.parent
    return None


def _one(nodes, message):
    _require(len(nodes) == 1, message)
    return nodes[0]


def _notices(node):
    # The publisher uses runs of NBSPs between notices; spaces within a notice
    # ("Milch und Laktose") are significant and must not split that notice.
    values = re.split(r"\xa0{2,}|\n+|\s*\|\s*", node.text())
    return [_text(value) for value in values if _text(value)]


def parse_menu(html, fetched_at=None):
    """Extract all source meals; raise ValueError instead of partial output."""
    _require(isinstance(html, str) and html.strip(), "Empty HTML source")
    try:
        return _parse_menu(html, fetched_at)
    except (TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
        raise ValueError(f"Invalid menu source: {exc}") from exc


def _parse_menu(html, fetched_at):
    dom, inventory = _DOM(), _RawInventory()
    dom.feed(html)
    dom.close()
    inventory.feed(html)
    inventory.close()
    nodes = list(dom.root.all())
    relevant = [node for node in nodes if node.classes & TRACKED_CLASSES
                or node.attrs.get("id", "").startswith(("day-", "tab-day-"))]
    _require(all(node.closed for node in relevant), "Truncated or malformed menu structure")

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
                    identity_occurrences[(day, category, name)] += 1
                    records.append({
                        "id": _meal_id(day, category, name, identity_occurrences[(day, category, name)]),
                        "translation_key": _translation_key(name, components),
                        "category": category, "location": location, "name_de": name,
                        "components": components, "notices": notices, "prices": prices,
                        "price_status": "verified" if prices is not None else "source_pending",
                        "price_source": {"date": day, "category": category, "name": name, "raw": raw}})
        days.append({"date": day, "meals": records})

    # This comparison uses the raw event inventory rather than the DOM query
    # population, catching renamed, orphaned, and unconsumed source structures.
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


def _fields(value, required, label):
    _require(isinstance(value, dict) and set(value) == set(required.split()), f"Invalid {label} fields")


def _string(value, label):
    _require(isinstance(value, str) and bool(value.strip()) and value == _text(value), f"Invalid {label}")


def _notice_list(value):
    _require(isinstance(value, list), "Notices must be a list")
    for notice in value:
        _string(notice, "notice")


def validate_menu(menu, previous=None):
    """Validate schema, source provenance, identities, and previous-data guards."""
    try:
        _validate_schema(menu)
        if previous is not None:
            _validate_schema(previous)
            _validate_previous(menu, previous)
    except (TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
        raise ValueError(f"Invalid menu data: {exc}") from exc


def _validate_schema(menu):
    _fields(menu, "schema_version source coverage days", "menu")
    _require(type(menu["schema_version"]) is int and menu["schema_version"] == 1, "Unsupported schema version")
    source = menu["source"]
    _fields(source, "url fetched_at sha256", "source")
    _require(source["url"] == SOURCE_URL, "Unexpected source URL")
    _require(isinstance(source["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]), "Invalid source hash")
    _require(isinstance(source["fetched_at"], str), "Invalid fetch timestamp")
    timestamp = datetime.fromisoformat(source["fetched_at"].replace("Z", "+00:00"))
    _require(timestamp.tzinfo is not None, "Fetch timestamp needs timezone")
    _fields(menu["coverage"], "start end", "coverage")
    _require(isinstance(menu["days"], list) and menu["days"], "No menu days")
    dates, identities, ids = [], Counter(), set()
    for day in menu["days"]:
        _fields(day, "date meals", "day")
        current_date = _iso_date(day["date"])
        dates.append(current_date)
        _require(isinstance(day["meals"], list), "Meals must be a list")
        for meal in day["meals"]:
            _fields(meal, "id translation_key category location name_de components notices prices price_status price_source", "meal")
            for name in ("id", "translation_key", "category", "location", "name_de"):
                _string(meal[name], name)
            _require(meal["category"] != "Information", "Information is not a meal")
            identity = (current_date, meal["category"], meal["name_de"])
            _require(meal["id"] not in ids, "Duplicate meal ID")
            identities[identity] += 1
            ids.add(meal["id"])
            _require(meal["id"] == _meal_id(*identity, identities[identity]), "Meal ID does not match source identity")
            _require(isinstance(meal["components"], list), "Components must be a list")
            for component in meal["components"]:
                _fields(component, "name_de notices", "component")
                _string(component["name_de"], "component name")
                _notice_list(component["notices"])
            _notice_list(meal["notices"])
            _require(meal["translation_key"] == _translation_key(meal["name_de"], meal["components"]), "Translation key mismatch")
            provenance = meal["price_source"]
            _fields(provenance, "date category name raw", "price provenance")
            _require((provenance["date"], provenance["category"], provenance["name"]) == identity,
                     "Price provenance does not match meal")
            if meal["prices"] is None:
                _require(meal["price_status"] == "source_pending" and provenance["raw"] is None,
                         "Pending price provenance mismatch")
            else:
                _fields(meal["prices"], "student staff guest", "prices")
                _require(meal["price_status"] == "verified", "Priced meal is not verified")
                _require(all(type(value) is int for value in meal["prices"].values()), "Prices must be integer cents")
                _require(meal["prices"] == _prices(provenance["raw"]), "Prices do not match raw source block")
    _require(dates == sorted(set(dates)), "Days must be unique and ordered")
    _require(menu["coverage"] == {"start": dates[0], "end": dates[-1]}, "Coverage does not match extracted dates")


def _validate_previous(menu, previous):
    current_days = {day["date"]: day["meals"] for day in menu["days"]}
    for old_day in previous["days"]:
        if old_day["date"] not in current_days:
            _require(not (menu["coverage"]["start"] <= old_day["date"] <= menu["coverage"]["end"]),
                     "Previously covered day disappeared inside current range")
            continue
        old_meals = old_day["meals"]
        current = current_days[old_day["date"]]
        # A small correction is possible; losing over a quarter of a published
        # day's meals is unsafe to publish automatically and needs review.
        _require(len(current) >= len(old_meals) * 0.75, "Suspicious loss of source meals on overlapping day")
        current_identities = {(meal["category"], meal["name_de"]) for meal in current}
        current_priced = Counter((meal["category"], meal["name_de"]) for meal in current if meal["prices"] is not None)
        old_priced = Counter((meal["category"], meal["name_de"]) for meal in old_meals if meal["prices"] is not None)
        for identity, count in old_priced.items():
            if identity in current_identities:
                _require(current_priced[identity] >= count, "Previously published same-meal prices disappeared")
