"""설정 로드: 샘플 주기, named_networks, 경로 등."""
from __future__ import annotations

import json
import os
from typing import Any, Dict

DEFAULT_DIR = os.path.expanduser("~/.netusage")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_DIR, "config.json")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DIR, "netusage.db")

DEFAULTS: Dict[str, Any] = {
    # 샘플링 주기(초). 이 간격마다 카운터를 읽어 사용량 delta 를 기록한다.
    "sample_interval_seconds": 60,
    # Wi-Fi 인터페이스 추정 기본값(기본 경로에서 못 구할 때만 사용).
    "wifi_interface": "en0",
    # SQLite DB 경로.
    "db_path": DEFAULT_DB_PATH,
    # named_networks ping 타임아웃(ms).
    "ping_timeout_ms": 1500,
    # Ethernet 등에서 ping/gateway 로 식별할 사용자 정의 네트워크 목록.
    #   [{"name": "Home", "ping": "192.168.0.1"},
    #    {"name": "Office", "gateway": "10.0.0.1"}]
    "named_networks": [],
}


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """기본값 위에 사용자 설정 파일(JSON)을 병합해 반환한다."""
    config = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                config.update(user)
        except (json.JSONDecodeError, OSError):
            pass
    config["db_path"] = os.path.expanduser(config.get("db_path", DEFAULT_DB_PATH))
    return config


def write_default_config(path: str = DEFAULT_CONFIG_PATH) -> bool:
    """설정 파일이 없으면 기본 설정을 생성한다. 생성했으면 True."""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    sample = dict(DEFAULTS)
    sample["named_networks"] = [
        {"name": "Home", "ping": "192.168.0.1"},
        {"name": "Office", "ping": "10.0.0.1"},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sample, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return True
