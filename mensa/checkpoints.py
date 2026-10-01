"""TranslationCheckpointStore가 구조를 검증한 번역 이력을 비공개 파일에 저장하고 읽습니다. 일부 메뉴의 번역만 있는 이력도 허용합니다."""

import json
import os
from pathlib import Path
import stat
import tempfile

from mensa.translation_contract import validate_cache


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


class TranslationCheckpointStore:
    """TranslationCheckpointStore가 한 작업자의 번역 이력을 비공개 translation-checkpoint.json에 저장합니다.

    호출자는 공개 출력 및 코드와 겹치지 않는 상태 디렉터리를 지정하고, 상위 디렉터리를
    미리 준비하며, 해당 작업자의 독점 사용을 보장해야 합니다. 이 저장소는 전체 메뉴의
    번역 완성 여부를 검사하거나 작성자 잠금을 제공하지 않습니다. 디렉터리 fsync와 정전 시
    내구성도 보장하지 않습니다.
    """

    def __init__(self, state_dir: Path):
        if not isinstance(state_dir, Path) or not state_dir.is_absolute():
            raise ValueError("state_dir must be an explicit absolute Path")
        self._state_dir = state_dir
        self._path = state_dir / "translation-checkpoint.json"

    def _directory_exists(self):
        try:
            mode = self._state_dir.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(mode):
            raise ValueError(f"Checkpoint state must be a directory, not a symlink: {self._state_dir}")
        if stat.S_IMODE(mode) & 0o077:
            raise ValueError(f"Checkpoint directory must have private permissions: {self._state_dir}")
        return True

    def _checkpoint_exists(self):
        try:
            mode = self._path.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(mode):
            raise ValueError(f"Checkpoint must be a regular file, not a symlink: {self._path}")
        return True

    @staticmethod
    def _serialize(cache):
        validate_cache(cache)
        return json.dumps(cache, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def load(self) -> dict | None:
        """TranslationCheckpointStore가 검증된 번역 이력을 독립적인 사전으로 읽어 반환하며, 저장된 이력이 없을 때만 None을 반환합니다."""
        if not self._directory_exists() or not self._checkpoint_exists():
            return None
        raw = self._path.read_bytes()
        try:
            cache = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            # load는 JSON 디코더가 표현할 수 있어도 UTF-8 작성기가 저장할 수 없는 숫자 오버플로와
            # 이스케이프된 잘못된 유니코드를 _serialize로 검사하여 거부합니다.
            self._serialize(cache)
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise ValueError(f"Invalid translation checkpoint {self._path}: {error}") from error
        return cache

    def save(self, cache) -> None:
        """TranslationCheckpointStore가 번역 캐시를 검증·직렬화한 뒤 상태 디렉터리와 임시 파일을 준비하고 체크포인트 파일을 원자적으로 교체합니다."""
        try:
            payload = self._serialize(cache)
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise ValueError(f"Invalid translation checkpoint cache for UTF-8 JSON: {error}") from error
        if not self._directory_exists():
            self._state_dir.mkdir(mode=0o700)
            self._directory_exists()
        self._checkpoint_exists()

        descriptor, temporary = tempfile.mkstemp(
            prefix=".translation-checkpoint-", suffix=".tmp", dir=self._state_dir
        )
        try:
            try:
                output = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with output:
                os.fchmod(output.fileno(), 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
        finally:
            Path(temporary).unlink(missing_ok=True)
