"""식단 갱신을 담당하는 RefreshService가 원본 수집, 메뉴 검사, 번역, 공개 함수를 순서대로 호출합니다."""

from mensa.menu_contract import validate_menu


class RefreshService:
    """RefreshService가 수집한 메뉴를 생성 전에 검사하고, 메뉴와 번역 캐시를 공개 전에 검증합니다. 실제 수집·번역·공개 작업은 호출자가 전달한 함수가 수행합니다."""

    def __init__(self, *, source, coverage_guard, translate, validator, publisher):
        for name, port in (
            ('source', source), ('coverage_guard', coverage_guard),
            ('translate', translate), ('validator', validator), ('publisher', publisher),
        ):
            if not callable(port):
                raise TypeError(f'{name} must be callable')
        self._source = source
        self._coverage_guard = coverage_guard
        self._translate = translate
        self._validator = validator
        self._publisher = publisher

    def refresh(self, previous, cache, phrases, *, checkpoint=None):
        """RefreshService.refresh가 원본 메뉴를 수집·검증하고 유효 기간을 확인한 뒤 번역 캐시를 만들고, 메뉴·번역 쌍을 검증·공개하여 반환합니다."""
        if checkpoint is not None and not callable(checkpoint):
            raise TypeError('checkpoint must be callable')
        menu = self._source()
        validate_menu(menu, previous=previous)
        self._coverage_guard(menu)
        candidate = self._translate(menu, cache, phrases, checkpoint=checkpoint)
        self._validator(menu, candidate)
        self._publisher(menu, candidate, previous=previous)
        return menu, candidate
