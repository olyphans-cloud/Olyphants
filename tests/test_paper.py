import json
import os
import tempfile
import unittest
from dataclasses import replace

from btc_robot.audit import verify_file
from btc_robot.config import Config, Mode
from btc_robot.data import DataError, generate_synthetic
from btc_robot.paper import paper_step

H = 3_600_000


class TestPaperStep(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cfg = replace(Config(), mode=Mode.PAPER, state_dir=self.td.name)
        self.data = generate_synthetic(900, 21)

    def tearDown(self):
        self.td.cleanup()

    def now_after(self, n):  # tepat setelah candle ke-(n-1) tutup
        return self.data[n - 1].ts + H + 5_000

    def test_init_then_incremental_and_audit_chain(self):
        r0 = paper_step(self.cfg, self.now_after(500), lambda: self.data[:500])
        self.assertEqual(r0["status"], "ok")
        self.assertEqual(r0["processed_bars"], 0)  # tidak backfill
        total = 0
        for n in range(501, 900):
            r = paper_step(self.cfg, self.now_after(n), lambda n=n: self.data[:n])
            self.assertEqual(r["status"], "ok")
            total += r["processed_bars"]
        self.assertEqual(total, 399)
        self.assertTrue(verify_file(os.path.join(self.td.name, "paper_audit.jsonl")))

    def test_rerun_same_data_is_idempotent(self):
        paper_step(self.cfg, self.now_after(500), lambda: self.data[:500])
        a = paper_step(self.cfg, self.now_after(600), lambda: self.data[:600])
        b = paper_step(self.cfg, self.now_after(600), lambda: self.data[:600])
        self.assertEqual(b["processed_bars"], 0)
        self.assertEqual(a["equity"], b["equity"])

    def test_stale_data_is_refused(self):
        r = paper_step(self.cfg, self.now_after(500) + 3 * H, lambda: self.data[:500])
        self.assertEqual(r["status"], "data_invalid")
        self.assertFalse(os.path.exists(os.path.join(self.td.name, "paper_state.json")))

    def test_fetch_failure_is_safe(self):
        def boom():
            raise DataError("jaringan mati")
        r = paper_step(self.cfg, self.now_after(500), boom)
        self.assertEqual(r["status"], "no_data")

    def test_unclosed_candle_dropped(self):
        # candle ke-500 belum tutup -> dibuang; sisanya sudah basi 30 menit (> batas 180 dtk) -> ditolak
        now = self.data[499].ts + H // 2
        r = paper_step(self.cfg, now, lambda: self.data[:500])
        self.assertEqual(r["status"], "data_invalid")

    def test_halt_persists_across_runs(self):
        paper_step(self.cfg, self.now_after(500), lambda: self.data[:500])
        path = os.path.join(self.td.name, "paper_state.json")
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        st["halted"], st["halt_reason"] = True, "uji"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        total_entries = 0
        for n in range(501, 900):
            r = paper_step(self.cfg, self.now_after(n), lambda n=n: self.data[:n])
            total_entries += r["counters"].get("entries", 0)
        self.assertEqual(total_entries, 0)


if __name__ == "__main__":
    unittest.main()
