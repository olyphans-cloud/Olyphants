import unittest
from btc_robot.config import StrategyConfig
from btc_robot.data import generate_synthetic
from btc_robot.mtf_strategy import MultiTimeframeStrategy, _aggregate_complete, M15, M30, H1
from btc_robot.models import Side

class MtfStrategyTests(unittest.TestCase):
    def test_aggregate_requires_complete_groups(self):
        bars = generate_synthetic(7, 5, M15)
        self.assertEqual(len(_aggregate_complete(bars, M30)), 3)
        self.assertEqual(len(_aggregate_complete(bars, H1)), 1)

    def test_strategy_returns_signal_type_and_uses_15m_input(self):
        bars = generate_synthetic(600, 21, M15)
        strategy = MultiTimeframeStrategy(StrategyConfig())
        strategy.prepare(bars)
        for i in range(len(bars)):
            sig = strategy.signal_at(i)
            self.assertIn(sig.side, (Side.LONG, Side.SHORT, Side.NO_TRADE))
            self.assertEqual(sig.strategy_version, strategy.VERSION)

    def test_incomplete_higher_timeframe_does_not_use_future_candle(self):
        bars = generate_synthetic(7, 8, M15)
        strategy = MultiTimeframeStrategy(StrategyConfig())
        strategy.prepare(bars[:3])
        sig = strategy.signal_at(2)
        self.assertEqual(sig.side, Side.NO_TRADE)
        self.assertIn("incomplete", sig.reason)

if __name__ == '__main__':
    unittest.main()
