import copy
import json
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from mensa import notice_contract
from scripts import notices


def sample_glossary():
    return {
        "vegan": {"en": "Vegan", "ko": "비건"},
        "Weizen": {"en": "Wheat", "ko": "밀"},
        "Senf": {"en": "Mustard", "ko": "겨자"},
        "Milch und Laktose": {"en": "Milk and lactose", "ko": "우유 및 유당"},
    }


def sample_menu():
    return {"days": [{"meals": [
        {"notices": ["vegan", "Weizen", "Weizen"], "components": [
            {"notices": ["Weizen", "Milch und Laktose"]},
            {"notices": ["Senf", "Senf"]},
        ]},
        {"notices": ["vegan"], "components": [{}]},
    ]}]}


class NoticeSelectionTests(unittest.TestCase):
    def selector(self):
        selector = getattr(notice_contract, "translate_notice_labels", None)
        self.assertTrue(callable(selector), "Pure translate_notice_labels helper is required")
        return selector

    def test_selects_only_requested_labels_in_sorted_order(self):
        glossary = sample_glossary()
        selected = self.selector()({"vegan", "Weizen", "Senf"}, glossary)
        self.assertEqual(list(selected), ["Senf", "Weizen", "vegan"])
        self.assertEqual(selected, {label: glossary[label] for label in ("Senf", "Weizen", "vegan")})
        self.assertNotIn("Milch und Laktose", selected)

    def test_accepts_frozenset_and_empty_label_sets(self):
        select = self.selector()
        glossary = sample_glossary()
        self.assertEqual(select(frozenset({"Weizen"}), glossary), {"Weizen": {"en": "Wheat", "ko": "밀"}})
        self.assertEqual(select(set(), glossary), {})
        self.assertEqual(select(frozenset(), {}), {})

    def test_returns_independent_entry_copies_without_mutating_inputs(self):
        labels = {"Weizen", "vegan"}
        glossary = sample_glossary()
        original = copy.deepcopy((labels, glossary))
        select = self.selector()
        first = select(labels, glossary)
        second = select(labels, glossary)
        self.assertEqual((labels, glossary), original)
        self.assertIsNot(first["Weizen"], glossary["Weizen"])
        self.assertIsNot(first["Weizen"], second["Weizen"])
        first["Weizen"]["en"] = "Changed result"
        first.pop("vegan")
        self.assertEqual(second["Weizen"]["en"], "Wheat")
        self.assertEqual((labels, glossary), original)

    def test_preserves_exact_labels_and_translation_text(self):
        glossary = {" Weizen ": {"en": " Wheat ", "ko": " 밀 "}}
        self.assertEqual(self.selector()({" Weizen "}, glossary), glossary)

    def test_rejects_label_containers_other_than_sets_with_exact_error(self):
        select = self.selector()
        for labels in (None, [], ["Weizen"], ("Weizen",), {"Weizen": True}, "Weizen", 1, True):
            with self.subTest(labels=labels), self.assertRaises(ValueError) as caught:
                select(labels, sample_glossary())
            self.assertEqual(str(caught.exception), "Notice labels must be a set of nonempty strings")

    def test_rejects_invalid_set_labels_with_exact_error(self):
        select = self.selector()
        for label in (None, 1, True, "", " ", "\nWeizen", "Wei\tzen", "x" * 1001):
            for factory in (set, frozenset):
                with self.subTest(label=label, container=factory.__name__), self.assertRaises(ValueError) as caught:
                    select(factory({label}), sample_glossary())
                self.assertEqual(str(caught.exception), "Notice labels must be a set of nonempty strings")

    def test_validates_complete_glossary_even_without_selected_labels(self):
        select = self.selector()
        for labels in (set(), {"Weizen"}):
            for malformed in (None, [], "invalid", 7):
                with self.subTest(labels=labels, glossary=malformed), self.assertRaises(ValueError) as caught:
                    select(labels, malformed)
                self.assertEqual(str(caught.exception), "Notice translations must be an object")

    def test_rejects_malformed_unused_glossary_entries(self):
        select = self.selector()
        for labels in (set(), {"Weizen"}):
            for source, entry, expected in (
                (" ", {"en": "Text", "ko": "텍스트"}, "Notice translation needs an exact German key and exactly en/ko text"),
                ("Unused", None, "Notice translation needs an exact German key and exactly en/ko text"),
                ("Unused", {"en": "Text"}, "Notice translation needs an exact German key and exactly en/ko text"),
                ("Unused", {"en": "Text", "ko": "텍스트", "fr": "Texte"}, "Notice translation needs an exact German key and exactly en/ko text"),
                ("Unused", {"en": " ", "ko": "텍스트"}, "Notice translations must contain nonempty en/ko text"),
                ("Unused", {"en": "Text", "ko": "텍\n스트"}, "Notice translations must contain nonempty en/ko text"),
                ("Unused", {"en": 7, "ko": "텍스트"}, "Notice translations must contain nonempty en/ko text"),
            ):
                glossary = {**sample_glossary(), source: entry}
                before = copy.deepcopy(glossary)
                with self.subTest(labels=labels, source=source, entry=entry), self.assertRaises(ValueError) as caught:
                    select(labels, glossary)
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(glossary, before)

    def test_rejects_unknown_labels_in_sorted_order_with_exact_existing_error(self):
        labels = {"weizen", "Weizen ", "Weizen", "Unreviewed 'label'"}
        original = labels.copy()
        with self.assertRaises(ValueError) as caught:
            self.selector()(labels, sample_glossary())
        self.assertEqual(str(caught.exception),
                         'Unknown notice labels: "Unreviewed \'label\'", \'Weizen \', \'weizen\'; '
                         'review and add en/ko translations to data/notice-translations.json before retrying')
        self.assertEqual(labels, original)

    def test_selection_requires_no_file_reads(self):
        select = self.selector()
        with patch("builtins.open", side_effect=AssertionError("Selection must avoid I/O")), \
                patch.object(Path, "read_text", side_effect=AssertionError("Selection must avoid I/O")):
            self.assertEqual(select({"Weizen"}, sample_glossary()), {"Weizen": {"en": "Wheat", "ko": "밀"}})


