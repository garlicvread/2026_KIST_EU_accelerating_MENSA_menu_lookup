"""이 테스트 모듈은 공개할 메뉴와 번역의 요구 조건을 확인하기 위해 실제 파서가 만든 메뉴 데이터와 메모리의 번역 데이터를 검사합니다."""

import copy
from datetime import date, datetime
import unittest
from unittest.mock import patch

from mensa.publication import validate_publication
from scripts.menu_source import parse_menu
from scripts.translations import source_for, source_key
from test_menu_source import meal, page


def translation_entry(source):
    return {
        "source": copy.deepcopy(source),
        "en": {"name": "Vegetable dish", "components": ["Rice", "Sauce and vegetables"]},
        "ko": {"name": "채소 요리", "components": ["쌀밥", "소스와 채소"]},
        "origin": "editorial-draft", "model": "fixture", "prompt_version": "menu-v4",
    }


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.menu = parse_menu(page(), "2026-09-21T10:00:00Z")
        self.record = self.menu["days"][0]["meals"][0]
        self.key = self.record["translation_key"]
        self.glossary = {
            "vegan": {"en": "Vegan", "ko": "비건"},
            "Weizen": {"en": "Wheat", "ko": "밀"},
            "Sellerie": {"en": "Celery", "ko": "셀러리"},
            "Milch und Laktose": {"en": "Milk and lactose", "ko": "우유 및 유당"},
        }
        self.cache = {
            "schema_version": 1,
            "entries": {self.key: translation_entry(source_for(self.record))},
            "notices": copy.deepcopy(self.glossary),
        }

    def validate(self, **kwargs):
        return validate_publication(
            self.menu, self.cache, notice_glossary=self.glossary, **kwargs
        )

    def test_accepts_complete_cache_without_implicit_clock(self):
        self.assertIsNone(self.validate())

    def test_missing_meal_fails_even_when_notices_are_complete(self):
        self.cache["entries"].clear()
        with self.assertRaisesRegex(ValueError, self.key):
            self.validate()

    def test_rejects_noninteger_prices(self):
        for amount in (True, 350.0, "350"):
            with self.subTest(amount=amount):
                self.record["prices"]["student"] = amount
                with self.assertRaisesRegex(ValueError, "integer cents"):
                    self.validate()


    def test_rejects_raw_price_provenance_for_another_meal(self):
        self.record["price_source"]["name"] = "Other meal"
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.validate()

    def test_rejects_overlapping_previous_price_loss(self):
        previous = copy.deepcopy(self.menu)
        self.menu = parse_menu(page(meal(prices="")), "2026-09-21T10:00:00Z")
        with self.assertRaisesRegex(ValueError, "prices disappeared"):
            self.validate(previous=previous)

    def test_unknown_current_notice_is_contextual(self):
        self.record["notices"].append("Unreviewed notice")
        self.cache["notices"]["Unreviewed notice"] = {"en": "Unknown", "ko": "미검토"}
        with self.assertRaisesRegex(ValueError, "Unreviewed notice"):
            self.validate()


    def test_missing_cache_notice_is_contextual(self):
        del self.cache["notices"]["Sellerie"]
        with self.assertRaisesRegex(ValueError, "Sellerie"):
            self.validate()

    def test_missing_notice_cache_object_fails(self):
        del self.cache["notices"]
        with self.assertRaisesRegex(ValueError, "notice|Notice"):
            self.validate()

    def test_reworded_cache_notice_is_contextual(self):
        for language in ("en", "ko"):
            with self.subTest(language=language):
                self.cache["notices"] = copy.deepcopy(self.glossary)
                self.cache["notices"]["Sellerie"][language] += " changed"
                with self.assertRaisesRegex(ValueError, "Sellerie"):
                    self.validate()

    def test_rejects_malformed_glossary_even_unused_labels(self):
        for malformed in (None, [], {"unused": {"en": "Only English"}}):
            with self.subTest(glossary=malformed), self.assertRaises(ValueError):
                validate_publication(self.menu, self.cache, notice_glossary=malformed)

    def test_rejects_missing_or_empty_language_text(self):
        for language in ("en", "ko"):
            for field in ("name", "components"):
                with self.subTest(language=language, field=field):
                    cache = copy.deepcopy(self.cache)
                    del cache["entries"][self.key][language][field]
                    with self.assertRaises(ValueError):
                        validate_publication(self.menu, cache, notice_glossary=self.glossary)
            with self.subTest(language=language, name="blank"):
                cache = copy.deepcopy(self.cache)
                cache["entries"][self.key][language]["name"] = " "
                with self.assertRaises(ValueError):
                    validate_publication(self.menu, cache, notice_glossary=self.glossary)


    def test_accepts_valid_historical_extra_entry(self):
        source = {"name_de": "Historical dish", "components": ["Reis", "Soße & Gemüse"]}
        self.cache["entries"][source_key(source)] = translation_entry(source)
        self.assertIsNone(self.validate())

    def test_rejects_invalid_historical_extra_entry(self):
        self.cache["entries"]["historical"] = None
        with self.assertRaises(ValueError):
            self.validate()

    def test_rejects_malformed_cache(self):
        for cache in (None, [], {}, {"schema_version": 1, "entries": []}):
            with self.subTest(cache=cache), self.assertRaises(ValueError):
                validate_publication(self.menu, cache, notice_glossary=self.glossary)

    def test_rejects_malformed_menu(self):
        for menu in (None, [], {}, {"schema_version": 1}):
            with self.subTest(menu=menu), self.assertRaises(ValueError):
                validate_publication(menu, self.cache, notice_glossary=self.glossary)

    def test_rejects_malformed_previous(self):
        with self.assertRaises(ValueError):
            self.validate(previous={})

    def test_rejects_stale_menu_only_when_today_is_supplied(self):
        for today in ("2026-09-22", date(2026, 9, 22)):
            with self.subTest(today=today), self.assertRaisesRegex(ValueError, "2026-09-21"):
                self.validate(today=today)

    def test_accepts_today_at_and_before_coverage_end(self):
        for today in ("2026-09-21", date(2026, 9, 21), "2026-09-20"):
            with self.subTest(today=today):
                self.assertIsNone(self.validate(today=today))

    def test_rejects_malformed_today(self):
        for today in ("", "2026-02-30", "20260921", "2026-W39-1", "21.09.2026",
                      "2026-09-21T00:00:00", 0, True, [], datetime(2026, 9, 21)):
            with self.subTest(today=today), self.assertRaisesRegex(ValueError, "today"):
                self.validate(today=today)

    def test_does_not_mutate_valid_or_rejected_inputs(self):
        previous = copy.deepcopy(self.menu)
        before = copy.deepcopy((self.menu, self.cache, self.glossary, previous))
        self.validate(previous=previous, today=date(2026, 9, 21))
        self.assertEqual((self.menu, self.cache, self.glossary, previous), before)
        self.cache["entries"].clear()
        before = copy.deepcopy((self.menu, self.cache, self.glossary, previous))
        with self.assertRaises(ValueError):
            self.validate(previous=previous)
        self.assertEqual((self.menu, self.cache, self.glossary, previous), before)

    def test_no_io_or_generation_after_fixtures_are_prepared(self):
        forbidden = AssertionError("Publication validation attempted I/O or generation")
        with (patch("builtins.open", side_effect=forbidden),
              patch("io.open", side_effect=forbidden),
              patch("os.open", side_effect=forbidden),
              patch("socket.socket", side_effect=forbidden),
              patch("scripts.menu_source.urlopen", side_effect=forbidden),
              patch("scripts.notices.load_glossary", side_effect=forbidden),
              patch("scripts.translations.request_translation", side_effect=forbidden)):
            self.assertIsNone(self.validate())


if __name__ == "__main__":
    unittest.main()
