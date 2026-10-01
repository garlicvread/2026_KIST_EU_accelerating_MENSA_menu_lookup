"""이 테스트 모듈은 TranslationService가 기본 파일 읽기나 통신 함수에 의존하지 않고 명시적으로 전달받은 용어집 제공 함수와 번역 요청 함수로 데이터를 처리하는지 확인합니다."""

import copy
import importlib
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import translations
from mensa.translation_contract import source_key, generation_identity


class TranslationServiceTests(unittest.TestCase):
    def setUp(self):
        self.glossary = {"Weizen": {"en": "Wheat", "ko": "밀"}}
        self.source = {"name_de": "Testgericht", "components": ["Beilage"]}
        self.meal = {"name_de": self.source["name_de"],
                     "components": [{"name_de": "Beilage", "notices": ["Weizen"]}],
                     "translation_key": source_key(self.source)}
        self.menu = {"days": [{"meals": [self.meal]}]}
        self.config = {"model": "fixture", "url": "http://127.0.0.1/api/chat", "provider": "ollama", "key": ""}
        self.result = {"en": {"name": "Test dish", "components": ["Side"]},
                       "ko": {"name": "시험 요리", "components": ["곁들임"]}}

    def service_class(self):
        path = Path(__file__).resolve().parents[1] / "mensa" / "translation_service.py"
        self.assertTrue(path.is_file(), "Translation application service is required")
        service = getattr(importlib.import_module("mensa.translation_service"), "TranslationService", None)
        self.assertTrue(callable(service), "TranslationService is required")
        return service

    def service(self, *, glossary=None, translate=None):
        return self.service_class()(glossary_provider=lambda: self.glossary if glossary is None else glossary,
                                    translate=translate if translate is not None else lambda *args: copy.deepcopy(self.result))

    def test_constructor_requires_callable_ports_without_calling_them(self):
        service = self.service_class()
        supplier, generate = Mock(), Mock()
        service(glossary_provider=supplier, translate=generate)
        supplier.assert_not_called()
        generate.assert_not_called()
        for bad in (None, False, 1, [], {}):
            with self.subTest(port="glossary", value=bad), self.assertRaises(TypeError):
                service(glossary_provider=bad, translate=generate)
            with self.subTest(port="translate", value=bad), self.assertRaises(TypeError):
                service(glossary_provider=supplier, translate=bad)

    def test_build_uses_only_supplied_ports_and_preserves_inputs(self):
        service = self.service()
        previous = {"schema_version": 1, "entries": {}}
        before = copy.deepcopy((self.menu, previous, self.glossary, self.config, self.result))
        with patch.object(translations, "load_glossary", side_effect=AssertionError("No adapter default")), \
                patch.object(translations, "request_translation", side_effect=AssertionError("No default transport")), \
                patch.object(Path, "read_text", side_effect=AssertionError("No file reads")):
            cache = service.build(self.menu, previous, {}, self.config)
        self.assertEqual(cache["entries"][self.meal["translation_key"]]["en"], self.result["en"])
        self.assertEqual((self.menu, previous, self.glossary, self.config, self.result), before)
        cache["notices"]["Weizen"]["en"] = "Changed"
        self.assertEqual(self.glossary["Weizen"]["en"], "Wheat")

    def test_one_glossary_snapshot_drives_notices_and_identity(self):
        supplier = Mock(side_effect=[self.glossary, {}])
        generate = Mock(return_value=self.result)
        service = self.service_class()(glossary_provider=supplier, translate=generate)
        cache = service.build(self.menu, {}, {}, self.config)
        supplier.assert_called_once_with()
        self.assertEqual(cache["notices"], self.glossary)
        self.assertEqual(cache["entries"][self.meal["translation_key"]]["generation_identity"],
                         generation_identity(self.config, notice_glossary=self.glossary))
        generate.assert_called_once_with(self.source, self.config)

    def test_invalid_checkpoint_or_previous_cache_fails_before_ports(self):
        supplier, generate = Mock(), Mock()
        service = self.service_class()(glossary_provider=supplier, translate=generate)
        with self.assertRaisesRegex(TypeError, "checkpoint must be callable"):
            service.build(self.menu, {}, {}, self.config, checkpoint=False)
        with self.assertRaises(ValueError):
            service.build(self.menu, {"schema_version": 2, "entries": {}}, {}, self.config)
        supplier.assert_not_called()
        generate.assert_not_called()

    def test_unknown_label_fails_before_generation(self):
        generate = Mock(side_effect=AssertionError("No generation before notice validation"))
        with self.assertRaisesRegex(ValueError, "Unknown notice labels: 'Weizen'"):
            self.service(glossary={}, translate=generate).build(self.menu, {}, {}, self.config)
        generate.assert_not_called()

    def test_supplier_failure_propagates_without_generation(self):
        failure = OSError("fixture read failure")
        generate = Mock()
        service = self.service_class()(glossary_provider=Mock(side_effect=failure), translate=generate)
        with self.assertRaises(OSError) as caught:
            service.build(self.menu, {}, {}, self.config)
        self.assertIs(caught.exception, failure)
        generate.assert_not_called()

    def test_editorial_and_offline_paths_do_not_call_generation(self):
        generate = Mock(side_effect=AssertionError("No generation"))
        service = self.service(translate=generate)
        phrases = {"Testgericht": {"en": "Editorial dish", "ko": "편집 요리"},
                   "Beilage": {"en": "Editorial side", "ko": "편집 곁들임"}}
        cache = service.build(self.menu, {}, phrases, self.config)
        self.assertEqual(cache["entries"][self.meal["translation_key"]]["origin"], "editorial-draft")
        retained = service.build(self.menu, cache, {}, None)
        self.assertEqual(retained, cache)
        missing = service.build(self.menu, {}, {}, None)
        self.assertEqual(missing["entries"], {})
        generate.assert_not_called()

    def test_checkpoint_can_resume_accepted_entries_without_regeneration(self):
        generate = Mock(return_value=self.result)
        service = self.service(translate=generate)
        snapshots = []
        final = service.build(self.menu, {}, {}, self.config, checkpoint=snapshots.append)
        self.assertEqual(snapshots, [final])
        self.assertIsNot(snapshots[0], final)
        resumed = service.build(self.menu, snapshots[0], {}, self.config,
                                checkpoint=lambda *args: self.fail("Reused entries have no new checkpoint"))
        generate.assert_called_once()
        self.assertEqual(resumed, final)


if __name__ == "__main__":
    unittest.main()