class NoticeAdapterTests(unittest.TestCase):
    def test_reviewed_glossary_covers_nuts_blackened_food_and_animal_rennet(self):
        # 실제 수집을 중단시킨 안내 문구를 메뉴와 구성품에서 검토 사전으로 조회합니다.
        menu = {"days": [{"meals": [{
            "notices": ["Pistazien", "Walnüsse"],
            "components": [
                {"notices": ["geschwärzt"]},
                {"notices": ["mit tierischem LAB"]},
            ],
        }]}]}
        expected = {
            "Pistazien": {"en": "Pistachios", "ko": "피스타치오"},
            "Walnüsse": {"en": "Walnuts", "ko": "호두"},
            "geschwärzt": {"en": "Blackened with iron compounds", "ko": "철 화합물로 검게 착색 처리됨"},
            "mit tierischem LAB": {"en": "Made with animal rennet", "ko": "동물성 렌넷 사용"},
        }
        self.assertEqual(notices.translated_notices(menu), expected)
        notices.require_notice_coverage(menu, {"notices": expected})

    def test_collects_deduplicated_meal_and_component_labels(self):
        menu = sample_menu()
        before = copy.deepcopy(menu)
        self.assertEqual(notice_contract.source_notices(menu), {"vegan", "Weizen", "Milch und Laktose", "Senf"})
        selected = notices.translated_notices(menu, glossary=sample_glossary())
        self.assertEqual(list(selected), ["Milch und Laktose", "Senf", "Weizen", "vegan"])
        self.assertEqual(selected, sample_glossary())
        self.assertEqual(menu, before)

    def test_explicit_glossary_avoids_default_load_and_file_reads(self):
        with patch.object(notices, "load_glossary", side_effect=AssertionError("Explicit glossary must avoid default load")), \
                patch("builtins.open", side_effect=AssertionError("Explicit glossary must avoid I/O")), \
                patch.object(Path, "read_text", side_effect=AssertionError("Explicit glossary must avoid I/O")):
            self.assertEqual(notices.translated_notices(sample_menu(), glossary=sample_glossary()), sample_glossary())
            self.assertEqual(notices.translated_notices({"days": []}, glossary={}), {})

    def test_default_glossary_loads_once_after_collecting_source_labels(self):
        events = []
        source = notice_contract.source_notices

        def collect(menu):
            events.append("source")
            return source(menu)

        def load():
            events.append("load")
            return sample_glossary()

        with patch.object(notices, "source_notices", side_effect=collect), \
                patch.object(notices, "load_glossary", side_effect=load) as loader:
            self.assertEqual(notices.translated_notices(sample_menu()), sample_glossary())
        self.assertEqual(events, ["source", "load"])
        loader.assert_called_once_with()

    def test_invalid_source_is_rejected_before_default_load(self):
        invalid = {"days": [{"meals": [{"notices": [" "]}]}]}
        with patch.object(notices, "load_glossary", side_effect=AssertionError("Invalid source must fail before load")) as loader:
            with self.assertRaises(ValueError) as caught:
                notices.translated_notices(invalid)
        self.assertEqual(str(caught.exception), "Source notices must be a list of nonempty strings")
        loader.assert_not_called()



    def test_default_and_explicit_glossary_files_use_the_reviewed_validation(self):
        with TemporaryDirectory() as folder:
            default, explicit = Path(folder) / 'default.json', Path(folder) / 'checkout.json'
            glossary = sample_glossary()
            checkout = copy.deepcopy(glossary)
            checkout['vegan'] = {'en': 'Checkout vegan', 'ko': '체크아웃 비건'}
            for path, data in ((default, glossary), (explicit, checkout)):
                path.write_text(json.dumps({'schema_version': 1, 'notices': data}), encoding='utf-8')
            with patch.object(notices, 'GLOSSARY_PATH', default):
                self.assertEqual(notices.load_glossary(), glossary)
                try:
                    selected = notices.load_glossary(explicit)
                except TypeError as error:
                    self.fail(f'Explicit glossary path is unavailable: {error}')
                self.assertEqual(selected, checkout)
                self.assertEqual(notices.load_glossary(), glossary)
                explicit.write_text(json.dumps({'schema_version': 2, 'notices': checkout}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Unsupported notice translation glossary'):
                    notices.load_glossary(explicit)


if __name__ == "__main__":
    unittest.main()
