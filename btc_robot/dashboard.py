"""Dashboard lokal olypahantstrade (PAPER saja, tanpa uang asli).

Prinsip keselamatan (semuanya diuji di tests/test_v030_dashboard.py):
- Hanya bind ke loopback; header Host divalidasi; POST wajib header X-Dashboard-Token + JSON kecil.
- ACC TIDAK PERNAH membuka order. ACC hanya menjalankan pemeriksaan awal (preflight) lalu 'mengaktifkan' sleeve.
  Order hanya lahir di dalam paper_step -> Engine -> pipeline HALT/Risk/SafeGuard (tidak ada jalur lain).
- Satu sleeve = satu state_dir per (timeframe, koin): saldo, posisi, ledger, statistik, HALT, cooldown terpisah.
- Semua teks yang tampil di browser dirender dengan textContent (bukan innerHTML).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence

from .config import Config, ConfigError, Mode, load_config, validate
from .engine import Engine
from .identity import check_identity
from .models import BookQuote, Candle
from .paper import paper_step
from .portfolio import ScanConfig, scan
from .recovery import diagnose

TIMEFRAMES = {"M15": "config.m15.example.json", "H1": "config.h1.example.json"}
SCAN_MODES = ("AI_SCAN", "MANUAL")
_COIN_RE = re.compile(r"^[A-Z0-9]{3,20}$")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 4096
REJECT_EVENTS = ("risk_decision", "safeguard_reject", "halt_reject", "portfolio_reject", "spread_reject")


class DashboardError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


class Dashboard:
    def __init__(self, base_dir: str, config_dir: str, coins: Sequence[str],
                 data_source: Optional[Callable[[str, str], List[Candle]]] = None,
                 spread_provider: Optional[Callable[[Candle], Optional[BookQuote]]] = None,
                 token: Optional[str] = None, now_ms: Optional[Callable[[], int]] = None,
                 scan_config: Optional[ScanConfig] = None,
                 after_step: Optional[Callable[[str, str], None]] = None):
        bad = [c for c in coins if not _COIN_RE.match(c)]
        if bad or not coins:
            raise ValueError(f"daftar koin tidak valid: {bad or 'kosong'}")
        self.base_dir = os.path.abspath(base_dir)
        self.coins = tuple(sorted(set(coins)))
        self.base_cfg = {tf: load_config(os.path.join(config_dir, f)) for tf, f in TIMEFRAMES.items()}
        self.data_source = data_source
        self.spread_provider = spread_provider
        self.token = token or secrets.token_urlsafe(24)
        self.now_ms = now_ms
        self.after_step = after_step
        self.scan_config = scan_config or ScanConfig(mode="auto", top_n=1, lookback_bars=96)
        self.lock = threading.RLock()
        self.scan_mode = "MANUAL"
        self.coin: Dict[str, Optional[str]] = {tf: None for tf in TIMEFRAMES}
        self.armed: Dict[str, bool] = {tf: False for tf in TIMEFRAMES}     # tidak dipersist: ACC ulang setelah restart
        self.last_step: Dict[str, Optional[dict]] = {tf: None for tf in TIMEFRAMES}
        self.last_scan: Dict[str, List[dict]] = {tf: [] for tf in TIMEFRAMES}
        self._load_selection()

    # ------------------------------------------------------------------ konfigurasi sleeve
    def sleeve_cfg(self, tf: str, coin: str) -> Config:
        base = self.base_cfg[tf]
        return replace(base, mode=Mode.PAPER, symbol=coin,
                       state_dir=os.path.join(self.base_dir, "state", f"{tf.lower()}-{coin.lower()}"),
                       shared_kill_switch_file=os.path.join(self.base_dir, "state", "KILL_ALL"))

    def _control_path(self) -> str:
        return os.path.join(self.base_dir, "control.json")

    def _load_selection(self) -> None:
        d = _read_json(self._control_path())
        if not d:
            return
        if d.get("scan_mode") in SCAN_MODES:
            self.scan_mode = d["scan_mode"]
        for tf in TIMEFRAMES:
            c = (d.get("coin") or {}).get(tf)
            if c in self.coins:
                self.coin[tf] = c

    def _save_selection(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        tmp = self._control_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"scan_mode": self.scan_mode, "coin": self.coin}, fh)
        os.replace(tmp, self._control_path())

    # ------------------------------------------------------------------ statistik per sleeve
    def _state_of(self, cfg: Config) -> Optional[dict]:
        return _read_json(os.path.join(cfg.state_dir, "paper_state.json"))

    def _open_position(self, cfg: Config) -> Optional[dict]:
        st = self._state_of(cfg)
        return ((st or {}).get("ledger") or {}).get("position")

    def _rejections(self, cfg: Config, tail: int = 2000) -> Dict[str, int]:
        path = os.path.join(cfg.state_dir, "paper_audit.jsonl")
        out: Dict[str, int] = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-tail:]
        except OSError:
            return out
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ev, p = e.get("event"), e.get("payload") or {}
            if ev not in REJECT_EVENTS:
                continue
            if ev == "risk_decision":
                if p.get("approved"):
                    continue
                key = "risk:" + str(p.get("reason"))
            else:
                key = ev.replace("_reject", "") + ":" + str(p.get("reason"))
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1])[:12])

    def stats(self, tf: str) -> Dict[str, Any]:
        coin = self.coin[tf]
        base = {"coin": coin, "armed": self.armed[tf], "balance": None, "trades": 0, "win_rate": 0.0, "profit": 0.0,
                "loss": 0.0, "net_pnl": 0.0, "drawdown_pct": 0.0, "position": None, "rejections": {},
                "robot_status": "IDLE", "halt": {"halted": False, "reason": None, "sources": []},
                "safeguard": None, "machine_state": None, "last_step": self.last_step[tf]}
        if not coin:
            base["robot_status"] = "BELUM_DIPILIH"
            return base
        cfg = self.sleeve_cfg(tf, coin)
        base["balance"] = cfg.initial_equity
        st = self._state_of(cfg)
        if st:
            led = st.get("ledger") or {}
            trades = led.get("trades") or []
            nets = [float(t.get("net_pnl", 0.0)) for t in trades]
            wins = [x for x in nets if x > 0]
            equity = float(st.get("last_equity", led.get("cash", cfg.initial_equity)))
            peak = float(led.get("peak_equity", equity)) or equity
            base.update(balance=equity, trades=len(nets), win_rate=(len(wins) / len(nets) if nets else 0.0),
                        profit=sum(wins), loss=sum(x for x in nets if x <= 0), net_pnl=sum(nets),
                        drawdown_pct=max(0.0, (peak - equity) / peak * 100.0) if peak > 0 else 0.0,
                        machine_state=st.get("machine_state"), safeguard=st.get("safeguard"))
            pos = led.get("position")
            if pos:
                base["position"] = {k: pos.get(k) for k in ("side", "qty", "entry_price", "stop_price", "take_profit")}
        base["rejections"] = self._rejections(cfg)
        d = diagnose(cfg)
        if d.get("state_exists") or d.get("halted"):
            base["halt"] = {"halted": bool(d.get("halted")), "reason": d.get("halt_reason") or d.get("safeguard_halt_reason"),
                            "sources": d.get("halt_sources", [])}
        if not d.get("identity_ok", True):
            base["halt"] = {"halted": True, "reason": d.get("identity_status"), "sources": ["identity"]}
        if d.get("kill_switch_active"):
            base["halt"]["kill_switch"] = True
        base["robot_status"] = ("HALT" if base["halt"]["halted"] else "KILL_SWITCH" if d.get("kill_switch_active")
                                else "AKTIF" if self.armed[tf] else "IDLE")
        return base

    def state(self) -> Dict[str, Any]:
        with self.lock:
            return {"mode": "PAPER", "real_money": False, "scan_mode": self.scan_mode, "coins": list(self.coins),
                    "timeframes": {tf: self.stats(tf) for tf in TIMEFRAMES}, "scan": self.last_scan}

    # ------------------------------------------------------------------ aksi
    def _check_tf(self, tf: Any) -> str:
        if tf not in TIMEFRAMES:
            raise DashboardError("timeframe harus M15 atau H1")
        return tf

    def select(self, scan_mode: Any, tf: Any, coin: Any = None) -> Dict[str, Any]:
        with self.lock:
            tf = self._check_tf(tf)
            if scan_mode not in SCAN_MODES:
                raise DashboardError("scan_mode harus AI_SCAN atau MANUAL")
            if scan_mode == "AI_SCAN":
                chosen = self._ai_pick(tf)
            else:
                if not isinstance(coin, str) or coin not in self.coins:
                    raise DashboardError("koin tidak ada dalam daftar yang diizinkan")
                chosen = coin
            prev = self.coin[tf]
            if prev and prev != chosen:
                if self._open_position(self.sleeve_cfg(tf, prev)):
                    raise DashboardError(f"posisi simulasi {tf}/{prev} masih terbuka; selesaikan dulu sebelum ganti koin", 409)
                self.armed[tf] = False            # ganti koin => wajib ACC ulang
            self.scan_mode, self.coin[tf] = scan_mode, chosen
            self._save_selection()
            return self.state()

    def _ai_pick(self, tf: str) -> str:
        if self.data_source is None:
            raise DashboardError("AI Scan butuh sumber data candle (belum dikonfigurasi)", 503)
        interval = self.base_cfg[tf].interval
        data = {}
        for c in self.coins:
            try:
                data[c] = self.data_source(c, interval)
            except Exception:  # noqa: BLE001 - sumber gagal = koin dianggap tanpa data
                continue
        res = scan(data, self.scan_config)
        self.last_scan[tf] = [{"symbol": r.symbol, "selected": r.selected, "score": r.score, "reasons": list(r.reasons)}
                              for r in res]
        picked = [r.symbol for r in res if r.selected]
        if not picked:
            raise DashboardError("AI Scan tidak memilih koin (semua gagal filter keselamatan)", 422)
        return picked[0]

    def preflight(self, tf: str) -> List[Dict[str, Any]]:
        checks: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "") -> bool:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})
            return bool(ok)
        coin = self.coin[tf]
        if not add("pilihan_lengkap", coin is not None, "pilih koin dulu"):
            return checks
        if not add("koin_diizinkan", coin in self.coins):
            return checks
        try:
            cfg = validate(self.sleeve_cfg(tf, coin))
            add("konfigurasi_valid", True)
        except ConfigError as exc:
            add("konfigurasi_valid", False, str(exc))
            return checks
        add("mode_paper_tanpa_uang_asli", cfg.mode == Mode.PAPER)
        ok_id, id_status, id_msg = check_identity(cfg)
        add("identitas_state", ok_id, "" if ok_id else id_msg)
        d = diagnose(cfg)
        add("tidak_halt", not d.get("halted"), str(d.get("halt_reason") or d.get("safeguard_halt_reason") or ""))
        add("kill_switch_mati", not d.get("kill_switch_active"))
        st = self._state_of(cfg)
        if st:
            led = st.get("ledger") or {}
            ms = st.get("machine_state")
            try:
                from .statemachine import State
                consistent = ms is None or State(ms) in Engine.expected_machine_states(
                    bool(led.get("position")), bool(st.get("pending")))
            except ValueError:
                consistent = False
            add("state_machine_konsisten", consistent, str(ms))
        others = [c for c in self.coins if c != coin and self._open_position(self.sleeve_cfg(tf, c))]
        add("tidak_ada_posisi_di_koin_lain", not others, ",".join(others))
        return checks

    def acc(self, tf: Any) -> Dict[str, Any]:
        """ACC = persetujuan operator + preflight. TIDAK membuka order dan TIDAK memanggil paper_step."""
        with self.lock:
            tf = self._check_tf(tf)
            checks = self.preflight(tf)
            ok = all(c["ok"] for c in checks)
            self.armed[tf] = ok
            return {"armed": ok, "checks": checks, "orders_opened": 0,
                    "reason": None if ok else next(c["name"] for c in checks if not c["ok"])}

    def disarm(self, tf: Any) -> Dict[str, Any]:
        with self.lock:
            self.armed[self._check_tf(tf)] = False
            return self.state()

    def step(self, tf: Any) -> Dict[str, Any]:
        """Satu siklus simulasi: satu-satunya jalur order, lewat paper_step (pipeline lengkap)."""
        with self.lock:
            tf = self._check_tf(tf)
            if not self.armed[tf] or not self.coin[tf]:
                raise DashboardError("sleeve belum di-ACC (aktifkan dulu)", 409)
            cfg = self.sleeve_cfg(tf, self.coin[tf])
            fetcher = None
            if self.data_source is not None:
                try:
                    window = self.data_source(cfg.symbol, cfg.interval)     # ambil data DULU, baru hitung waktu
                    fetcher = lambda: window  # noqa: E731
                except Exception as exc:  # noqa: BLE001 - paper_step mencatatnya sebagai no_data
                    err = exc

                    def fetcher():  # type: ignore[misc]
                        raise err
            try:
                now_ms = self.now_ms() if self.now_ms else None
            except Exception:  # noqa: BLE001 - jam gagal => paper_step memakai jam sistem
                now_ms = None
            res = paper_step(cfg, now_ms=now_ms, fetcher=fetcher, spread_provider=self.spread_provider)
            if self.after_step is not None:
                self.after_step(cfg.symbol, cfg.interval)
            self.last_step[tf] = {k: res.get(k) for k in ("status", "processed_bars", "equity", "position", "halted", "error")}
            if res.get("halted"):
                self.armed[tf] = False
            return {"result": self.last_step[tf], "counters": res.get("counters", {}), "state": self.state()}


class DemoFeed:
    """Sumber candle SINTETIS untuk mode demo (menjalankan siklus tanpa jaringan). Bukan data pasar."""
    def __init__(self, coins: Sequence[str]):
        from .data import generate_synthetic
        self.coins = list(coins)
        self.data: Dict[tuple, List[Candle]] = {}
        self.k: Dict[tuple, int] = {}
        for i, c in enumerate(self.coins):
            self.data[(c, "1h")] = generate_synthetic(3000, 3 + i, 3_600_000, start_price=30000.0 / (i + 1))
            self.data[(c, "1m")] = generate_synthetic(20000, 3 + i, 60_000, vol=0.0007, start_price=30000.0 / (i + 1))
            self.k[(c, "1h")], self.k[(c, "1m")] = 300, 700
        self.cur: Optional[tuple] = None

    def source(self, symbol: str, interval: str) -> List[Candle]:
        key = (symbol, interval)
        self.cur = key
        return self.data[key][max(0, self.k[key] - 499):self.k[key] + 1]

    def advance(self, symbol: str, interval: str) -> None:
        self.k[(symbol, interval)] += 1

    def now_ms(self) -> int:
        itv = 3_600_000 if self.cur[1] == "1h" else 60_000
        return self.data[self.cur][self.k[self.cur]].ts + itv + 15_000


# ---------------------------------------------------------------------- HTTP
INDEX_HTML = """<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="dash-token" content="__TOKEN__">
<title>olypahantstrade - Demo PAPER</title>
<style>body{font-family:system-ui,sans-serif;margin:12px;max-width:980px}h1{font-size:1.15rem}
.card{border:1px solid #bbb;border-radius:8px;padding:10px;margin:10px 0}table{border-collapse:collapse;width:100%}
td{padding:2px 6px;border-bottom:1px solid #eee}.halt{color:#b00020;font-weight:700}.ok{color:#0a7d32;font-weight:700}
button{margin:2px;padding:6px 10px}.banner{background:#fff4d6;padding:6px;border-radius:6px}</style></head><body>
<h1>olypahantstrade &mdash; Demo PAPER <span class="banner">TANPA UANG ASLI</span></h1>
<div class="card"><b>Mode pemilihan koin:</b>
<label><input type="radio" name="scanmode" id="scan-ai" value="AI_SCAN"> AI Scan</label>
<label><input type="radio" name="scanmode" id="scan-manual" value="MANUAL" checked> Manual</label>
<div id="msg" role="status"></div></div>
<div id="panels"></div>
<script>
const TOKEN=document.querySelector('meta[name=dash-token]').content;const TFS=['M15','H1'];
async function api(path,body){const o=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':TOKEN},body:JSON.stringify(body)};
const r=await fetch(path,o);const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
function el(t,a,txt){const e=document.createElement(t);if(a)Object.assign(e,a);if(txt!==undefined)e.textContent=txt;return e}
function say(m,bad){const e=document.getElementById('msg');e.textContent=m;e.className=bad?'halt':'ok'}
function scanMode(){return document.getElementById('scan-ai').checked?'AI_SCAN':'MANUAL'}
async function act(fn){try{const r=await fn();say('OK');render(r.state||r)}catch(e){say(e.message,true);refresh()}}
function row(t,k,v,id){const r=el('tr');r.appendChild(el('td',null,k));const d=el('td',{id:id},v);r.appendChild(d);t.appendChild(r)}
function render(s){document.getElementById(s.scan_mode==='AI_SCAN'?'scan-ai':'scan-manual').checked=true;
const P=document.getElementById('panels');P.textContent='';
for(const tf of TFS){const t=s.timeframes[tf];const c=el('div',{className:'card',id:'panel-'+tf});
c.appendChild(el('b',null,'Timeframe '+tf+' ('+(tf==='M15'?'15 menit':'1 jam')+')'));
const sel=el('select',{id:'coin-'+tf});for(const x of s.coins){const o=el('option',{value:x},x);if(x===t.coin)o.selected=true;sel.appendChild(o)}
c.appendChild(sel);
const b=(id,label,fn)=>{const x=el('button',{id:id+'-'+tf},label);x.onclick=()=>act(fn);c.appendChild(x)};
b('apply','Terapkan pilihan',()=>api('/api/select',{scan_mode:scanMode(),timeframe:tf,coin:sel.value}));
b('acc','ACC',()=>api('/api/acc',{timeframe:tf}).then(r=>{if(!r.armed)throw new Error('ACC ditolak: '+r.reason);return s}));
b('disarm','Nonaktifkan',()=>api('/api/disarm',{timeframe:tf}));
b('step','Jalankan 1 siklus simulasi',()=>api('/api/step',{timeframe:tf}));
const T=el('table');row(T,'Status robot',t.robot_status,'status-'+tf);
row(T,'Saldo demo',t.balance===null?'-':t.balance.toFixed(2),'balance-'+tf);row(T,'Jumlah transaksi',String(t.trades),'trades-'+tf);
row(T,'Win rate',(t.win_rate*100).toFixed(1)+'%','winrate-'+tf);row(T,'Profit (jumlah menang)',t.profit.toFixed(2),'profit-'+tf);
row(T,'Loss (jumlah rugi)',t.loss.toFixed(2),'loss-'+tf);row(T,'PnL bersih',t.net_pnl.toFixed(2),'net-'+tf);
row(T,'Drawdown',t.drawdown_pct.toFixed(2)+'%','dd-'+tf);
row(T,'Posisi aktif',t.position?JSON.stringify(t.position):'tidak ada','pos-'+tf);
row(T,'Safe Guard',t.safeguard?JSON.stringify(t.safeguard):'-','sg-'+tf);
row(T,'HALT',t.halt.halted?('YA: '+(t.halt.reason||'')):'tidak','halt-'+tf);
row(T,'Alasan order ditolak',Object.keys(t.rejections).length?JSON.stringify(t.rejections):'-','rej-'+tf);
c.appendChild(T);P.appendChild(c)}}
async function refresh(){try{render(await api('/api/state'))}catch(e){say(e.message,true)}}
document.getElementById('scan-ai').onchange=()=>{};refresh();
</script></body></html>"""


def make_handler(dash: Dashboard, port_getter: Callable[[], int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "olypahantstrade-dashboard"

        def log_message(self, *a):  # tidak mencatat header/body (token)
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in LOOPBACK

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "host tidak diizinkan"})
            if self.path == "/":
                return self._send(200, INDEX_HTML.replace("__TOKEN__", dash.token).encode(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._json(200, dash.state())
            return self._json(404, {"error": "tidak ditemukan"})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "host tidak diizinkan"})
            if not hmac.compare_digest(self.headers.get("X-Dashboard-Token") or "", dash.token):
                return self._json(403, {"error": "token tidak valid"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "Content-Type harus application/json"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                return self._json(413, {"error": "body terlalu besar/tidak valid"})
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "JSON tidak valid"})
            routes = {"/api/select": lambda: dash.select(data.get("scan_mode"), data.get("timeframe"), data.get("coin")),
                      "/api/acc": lambda: dash.acc(data.get("timeframe")),
                      "/api/disarm": lambda: dash.disarm(data.get("timeframe")),
                      "/api/step": lambda: dash.step(data.get("timeframe"))}
            fn = routes.get(self.path)
            if fn is None:
                return self._json(404, {"error": "tidak ditemukan"})
            try:
                return self._json(200, fn())
            except DashboardError as exc:
                return self._json(exc.status, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - jangan bocorkan traceback
                return self._json(500, {"error": f"kesalahan internal: {type(exc).__name__}"})
    return Handler


def serve(dash: Dashboard, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in LOOPBACK:
        raise ValueError("dashboard hanya boleh bind ke loopback (127.0.0.1/localhost/::1)")
    srv = ThreadingHTTPServer((host, port), make_handler(dash, lambda: srv.server_address[1]))
    return srv
