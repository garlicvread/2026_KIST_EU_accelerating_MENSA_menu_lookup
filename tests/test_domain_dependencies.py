"""이 테스트 모듈은 메뉴 데이터의 형식 검사 코드가 원문 수집 코드나 파일·프로세스 작업 코드에 의존하지 않는지 확인하여 데이터 검사와 외부 작업을 분리합니다."""

import ast
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "mensa" / "menu_contract.py"
NOTICE_CONTRACT = ROOT / "mensa" / "notice_contract.py"
TRANSLATION_CONTRACT = ROOT / "mensa" / "translation_contract.py"
GENERATION = ROOT / "mensa" / "generation.py"
ERRORS = ROOT / "mensa" / "errors.py"
OPERATIONAL_ROOTS = {"os", "pathlib", "subprocess", "socket", "http", "urllib",
                     "time", "asyncio", "threading", "multiprocessing", "tempfile",
                     "shutil", "importlib", "scripts"}


def import_edges(path, *, include_symbols=True):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            yield module
            if include_symbols:
                yield from (f"{module}.{alias.name}" for alias in node.names)


def assert_pure_direct_dependencies(testcase, path, allowed_mensa):
    """assert_pure_direct_dependencies는 허용한 모듈만 직접 가져오는지 검사하여 외부 작업 코드의 직접 의존성을 막습니다. 이 검사만으로 간접 의존성의 외부 작업 여부까지 보장하지는 않습니다."""
    for module in import_edges(path, include_symbols=False):
        with testcase.subTest(source=path.name, import_edge=module):
            testcase.assertFalse(module.startswith("."), "Relative dependency is unresolved")
            root = module.split(".")[0]
            testcase.assertNotIn(root, OPERATIONAL_ROOTS,
                                 "Pure application code must not import operational adapters")
            testcase.assertTrue(module in allowed_mensa or root in sys.stdlib_module_names,
                                "Unknown or unapproved direct dependency")


class DomainDependencyTests(unittest.TestCase):
    def test_generation_errors_are_pure_and_cannot_import_transport_or_runtime(self):
        self.assertTrue(ERRORS.is_file(), "Pure generation failure module is missing")
        allowed = {"datetime", "datetime.datetime", "email.utils", "email.utils.parsedate_to_datetime"}
        for edge in import_edges(ERRORS):
            with self.subTest(import_edge=edge):
                self.assertIn(edge, allowed, "Generation failures must remain independent of transport and runtime")

    def test_generation_is_present_and_imports_only_transport_and_contract_dependencies(self):
        self.assertTrue(GENERATION.is_file(), "Generation transport module is missing")
        allowed = {"ipaddress", "json", "urllib.error", "urllib.parse", "urllib.request",
                   "datetime", "datetime.datetime", "datetime.timezone", "http.client",
                   "mensa.errors", "mensa.errors.GenerationError", "mensa.errors.parse_retry_after",
                   "mensa.translation_contract", "mensa.translation_contract.validate_result"}
        edges = set(import_edges(GENERATION))
        for edge in edges:
            with self.subTest(import_edge=edge):
                self.assertIn(edge, allowed,
                              "Generation transport must not import scripts, cache, glossary or runtime adapters")

    def test_translation_contract_is_present_and_imports_only_pure_dependencies(self):
        self.assertTrue(TRANSLATION_CONTRACT.is_file(), "Pure translation contract module is missing")
        allowed = {"hashlib", "json", "re", "mensa.notice_contract",
                   "mensa.notice_contract.validate_notice_translations"}
        edges = set(import_edges(TRANSLATION_CONTRACT))
        for edge in edges:
            with self.subTest(import_edge=edge):
                self.assertIn(edge, allowed,
                              "Translation contracts must not import operational APIs or adapters")

    def test_notice_contract_is_present_and_imports_only_pure_standard_library(self):
        self.assertTrue(NOTICE_CONTRACT.is_file(), "Pure notice contract module is missing")
        forbidden = {"os", "pathlib", "subprocess", "socket", "http", "urllib",
                     "time", "asyncio", "threading", "multiprocessing", "tempfile",
                     "shutil", "importlib"}
        for edge in import_edges(NOTICE_CONTRACT):
            with self.subTest(import_edge=edge):
                root = edge.split(".")[0]
                self.assertIn(root, sys.stdlib_module_names,
                              "Notice contracts must not depend on application adapters")
                self.assertNotIn(root, forbidden,
                                 "Notice contracts must not import operational APIs")

    def test_menu_contract_is_present_and_imports_only_pure_standard_library(self):
        self.assertTrue(CONTRACT.is_file(), "Pure menu contract module is missing")
        forbidden = {"os", "pathlib", "subprocess", "socket", "http", "urllib", "time"}
        for edge in import_edges(CONTRACT):
            with self.subTest(import_edge=edge):
                root = edge.split(".")[0]
                self.assertIn(root, sys.stdlib_module_names,
                              "Menu contracts must not depend on application adapters")
                self.assertNotIn(root, forbidden,
                                 "Menu contracts must not import operational APIs")
                self.assertFalse(edge == "html.parser" or edge.startswith("html.parser."),
                                 "HTML parsing belongs to the collector")

    def test_publication_imports_only_pure_contracts_and_standard_library(self):
        assert_pure_direct_dependencies(self, ROOT / "mensa" / "publication.py",
                                       {"mensa.menu_contract", "mensa.notice_contract",
                                        "mensa.translation_contract"})

    def test_application_modules_import_only_approved_pure_dependencies(self):
        allowed = {
            "translation_service": {"mensa.notice_contract", "mensa.translation_contract"},
            "refresh_service": {"mensa.menu_contract"},
            "translation_runner": {"mensa.translation_contract", "mensa.notice_contract",
                                   "mensa.translation_service"},
        }
        for module, dependencies in allowed.items():
            with self.subTest(application_module=module):
                assert_pure_direct_dependencies(self, ROOT / "mensa" / f"{module}.py", dependencies)
