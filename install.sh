#!/usr/bin/env bash
# netusage 설치/시작 도우미 (macOS 전용)
#
#  - 기본 설정 파일과 launchd LaunchAgent 를 생성하고 백그라운드 모니터를 시작한다.
#  - 코드를 옮겨도 동작하도록 plist 안에 이 디렉터리를 PYTHONPATH 로 박는다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
LABEL="com.user.netusage"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "이 스크립트는 macOS 에서 실행해야 합니다 (현재: $(uname))." >&2
  exit 1
fi

echo "==> LaunchAgent 및 기본 설정 생성"
PYTHONPATH="$HERE" "$PY" -m netusage install

echo "==> 백그라운드 모니터 시작"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo
echo "완료. 상태/리포트 확인:"
echo "  PYTHONPATH=\"$HERE\" $PY -m netusage status"
echo "  PYTHONPATH=\"$HERE\" $PY -m netusage report --by network --since 7d"
echo
echo "중지: launchctl unload \"$PLIST\""
