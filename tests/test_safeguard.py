import json
import os
import tempfile
import unittest
from btc_robot.safeguard import SafeguardExecutionEngine

class TestSafeguard(unittest.TestCase):
    def test_spread_valid_and_rejected(self):
        g = SafeguardExecutionEngine(max_spread_pct=.15)
        self.assertTrue(g.validate_spread_gate(100, 100.1, observed_at=10, now=10).allowed)
        self.assertEqual(g.validate_spread_gate(100, 100.2, observed_at=10, now=10).reason, 'spread_limit')
        self.assertEqual(g.validate_spread_gate(100, 99, observed_at=10, now=10).reason, 'orderbook_price_invalid')
        self.assertEqual(g.validate_spread_gate(100, 100.1, observed_at=1, now=10).reason, 'orderbook_stale_or_future')
        self.assertFalse(g.validate_spread_gate(float('nan'), 100.1, now=10).allowed)

    def test_loss_streak_cooldown_and_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'state.json')
            g = SafeguardExecutionEngine(max_loss_streak=3, cooldown_sec=60, state_path=path)
            g.record_closed_trade(-1); g.record_closed_trade(-2); g.record_closed_trade(-3)
            self.assertEqual(g.check_circuit_breaker(now=100).reason, 'loss_streak_cooldown_started')
            g2 = SafeguardExecutionEngine(state_path=path)
            self.assertGreater(g2.status(now=101)['cooldown_remaining_sec'], 0)
            self.assertEqual(g2.check_circuit_breaker(now=101).reason, 'cooldown')

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 's.json')
            with open(p, 'w') as f: f.write('{broken')
            g = SafeguardExecutionEngine(state_path=p)
            self.assertFalse(g.check_circuit_breaker(now=100).allowed)
            self.assertTrue(g.halted)

    def test_trailing_stop_monotonic(self):
        g = SafeguardExecutionEngine()
        s1 = g.update_dynamic_trailing_stop(110, 100, 99, trail_pct=1, side='LONG')
        s2 = g.update_dynamic_trailing_stop(108, 100, s1, trail_pct=1, side='LONG')
        self.assertGreater(s1, 99)
        self.assertEqual(s2, s1)
        short1 = g.update_dynamic_trailing_stop(90, 100, 101, trail_pct=1, side='SHORT')
        short2 = g.update_dynamic_trailing_stop(92, 100, short1, trail_pct=1, side='SHORT')
        self.assertLess(short1, 101)
        self.assertEqual(short2, short1)

    def test_max_hold(self):
        g = SafeguardExecutionEngine()
        self.assertTrue(g.evaluate_max_hold_limit(100, 90, now=190))
        self.assertFalse(g.evaluate_max_hold_limit(100, 90, now=189.9))

    def test_halt_persists(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 's.json')
            g = SafeguardExecutionEngine(state_path=p); g.halt('operator')
            g2 = SafeguardExecutionEngine(state_path=p)
            self.assertFalse(g2.check_circuit_breaker(now=100).allowed)
            self.assertEqual(g2.halt_reason, 'operator')

if __name__ == '__main__': unittest.main()
