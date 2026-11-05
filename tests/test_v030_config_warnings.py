"""v0.3.0 - Peringatan konfigurasi statis (biaya vs stop/trailing). Tidak mengubah perilaku apa pun."""
import subprocess
import sys
import unittest
from dataclasses import replace

from btc_robot.config import Config, config_warnings, load_config, roundtrip_cost_pct


class WarningTests(unittest.TestCase):
    def test_roundtrip_cost_formula(self):
        m1 = load_config("config.m1.example.json")
        self.assertAlmostEqual(roundtrip_cost_pct(m1), 2 * (0.075 + (0.5 + 1.0) / 100), places=9)   # 0,18%
        self.assertAlmostEqual(roundtrip_cost_pct(Config()), 2 * (0.1 + (1.0 + 3.0) / 100), places=9)  # 0,28%

    def test_m1_example_warns_about_stop_floor_and_trailing_lock(self):
        w = config_warnings(load_config("config.m1.example.json"))
        self.assertTrue(any("min_stop_distance_pct" in x for x in w), w)
        self.assertTrue(any("trailing" in x for x in w), w)

    def test_trailing_that_locks_more_than_costs_does_not_warn(self):
        cfg = Config()
        cfg = replace(cfg, risk=replace(cfg.risk, trailing_trail_pct=1.0, trailing_activation_pct=2.0,
                                        min_stop_distance_pct=0.5))
        self.assertEqual(config_warnings(cfg), [])

    def test_default_config_has_no_trailing_warning(self):
        self.assertFalse(any("trailing" in x for x in config_warnings(Config())))

    def test_cli_prints_warnings_to_stderr_not_stdout(self):
        p = subprocess.run([sys.executable, "-m", "btc_robot", "--config", "config.m1.example.json", "backtest",
                            "--synthetic", "300"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertIn("PERINGATAN KONFIGURASI", p.stderr)
        self.assertNotIn("PERINGATAN", p.stdout)


if __name__ == "__main__":
    unittest.main()
