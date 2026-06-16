"""샘플링 루프: 카운터를 읽어 현재 네트워크에 사용량 delta 를 귀속시킨다."""
from __future__ import annotations

import signal
from typing import Optional

from . import ifstats, netinfo
from .storage import Storage
from .util import human_bytes, now_ts


def sample_once(
    storage: Storage, config: dict, record: bool = True
) -> Optional[dict]:
    """한 번 샘플링한다.

    모든 인터페이스의 기준값(baseline)을 갱신해 기본 경로가 바뀌어도 delta 가
    정확하도록 하고, 실제 기록은 활성(기본 경로) 인터페이스에 대해서만 한다.

    record=False 면 기준값만 잡고 기록하지 않는다(데몬 시작 시 priming 용).
    반환값: 기록된 샘플 dict 또는 None.
    """
    counters = ifstats.read_counters()
    if not counters:
        return None
    ts = now_ts()
    ident = netinfo.identify_network(config)
    iface = ident.iface

    recorded: Optional[dict] = None
    for name, (rx, tx) in counters.items():
        last = storage.get_last_counter(name)
        storage.set_last_counter(name, rx, tx, ts)
        if name != iface or not record:
            continue
        if last is None:
            # 이 인터페이스를 처음 봄: 기준값만 잡고 기록은 하지 않는다.
            continue
        d_rx = rx - last[0]
        d_tx = tx - last[1]
        if d_rx < 0 or d_tx < 0:
            # 카운터 리셋(재부팅/인터페이스 down). 기준값만 갱신하고 건너뜀.
            continue
        storage.insert_sample(
            ts, iface, ident.conn_type, ident.network,
            ident.ssid, ident.gateway, d_rx, d_tx,
        )
        recorded = {
            "ts": ts,
            "iface": iface,
            "conn_type": ident.conn_type,
            "network": ident.network,
            "rx": d_rx,
            "tx": d_tx,
        }
    return recorded


class _Stopper:
    """SIGINT/SIGTERM 을 받으면 루프를 멈추게 하는 플래그."""

    def __init__(self) -> None:
        self.stop = False

    def __call__(self, *_args) -> None:
        self.stop = True


def monitor_loop(storage: Storage, config: dict, verbose: bool = False) -> None:
    """주기적으로 sample_once 를 호출한다. 신호를 받으면 정리 후 종료."""
    import time

    interval = max(5, int(config.get("sample_interval_seconds", 60)))
    stopper = _Stopper()
    signal.signal(signal.SIGINT, stopper)
    signal.signal(signal.SIGTERM, stopper)

    # 시작 시 첫 delta 가 비정상적으로 커지지 않도록 기준값만 잡는다.
    sample_once(storage, config, record=False)
    if verbose:
        print(f"netusage monitor started (interval={interval}s, db={storage.db_path})",
              flush=True)

    while not stopper.stop:
        # 신호에 빠르게 반응하도록 1초 단위로 나눠 잔다.
        slept = 0
        while slept < interval and not stopper.stop:
            time.sleep(1)
            slept += 1
        if stopper.stop:
            break
        rec = sample_once(storage, config, record=True)
        if verbose and rec:
            print(
                f"[{rec['ts']}] {rec['network']} ({rec['conn_type']}) "
                f"rx={human_bytes(rec['rx'])} tx={human_bytes(rec['tx'])}",
                flush=True,
            )
    if verbose:
        print("netusage monitor stopped", flush=True)
