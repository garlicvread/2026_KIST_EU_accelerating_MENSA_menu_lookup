"""scripts.refresh_queue를 가져오는 호출자에게 큐 함수와 기본 macOS 실행 조건을 제공합니다.

큐 상태의 검증·전이·JSON 저장·잠금은 mensa.queue에서 관리합니다. 이 모듈은 해당
함수를 재노출하고 resource_status에서 자원 조건만 지정합니다. 실제 자원 측정은
mensa.resources가 수행하며, 이 모듈을 가져오는 것만으로 큐나 모델을 실행하지 않습니다.
"""

from mensa.config import ResourceSettings
from mensa.resources import macos_resources, resource_status as configured_resource_status

from mensa.queue import (
    BERLIN, PENDING_FIELDS, BusyError, GenerationError, due_period, validate_state,
    load_state, save_state, reconcile, failed, request_retry, completed, exclusive_lock,
)


MIN_AVAILABLE_BYTES = 32 * 1024 ** 3



def resource_status():
    """macOS 자원을 조회하여 지금 실행할 수 있는지와 그 이유를 (bool, 문자열)로 반환합니다.

    ResourceSettings로 최소 32 GiB 가용 메모리, CPU당 부하 0.6과 최소 부하 한계 2를
    전달합니다. 허용 부하는 max(2, 0.6 * CPU 수)이며 AC 전원 또는 배터리가 없는 상태를
    요구합니다. 측정 오류는 실행 불가로 처리하고, 메모리나 CPU를 예약하지는 않습니다.
    """
    return configured_resource_status(
        ResourceSettings('macos', MIN_AVAILABLE_BYTES, 0.6),
        probe=macos_resources,
        minimum_load=2,
    )
