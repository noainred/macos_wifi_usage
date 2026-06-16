# netusage — macOS 네트워크 사용량 모니터

macOS에서 **어떤 네트워크에 연결되어 있을 때 인터넷을 얼마나 썼는지**를 주기적으로
측정해 **SQLite DB에 시계열로 저장**하고, **원하는 형태로 가공해서 리포트/내보내기**할 수 있는
CLI 도구입니다.

- **Wi-Fi**: 연결된 **SSID별**로 송·수신량을 집계합니다.
- **Ethernet**(또는 SSID를 못 얻는 경우): **특정 IP로 ping이 되는지 / 게이트웨이가 무엇인지**로
  네트워크를 식별해(`named_networks`) 그 네트워크별로 사용량을 집계합니다.
- 추가 의존성 없음 — **파이썬 표준 라이브러리만** 사용하고, macOS 내장 명령
  (`netstat`, `route`, `networksetup`, `ipconfig`, `system_profiler`, `ping`)을 활용합니다.

> 측정·식별은 macOS 명령에 의존하므로 **실제 동작은 macOS에서** 해야 합니다.
> (파싱·집계 등 핵심 로직은 다른 OS에서도 테스트할 수 있도록 분리되어 있습니다.)

---

## 동작 원리

1. **카운터 측정**: `netstat -ib`로 인터페이스별 누적 송·수신 바이트를 읽습니다.
2. **네트워크 식별**: `route -n get default`로 현재 기본 경로 인터페이스/게이트웨이를 찾고,
   - Wi-Fi면 SSID를 구합니다(`networksetup` → `ipconfig getsummary` → `system_profiler` 순으로 시도).
   - 그 외(주로 Ethernet)면 설정한 `named_networks`의 `gateway` 일치 또는 `ping` 도달 여부로 네트워크를 식별합니다.
3. **사용량 귀속**: 직전 샘플과의 **delta(증가분)** 를 현재 식별된 네트워크에 귀속시켜 한 행으로 DB에 저장합니다.
   - 모든 인터페이스의 기준값을 매번 갱신하므로 Wi-Fi↔Ethernet 전환에도 delta가 정확합니다.
   - 재부팅 등으로 카운터가 리셋(현재값 < 직전값)되면 그 구간은 건너뛰어 비정상 급증을 막습니다.
4. **저장/가공**: 모든 샘플은 SQLite에 쌓이고, `report`(집계)와 `export`(원본 CSV/JSON)로 원하는 형태로 가공합니다.

---

## 빠른 시작

```bash
# 1) 저장소 디렉터리에서 (설치 없이 바로 실행 가능)
cd macos_wifi_usage

# 2) 현재 상태 확인 (인터페이스/SSID/게이트웨이/식별 결과)
python3 -m netusage status

# 3) 한 번만 샘플링해보기 (두 번째 실행부터 delta가 기록됨)
python3 -m netusage monitor --once
python3 -m netusage monitor --once

# 4) 포그라운드로 계속 측정 (Ctrl+C로 종료)
python3 -m netusage monitor --verbose

# 5) 리포트
python3 -m netusage report --by network --since 7d
```

선택적으로 설치하면 `netusage` 명령으로 바로 쓸 수 있습니다:

```bash
pip install -e .
netusage status
```

---

## 백그라운드 자동 실행 (launchd)

로그인 후 자동으로 계속 측정하도록 LaunchAgent를 등록합니다.

```bash
# 방법 A: 도우미 스크립트 (생성 + 시작까지)
./install.sh

# 방법 B: 수동
python3 -m netusage install          # ~/Library/LaunchAgents/com.user.netusage.plist 생성
launchctl load ~/Library/LaunchAgents/com.user.netusage.plist   # 시작
launchctl unload ~/Library/LaunchAgents/com.user.netusage.plist # 중지
```

- 로그: `~/.netusage/monitor.log`, `~/.netusage/monitor.err.log`
- plist는 현재 파이썬 경로와 이 프로젝트 경로(PYTHONPATH)를 절대경로로 박아 생성됩니다.
  내용만 확인하려면: `python3 -m netusage install --print`

---

## 설정 (`~/.netusage/config.json`)

`config.example.json` 참고. 처음 `install` 시 기본 파일이 만들어집니다.

```json
{
  "sample_interval_seconds": 60,
  "wifi_interface": "en0",
  "db_path": "~/.netusage/netusage.db",
  "ping_timeout_ms": 1500,
  "named_networks": [
    { "name": "Home",   "ping": "192.168.0.1" },
    { "name": "Office", "ping": "10.0.0.1" },
    { "name": "Lab",    "gateway": "172.16.5.1" }
  ]
}
```

