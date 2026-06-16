"""저장된 샘플로부터 사용량 리포트를 표/JSON/CSV 로 만든다."""
from __future__ import annotations

import csv
import io
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .util import human_bytes


def _fmt_time(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def format_table(
    rows: List[Dict[str, Any]],
    total: Tuple[int, int],
    title: str = "Network Usage",
    label_header: str = "NETWORK",
) -> str:
    """집계 결과를 정렬된 표 문자열로 만든다."""
    labels = [str(r.get("label") or "unknown") for r in rows]
    label_w = max([len(label_header)] + [len(s) for s in labels]) if labels else len(label_header)
    label_w = min(max(label_w, len(label_header)), 36)

    header = (
        f"{label_header:<{label_w}}  {'TYPE':<8}  {'RECV':>11}  "
        f"{'SENT':>11}  {'TOTAL':>11}  {'SAMPLES':>7}"
    )
    out: List[str] = [title, "=" * len(title), header, "-" * len(header)]

    for r in rows:
        label = str(r.get("label") or "unknown")
        if len(label) > label_w:
            label = label[: label_w - 1] + "…"
        rx = r.get("rx") or 0
        tx = r.get("tx") or 0
        out.append(
            f"{label:<{label_w}}  {str(r.get('conn_type') or '-'):<8}  "
            f"{human_bytes(rx):>11}  {human_bytes(tx):>11}  "
            f"{human_bytes(rx + tx):>11}  {r.get('samples', 0):>7}"
        )

    out.append("-" * len(header))
    trx, ttx = total
    out.append(
        f"{'TOTAL':<{label_w}}  {'':<8}  {human_bytes(trx):>11}  "
        f"{human_bytes(ttx):>11}  {human_bytes(trx + ttx):>11}"
    )
    return "\n".join(out)


def build_payload(
    rows: List[Dict[str, Any]], total: Tuple[int, int]
) -> Dict[str, Any]:
    """집계 결과를 dict 로 만든다(JSON/웹 API 공용). 사람이 읽기 좋은 필드 포함."""
    return {
        "rows": [
            {
                "label": r.get("label"),
                "conn_type": r.get("conn_type"),
                "rx_bytes": r.get("rx") or 0,
                "tx_bytes": r.get("tx") or 0,
                "total_bytes": (r.get("rx") or 0) + (r.get("tx") or 0),
                "rx_human": human_bytes(r.get("rx") or 0),
                "tx_human": human_bytes(r.get("tx") or 0),
                "total_human": human_bytes((r.get("rx") or 0) + (r.get("tx") or 0)),
                "samples": r.get("samples", 0),
                "first_ts": r.get("first_ts"),
                "last_ts": r.get("last_ts"),
                "first": _fmt_time(r.get("first_ts")),
                "last": _fmt_time(r.get("last_ts")),
            }
            for r in rows
        ],
        "total": {
            "rx_bytes": total[0],
            "tx_bytes": total[1],
            "total_bytes": total[0] + total[1],
            "rx_human": human_bytes(total[0]),
            "tx_human": human_bytes(total[1]),
            "total_human": human_bytes(total[0] + total[1]),
        },
    }


def format_json(
    rows: List[Dict[str, Any]], total: Tuple[int, int]
) -> str:
    """집계 결과를 JSON 문자열로."""
    return json.dumps(build_payload(rows, total), ensure_ascii=False, indent=2)


def samples_to_csv(samples: List[Dict[str, Any]]) -> str:
    """원본 샘플 리스트를 CSV 문자열로 변환한다(사용자 가공용)."""
    buf = io.StringIO()
    fields = ["ts", "datetime", "iface", "conn_type", "network",
              "ssid", "gateway", "rx_bytes", "tx_bytes"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for s in samples:
        row = dict(s)
        row["datetime"] = _fmt_time(s.get("ts"))
        writer.writerow({k: row.get(k, "") for k in fields})
    return buf.getvalue()
