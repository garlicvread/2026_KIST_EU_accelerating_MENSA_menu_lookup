"""Fail-closed source collection regression tests."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "menu_source.py"
if MODULE.exists():
    spec = importlib.util.spec_from_file_location("menu_source", MODULE)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
else:
    collector = None


PRICES = "<p><strong>Preise:</strong> S: 3,50 | M: 4,65 | G: 5,35</p>"


def meal(name="Vegan: Ägyptisches Kushari", date="21.09.2026", category="Wahlessen", prices=PRICES):
    return f"""<div class='meal'><strong><a class='open-feedback'
      data-date='{date}' data-counter='{category}' data-meal='{name}'>{name}</a></strong>
      <div class='meal-notices notices'>vegan&nbsp;&nbsp;&nbsp;&nbsp;Weizen</div>
      <div class='component-item'><div class='component-name'>Reis</div></div>
      <div class='component-item'><div class='component-name'>Soße &amp; Gemüse</div>
      <div class='component-notices notices'>Sellerie&nbsp;&nbsp;&nbsp;&nbsp;Milch und Laktose</div></div>
      {prices}</div>"""


def page(content=None, category="Wahlessen", date="21.09.2026"):
    if content is None:
        content = meal(date=date, category=category)
    return f"""<!doctype html><html><body>
      <ul class='uk-tab' aria-label='Speiseplan-Tage'><li><a id='tab-day-0'
      role='tab' aria-controls='day-0'>{date}</a></li></ul>
      <ul class='uk-switcher'><li id='day-0' role='tabpanel' aria-labelledby='tab-day-0'>
      <div class='counter'><h3>{category}</h3><p>Aufgang C</p>{content}</div>
      </li></ul></body></html>"""


class MenuSourceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(collector, "menu_source implementation is missing")

    def parse(self, html=None):
        return collector.parse_menu(page() if html is None else html, "2026-09-21T10:00:00Z")

    def first(self, menu):
        return menu["days"][0]["meals"][0]

    def test_decimal_comma_prices_have_exact_integer_cents_and_provenance(self):
        result = self.parse()
        record = self.first(result)
        self.assertEqual(record["prices"], {"student": 350, "staff": 465, "guest": 535})
        self.assertEqual(record["price_status"], "verified")
        self.assertEqual(record["price_source"], {
            "date": "2026-09-21", "category": "Wahlessen", "name": record["name_de"],
            "raw": "S: 3,50 | M: 4,65 | G: 5,35"})
        self.assertEqual(result["coverage"], {"start": "2026-09-21", "end": "2026-09-21"})
        self.assertEqual(result["source"]["sha256"], hashlib.sha256(page().encode()).hexdigest())

    def test_preserves_every_component_and_notice(self):
        record = self.first(self.parse())
        self.assertEqual(record["notices"], ["vegan", "Weizen"])
        self.assertEqual(record["components"], [
            {"name_de": "Reis", "notices": []},
            {"name_de": "Soße & Gemüse", "notices": ["Sellerie", "Milch und Laktose"]}])

    def test_translation_key_matches_canonical_contract_and_changes_with_component(self):
        result = self.first(self.parse())
        source = {"name_de": result["name_de"], "components": [c["name_de"] for c in result["components"]]}
        digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(result["translation_key"], digest)
        self.assertNotEqual(result["translation_key"], self.first(self.parse(page().replace("Reis", "Nudeln")))["translation_key"])

    def test_source_genuinely_pending_remains_null(self):
        record = self.first(self.parse(page(meal(prices=""))))
        self.assertIsNone(record["prices"])
        self.assertEqual(record["price_status"], "source_pending")
        self.assertIsNone(record["price_source"]["raw"])

    def test_prices_elsewhere_never_used_for_pending_meal(self):
        html = page(meal(prices="")).replace("<body>", f"<body><aside>{PRICES}</aside>")
        self.assertIsNone(self.first(self.parse(html))["prices"])

    def test_second_meals_price_is_not_inherited(self):
        records = self.parse(page(meal(prices="") + meal(name="Soup")))["days"][0]["meals"]
        self.assertIsNone(records[0]["prices"])
        self.assertEqual(records[1]["prices"]["student"], 350)

    def test_missing_price_group_fails(self):
        with self.assertRaises(ValueError):
            self.parse(page(meal(prices=PRICES.replace(" | G: 5,35", ""))))

    def test_malformed_price_amounts_fail(self):
        for amount in ("3,5", "3.500", "-3,50", "NaN", "3,50 € extra"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.parse(page(meal(prices=PRICES.replace("3,50", amount))))

    def test_duplicate_price_block_and_group_fail(self):
        for prices in (PRICES * 2, PRICES.replace(" | M:", " | S: 3,50 | M:")):
            with self.subTest(prices=prices), self.assertRaises(ValueError):
                self.parse(page(meal(prices=prices)))

    def test_price_marker_moved_or_lost_by_selector_fails(self):
        for prices in (PRICES.replace("<p>", "<section>").replace("</p>", "</section>"),
                       PRICES.replace("<strong>", "<b>").replace("</strong>", "</b>")):
            with self.subTest(prices=prices), self.assertRaises(ValueError):
                self.parse(page(meal(prices=prices)))

    def test_only_information_category_is_excluded(self):
        html = page().replace("</li></ul></body>",
            "<div class='counter'><h3>Information</h3><p>Info</p>" +
            meal(name="Hinweis", category="Information", prices="") + "</div></li></ul></body>")
        self.assertEqual(len(self.parse(html)["days"][0]["meals"]), 1)
        self.assertEqual(len(self.parse(page(meal(name="Information zum Gericht")))["days"][0]["meals"]), 1)

    def test_duplicate_source_meals_are_preserved_with_unique_stable_occurrence_ids(self):
        try:
            records = self.parse(page(meal() + meal()))["days"][0]["meals"]
        except ValueError as exc:
            self.fail(f"Valid repeated source meal must be preserved: {exc}")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["id"], records[0]["id"] + "-2")
        self.assertEqual(records[0]["translation_key"], records[1]["translation_key"])
        self.assertEqual(records, self.parse(page(meal() + meal()))["days"][0]["meals"])

    def test_invalid_top_level_shapes_always_raise_value_error(self):
        for value in (None, [], {}, {"schema_version": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                collector.validate_menu(value)

    def test_boolean_html_attributes_fail_as_value_errors(self):
        for old, new in (("class='meal'", "class"), ("id='day-0'", "id"),
                         ("data-meal='Vegan: Ägyptisches Kushari'", "data-meal")):
            with self.subTest(old=old):
                try:
                    self.parse(page().replace(old, new))
                except ValueError:
                    pass
                except Exception as exc:
                    self.fail(f"Invalid source must raise ValueError, got {type(exc).__name__}")
                else:
                    self.fail("Invalid boolean metadata was accepted")

    def test_previously_covered_day_inside_current_range_cannot_disappear(self):
        first = self.parse()
        second = self.parse(page(date="22.09.2026"))
        third = self.parse(page(date="23.09.2026"))
        previous = copy.deepcopy(first)
        previous["days"] += second["days"] + third["days"]
        previous["coverage"]["end"] = "2026-09-23"
        current = copy.deepcopy(previous)
        del current["days"][1]
        with self.assertRaises(ValueError):
            collector.validate_menu(current, previous)

    def test_orphan_metadata_and_meal_are_rejected(self):
        for html in (page().replace("class='meal'", "class='renamed'"),
                     page().replace("class='open-feedback'", "class='renamed'"),
                     page().replace("class='counter'", "class='renamed'")):
            with self.subTest(html=html), self.assertRaises(ValueError):
                self.parse(html)

    def test_missing_day_and_counter_metadata_are_rejected(self):
        for token in ("data-date='21.09.2026'", "data-counter='Wahlessen'", "data-meal='Vegan: Ägyptisches Kushari'",
                      "id='day-0'", "id='tab-day-0'"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.parse(page().replace(token, ""))

    def test_counter_and_link_name_mismatch_are_rejected(self):
        for old, new in (("<h3>Wahlessen</h3>", "<h3>Soup</h3>"),
                         (">Vegan: Ägyptisches Kushari</a>", ">Soup</a>")):
            with self.subTest(old=old), self.assertRaises(ValueError):
                self.parse(page().replace(old, new))

    def test_mixed_dates_in_day_are_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(page(meal() + meal(name="Soup", date="22.09.2026")))

    def test_missing_panel_and_truncation_are_rejected(self):
        for html in (page().replace("</ul></body>",
                         "</ul><a id='tab-day-1' aria-controls='day-1'>22.09.2026</a></body>"),
                     page().split("<p><strong>Preise:")[0]):
            with self.subTest(html=html), self.assertRaises(ValueError):
                self.parse(html)

    def test_orphan_component_and_notice_are_rejected(self):
        for old, new in (("class='component-item'", "class='renamed'"),
                         ("class='component-name'", "class='renamed'"),
                         ("class='component-notices notices'", "class='renamed notices'")):
            with self.subTest(old=old), self.assertRaises(ValueError):
                self.parse(page().replace(old, new))

    def test_schema_validation_rejects_mismatched_price_and_source(self):
        result = self.parse()
        self.first(result)["prices"]["student"] = 1
        with self.assertRaises(ValueError):
            collector.validate_menu(result)

    def test_schema_validation_rejects_fabricated_or_duplicate_ids(self):
        result = self.parse()
        self.first(result)["id"] = "invented"
        with self.assertRaises(ValueError):
            collector.validate_menu(result)
        result = self.parse()
        result["days"][0]["meals"].append(copy.deepcopy(self.first(result)))
        with self.assertRaises(ValueError):
            collector.validate_menu(result)

    def test_schema_validation_rejects_bool_prices_invalid_dates_and_hashes(self):
        for field in ("bool", "date", "key", "coverage", "notices"):
            result = self.parse()
            if field == "bool": self.first(result)["prices"]["student"] = True
            if field == "date": result["days"][0]["date"] = "2026-02-30"
            if field == "key": self.first(result)["translation_key"] = "0" * 64
            if field == "coverage": result["coverage"]["end"] = "2026-10-01"
            if field == "notices": self.first(result)["notices"] = "vegan"
            with self.subTest(field=field), self.assertRaises(ValueError):
                collector.validate_menu(result)

    def test_previous_price_disappearance_is_rejected(self):
        previous = self.parse()
        current = self.parse(page(meal(prices="")))
        with self.assertRaises(ValueError):
            collector.validate_menu(current, previous)

    def test_suspicious_loss_of_meals_on_overlapping_day_is_rejected(self):
        previous = self.parse(page("".join(meal(name=f"Dish {i}") for i in range(6))))
        current = self.parse(page(meal(name="Dish 0")))
        with self.assertRaises(ValueError):
            collector.validate_menu(current, previous)

    def test_legitimate_rolling_coverage_is_allowed(self):
        previous = self.parse()
        current = self.parse(page(date="28.09.2026"))
        collector.validate_menu(current, previous)

    def test_fetch_has_bounded_retries_and_timeout(self):
        with (patch.object(collector, "urlopen", side_effect=OSError("offline")) as fetch,
              patch.object(collector.time, "sleep")):
            with self.assertRaises(ValueError):
                collector.fetch_html()
        self.assertGreater(fetch.call_count, 0)
        self.assertLessEqual(fetch.call_count, 3)
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 30 for call in fetch.call_args_list))


if __name__ == "__main__":
    unittest.main()
