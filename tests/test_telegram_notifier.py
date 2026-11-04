import unittest
from unittest.mock import patch
from btc_robot.telegram_notifier import TelegramNotifier

class TelegramNotifierTests(unittest.TestCase):
    def test_disabled_returns_false(self):
        notifier = TelegramNotifier(token="x", chat_id="y", enabled=False)
        self.assertFalse(notifier.send_message("test"))

    def test_signal_formats_message(self):
        notifier = TelegramNotifier(token="x", chat_id="y", enabled=True)
        with patch.object(notifier, "send_message", return_value=True) as send:
            self.assertTrue(notifier.send_signal("LONG", 123.45, "reason"))
            self.assertIn("LONG", send.call_args.args[0])
            self.assertIn("123.45", send.call_args.args[0])

if __name__ == "__main__":
    unittest.main()
