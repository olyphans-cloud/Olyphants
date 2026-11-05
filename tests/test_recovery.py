import json
import os
import tempfile
import unittest
from dataclasses import replace

from btc_robot.audit import AuditLog, verify_file
from btc_robot.config import Config
from btc_robot.identity import write_identity
from btc_robot.recovery import diagnose, recover


class TestGuidedHaltRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = replace(Config(), state_dir=self.tmp.name)
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        write_identity(self.cfg)  # state yang valid selalu punya identitas (lihat tes isolasi untuk state lama)
        with open(os.path.join(self.cfg.state_dir, "paper_state.json"), "w", encoding="utf-8") as f:
            json.dump({"ledger": {"equity": 1000}, "halted": True,
                       "halt_reason": "max_drawdown test", "last_ts": 123}, f)
        AuditLog(os.path.join(self.cfg.state_dir, "paper_audit.jsonl")).log("test_halt", 1, reason="test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_recovery_requires_confirmation(self):
        with self.assertRaises(ValueError):
            recover(self.cfg, "operator", "akar masalah diperbaiki", False)
        self.assertTrue(diagnose(self.cfg)["halted"])

    def test_kill_switch_blocks_recovery(self):
        open(self.cfg.kill_switch_file, "w", encoding="utf-8").close()
        with self.assertRaisesRegex(ValueError, "KILL_SWITCH"):
            recover(self.cfg, "operator", "sudah diperbaiki", True)

    def test_recover_preserves_ledger_and_audits(self):
        before = diagnose(self.cfg)
        self.assertTrue(before["recovery_allowed"])
        result = recover(self.cfg, "operator", "sumber pemicu telah diperbaiki", True)
        self.assertFalse(result["halted"])
        with open(os.path.join(self.cfg.state_dir, "paper_state.json"), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["ledger"], {"equity": 1000})
        self.assertEqual(state["last_ts"], 123)
        self.assertFalse(state["halted"])
        self.assertEqual(state["recovery"]["approved_by"], "operator")
        self.assertTrue(verify_file(os.path.join(self.cfg.state_dir, "paper_audit.jsonl")))

    def test_bad_audit_blocks_recovery(self):
        with open(os.path.join(self.cfg.state_dir, "paper_audit.jsonl"), "a", encoding="utf-8") as f:
            f.write('{"bad":"entry"}\n')
        with self.assertRaises(ValueError):
            recover(self.cfg, "operator", "sudah diperbaiki", True)


if __name__ == "__main__":
    unittest.main()
