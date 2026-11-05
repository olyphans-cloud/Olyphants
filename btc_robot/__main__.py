"""CLI: python -m btc_robot <perintah> ..."""
from __future__ import annotations


import argparse
import json
import os
import sys
from dataclasses import replace

from . import gates
from .backtest import run_backtest
from .config import Config, ConfigError, Mode, config_warnings, load_config, validate
from .data import DataError, generate_synthetic, load_csv, validate_candles
from .paper import paper_step
from .portfolio import synth_vol, load_portfolio_spec, run_portfolio_backtest, scan as run_scan
from .recovery import diagnose as diagnose_halt, recover as recover_halt
from .validation import evaluate_holdout_once, split_holdout, walk_forward


def _cfg(args) -> Config:
    cfg = load_config(getattr(args, "config", None))
    for w in config_warnings(cfg):
        print(f"PERINGATAN KONFIGURASI: {w}", file=sys.stderr)
    return cfg


def _candles(args, cfg: Config):
    if getattr(args, "csv", None):
        return load_csv(args.csv)
    if getattr(args, "synthetic", None):
        return generate_synthetic(args.synthetic, args.seed, cfg.interval_ms, vol=synth_vol(cfg.interval))
    raise SystemExit("beri --csv FILE atau --synthetic N")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="btc_robot", description=__doc__)
    p.add_argument("--config", help="berkas JSON konfigurasi")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("validate-data", "backtest", "walk-forward", "holdout"):
        sp = sub.add_parser(name)
        sp.add_argument("--csv")
        sp.add_argument("--synthetic", type=int, help="jumlah candle SINTETIS (hanya tes mesin)")
        sp.add_argument("--seed", type=int, default=1)
        if name == "backtest":
            sp.add_argument("--audit", help="path audit log JSONL")
        if name == "holdout":
            sp.add_argument("--marker", default="state/holdout_used.json")
    pb = sub.add_parser("portfolio-backtest", help="simulasi beberapa sleeve (simbol x mode) dengan risiko bersama")
    pb.add_argument("--spec", required=True, help="berkas JSON spesifikasi portofolio")
    sc = sub.add_parser("scan", help="scanner koin (manual/auto) dari data di spesifikasi")
    sc.add_argument("--spec", required=True)
    ad = sub.add_parser("adopt-state", help="tetapkan identitas untuk state lama tanpa identity.json")
    ad.add_argument("--confirm-identity", action="store_true")
    sub.add_parser("paper-step")
    sub.add_parser("halt-status", help="diagnosis halt/KILL_SWITCH/audit untuk PAPER")
    rec = sub.add_parser("recover-halt", help="pemulihan terpandu halt PAPER; butuh konfirmasi manusia")
    rec.add_argument("--approved-by", required=True, help="identitas operator yang menyetujui")
    rec.add_argument("--reason", required=True, help="alasan dan perbaikan yang telah dilakukan")
    rec.add_argument("--confirm-resolved", action="store_true", help="konfirmasi akar masalah sudah diperbaiki")
    sub.add_parser("gates")
    db = sub.add_parser("dashboard", help="dashboard lokal PAPER (loopback saja, tanpa uang asli)")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--coins", default="BTCUSDT,ETHUSDT")
    db.add_argument("--demo", action="store_true", help="data sintetis (tanpa jaringan)")
    db.add_argument("--dir", default=".", help="folder kerja (state/ dan control.json)")
    args = p.parse_args(argv)

    try:
        if args.cmd == "dashboard":
            from .dashboard import Dashboard, DemoFeed, serve
            from .data import fetch_tokocrypto_klines
            coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
            if args.demo:
                feed = DemoFeed(coins)
                dash = Dashboard(args.dir, os.getcwd(), coins, data_source=feed.source, now_ms=feed.now_ms,
                                 spread_provider=lambda bar: __import__("btc_robot.models", fromlist=["BookQuote"]).BookQuote(
                                     100.0, 100.003, 1.0, 1.0),
                                 after_step=feed.advance)
            else:
                dash = Dashboard(args.dir, os.getcwd(), coins,
                                 data_source=lambda s, i: fetch_tokocrypto_klines(s, i, 500))
            srv = serve(dash, "127.0.0.1", args.port)
            print(f"Dashboard: http://127.0.0.1:{srv.server_address[1]}/  ({'DEMO sintetis' if args.demo else 'data publik'}; "
                  "PAPER saja, tanpa uang asli). Ctrl+C untuk berhenti.")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        cfg = _cfg(args)
        if args.cmd == "halt-status":
            report = diagnose_halt(cfg)
            print(json.dumps(report, indent=2, default=str))
            return 0 if report.get("state_exists") else 2
        if args.cmd == "recover-halt":
            report = recover_halt(cfg, args.approved_by, args.reason, args.confirm_resolved)
            print(json.dumps(report, indent=2, default=str))
            return 0
        if args.cmd in ("portfolio-backtest", "scan"):
            specs, limits, scan_cfg = load_portfolio_spec(args.spec)
            if args.cmd == "scan":
                if scan_cfg is None:
                    raise ConfigError("spesifikasi tidak punya bagian 'scan'")
                res = run_scan({s.cfg.symbol: s.candles for s in specs}, scan_cfg)
                print(json.dumps([{"symbol": r.symbol, "selected": r.selected, "score": r.score,
                                   "reasons": list(r.reasons), "metrics": dict(r.metrics)} for r in res], indent=2))
                return 0
            pr = run_portfolio_backtest(specs, limits, scan_cfg)
            print(json.dumps({"metrics": pr.metrics, "sleeves": pr.sleeves, "gate_counters": pr.gate_counters,
                              "skipped": pr.skipped, "result_hash": pr.result_hash}, indent=2, default=str))
            return 0
        if args.cmd == "adopt-state":
            from .identity import adopt
            print(adopt(replace(cfg, mode=Mode.PAPER), args.confirm_identity))
            return 0
        if args.cmd == "gates":
            print(gates.status_report(cfg.gates_file))
            return 0
        if args.cmd == "paper-step":
            if cfg.strategy_mode != "d1h4_futures" or cfg.interval != "4h":
                raise ConfigError(
                    "Profil PAPER operasional hanya mengizinkan d1h4_futures interval 4h. "
                    "Gunakan --config config.active.json; strategi lama hanya untuk riset/backtest."
                )
            cfg = replace(cfg, mode=Mode.PAPER)
            print(json.dumps(paper_step(cfg), indent=2, default=str))
            return 0
        cfg = validate(replace(cfg, mode=Mode.BACKTEST))
        candles = _candles(args, cfg)
        if args.cmd == "validate-data":
            rep = validate_candles(candles, cfg.interval_ms)
            print(json.dumps({"ok": rep.ok, "n": rep.n, "issues": rep.issues[:20]}, indent=2))
            return 0 if rep.ok else 2
        if args.cmd == "backtest":
            res = run_backtest(candles, cfg, getattr(args, "audit", None))
            print(json.dumps({"metrics": res.metrics, "counters": res.counters,
                              "result_hash": res.result_hash}, indent=2, default=str))
            return 0
        if args.cmd == "walk-forward":
            print(json.dumps(walk_forward(candles, cfg), indent=2))
            return 0
        if args.cmd == "holdout":
            _, hold = split_holdout(candles)
            print(json.dumps(evaluate_holdout_once(hold, cfg, args.marker), indent=2, default=str))
            return 0
    except (ConfigError, DataError, ValueError, RuntimeError) as exc:
        print(f"GAGAL (aman): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
