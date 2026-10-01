"""GenerationError가 안전한 생성 실패 정보를 제공하고 parse_retry_after가 HTTP 재시도 지연 헤더를 해석합니다."""

from datetime import datetime
from email.utils import parsedate_to_datetime


MAX_RETRY_AFTER = 86400
_MESSAGES = {
    "authentication": "Translation endpoint rejected authentication",
    "http_error": "Translation endpoint rejected the request",
    "http_retryable": "Translation endpoint is temporarily unavailable",
    "timeout": "Translation request timed out",
    "network": "Translation request was interrupted",
    "redirect": "Translation endpoint redirected; use its final HTTPS URL",
    "invalid_config": "Invalid translation configuration",
    "invalid_ollama_endpoint": "Native Ollama requires a plain loopback /api/chat URL",
    "invalid_json": "Translation response contains invalid JSON",
    "invalid_envelope": "Translation response has an invalid envelope",
    "incomplete_generation": "Translation generation did not finish",
    "invalid_result": "Translation response does not match the required schema",
    "response_too_large": "Translation response too large",
}
_RETRYABLE = frozenset({"http_retryable", "timeout", "network"})


class GenerationError(ValueError):
    """GenerationError는 실패 코드로 정한 로컬 메시지와 선택적인 HTTP 상태·재시도 지연을 담습니다.

    retryable은 해당 실패가 재시도 가능한지를 나타내며, 오류 객체나 전송 함수가 직접
    재시도하지는 않습니다. 이 객체는 원격 응답의 텍스트, URL, 자격 증명 및 응답 객체를
    받지 않습니다.
    """

    def __init__(self, code, *, status_code=None, retry_after=None):
        if not isinstance(code, str) or code not in _MESSAGES:
            raise ValueError("Unknown generation failure code")
        if status_code is not None and type(status_code) is not int:
            raise ValueError("Generation status code must be an integer")
        if retry_after is not None and (type(retry_after) not in (int, float)
                                        or not 0 <= retry_after <= MAX_RETRY_AFTER):
            raise ValueError("Generation retry delay must be bounded and nonnegative")
        self.code = code
        self.retryable = code in _RETRYABLE
        self.status_code = status_code
        self.retry_after = retry_after if self.retryable else None
        super().__init__(_MESSAGES[code])


def parse_retry_after(value, now):
    """parse_retry_after가 시간대 정보가 있는 현재 시각과 HTTP 헤더를 받아 재시도 지연을 초 단위로 반환하고 상한을 적용합니다."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("Retry-After requires an aware current datetime")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value and value.isascii() and value.isdecimal():
        # parse_retry_after는 헤더의 정수가 아무리 길어도 상한을 유지하도록 정수 변환 전에 비교합니다.
        digits = value.lstrip("0") or "0"
        if len(digits) > 5 or (len(digits) == 5 and digits > str(MAX_RETRY_AFTER)):
            return MAX_RETRY_AFTER
        return int(digits)
    try:
        date = parsedate_to_datetime(value)
        if date.utcoffset() is None:
            return None
        return min(MAX_RETRY_AFTER, max(0, (date - now).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None
