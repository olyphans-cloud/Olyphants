import unittest

from btc_robot.data import generate_synthetic
from btc_robot.config import StrategyConfig
from btc_robot.strategies_lab import LAB


class LabNoLookahead(unittest.TestCase):
    def test_signal_same_on_truncated_data(self):
        candles = generate_synthetic(900, seed=7, interval_ms=900_000, vol=0.0035)
        for name, cls in LAB.items():
            full = cls(StrategyConfig(atr_stop_mult=1.5))
            full.prepare(candles)
            for i in range(300, 900, 37):
                part = cls(StrategyConfig(atr_stop_mult=1.5))
                part.prepare(candles[:i + 1])
                a, b = full.signal_at(i), part.signal_at(i)
                self.assertEqual((a.side, a.stop_price, a.take_profit), (b.side, b.stop_price, b.take_profit), name)

    def test_signals_have_valid_geometry(self):
        candles = generate_synthetic(3000, seed=3, interval_ms=900_000, vol=0.0035)
        for name, cls in LAB.items():
            s = cls(StrategyConfig(atr_stop_mult=1.5))
            s.prepare(candles)
            n = 0
            for i in range(len(candles)):
                g = s.signal_at(i)
                if g.side.value == "LONG":
                    n += 1
                    self.assertLess(g.stop_price, g.ref_price)
                    self.assertGreater(g.take_profit, g.ref_price)
                elif g.side.value == "SHORT":
                    n += 1
                    self.assertGreater(g.stop_price, g.ref_price)
                    self.assertLess(g.take_profit, g.ref_price)
            self.assertGreater(n, 0, name)


if __name__ == "__main__":
    unittest.main()
