"""샘플링 루프: 카운터를 읽어 현재 네트워크에 사용량 delta 를 귀속시킨다.

CLI 포그라운드 실행(시그널로 종료)과 웹 포탈의 백그라운드 스레드 실행
(threading.Event 로 종료)을 모두 지원한다.
"""
from __future__ import annotations

import signal
import threading
import time
from typing import Callable, Optional

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
            "ssid": ident.ssid,
            "rx": d_rx,
            "tx": d_tx,
        }
    return recorded


def monitor_loop(
    storage: Storage,
    config: dict,
    verbose: bool = False,
    stop_event: Optional[threading.Event] = None,
    on_sample: Optional[Callable[[Optional[dict]], None]] = None,
) -> None:
    """주기적으로 sample_once 를 호출한다.

    stop_event 가 주어지면 그것이 set 될 때까지 돈다(스레드용).
    없으면 직접 SIGINT/SIGTERM 핸들러를 설치한다(메인 스레드 포그라운드용).
    """
    interval = max(5, int(config.get("sample_interval_seconds", 60)))

    if stop_event is None:
        stop_event = threading.Event()

        def _handler(*_args):
            stop_event.set()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    # 시작 시 첫 delta 가 비정상적으로 커지지 않도록 기준값만 잡는다.
    sample_once(storage, config, record=False)
    if verbose:
        print(f"netusage monitor started (interval={interval}s, db={storage.db_path})",
              flush=True)

    while not stop_event.is_set():
        # 종료에 빠르게 반응하도록 1초 단위로 나눠 잔다.
        slept = 0
        while slept < interval and not stop_event.is_set():
            time.sleep(1)
            slept += 1
        if stop_event.is_set():
            break
        rec = sample_once(storage, config, record=True)
        if on_sample is not None:
            on_sample(rec)
        if verbose and rec:
            print(
                f"[{rec['ts']}] {rec['network']} ({rec['conn_type']}) "
                f"rx={human_bytes(rec['rx'])} tx={human_bytes(rec['tx'])}",
                flush=True,
            )
    if verbose:
        print("netusage monitor stopped", flush=True)


class MonitorController:
    """모니터 루프를 백그라운드 스레드로 시작/중지한다(웹 포탈용).

    각 스레드는 자신의 SQLite 연결을 갖는다(스레드 간 연결 공유 회피).
    config 는 웹 포탈과 공유하는 dict 객체로, 설정 저장 시 갱신되면 반영된다.
    """

    def __init__(self, config: dict):
        self.config = config
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.started_at: Optional[int] = None
        self.last_sample: Optional[dict] = None
        self.last_error: Optional[str] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.is_running():
                return False
            self._stop.clear()
            self.last_error = None
            self.started_at = now_ts()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            storage = Storage(self.config["db_path"])
        except Exception as exc:  # pragma: no cover - 방어용
            self.last_error = str(exc)
            return
        try:
            monitor_loop(
                storage,
                self.config,
                stop_event=self._stop,
                on_sample=self._on_sample,
            )
        except Exception as exc:  # pragma: no cover - 방어용
            self.last_error = str(exc)
        finally:
            storage.close()

    def _on_sample(self, rec: Optional[dict]) -> None:
        if rec:
            self.last_sample = rec

    def stop(self, timeout: float = 10.0) -> bool:
        with self._lock:
            running = self.is_running()
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self.started_at = None
        return running
