"""netusage 명령행 인터페이스."""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import List, Optional

from . import __version__, ifstats, netinfo, report
from .config import (
    DEFAULT_CONFIG_PATH,
    load_config,
    write_default_config,
)
from .storage import GROUP_KEYS, Storage
from .util import human_bytes


# ---------------------------------------------------------------------------
# 시간 표현 파싱
# ---------------------------------------------------------------------------
def parse_time_spec(spec: str) -> int:
    """'7d', '24h', '2w', 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM' 을 epoch 초로 변환."""
    spec = spec.strip()
    now = int(time.time())
    m = re.fullmatch(r"(\d+)\s*([smhdw])", spec.lower())
    if m:
        n = int(m.group(1))
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return now - n * mult
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(spec, fmt)))
        except ValueError:
            continue
    raise ValueError(f"시간 형식을 해석할 수 없습니다: {spec!r} "
                     f"(예: 24h, 7d, 2w, 2026-06-01)")


def _resolve_range(args) -> tuple[Optional[int], Optional[int]]:
    since = parse_time_spec(args.since) if getattr(args, "since", None) else None
    until = parse_time_spec(args.until) if getattr(args, "until", None) else None
    return since, until


# ---------------------------------------------------------------------------
# 명령 구현
# ---------------------------------------------------------------------------
def cmd_monitor(args, config) -> int:
    from .monitor import monitor_loop, sample_once

    if args.interval:
        config["sample_interval_seconds"] = args.interval
    storage = Storage(config["db_path"])
    try:
        if args.once:
            rec = sample_once(storage, config, record=True)
            if rec:
                print(f"기록됨: {rec['network']} ({rec['conn_type']}) "
                      f"rx={human_bytes(rec['rx'])} tx={human_bytes(rec['tx'])}")
            else:
                print("기록 없음(기준값 설정 또는 카운터 미가용). 잠시 후 다시 실행하세요.")
            return 0
        monitor_loop(storage, config, verbose=args.verbose)
        return 0
    finally:
        storage.close()


def cmd_status(args, config) -> int:
    ident = netinfo.identify_network(config)
    counters = ifstats.read_counters()
    cur = counters.get(ident.iface)

    print("현재 네트워크 상태")
    print("==================")
    print(f"인터페이스 : {ident.iface or '-'}")
    print(f"연결 유형  : {ident.conn_type}")
    print(f"SSID       : {ident.ssid or '-'}")
    print(f"게이트웨이 : {ident.gateway or '-'}")
    if ident.matched_name:
        print(f"매칭 이름  : {ident.matched_name} (named_networks)")
    print(f"대표 라벨  : {ident.network}")
    if cur:
        print(f"누적 카운터: rx={human_bytes(cur[0])}  tx={human_bytes(cur[1])} "
              f"(부팅 이후 인터페이스 총량)")
    else:
        print("누적 카운터: (읽을 수 없음 — macOS 에서 실행하세요)")
    return 0


def cmd_report(args, config) -> int:
    if args.by not in GROUP_KEYS:
        print(f"--by 값이 올바르지 않습니다. 사용 가능: {', '.join(GROUP_KEYS)}",
              file=sys.stderr)
        return 2
    since, until = _resolve_range(args)
    storage = Storage(config["db_path"])
    try:
        rows = storage.aggregate(since=since, until=until, group_by=args.by)
        total = storage.totals(since=since, until=until)
    finally:
        storage.close()

    if args.format == "json":
        print(report.format_json(rows, total))
    else:
        headers = {
            "network": "NETWORK", "ssid": "SSID", "type": "TYPE",
            "iface": "IFACE", "gateway": "GATEWAY", "hour": "HOUR",
            "day": "DAY", "week": "WEEK", "month": "MONTH",
        }
        title = f"Network Usage (by {args.by})"
        print(report.format_table(rows, total, title=title,
                                  label_header=headers.get(args.by, "NETWORK")))
    return 0


def cmd_export(args, config) -> int:
    since, until = _resolve_range(args)
    storage = Storage(config["db_path"])
    try:
        samples = storage.iter_samples(since=since, until=until)
    finally:
        storage.close()

    if args.format == "json":
        import json
        text = json.dumps(samples, ensure_ascii=False, indent=2)
    else:
        text = report.samples_to_csv(samples)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"{len(samples)}개 샘플을 {args.output} 에 저장했습니다.")
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_networks(args, config) -> int:
    named = config.get("named_networks") or []
    print("설정된 named_networks")
    print("=====================")
    if not named:
        print("(없음) — 설정 파일에서 named_networks 를 추가하세요.")
    for net in named:
        keys = []
        if net.get("ping"):
            keys.append(f"ping={net['ping']}")
        if net.get("gateway"):
            keys.append(f"gateway={net['gateway']}")
        print(f"  - {net.get('name', '?'):<16} {'  '.join(keys)}")
    print()
    ident = netinfo.identify_network(config)
    print(f"현재 식별: {ident.network} ({ident.conn_type})")
    return 0


