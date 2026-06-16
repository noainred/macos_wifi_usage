"""현재 네트워크 식별: 기본 인터페이스, 연결 유형, Wi-Fi SSID, 게이트웨이, ping."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .util import run_cmd


@dataclass
class NetworkIdentity:
    """한 시점에 식별된 네트워크 정보."""

    iface: str = ""
    conn_type: str = "other"        # 'wifi' | 'ethernet' | 'other'
    network: str = "unknown"        # 리포트에 쓰는 대표 라벨
    ssid: Optional[str] = None
    gateway: Optional[str] = None
    matched_name: Optional[str] = None  # named_networks 에서 매칭된 이름


# ---------------------------------------------------------------------------
# 기본 경로(default route)
# ---------------------------------------------------------------------------
def parse_default_route(text: str) -> Dict[str, str]:
    """`route -n get default` 출력에서 interface, gateway 를 뽑는다."""
    info: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            info["interface"] = line.split(":", 1)[1].strip()
        elif line.startswith("gateway:"):
            info["gateway"] = line.split(":", 1)[1].strip()
    return info


def get_default_route() -> Dict[str, str]:
    res = run_cmd(["route", "-n", "get", "default"])
    if res is None or res[0] != 0:
        return {}
    return parse_default_route(res[1])


# ---------------------------------------------------------------------------
# 하드웨어 포트 매핑 (en0 -> "Wi-Fi")
# ---------------------------------------------------------------------------
def parse_hardware_ports(text: str) -> Dict[str, str]:
    """`networksetup -listallhardwareports` 를 {device: port_label} 로 파싱."""
    ports: Dict[str, str] = {}
    current_port: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            current_port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:"):
            device = line.split(":", 1)[1].strip()
            if current_port is not None and device:
                ports[device] = current_port
    return ports


def get_hardware_ports() -> Dict[str, str]:
    res = run_cmd(["networksetup", "-listallhardwareports"])
    if res is None or res[0] != 0:
        return {}
    return parse_hardware_ports(res[1])


def conn_type_for(port_label: str) -> str:
    """하드웨어 포트 라벨로 연결 유형을 분류한다."""
    p = (port_label or "").lower()
    if "wi-fi" in p or "airport" in p or "wifi" in p:
        return "wifi"
    if "ethernet" in p or "lan" in p or "thunderbolt bridge" in p:
        return "ethernet"
    return "other"


# ---------------------------------------------------------------------------
# Wi-Fi SSID (여러 방법을 순차 시도)
# ---------------------------------------------------------------------------
def parse_airport_ssid(text: str) -> Optional[str]:
    """`networksetup -getairportnetwork enX` 출력에서 SSID 추출."""
    for line in text.splitlines():
        line = line.strip()
        for prefix in ("Current Wi-Fi Network:", "Current AirPort Network:"):
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
    return None


def parse_ipconfig_ssid(text: str) -> Optional[str]:
    """`ipconfig getsummary enX` 출력에서 'SSID : ...' 라인 추출 (최신 macOS)."""
    m = re.search(r"^\s*SSID\s*(?:\([^)]*\))?\s*:\s*(.+?)\s*$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


def parse_system_profiler_ssid(text: str) -> Optional[str]:
    """`system_profiler SPAirPortDataType` 에서 현재 네트워크 키(SSID) 추출."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "Current Network Information:" in line:
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if not s:
                    continue
                if s.endswith(":"):
                    return s[:-1].strip()
                return None
    return None


def get_ssid(iface: str) -> Optional[str]:
    """현재 연결된 Wi-Fi SSID 를 여러 방법으로 시도해서 반환한다."""
    res = run_cmd(["networksetup", "-getairportnetwork", iface])
    if res is not None and res[0] == 0:
        ssid = parse_airport_ssid(res[1])
        if ssid:
            return ssid

    res = run_cmd(["ipconfig", "getsummary", iface])
    if res is not None and res[0] == 0:
        ssid = parse_ipconfig_ssid(res[1])
        if ssid:
            return ssid

    res = run_cmd(["system_profiler", "SPAirPortDataType"], timeout=12.0)
    if res is not None and res[0] == 0:
        ssid = parse_system_profiler_ssid(res[1])
        if ssid:
            return ssid
    return None


# ---------------------------------------------------------------------------
# ping 기반 네트워크 식별 (주로 Ethernet 용)
# ---------------------------------------------------------------------------
def ping(ip: str, timeout_ms: int = 1500) -> bool:
    """대상 IP 로 1패킷 ping 을 보내 응답 여부를 bool 로 반환한다 (macOS ping)."""
    overall = max(2, int(round(timeout_ms / 1000.0)) + 2)
    res = run_cmd(
        ["ping", "-c", "1", "-W", str(timeout_ms), "-n", "-q", ip],
        timeout=overall,
    )
    return res is not None and res[0] == 0


def match_named_network(
    named: List[dict], gateway: Optional[str], ping_timeout_ms: int
) -> Optional[str]:
    """설정된 named_networks 중 현재 네트워크와 일치하는 이름을 찾는다.

    1) gateway 값이 일치하면 트래픽 없이 즉시 매칭.
    2) 그 다음 'ping' 대상에 도달 가능하면 매칭.
    """
    for net in named:
        gw = net.get("gateway")
        if gw and gateway and gw == gateway:
            return net.get("name")
    for net in named:
        target = net.get("ping")
        if target and ping(target, ping_timeout_ms):
            return net.get("name")
    return None


def identify_network(config: dict) -> NetworkIdentity:
    """현재 활성(기본 경로) 인터페이스의 네트워크 정체성을 종합 판단한다."""
    route = get_default_route()
    iface = route.get("interface") or config.get("wifi_interface", "en0")
    gateway = route.get("gateway")
    ports = get_hardware_ports()
    ctype = conn_type_for(ports.get(iface, ""))

    ident = NetworkIdentity(iface=iface, conn_type=ctype, gateway=gateway)

    ssid: Optional[str] = None
    if ctype == "wifi":
        ssid = get_ssid(iface)
        ident.ssid = ssid

    # Wi-Fi 는 SSID 가 곧 정체성이므로, SSID 를 못 얻었거나 Wi-Fi 가 아닐 때만 ping 매칭.
    matched: Optional[str] = None
    named = config.get("named_networks") or []
    if named and (ctype != "wifi" or not ssid):
        matched = match_named_network(named, gateway, config.get("ping_timeout_ms", 1500))
    ident.matched_name = matched

    # 대표 라벨 결정: Wi-Fi→SSID, 그 외→named 매칭→gateway→iface
    if ctype == "wifi" and ssid:
        ident.network = ssid
    elif matched:
        ident.network = matched
    elif gateway:
        prefix = "eth" if ctype == "ethernet" else (iface or "net")
        ident.network = f"{prefix}:{gateway}"
    else:
        ident.network = iface or "unknown"
    return ident
