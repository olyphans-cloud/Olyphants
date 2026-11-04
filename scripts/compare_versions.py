#!/usr/bin/env python3
"""Bandingkan dua pohon kode (mis. v4 vs baru) pada data sintetis, biaya, dan config yang SAMA.

  python3 scripts/compare_versions.py /path/ke/v4 /path/ke/baru  -> docs/before_after.json
Setiap pohon dijalankan di subprocess sendiri memakai config contohnya masing-masing."""
import json
import os
import subprocess
import sys

CASES = [("MTF 15m", "config.mtf.example.json", 900_000, 0.008, 3000),
         ("H1 1h", "config.h1.example.json", 3_600_000, 0.008, 6000),
         ("M1 1m", "config.m1.example.json", 60_000, 0.0007, 20000)]
CODE = '''
import sys, json
sys.path.insert(0, "{root}")
from btc_robot.config import load_config, validate
from btc_robot.data import generate_synthetic
from btc_robot.backtest import run_backtest
cfg = validate(load_config("{root}/{cfg}"))
r = run_backtest(generate_synthetic({n}, 1, {itv}, vol={vol}), cfg)
end = [e for e in r.audit.entries if e["event"] == "backtest_end"][-1]["payload"]
m = r.metrics
print(json.dumps({{"trades": m["trades"], "return_pct": round(m["return_pct"], 4), "max_drawdown_pct": round(m["max_drawdown_pct"], 4),
  "total_fees": round(m["total_fees"], 4), "win_rate": round(m["win_rate"], 4), "halted": end["halted"],
  "halt_reason": end["halt_reason"], "hash": r.result_hash[:12]}}))
'''


def run(root, cfg, itv, vol, n):
    out = subprocess.run([sys.executable, "-c", CODE.format(root=root, cfg=cfg, itv=itv, vol=vol, n=n)],
                         capture_output=True, text=True, cwd=root)
    if out.returncode != 0:
        return {"error": out.stderr[-300:]}
    return json.loads(out.stdout.strip().splitlines()[-1])


def main():
    old, new = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    res = {name: {"sebelum": run(old, cfg, itv, vol, n), "sesudah": run(new, cfg, itv, vol, n)}
           for name, cfg, itv, vol, n in CASES}
    for name, r in res.items():
        print(name)
        for k in ("sebelum", "sesudah"):
            print(f"  {k:8s}", r[k])
    out = os.path.join(new, "docs", "m1_audit", "before_after.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
