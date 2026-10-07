import copy
import unittest

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
        self.assertEqual(selected, sample_glossary())
        self.assertEqual(menu, before)


if __name__ == "__main__":
    unittest.main()
