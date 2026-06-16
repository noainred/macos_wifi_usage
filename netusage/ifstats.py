"""`netstat -ib` 로 인터페이스별 누적 바이트 카운터를 읽는다."""
from __future__ import annotations

from typing import Dict, Tuple

from .util import run_cmd

# iface -> (rx_bytes, tx_bytes) 누적값
Counters = Dict[str, Tuple[int, int]]


def parse_netstat_ib(text: str) -> Counters:
    """`netstat -ib` 출력을 {iface: (rx_bytes, tx_bytes)} 로 파싱한다.

    각 인터페이스는 link-level 행(Network 열이 "<Link#N>")을 정확히 하나 가지며,
    그 행의 바이트 열이 인터페이스 누적 총량이다. 같은 인터페이스의 다른 행
    (IPv4/IPv6)은 동일한 값을 반복하므로 link 행만 사용해 중복을 피한다.

    Address 열은 일부 인터페이스(lo0, gif0 ...)에서 비어 있어 열 위치가 달라지므로,
    값은 행의 끝에서 읽는다. 마지막 7개 필드는 항상 다음과 같다::

        Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
    """
    counters: Counters = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        # Network 열은 항상 3번째 필드(Name, Mtu, Network, ...).
        if not parts[2].startswith("<Link#"):
            continue
        name = parts[0]
        try:
            rx = int(parts[-5])  # Ibytes
            tx = int(parts[-2])  # Obytes
        except ValueError:
            continue
        counters[name] = (rx, tx)
    return counters


def read_counters() -> Counters:
    """실제 시스템에서 인터페이스 카운터를 읽는다. 실패 시 빈 dict."""
    res = run_cmd(["netstat", "-ib"])
    if res is None:
        return {}
    rc, out, _ = res
    if rc != 0:
        return {}
    return parse_netstat_ib(out)