- **`named_networks`**: Ethernet 등 SSID가 없는 환경에서 네트워크에 이름을 붙이는 핵심입니다.
  - `ping`: 그 IP에 ping이 되면 해당 이름으로 식별합니다. (질문하신 "어떤 IP로 ping이 될 때")
  - `gateway`: 기본 게이트웨이가 이 값과 같으면 ping 없이 즉시 식별합니다(더 빠르고 조용함).
  - 매칭이 없으면 `eth:<게이트웨이IP>` 형태의 라벨로 저장됩니다.

---

## 리포트 — 원하는 형태로 가공하기

```bash
# 네트워크(SSID/이름)별 — 가장 기본
python3 -m netusage report --by network --since 7d

# SSID별만 / 연결 유형별(wifi vs ethernet) / 인터페이스별 / 게이트웨이별
python3 -m netusage report --by ssid
python3 -m netusage report --by type
python3 -m netusage report --by iface
python3 -m netusage report --by gateway

# 시간 단위 집계: 시간/일/주/월
python3 -m netusage report --by day   --since 30d
python3 -m netusage report --by month

# 기간 지정 (상대: 24h, 7d, 2w / 절대: 2026-06-01, "2026-06-01 09:00")
python3 -m netusage report --by network --since 2026-06-01 --until 2026-06-15

# JSON으로 받아서 다른 도구로 가공
python3 -m netusage report --by network --since 7d --format json
```

예시 출력:

```
Network Usage (by network)
==========================
NETWORK        TYPE             RECV         SENT        TOTAL  SAMPLES
-----------------------------------------------------------------------
Office         ethernet     5.18 GiB   940.00 MiB     6.09 GiB        2
MyHomeWiFi     wifi         2.34 GiB   160.00 MiB     2.50 GiB        2
CoffeeShop_5G  wifi       300.00 MiB    25.00 MiB   325.00 MiB        1
-----------------------------------------------------------------------
TOTAL                       7.81 GiB     1.10 GiB     8.91 GiB
```

### 원본 데이터 내보내기 (직접 가공용)

집계 말고 **샘플 원본**을 CSV/JSON으로 빼서 Excel·pandas 등으로 자유롭게 가공할 수 있습니다.

```bash
python3 -m netusage export --format csv --since 30d -o usage.csv
python3 -m netusage export --format json --since 7d
```

CSV 컬럼: `ts, datetime, iface, conn_type, network, ssid, gateway, rx_bytes, tx_bytes`

### DB 직접 질의

SQLite 파일을 직접 열어 원하는 쿼리로 가공해도 됩니다.

```bash
sqlite3 ~/.netusage/netusage.db \
  "SELECT network, SUM(rx_bytes+tx_bytes) AS total
     FROM samples GROUP BY network ORDER BY total DESC;"
```

**스키마 `samples`**

| 컬럼 | 의미 |
|---|---|
| `ts` | Unix epoch(초) |
| `iface` | 인터페이스 (en0 등) |
| `conn_type` | `wifi` / `ethernet` / `other` |
| `network` | 대표 라벨(SSID·named 이름·`eth:게이트웨이`) |
| `ssid` | Wi-Fi SSID (있을 때) |
| `gateway` | 기본 게이트웨이 IP |
| `rx_bytes` | 해당 구간 수신 delta |
| `tx_bytes` | 해당 구간 송신 delta |

---

## 명령 요약

| 명령 | 설명 |
|---|---|
| `status` | 현재 인터페이스/SSID/게이트웨이/식별 결과와 누적 카운터 |
| `monitor [--once] [--interval N] [--verbose]` | 측정 루프 (launchd가 이걸 실행) |
| `report --by <키> [--since/--until] [--format table\|json]` | 집계 리포트 |
| `export [--format csv\|json] [--since/--until] [-o FILE]` | 원본 샘플 내보내기 |
| `networks` | 설정된 `named_networks`와 현재 식별 결과 |
| `install [--print]` | launchd LaunchAgent(+기본 설정) 생성 |
| `config` | 유효 설정/경로 출력 |

`--by` 키: `network, ssid, type, iface, gateway, hour, day, week, month`

전역 옵션: `--config <경로>`, `--db <경로>`

---

## 참고 / 한계

- **SSID 권한(최신 macOS)**: Sonoma/Sequoia에서는 SSID 조회에 위치 서비스 권한이 필요할 수 있습니다.
  SSID가 빈 값으로 나오면 *시스템 설정 → 개인정보 보호 및 보안 → 위치 서비스*에서 터미널(또는 실행 앱)을 허용하세요.
  그래도 안 되면 Wi-Fi 환경에서도 `named_networks`의 `gateway` 매칭을 사용하면 됩니다.
- **귀속 단위**: 기본 경로(default route) 인터페이스의 트래픽을 현재 식별된 네트워크에 귀속시킵니다
  (즉 "지금 인터넷을 나가는 그 네트워크"의 사용량). 샘플 주기(기본 60초) 해상도로 측정됩니다.
- **데이터 위치**: 기본 `~/.netusage/`. `db_path`로 변경 가능.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```
