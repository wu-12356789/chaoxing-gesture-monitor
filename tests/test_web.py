import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
import web


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=main.ROOT / "work")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "work").mkdir()
        (self.root / "config.json").write_bytes((main.ROOT / "config.json").read_bytes())
        self.fake = Mock(uid="test-user")
        self.fake.courses.return_value = [{"courseId": "1", "classId": "2"}]
        self.fake.activities.return_value = [{"courseId": "1", "classId": "2", "activeId": "3"}]
        self.fake.check.side_effect = [False, True]
        self.monitor = web.Monitor(self.root, client_factory=lambda: self.fake)
        self.addCleanup(self.monitor.close)
        self.app = web.create_app(self.monitor, "test-access-key")
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer test-access-key"}

    def post(self, action, data=None, **kwargs):
        return self.client.post("/api/" + action, json=data or {}, headers=self.headers, **kwargs)

    def test_html_static_assets_and_security_headers(self):
        for path in ("/", "/assets/app.js", "/assets/style.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            response.close()

    def test_state_and_mutations_require_key(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.post("/api/start", json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/status", headers=self.headers).status_code, 200)

    def test_cross_origin_rejected_even_with_key(self):
        headers = {**self.headers, "Origin": "https://unrelated.example"}
        self.assertEqual(self.client.post("/api/start", headers=headers, json={}).status_code, 403)

    def test_remote_password_requires_https(self):
        response = self.post("login", {"account": "test", "password": "secret"}, environ_base={"REMOTE_ADDR": "198.51.100.1"})
        self.assertEqual(response.status_code, 400)
        self.fake.login.assert_not_called()
        response = self.post("login", {"account": "test", "password": "secret"}, base_url="https://example.test", environ_base={"REMOTE_ADDR": "198.51.100.1"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_start_requires_login(self):
        self.assertEqual(self.post("start").status_code, 400)

    def test_unreadable_scan_alerts_once_then_recovers_read_only(self):
        self.monitor.login("user", "test")
        activity = self.fake.activities.return_value[0]
        self.fake.activities.side_effect = [
            web.engine.ActivityListUnavailable("列表不可读"),
            web.engine.ActivityListUnavailable("列表不可读"), [activity],
        ]
        self.monitor.mail = Mock()
        self.monitor.mail.settings.return_value = {}
        scans = []

        def wait(seconds):
            if not seconds:
                return
            scans.append((seconds, self.monitor.last_scan))
            if len(scans) == 3:
                raise web.MonitorStopped()

        self.monitor.wait = wait
        self.monitor.run()
        self.assertEqual(scans[:2], [(300, None), (300, None)])
        self.assertIsNotNone(scans[2][1])
        self.monitor.mail.send.assert_called_once()
        self.monitor.mail.remind.assert_called_once()
        self.fake.prepare.assert_not_called()
        self.fake.submit.assert_not_called()

    def test_config_saved_and_invalid_config_preserves_previous(self):
        config = dict(self.monitor.config, poll_seconds=45)
        self.assertEqual(self.post("config", config).status_code, 200)
        self.assertEqual(main.read_config(self.root / "config.json")["poll_seconds"], 45)
        self.assertEqual(self.post("config", {**config, "gestures": ["0"]}).status_code, 400)
        self.assertEqual(main.read_config(self.root / "config.json")["gestures"], ["1235789", "14789"])

    def test_login_failure_not_cached(self):
        self.fake.login.side_effect = main.LoginExpired("failed")
        self.assertEqual(self.post("login", {"account": "test", "password": "secret"}).status_code, 400)
        self.assertIsNone(self.monitor.client)
        self.fake.session.close.assert_called_once()

    def test_login_start_sign_stop_full_flow(self):
        self.assertEqual(self.post("login", {"account": "test", "password": "secret"}).status_code, 200)
        self.monitor.config["attempt_seconds"] = 0.001
        self.assertEqual(self.post("start").status_code, 200)
        self.assertEqual(self.post("start").status_code, 400)
        self.assertEqual(self.post("login", {"account": "test", "password": "secret"}).status_code, 400)
        deadline = time.monotonic() + 2
        while not self.monitor.last_scan and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(self.monitor.last_scan)
        self.fake.submit.assert_called_once_with({"courseId": "1", "classId": "2", "activeId": "3"}, "14789")
        self.assertEqual(self.post("stop").status_code, 200)
        self.monitor.thread.join(timeout=2)
        self.assertFalse(self.monitor.active())
        stored = json.loads((self.root / "work" / "state.json").read_text())
        self.assertEqual(stored["activities"]["test-user:1:2:3"]["status"], "success")

    def test_stop_interrupts_candidate_wait(self):
        self.monitor.login("test", "secret")
        self.monitor.start()
        self.monitor.stop()
        self.monitor.thread.join(timeout=2)
        self.assertFalse(self.monitor.active())
        self.fake.submit.assert_not_called()

    def test_proxy_https_forwarding(self):
        import requests
        from waitress import create_server

        app = web.create_app(self.monitor, "test-access-key", behind_proxy=True)
        for peer, scheme, expected in [
            ("127.0.0.1", "https", "请输入账号和密码"),
            ("127.0.0.1", "http", "远程登录请使用 HTTPS 地址"),
            ("192.0.2.1", "https", "远程登录请使用 HTTPS 地址"),
        ]:
            with self.subTest(peer=peer, scheme=scheme):
                server = create_server(app, host="127.0.0.1", port=0, asyncore_loop_timeout=0.05,
                                       **web.proxy_options(peer))
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                try:
                    response = requests.post(f"http://127.0.0.1:{server.effective_port}/api/login",
                                             headers={**self.headers, "X-Forwarded-Proto": scheme},
                                             json={}, timeout=4)
                    self.assertEqual(response.json()["error"], expected)
                finally:
                    server.task_dispatcher.shutdown()
                    server.trigger.pull_trigger(lambda: server.asyncore.close_all(server._map))
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
