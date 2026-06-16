"""여러 모듈에서 공유하는 작은 헬퍼들."""
from __future__ import annotations

import subprocess
import time
from typing import List, Optional, Tuple

_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def now_ts() -> int:
    """현재 시각을 Unix epoch 초(int)로 반환한다."""
    return int(time.time())


def human_bytes(n: int) -> str:
    """바이트 수를 사람이 읽기 좋은 문자열로 변환한다 (예: '1.23 GiB')."""
    value = float(n)
    for unit in _UNITS:
        if abs(value) < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PiB"


def run_cmd(args: List[str], timeout: float = 5.0) -> Optional[Tuple[int, str, str]]:
    """명령을 실행하고 (returncode, stdout, stderr)를 반환한다.

    실행 파일이 없거나(macOS가 아닌 호스트 등) 타임아웃이 나면 None을 돌려주어
    호출 측이 우아하게 degrade 할 수 있게 한다. 예외를 던지지 않는다.
    """
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode, proc.stdout, proc.stderr
