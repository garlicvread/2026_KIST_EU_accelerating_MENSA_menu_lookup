"""이 테스트 모듈은 공개할 메뉴와 번역의 요구 조건을 확인하기 위해 실제 파서가 만든 메뉴 데이터와 메모리의 번역 데이터를 검사합니다."""

import copy
import unittest

from mensa.publication import validate_publication
from scripts.menu_source import parse_menu
from scripts.translations import source_for
from test_menu_source import page


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


    def test_rejects_raw_price_provenance_for_another_meal(self):
        self.record["price_source"]["name"] = "Other meal"
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.validate()


    def test_unknown_current_notice_is_contextual(self):
        self.record["notices"].append("Unreviewed notice")
        self.cache["notices"]["Unreviewed notice"] = {"en": "Unknown", "ko": "미검토"}
        with self.assertRaisesRegex(ValueError, "Unreviewed notice"):
            self.validate()


    def test_reworded_cache_notice_is_contextual(self):
        for language in ("en", "ko"):
            with self.subTest(language=language):
                self.cache["notices"] = copy.deepcopy(self.glossary)
                self.cache["notices"]["Sellerie"][language] += " changed"
                with self.assertRaisesRegex(ValueError, "Sellerie"):
                    self.validate()


    def test_rejects_missing_korean_translation(self):
        cache = copy.deepcopy(self.cache)
        del cache["entries"][self.key]["ko"]
        with self.assertRaises(ValueError):
            validate_publication(self.menu, cache, notice_glossary=self.glossary)


    def test_rejects_malformed_cache(self):
        with self.assertRaises(ValueError):
            validate_publication(self.menu, {"schema_version": 1, "entries": []}, notice_glossary=self.glossary)


if __name__ == "__main__":
    unittest.main()
