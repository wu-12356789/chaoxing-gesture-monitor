import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main


ACTIVITY = {"courseId": "100", "classId": "200", "activeId": "300"}
CONFIG = {"gestures": ["1235789", "14789"], "poll_seconds": 30, "attempt_seconds": 3}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=main.ROOT / "work")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = main.State(self.directory / "state.json")
        self.client = Mock(uid="user1")
        self.client.check.side_effect = [False, True]

    def process(self, config=None, state=None):
        main.process_activity(self.client, state or self.state, ACTIVITY, config or CONFIG, sleep=lambda _: None)

    def test_second_preset_succeeds_in_order_and_survives_restart(self):
        self.process()
        self.assertEqual([call.args[1] for call in self.client.check.call_args_list], CONFIG["gestures"])
        self.client.submit.assert_called_once_with(ACTIVITY, "14789")
        self.process(state=main.State(self.state.path))
        self.assertEqual(self.client.check.call_count, 2)

    def test_first_success_stops(self):
        self.client.check.side_effect = [True]
        self.process()
        self.client.check.assert_called_once_with(ACTIVITY, "1235789")

    def test_exhausted_presets_not_retried_and_new_preset_works(self):
        self.client.check.side_effect = [False, False, True]
        self.process()
        self.process(state=main.State(self.state.path))
        self.assertEqual(self.client.check.call_count, 2)
        self.process({**CONFIG, "gestures": [*CONFIG["gestures"], "12369"]})
        self.client.submit.assert_called_once_with(ACTIVITY, "12369")

    def test_check_timeout_retains_current_preset(self):
        self.client.check.side_effect = [False, main.RetryLater("timeout"), True]
        with self.assertRaises(main.RetryLater):
            self.process()
        self.process(state=main.State(self.state.path))
        self.assertEqual([call.args[1] for call in self.client.check.call_args_list], ["1235789", "14789", "14789"])

    def test_submit_timeout_does_not_mark_success(self):
        self.client.check.side_effect = [True, True]
        self.client.submit.side_effect = [main.RetryLater("timeout"), None]
        with self.assertRaises(main.RetryLater):
            self.process()
        self.assertEqual(self.state.record("user1:100:200:300")["status"], "pending")
        self.process()
        self.assertEqual(self.state.record("user1:100:200:300")["status"], "success")

    def test_unknown_check_response_blocks_without_guessing(self):
        self.client.check.side_effect = main.ProtocolError("unknown")
        self.process()
        self.process(state=main.State(self.state.path))
        self.client.check.assert_called_once()
        self.client.submit.assert_not_called()

    def test_submit_extra_verification_is_not_success(self):
        self.client.check.side_effect = [True]
        self.client.submit.side_effect = main.ProtocolError("validate")
        self.process()
        self.assertEqual(self.state.record("user1:100:200:300")["status"], "blocked")

    def test_login_expiry_stops_without_blocking_activity(self):
        self.client.check.side_effect = main.LoginExpired("expired")
        with self.assertRaises(main.LoginExpired):
            self.process()
        self.assertEqual(self.state.record("user1:100:200:300")["status"], "pending")

    def test_account_state_isolation(self):
        self.client.check.side_effect = [True, True]
        self.process()
        self.client.uid = "user2"
        self.process()
        self.assertEqual(self.client.submit.call_count, 2)

    def test_corrupt_state_is_preserved(self):
        self.state.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            main.State(self.state.path)
        self.assertEqual(self.state.path.read_text(), "broken")

    def test_single_instance_lock_releases(self):
        path = self.directory / "lock"
        with main.single_instance(path):
            with self.assertRaises(ValueError):
                with main.single_instance(path):
                    self.fail("second instance acquired lock")
        with main.single_instance(path):
            pass


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = main.Client()
        self.addCleanup(self.client.session.close)

    def test_all_active_gestures_in_mixed_activity_list(self):
        self.client.json = Mock(return_value={"data": {"activeList": [
            {"id": 1, "status": 1, "otherId": 0},
            {"id": 2, "status": 0, "otherId": 3},
            {"id": 3, "status": 1, "otherId": 3},
            {"id": 4, "status": "1", "otherId": "3"},
        ]}})
        activities = self.client.activities({"courseId": "100", "classId": "200"})
        self.assertEqual([a["activeId"] for a in activities], ["3", "4"])

    def test_rejection_vs_captcha(self):
        self.client.json = Mock(side_effect=[
            {"result": 0, "errorMsg": "手势不正确"},
            {"result": 1},
            {"result": 0, "errorMsg": "请完成验证码"},
        ])
        self.assertFalse(self.client.check(ACTIVITY, "1235789"))
        self.assertTrue(self.client.check(ACTIVITY, "14789"))
        with self.assertRaises(main.ProtocolError):
            self.client.check(ACTIVITY, "14789")

    def test_submit_only_recognizes_explicit_success(self):
        self.client.request = Mock(side_effect=[Mock(text="success"), Mock(text="您已签到过了"), Mock(text="validate")])
        self.client.submit(ACTIVITY, "14789")
        self.client.submit(ACTIVITY, "14789")
        with self.assertRaises(main.ProtocolError):
            self.client.submit(ACTIVITY, "14789")

    def test_rate_limit_and_login_redirect(self):
        self.client.session.request = Mock(side_effect=[
            Mock(status_code=429, headers={"Retry-After": "120"}),
            Mock(status_code=200, url="https://passport2.chaoxing.com/login"),
        ])
        with self.assertRaises(main.RetryLater) as caught:
            self.client.request("GET", main.BASE)
        self.assertEqual(caught.exception.delay, 120)
        with self.assertRaises(main.LoginExpired):
            self.client.request("GET", main.BASE)

    def test_login_uses_encoded_form_and_extracts_identity(self):
        self.client.session.cookies.set("_uid", "user1")
        self.client.json = Mock(return_value={"status": True, "name": "测试"})
        self.client.login("test-account", "test-secret")
        payload = self.client.json.call_args.kwargs["data"]
        self.assertEqual(payload["uname"], "test-account")
        self.assertNotEqual(payload["password"], "test-secret")
        self.assertEqual(self.client.uid, "user1")

    def test_config_is_user_presets(self):
        config = main.read_config(main.ROOT / "config.json")
        self.assertEqual(config["gestures"], ["1235789", "14789"])

    def test_dry_run_entry_point_never_checks_or_submits(self):
        client = Mock(uid="dry")
        client.courses.return_value = [{"courseId": "100", "classId": "200"}]
        client.activities.return_value = [ACTIVITY]
        with tempfile.TemporaryDirectory(dir=main.ROOT / "work") as temporary:
            root = Path(temporary)
            (root / "config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
            with patch.object(main, "ROOT", root), patch.object(main, "Client", return_value=client), \
                    patch("builtins.input", return_value="test"), patch.object(main.getpass, "getpass", return_value="test"), \
                    patch.object(main.logging, "basicConfig"), patch.object(main.logging, "FileHandler"):
                main.run(Mock(check_config=False, dry_run=True, once=True))
            client.prepare.assert_not_called()
            client.check.assert_not_called()
            client.submit.assert_not_called()
            client.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
