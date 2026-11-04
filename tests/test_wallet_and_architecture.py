import ast
import os
import unittest

PKG = os.path.join(os.path.dirname(__file__), "..", "btc_robot")

def imports_of(fname):
    with open(os.path.join(PKG, fname), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mods.add(((n.module or "").split(".")[0] if n.level == 0 else (n.module or "")))
            for a in n.names:
                mods.add(a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
    return mods


class TestArchitecture(unittest.TestCase):
    """Dok 01 butir 10: tidak ada jalur strategi/AI/tracker ke eksekusi tanpa risk engine."""
    FORBIDDEN_FOR_SIGNAL_SIDE = {"risk", "execution", "ledger", "engine", "paper", "backtest"}

    def test_strategy_and_indicators_isolated(self):
        for f in ("strategy.py", "indicators.py"):
            bad = imports_of(f) & self.FORBIDDEN_FOR_SIGNAL_SIDE
            self.assertFalse(bad, f"{f} meng-import {bad}")

    def test_only_risk_creates_approved_orders(self):
        for f in os.listdir(PKG):
            if not f.endswith(".py") or f in ("risk.py", "models.py"):
                continue
            with open(os.path.join(PKG, f), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ApprovedOrder":
                    self.fail(f"{f} membuat ApprovedOrder langsung")

    def test_no_secrets_in_source(self):
        import re
        pat = re.compile(r"(sk-[A-Za-z0-9]{20,}|0x[a-fA-F0-9]{64}\b|api_?key\s*=\s*['\"][A-Za-z0-9]{16,})")
        for f in os.listdir(PKG):
            if f.endswith(".py"):
                with open(os.path.join(PKG, f), encoding="utf-8") as fh:
                    self.assertIsNone(pat.search(fh.read()), f)


if __name__ == "__main__":
    unittest.main()
