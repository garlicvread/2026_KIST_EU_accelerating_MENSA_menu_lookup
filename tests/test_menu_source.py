"""이 테스트 모듈은 원본 식단의 가격·메뉴·날짜를 수집하고, 정보가 누락되면 게시를 중단하는지 확인합니다."""

import copy
import unittest
from unittest.mock import patch


from scripts import menu_source as collector


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

    def test_preserves_every_component_and_notice(self):
        record = self.first(self.parse())
        self.assertEqual(record["notices"], ["vegan", "Weizen"])
        self.assertEqual(record["components"], [
            {"name_de": "Reis", "notices": []},
            {"name_de": "Soße & Gemüse", "notices": ["Sellerie", "Milch und Laktose"]}])


    def test_source_genuinely_pending_remains_null(self):
        record = self.first(self.parse(page(meal(category="Mensacafé", prices=""), category="Mensacafé")))
        self.assertIsNone(record["prices"])
        self.assertEqual(record["price_status"], "source_pending")
        self.assertIsNone(record["price_source"]["raw"])

    def test_prices_elsewhere_never_used_for_pending_meal(self):
        html = page(meal(prices="")).replace("<body>", f"<body><aside>{PRICES}</aside>")
        self.assertIsNone(self.first(self.parse(html))["prices"])

    def test_single_trailing_price_applies_to_every_dish_in_counter(self):
        records = self.parse(page(meal(name="Käsespätzle", prices="") +
                                  meal(name="Vegan: Rote Bete Puffer", prices="") +
                                  meal(name="Salatbuffet")))["days"][0]["meals"]
        for record in records:
            self.assertEqual(record["prices"], {"student": 350, "staff": 465, "guest": 535})
            self.assertEqual(record["price_status"], "verified")
        for record in records[:-1]:
            self.assertEqual(record["price_source"]["scope"], "counter")
            self.assertEqual(record["price_source"]["name"], "Salatbuffet")
            self.assertEqual(record["price_source"]["raw"], records[-1]["price_source"]["raw"])

    def test_nontrailing_or_multiple_price_blocks_keep_individual_prices(self):
        for content in (meal(name="A") + meal(name="B", prices=""),
                        meal(name="A") + meal(name="B", prices="") +
                        meal(name="C", prices=PRICES.replace("3,50", "4,50"))):
            with self.subTest(content=content):
                records = self.parse(page(content))["days"][0]["meals"]
                self.assertEqual(records[0]["prices"]["student"], 350)
                self.assertIsNone(records[1]["prices"])
                if len(records) == 3:
                    self.assertEqual(records[-1]["prices"]["student"], 450)

    def test_shared_price_provenance_cannot_cross_counter_or_change_owner(self):
        original = self.parse(page(meal(name="A", prices="") + meal(name="B")))
        for field, value in (("location", "Other counter"), ("source_name", "Unknown dish"),
                             ("source_raw", "S: 4,50 | M: 4,65 | G: 5,35")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                menu = copy.deepcopy(original)
                record = menu["days"][0]["meals"][0]
                if field == "location":
                    record[field] = value
                else:
                    record["price_source"][field.removeprefix("source_")] = value
                collector.validate_menu(menu)

    def test_missing_price_group_fails(self):
        with self.assertRaises(ValueError):
            self.parse(page(meal(prices=PRICES.replace(" | G: 5,35", ""))))


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
        html = page(meal() + meal() + meal(name="Vegan: Rote Bete Puffer"))
        records = self.parse(html)["days"][0]["meals"]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[1]["id"], records[0]["id"] + "-2")
        self.assertEqual(records[0]["translation_key"], records[1]["translation_key"])
        self.assertEqual(records[2]["name_de"], "Vegan: Rote Bete Puffer")
        self.assertNotEqual(records[0]["translation_key"], records[2]["translation_key"])
        self.assertNotEqual(records[0]["id"], records[2]["id"])
        self.assertEqual(records, self.parse(html)["days"][0]["meals"])


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
        with self.assertRaises(ValueError):
            self.parse(page().replace("class='meal'", "class='renamed'"))


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


    def test_suspicious_loss_of_meals_on_overlapping_day_is_rejected(self):
        previous = self.parse(page("".join(meal(name=f"Dish {i}") for i in range(6))))
        current = self.parse(page(meal(name="Dish 0")))
        with self.assertRaises(ValueError):
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
