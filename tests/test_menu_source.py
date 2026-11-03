"""이 테스트 모듈은 원본 식단의 가격·메뉴·날짜를 보존하고, 잘못된 구조·가격 출처를 거부하는지 확인합니다."""

import copy
from itertools import permutations
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

    def test_labeled_price_formats_preserve_amounts_and_raw_source(self):
        entries = ("S : 3,50 €", "M: 4.65€", "G: 5,35")
        variants = [" | ".join(order) for order in permutations(entries)]
        variants += ["S: 3,50 € | M: 4,65 € | G: 5,35 €",
                     "S:\t3.50\u00a0| M: 4.65 | G: 5.35"]
        baseline = self.first(self.parse())
        for raw in variants:
            with self.subTest(raw=raw):
                record = self.first(self.parse(page(meal(prices=f"<p><strong>Preise:</strong> {raw}</p>"))))
                expected = copy.deepcopy(baseline)
                expected["price_source"]["raw"] = " ".join(raw.split())
                self.assertEqual(record, expected)

    def test_price_paragraph_does_not_depend_on_inline_bolding(self):
        for label in ("<b>Preise:</b>", "<span>Preise:</span>",
                      "<strong><span>Preise:</span></strong>", "Preise:", "<b>Preise :</b>"):
            with self.subTest(label=label):
                prices = f"<p>{label} S: 3,50 | M: 4,65 | G: 5,35</p>"
                self.assertEqual(self.first(self.parse(page(meal(prices=prices)))), self.first(self.parse()))

    def test_inline_elements_can_split_price_marker_and_group_labels(self):
        prices = "<p><b>Preise</b>: <span>S</span>: 3,50 | M<span>:</span> 4,65 | G: 5,35</p>"
        self.assertEqual(self.first(self.parse(page(meal(prices=prices)))), self.first(self.parse()))

    def test_unconsumed_price_labels_split_across_inline_elements_are_rejected(self):
        orphan = "<div><span>S</span>: 9,00 | <b>M</b>: 9,00 | G<span>:</span> 9,00</div>"
        with self.assertRaisesRegex(ValueError, "Source inventory mismatch"):
            self.parse(page(meal(prices="") + orphan))

    def test_price_contract_rejects_ambiguous_or_invalid_labeled_amounts(self):
        invalid = (
            "S: 3,50 | S: 4,65 | G: 5,35",
            "S: 3,50 | G: 5,35",
            "S: 3,50 | M: 4,65 | X: 5,35",
            "S: 3,50 | M: 4,65 | G: 5,35 | G: 5,35",
            "S: 3,5 | M: 4,65 | G: 5,35",
            "S: -3,50 | M: 4,65 | G: 5,35",
            "S: 0,00 | M: 4,65 | G: 5,35",
            "S: 1000,01 | M: 4,65 | G: 5,35",
            "S: 3,50 EUR | M: 4,65 | G: 5,35",
            "S: 3,50 | M: 4,65 | G: 5,35 extra",
            "S: ٣,٥٠ | M: 4,65 | G: 5,35",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                result = self.parse()
                self.first(result)["price_source"]["raw"] = raw
                collector.validate_menu(result)

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

    def test_component_option_prices_do_not_block_other_meal_prices(self):
        option = "oder Nusscremefüllung\nS: 2,50 € B: 2,50 € G: 3,10 €"
        crepe_prices = "<p><strong>Preise:</strong> S: 2,20 | M: 2,20 | G: 2,80</p>"
        brew_prices = "<p><strong>Preise:</strong> S: 3,90 | M: 5,05 | G: 5,75</p>"
        crepe = meal(name="Crêpes mit Zimt und Zucker", category="Mensacafé", prices=crepe_prices)
        crepe = crepe.replace("Reis</div>", option + "</div>", 1)
        brew = meal(name="Grünes Thai-Curry mit Brew Bites", category="Mensacafé", prices=brew_prices)
        records = self.parse(page(crepe + brew, category="Mensacafé"))["days"][0]["meals"]
        self.assertEqual(records[0]["components"][0]["name_de"], " ".join(option.split()))
        self.assertEqual(records[0]["prices"], {"student": 220, "staff": 220, "guest": 280})
        self.assertEqual(records[1]["prices"], {"student": 390, "staff": 505, "guest": 575})
        self.assertEqual(records[1]["price_source"]["name"], "Grünes Thai-Curry mit Brew Bites")

    def test_component_price_group_text_stays_text_when_meal_has_no_price(self):
        option = "Wahlweise S: 2,50 | M: 2,50 | G: 3,10"
        html = page(meal(prices="").replace("Reis</div>", option + "</div>", 1))
        record = self.first(self.parse(html))
        self.assertEqual(record["components"][0]["name_de"], option)
        self.assertIsNone(record["prices"])
        self.assertEqual(record["price_status"], "source_pending")

    def test_formatted_price_text_in_component_is_not_a_meal_price_block(self):
        option = "Alternative <strong>Preise:</strong> S: 2,50 | M: 2,50 | G: 3,10"
        for price in (PRICES, ""):
            with self.subTest(price=price):
                html = page(meal(prices=price).replace("Reis</div>", option + "</div>", 1))
                record = self.first(self.parse(html))
                self.assertEqual(record["components"][0]["name_de"], "Alternative Preise: S: 2,50 | M: 2,50 | G: 3,10")
                self.assertEqual(record["prices"], {"student": 350, "staff": 465, "guest": 535} if price else None)

    def test_unextracted_price_groups_outside_components_are_still_rejected(self):
        html = page(meal().replace(PRICES, PRICES + "<div>S: 9,00 | M: 9,00 | G: 9,00</div>"))
        with self.assertRaisesRegex(ValueError, "Source inventory mismatch"):
            self.parse(html)

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


    def test_price_block_requires_closed_paragraph_and_prefix(self):
        for prices in (PRICES.replace("<p>", "<section>").replace("</p>", "</section>"),
                       PRICES.replace("</p>", ""),
                       PRICES.replace("<p>", "<p>Alternative "), PRICES + PRICES):
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


    def test_meal_count_changes_on_overlapping_day_are_accepted(self):
        previous = self.parse(page("".join(meal(name=f"Dish {i}") for i in range(6))))
        for count in (1, 3, 6, 12):
            with self.subTest(count=count):
                current = self.parse(page("".join(meal(name=f"Dish {i}") for i in range(count))))
                collector.validate_menu(current, previous)

    def test_duplicate_meal_count_changes_with_prices_are_accepted(self):
        previous = self.parse(page(meal() * 4))
        for count in (3, 6):
            with self.subTest(count=count):
                current = self.parse(page(meal() * count))
                collector.validate_menu(current, previous)

    def test_replacing_or_removing_named_meals_is_accepted(self):
        previous = self.parse(page("".join(meal(name=f"Old dish {i}") for i in range(6))))
        current = self.parse(page(meal(name="Replacement dish")))
        collector.validate_menu(current, previous)

    def test_information_only_day_can_replace_previous_meals(self):
        previous = self.parse()
        current = self.parse(page(meal(name="Mensa geschlossen", category="Information", prices=""),
                                  category="Information"))
        self.assertEqual(current["days"][0]["meals"], [])
        collector.validate_menu(current, previous)

    def test_valid_empty_counter_can_replace_previous_meals(self):
        previous = self.parse()
        current = self.parse(page("<div>Heute geschlossen</div>"))
        self.assertEqual(current["days"][0]["meals"], [])
        collector.validate_menu(current, previous)

    def test_valid_day_without_counters_can_replace_previous_meals(self):
        previous = self.parse()
        html = page("").replace("<div class='counter'><h3>Wahlessen</h3><p>Aufgang C</p></div>",
                                "<div>Mensa geschlossen</div>")
        current = self.parse(html)
        self.assertEqual(current["days"][0]["meals"], [])
        collector.validate_menu(current, previous)

    def test_mixed_priced_and_pending_duplicates_remain_accepted(self):
        previous = self.parse(page(meal() + meal(prices="")))
        collector.validate_menu(copy.deepcopy(previous), previous)

    def test_current_source_missing_prices_replace_previous_prices_without_guessing(self):
        previous = self.parse(page(meal() * 3))
        original = copy.deepcopy(previous)
        current = self.parse(page(meal(prices="") * 3))
        collector.validate_menu(current, previous)
        self.assertEqual(previous, original)
        self.assertEqual(len(current["days"][0]["meals"]), 3)
        for record in current["days"][0]["meals"]:
            self.assertIsNone(record["prices"])
            self.assertEqual(record["price_status"], "source_pending")
            self.assertIsNone(record["price_source"]["raw"])


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
