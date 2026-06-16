"""netusage 웹 포탈: 브라우저에서 동작(시작/중지/샘플)·수정(설정)·조회(리포트/내보내기).

표준 라이브러리 http.server 만 사용한다. 모니터는 이 프로세스 안의 백그라운드
스레드(MonitorController)로 제어한다. 기본은 127.0.0.1(로컬 전용) 바인드.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__, ifstats, netinfo, report
from .config import load_config, save_config
from .monitor import MonitorController, sample_once
from .storage import GROUP_KEYS, Storage
from .util import parse_time_spec


class AppState:
    """웹 서버가 공유하는 상태: 설정(dict)과 모니터 컨트롤러."""

    def __init__(self, config: dict, config_path: str):
        self.config = config
        self.config_path = config_path
        self.controller = MonitorController(config)


# ---------------------------------------------------------------------------
# 페이로드 생성 헬퍼 (핸들러에서 호출)
# ---------------------------------------------------------------------------
def _range(qs: Dict[str, list]) -> Tuple[Optional[int], Optional[int]]:
    since = qs.get("since", [None])[0]
    until = qs.get("until", [None])[0]
    s = parse_time_spec(since) if since else None
    u = parse_time_spec(until) if until else None
    return s, u


def status_payload(app: AppState) -> Dict[str, Any]:
    ident = netinfo.identify_network(app.config)
    counters = ifstats.read_counters()
    cur = counters.get(ident.iface)
    storage = Storage(app.config["db_path"])
    try:
        rx, tx = storage.totals()
        row = storage.conn.execute("SELECT MAX(ts) AS m FROM samples").fetchone()
        last_ts = row["m"] if row else None
    finally:
        storage.close()
    ctrl = app.controller
    return {
        "identity": {
            "iface": ident.iface,
            "conn_type": ident.conn_type,
            "ssid": ident.ssid,
            "gateway": ident.gateway,
            "network": ident.network,
            "matched_name": ident.matched_name,
        },
        "counters": ({"rx": cur[0], "tx": cur[1]} if cur else None),
        "monitor": {
            "running": ctrl.is_running(),
            "started_at": ctrl.started_at,
            "last_sample": ctrl.last_sample,
            "last_error": ctrl.last_error,
            "interval": int(app.config.get("sample_interval_seconds", 60)),
        },
        "totals": {"rx": rx, "tx": tx, "total": rx + tx},
        "last_ts": last_ts,
        "version": __version__,
    }


def report_payload(app: AppState, qs: Dict[str, list]) -> Dict[str, Any]:
    by = qs.get("by", ["network"])[0]
    if by not in GROUP_KEYS:
        raise ValueError(f"잘못된 by 값입니다. 사용 가능: {', '.join(GROUP_KEYS)}")
    since, until = _range(qs)
    storage = Storage(app.config["db_path"])
    try:
        rows = storage.aggregate(since=since, until=until, group_by=by)
        total = storage.totals(since=since, until=until)
    finally:
        storage.close()
    payload = report.build_payload(rows, total)
    payload["by"] = by
    return payload


def export_text(app: AppState, qs: Dict[str, list]) -> Tuple[str, str, str]:
    """(본문, content_type, 파일명) 반환."""
    fmt = qs.get("format", ["csv"])[0]
    since, until = _range(qs)
    storage = Storage(app.config["db_path"])
    try:
        samples = storage.iter_samples(since=since, until=until)
    finally:
        storage.close()
    if fmt == "json":
        return (json.dumps(samples, ensure_ascii=False, indent=2),
                "application/json; charset=utf-8", "netusage_export.json")
    return (report.samples_to_csv(samples),
            "text/csv; charset=utf-8", "netusage_export.csv")


# ---------------------------------------------------------------------------
# HTTP 핸들러
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "netusage/" + __version__

    @property
    def app(self) -> AppState:
        return self.server.app  # type: ignore[attr-defined]

    # -- 응답 헬퍼 --------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:  # 조용히
        return

    # -- 라우팅 -----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(status_payload(self.app))
            elif path == "/api/report":
                self._json(report_payload(self.app, qs))
            elif path == "/api/networks":
                ident = netinfo.identify_network(self.app.config)
                self._json({
                    "named_networks": self.app.config.get("named_networks") or [],
                    "current": {
                        "network": ident.network, "conn_type": ident.conn_type,
                        "ssid": ident.ssid, "gateway": ident.gateway,
                        "matched_name": ident.matched_name,
                    },
                })
            elif path == "/api/config":
                self._json({"path": self.app.config_path, "config": self.app.config})
            elif path == "/api/export":
                body, ctype, filename = export_text(self.app, qs)
                self._send(200, body.encode("utf-8"), ctype,
                           {"Content-Disposition": f'attachment; filename="{filename}"'})
            else:
                self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - 방어용
            self._error(500, str(exc))

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                self._handle_save_config()
            elif path == "/api/monitor/start":
                started = self.app.controller.start()
                self._json({"ok": True, "started": started,
                            "running": self.app.controller.is_running()})
            elif path == "/api/monitor/stop":
                was = self.app.controller.stop()
                self._json({"ok": True, "stopped": was,
                            "running": self.app.controller.is_running()})
            elif path == "/api/sample":
                storage = Storage(self.app.config["db_path"])
                try:
                    rec = sample_once(storage, self.app.config, record=True)
                finally:
                    storage.close()
                self._json({"ok": True, "recorded": rec})
            else:
                self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - 방어용
            self._error(500, str(exc))

    def _handle_save_config(self) -> None:
        data = self._read_json()
        if not isinstance(data, dict):
            self._error(400, "설정은 JSON 객체여야 합니다.")
            return
        # named_networks 형식 가벼운 검증
        nn = data.get("named_networks", [])
        if not isinstance(nn, list):
            self._error(400, "named_networks 는 배열이어야 합니다.")
            return
        for item in nn:
            if not isinstance(item, dict) or "name" not in item:
                self._error(400, "named_networks 의 각 항목에는 name 이 필요합니다.")
                return
        save_config(data, self.app.config_path)
        # 공유 config dict 를 제자리에서 갱신 → 모니터/식별에 즉시 반영
        reloaded = load_config(self.app.config_path)
        self.app.config.clear()
        self.app.config.update(reloaded)
        # 실행 중이면 재시작해 interval 등 변경을 적용
        restarted = False
        if self.app.controller.is_running():
            self.app.controller.stop()
            self.app.controller.start()
            restarted = True
        self._json({"ok": True, "restarted": restarted, "config": self.app.config})


# ---------------------------------------------------------------------------
# 서버 구동
# ---------------------------------------------------------------------------
def make_server(app: AppState, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.app = app  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def serve(config: dict, host: str = "127.0.0.1", port: int = 8765,
          config_path: str = "", autostart: bool = True,
          open_browser: bool = False) -> None:
    app = AppState(config, config_path)
    if autostart:
        app.controller.start()
    httpd = make_server(app, host, port)
    actual = httpd.server_address[1]
    url = f"http://{host}:{actual}/"
    print(f"netusage 웹 포탈 실행: {url}")
    print(f"  모니터 자동시작: {'예' if autostart else '아니오'}   (Ctrl+C 로 종료)")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료 중...")
    finally:
        app.controller.stop()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 내장 단일 페이지 UI (외부 의존성 없음)
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>netusage 포탈</title>
<style>
  :root { --bg:#0f1419; --card:#1b232c; --fg:#e6edf3; --mut:#8b97a4;
          --acc:#4aa3ff; --ok:#3fb950; --bad:#f85149; --line:#2b3641; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:14px 20px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:12px; }
  header h1 { font-size:17px; margin:0; font-weight:600; }
  header .ver { color:var(--mut); font-size:12px; }
  main { max-width:980px; margin:0 auto; padding:18px 20px 60px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px 18px; margin-bottom:18px; }
  .card h2 { font-size:14px; margin:0 0 12px; color:var(--mut);
             text-transform:uppercase; letter-spacing:.04em; }
  .row { display:flex; flex-wrap:wrap; gap:10px 22px; align-items:center; }
  .kv { display:flex; flex-direction:column; }
  .kv .k { font-size:11px; color:var(--mut); }
  .kv .v { font-size:15px; font-weight:600; }
  .badge { padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; }
  .badge.on { background:rgba(63,185,80,.15); color:var(--ok); }
  .badge.off { background:rgba(248,81,73,.15); color:var(--bad); }
  button { background:var(--acc); color:#06121f; border:0; border-radius:7px;
           padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--fg); border:1px solid var(--line); }
  button:disabled { opacity:.45; cursor:default; }
  select, input, textarea { background:#0d1218; color:var(--fg);
           border:1px solid var(--line); border-radius:6px; padding:7px 9px; font-size:13px; }
  textarea { width:100%; min-height:230px; font-family:ui-monospace,Menlo,monospace; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:right; padding:7px 10px; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child { text-align:left; }
  thead th { color:var(--mut); font-weight:600; }
  tfoot td { font-weight:700; }
  .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; }
  .msg { font-size:12px; margin-left:8px; }
  .msg.ok { color:var(--ok); } .msg.bad { color:var(--bad); }
  a.dl { color:var(--acc); font-size:13px; margin-right:14px; text-decoration:none; }
  .spacer { flex:1; }
</style>
</head>
<body>
<header>
  <h1>netusage</h1><span class="ver" id="ver"></span>
  <div class="spacer"></div>
  <span id="monBadge" class="badge off">모니터 중지됨</span>
</header>
<main>

  <div class="card">
    <h2>현재 상태 · 동작</h2>
    <div class="row" id="statusRow"></div>
    <div class="row" style="margin-top:14px;">
      <button id="btnStart">모니터 시작</button>
      <button id="btnStop" class="ghost">중지</button>
      <button id="btnSample" class="ghost">지금 한 번 샘플</button>
      <span id="ctrlMsg" class="msg"></span>
    </div>
  </div>

  <div class="card">
    <h2>조회 · 리포트</h2>
    <div class="controls">
      <label>집계
        <select id="by">
          <option value="network">network</option>
          <option value="ssid">ssid</option>
          <option value="type">type</option>
          <option value="iface">iface</option>
          <option value="gateway">gateway</option>
          <option value="hour">hour</option>
          <option value="day">day</option>
          <option value="week">week</option>
          <option value="month">month</option>
        </select>
      </label>
      <label>기간(since) <input id="since" placeholder="예: 7d, 24h, 2026-06-01" size="20"></label>
      <label>until <input id="until" placeholder="(선택)" size="14"></label>
      <button id="btnReport">새로고침</button>
      <span class="spacer"></span>
      <a class="dl" id="dlCsv" href="#">CSV 내보내기</a>
      <a class="dl" id="dlJson" href="#">JSON 내보내기</a>
    </div>
    <table id="repTable">
      <thead><tr><th>라벨</th><th>유형</th><th>수신</th><th>송신</th><th>합계</th><th>샘플</th></tr></thead>
      <tbody></tbody>
      <tfoot></tfoot>
    </table>
  </div>

  <div class="card">
    <h2>수정 · 설정 (config.json)</h2>
    <p style="color:var(--mut);font-size:12px;margin:0 0 8px;">
      named_networks 에 <code>{"name":"Office","ping":"10.0.0.1"}</code> 또는
      <code>{"name":"Lab","gateway":"172.16.5.1"}</code> 처럼 추가하면 Ethernet 네트워크를 식별합니다.
      저장 시 모니터가 실행 중이면 자동 재시작되어 즉시 반영됩니다.
    </p>
    <textarea id="cfg" spellcheck="false"></textarea>
    <div class="row" style="margin-top:10px;">
      <button id="btnSave">설정 저장</button>
      <button id="btnReload" class="ghost">다시 불러오기</button>
      <span id="cfgMsg" class="msg"></span>
      <span class="spacer"></span><span id="cfgPath" style="color:var(--mut);font-size:12px;"></span>
    </div>
  </div>

</main>
<script>
const $ = (s) => document.querySelector(s);
const fmtTs = (ts) => ts ? new Date(ts*1000).toLocaleString() : "-";

async function api(path, opts) {
  const r = await fetch(path, opts);
  const t = await r.text();
  let j = null; try { j = t ? JSON.parse(t) : null; } catch(e) {}
  if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
  return j;
}

function setMsg(el, text, ok) { el.textContent = text; el.className = "msg " + (ok ? "ok" : "bad"); }

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("#ver").textContent = "v" + s.version;
    const id = s.identity, m = s.monitor, t = s.totals;
    const c = s.counters;
    $("#statusRow").innerHTML = [
      ["네트워크", id.network || "-"],
      ["유형", id.conn_type || "-"],
      ["SSID", id.ssid || "-"],
      ["게이트웨이", id.gateway || "-"],
      ["인터페이스", id.iface || "-"],
      ["누적 사용(기록)", t.total ? human(t.total) : "0 B"],
      ["마지막 샘플", fmtTs(s.last_ts)],
      ["주기", m.interval + "s"],
    ].map(([k,v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join("");
    const b = $("#monBadge");
    if (m.running) { b.className = "badge on"; b.textContent = "모니터 실행 중"; }
    else { b.className = "badge off"; b.textContent = "모니터 중지됨"; }
    $("#btnStart").disabled = m.running;
    $("#btnStop").disabled = !m.running;
    if (m.last_error) setMsg($("#ctrlMsg"), "오류: " + m.last_error, false);
  } catch(e) { /* 무시 */ }
}

function human(n) {
  const u = ["B","KiB","MiB","GiB","TiB","PiB"]; let v = n, i = 0;
  while (Math.abs(v) >= 1024 && i < u.length-1) { v/=1024; i++; }
  return (i===0 ? v : v.toFixed(2)) + " " + u[i];
}
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function exportUrl(fmt) {
  const p = new URLSearchParams({ format: fmt });
  if ($("#since").value) p.set("since", $("#since").value);
  if ($("#until").value) p.set("until", $("#until").value);
  return "/api/export?" + p.toString();
}

async function refreshReport() {
  const p = new URLSearchParams({ by: $("#by").value });
  if ($("#since").value) p.set("since", $("#since").value);
  if ($("#until").value) p.set("until", $("#until").value);
  try {
    const d = await api("/api/report?" + p.toString());
    const tb = $("#repTable tbody"); tb.innerHTML = "";
    for (const r of d.rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(r.label==null?"unknown":r.label)}</td><td>${esc(r.conn_type||"-")}</td>`+
        `<td>${r.rx_human}</td><td>${r.tx_human}</td><td>${r.total_human}</td><td>${r.samples}</td>`;
      tb.appendChild(tr);
    }
    $("#repTable tfoot").innerHTML =
      `<tr><td>합계</td><td></td><td>${d.total.rx_human}</td><td>${d.total.tx_human}</td>`+
      `<td>${d.total.total_human}</td><td></td></tr>`;
  } catch(e) { alert("리포트 오류: " + e.message); }
  $("#dlCsv").href = exportUrl("csv");
  $("#dlJson").href = exportUrl("json");
}

async function loadConfig() {
  const d = await api("/api/config");
  $("#cfg").value = JSON.stringify(d.config, null, 2);
  $("#cfgPath").textContent = d.path;
  setMsg($("#cfgMsg"), "", true);
}

$("#btnStart").onclick = async () => {
  try { await api("/api/monitor/start", {method:"POST"}); setMsg($("#ctrlMsg"),"시작됨",true); }
  catch(e){ setMsg($("#ctrlMsg"), e.message, false); } refreshStatus();
};
$("#btnStop").onclick = async () => {
  try { await api("/api/monitor/stop", {method:"POST"}); setMsg($("#ctrlMsg"),"중지됨",true); }
  catch(e){ setMsg($("#ctrlMsg"), e.message, false); } refreshStatus();
};
$("#btnSample").onclick = async () => {
  try { const d = await api("/api/sample", {method:"POST"});
    setMsg($("#ctrlMsg"), d.recorded ? ("기록: "+d.recorded.network) : "기록 없음(기준값/카운터)", true);
  } catch(e){ setMsg($("#ctrlMsg"), e.message, false); }
  refreshStatus(); refreshReport();
};
$("#btnReport").onclick = refreshReport;
$("#by").onchange = refreshReport;
$("#btnReload").onclick = loadConfig;
$("#btnSave").onclick = async () => {
  let parsed;
  try { parsed = JSON.parse($("#cfg").value); }
  catch(e){ setMsg($("#cfgMsg"), "JSON 파싱 오류: " + e.message, false); return; }
  try {
    const d = await api("/api/config", {method:"POST", headers:{"Content-Type":"application/json"},
                                        body: JSON.stringify(parsed)});
    setMsg($("#cfgMsg"), "저장됨" + (d.restarted ? " (모니터 재시작)" : ""), true);
    $("#cfg").value = JSON.stringify(d.config, null, 2);
    refreshStatus();
  } catch(e){ setMsg($("#cfgMsg"), e.message, false); }
};

loadConfig();
refreshStatus();
refreshReport();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""
