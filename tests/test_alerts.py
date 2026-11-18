from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from alerts import Mail


class PhysicsReminders(unittest.TestCase):
    def test_only_physics_classes_selected_before_activity_queries(self):
        client = main.Client()
        self.addCleanup(client.session.close)
        titles = ["大学物理", " 大学物理（下）", "大学物理Ⅱ", "大学物理实验", "高等数学", None]
        client.json = Mock(return_value={"channelList": [{"content": {
            "id": 7, "course": {"data": [dict(id=i, name=name) for i, name in enumerate(titles)]}
        }}]})
        self.assertEqual([c["courseId"] for c in client.courses()], ["0", "1", "2"])

    def test_manual_activity_is_attendance_only(self):
        client = main.Client()
        self.addCleanup(client.session.close)
        client.json = Mock(return_value={"data": {"activeList": [
            dict(id=1, status=1, activeType=2, otherId=0),
            dict(id=2, status=1, activeType=4, otherId=3),
            dict(id=3, status=0, activeType=2, otherId=0),
        ]}})
        self.assertEqual(client.activities({}), [{"activeId": "1", "manual_only": True}])

    def test_reminder_delivery_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "work").mkdir()
            mail = Mail(root)
            state = main.State(root / "state.json")
            client = Mock(uid="u")
            act = dict(activeId="a", courseId="c", classId="k", manual_only=True)
            conf = dict(gestures=["12"], attempt_seconds=1)
            with patch.object(mail, "send", side_effect=[ValueError("offline"), None]) as send, \
                    patch("alerts.time.time", return_value=1000) as now:
                main.handle_activity(client, state, act, conf, mail)
                main.handle_activity(client, state, act, conf, mail)
                self.assertEqual(send.call_count, 1)
                now.return_value = 1061
                main.handle_activity(client, main.State(state.path), act, conf, mail)
                main.handle_activity(client, main.State(state.path), act, conf, mail)
                self.assertEqual(send.call_count, 2)
                client.prepare.assert_not_called()

    def test_failure_only(self):
        with tempfile.TemporaryDirectory() as folder:
            state = main.State(Path(folder) / "state.json")
            client, mail = Mock(uid="u"), Mock()
            client.check.return_value = False
            act = dict(activeId="a", courseId="c", classId="k")
            conf = dict(gestures=["12"], attempt_seconds=1)
            main.handle_activity(client, state, act, conf, mail, sleep=lambda _: None)
            mail.remind.assert_called_once()
            mail.reset_mock()
            client.check.return_value = True
            main.handle_activity(client, state, dict(act, activeId="b"), conf, mail, sleep=lambda _: None)
            mail.remind.assert_not_called()

    def test_configuration_requires_successful_test_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "work").mkdir()
            mail = Mail(root)
            with patch.object(mail, "send", side_effect=[ValueError("offline"), None]):
                with self.assertRaises(ValueError):
                    mail.configure("sender@qq.com", "user@example.com", "test")
                self.assertFalse(mail.path.exists())
                mail.configure("sender@qq.com", "user@example.com", "test")
            self.assertEqual(mail.recipient(), "user@example.com")