def cmd_install(args, config) -> int:
    """현재 사용자용 launchd LaunchAgent plist 를 생성한다."""
    label = "com.user.netusage"
    python = sys.executable or "/usr/bin/python3"
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.dirname(config["db_path"])
    out_log = os.path.join(log_dir, "monitor.log")
    err_log = os.path.join(log_dir, "monitor.err.log")

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>netusage</string>
        <string>monitor</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>{pkg_parent}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{out_log}</string>
    <key>StandardErrorPath</key>
    <string>{err_log}</string>
</dict>
</plist>
"""
    agents_dir = os.path.expanduser("~/Library/LaunchAgents")
    plist_path = os.path.join(agents_dir, f"{label}.plist")
    if args.print:
        sys.stdout.write(plist)
        return 0

    created = write_default_config(args.config or DEFAULT_CONFIG_PATH)
    if created:
        print(f"기본 설정 파일 생성: {args.config or DEFAULT_CONFIG_PATH}")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(agents_dir, exist_ok=True)
    with open(plist_path, "w", encoding="utf-8") as fh:
        fh.write(plist)
    print(f"LaunchAgent 작성: {plist_path}")
    print("백그라운드 모니터를 시작하려면:")
    print(f"  launchctl unload {plist_path} 2>/dev/null")
    print(f"  launchctl load {plist_path}")
    print("중지하려면:")
    print(f"  launchctl unload {plist_path}")
    return 0


def cmd_config(args, config) -> int:
    import json
    print(f"설정 파일 경로: {args.config or DEFAULT_CONFIG_PATH}")
    print(f"DB 경로       : {config['db_path']}")
    print("유효 설정:")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 파서 구성
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="netusage",
        description="macOS Wi-Fi(SSID)/Ethernet 네트워크 사용량 모니터 및 리포트",
    )
    p.add_argument("--version", action="version", version=f"netusage {__version__}")
    p.add_argument("--config", help=f"설정 파일 경로 (기본: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--db", help="DB 경로 재정의")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("monitor", help="사용량 측정 루프 실행(포그라운드/데몬)")
    m.add_argument("--interval", type=int, help="샘플 주기(초) 재정의")
    m.add_argument("--once", action="store_true", help="한 번만 샘플링하고 종료")
    m.add_argument("--verbose", "-v", action="store_true", help="각 샘플 출력")
    m.set_defaults(func=cmd_monitor)

    s = sub.add_parser("status", help="현재 네트워크/카운터 상태 출력")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("report", help="사용량 집계 리포트 출력")
    r.add_argument("--by", default="network",
                   help=f"집계 기준 ({', '.join(GROUP_KEYS)}) [기본: network]")
    r.add_argument("--since", help="시작 시각 (예: 24h, 7d, 2026-06-01)")
    r.add_argument("--until", help="끝 시각")
    r.add_argument("--format", choices=["table", "json"], default="table")
    r.set_defaults(func=cmd_report)

    e = sub.add_parser("export", help="원본 샘플을 CSV/JSON 으로 내보내기")
    e.add_argument("--since", help="시작 시각")
    e.add_argument("--until", help="끝 시각")
    e.add_argument("--format", choices=["csv", "json"], default="csv")
    e.add_argument("--output", "-o", help="출력 파일 (없으면 표준출력)")
    e.set_defaults(func=cmd_export)

    n = sub.add_parser("networks", help="설정된 named_networks 와 현재 식별 결과")
    n.set_defaults(func=cmd_networks)

    i = sub.add_parser("install", help="백그라운드 실행용 launchd LaunchAgent 생성")
    i.add_argument("--print", action="store_true", help="파일을 쓰지 않고 plist 출력만")
    i.set_defaults(func=cmd_install)

    c = sub.add_parser("config", help="유효 설정과 경로 출력")
    c.set_defaults(func=cmd_config)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config or DEFAULT_CONFIG_PATH)
    if args.db:
        config["db_path"] = os.path.expanduser(args.db)
    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
