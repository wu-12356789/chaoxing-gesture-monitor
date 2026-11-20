"""Single-user mobile control panel for the persistent monitor."""
import argparse
from collections import deque
from datetime import datetime
from functools import wraps
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import threading
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory
from waitress import serve

import main as engine


class MonitorStopped(Exception):
    pass


class EventLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = deque(maxlen=100)
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.events.append(self.format(record))

    def snapshot(self):
        with self.lock:
            return list(self.events)


class Monitor:
    def __init__(self, root, client_factory=engine.Client):
        self.root = root
        self.mail = engine.Mail(root)
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.client = None
        self.status = "未登录"
        self.last_scan = None
        self.read_detail = ""
        self.config = engine.read_config(root / "config.json")
        self.events = EventLog()
        engine.LOG.addHandler(self.events)
        engine.LOG.setLevel(logging.INFO)

    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def login(self, account, password):
        with self.lock:
            if self.active():
                raise ValueError("请先停止监测，再登录账号")
            client = self.client_factory()
            try:
                client.login(account, password)
            except Exception:
                client.session.close()
                raise
            if self.client:
                self.client.session.close()
            self.client = client
            self.status = "已登录，等待开始"
            self.last_scan = None
            self.read_detail = ""
            with self.events.lock:
                self.events.events.clear()
            engine.LOG.info("账号登录成功")

    def save_config(self, data):
        config = engine.validate_config(data)
        with self.lock:
            path = self.root / "config.json"
            temporary = self.root / "config.tmp"
            temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            self.config = config
        return config

    def check_read(self):
        with self.lock:
            if not self.client:
                raise ValueError("请先登录学习通")
            if self.active():
                raise ValueError("正在监测，请查看本轮读取结果")
            self.read_detail = ""
            selected = self.config["course_ids"]
            courses = [course for course in self.client.courses() if not selected or course["courseId"] in selected]
            try:
                for course in courses:
                    self.client.activities(course)
            except engine.ActivityListUnavailable as exc:
                self.read_detail = exc.panel_detail
                self.status = f"只读检查失败：{exc}"
                return
            self.status = "只读检查完成，大学物理活动列表可读取" if courses else "本轮未找到匹配的大学物理课程"
            self.last_scan = datetime.now().astimezone().isoformat(timespec="seconds")

    def start(self):
        with self.lock:
            if self.active():
                raise ValueError("监测已经运行或正在停止")
            if self.client is None:
                raise ValueError("请先登录学习通")
            self.stop_event.clear()
            self.status = "正在启动"
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            self.stop_event.set()
            if self.active():
                self.status = "正在停止，等待当前请求结束"

    def wait(self, seconds):
        if self.stop_event.wait(seconds):
            raise MonitorStopped()

    def run(self):
        manual_only = False
        try:
            with engine.single_instance(self.root / "work" / "monitor.lock"):
                state = engine.State(self.root / "work" / "state.json")
                while not self.stop_event.is_set():
                    with self.lock:
                        config = dict(self.config)
                        self.status = "只读监测中，签到需手动完成" if manual_only else "监测中"
                    delay = config["poll_seconds"]
                    try:
                        courses = self.client.courses()
                        matched = 0
                        read_error = None
                        for course in courses:
                            self.wait(0)
                            if config["course_ids"] and course["courseId"] not in config["course_ids"]:
                                continue
                            matched += 1
                            try:
                                activities = self.client.activities(course)
                            except engine.ActivityListUnavailable as exc:
                                manual_only = True
                                read_error = exc
                                continue
                            for activity in activities:
                                self.wait(0)
                                if manual_only:
                                    activity = dict(activity, manual_only=True)
                                engine.handle_activity(self.client, state, activity, config, self.mail, sleep=self.wait)
                        if read_error is not None:
                            raise read_error
                        incidents = state.data.get("unreadable_courses", {})
                        if matched and self.client.uid in incidents:
                            del incidents[self.client.uid]
                            state.save()
                        with self.lock:
                            if not matched:
                                self.status = "本轮未找到匹配的大学物理课程"
                            self.last_scan = datetime.now().astimezone().isoformat(timespec="seconds")
                            self.read_detail = ""
                    except engine.ActivityListUnavailable as exc:
                        delay = max(delay, 300)
                        with self.lock:
                            self.status = f"本轮读取失败，{delay:g} 秒后只读复查"
                            self.read_detail = exc.panel_detail
                        engine.LOG.warning("%s：%s", self.status, exc)
                        incidents = state.data.setdefault("unreadable_courses", {})
                        if not incidents.get(self.client.uid):
                            try:
                                self.mail.send(self.mail.settings(), "大学物理监测暂时无法读取活动",
                                               f"{exc}。请打开学习通查看大学物理课程是否有签到。本轮未完成检测。")
                            except (ValueError, OSError):
                                engine.LOG.warning("监测故障邮件未发出，下次读取仍失败时重试")
                            else:
                                incidents[self.client.uid] = True
                                state.save()
                                engine.LOG.info("监测故障提醒已交给邮件服务器")
                    except engine.RetryLater as exc:
                        engine.LOG.warning("%s", exc)
                        delay = max(delay, exc.delay)
                        with self.lock:
                            self.status = "连接暂不可用，等待重试"
                    self.wait(delay)
        except MonitorStopped:
            pass
        except engine.LoginExpired:
            with self.lock:
                self.client.session.close()
                self.client = None
            engine.LOG.warning("登录已失效，请重新登录")
        except (engine.ProtocolError, ValueError) as exc:
            engine.LOG.warning("监测已停止：%s", exc)
        except (OSError, engine.requests.RequestException):
            engine.LOG.warning("监测已停止，请检查服务器网络和数据目录权限")
        except Exception:
            engine.LOG.error("监测遇到未识别错误，已停止，请检查版本与配置")
        finally:
            with self.lock:
                self.status = "已停止" if self.client else "未登录"

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "read_detail": self.read_detail if self.client else "",
                    "running": self.active(), "logged_in": self.client is not None,
                    "last_scan": self.last_scan, "config": self.config, "events": self.events.snapshot(),
                    "mail_configured": self.mail.path.exists(), "mail_recipient": self.mail.recipient()}

    def close(self):
        self.stop()
        if self.thread:
            self.thread.join(timeout=40)
        if self.client and not self.active():
            self.client.session.close()
        engine.LOG.removeHandler(self.events)


def create_app(monitor, token, behind_proxy=False):
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 16384

    def authorized(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return jsonify(error="访问密钥不正确"), 401
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error="请求来源不匹配"), 403
            return function(*args, **kwargs)
        return wrapper

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/")
    def index():
        return send_from_directory(Path(__file__).parent / "mobile", "index.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(Path(__file__).parent / "mobile", name)

    @app.get("/api/status")
    @authorized
    def status():
        return jsonify(monitor.snapshot())

    @app.post("/api/<action>")
    @authorized
    def action(action):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="请求必须为 JSON 对象"), 400
        try:
            if action == "login":
                if not request.is_secure and (behind_proxy or request.remote_addr not in ("127.0.0.1", "::1")):
                    return jsonify(error="远程登录请使用 HTTPS 地址"), 400
                account, password = data.get("account"), data.get("password")
                if not isinstance(account, str) or not isinstance(password, str) or not account.strip() or not password:
                    raise ValueError("请输入账号和密码")
                monitor.login(account.strip(), password)
            elif action == "mail":
                if not request.is_secure and (behind_proxy or request.remote_addr not in ("127.0.0.1", "::1")):
                    return jsonify(error="远程配置邮件请使用 HTTPS 地址"), 400
                monitor.mail.configure(data.get("sender"), data.get("recipient"), data.get("password"))
            elif action == "config":
                monitor.save_config(data)
            elif action == "read-check":
                monitor.check_read()
            elif action == "start":
                monitor.start()
            elif action == "stop":
                monitor.stop()
            else:
                return jsonify(error="未知操作"), 404
        except (ValueError, engine.ProtocolError, engine.RetryLater) as exc:
            return jsonify(error=str(exc)), 400
        except (OSError, engine.requests.RequestException):
            return jsonify(error="操作失败，请检查服务器网络或目录权限"), 503
        return jsonify(monitor.snapshot())

    return app


def proxy_options(peer):
    return {"trusted_proxy": socket.gethostbyname(peer), "trusted_proxy_headers": {"x-forwarded-proto"}}


def main():
    parser = argparse.ArgumentParser(description="iPhone 签到控制面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--behind-proxy", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ.get("SIGN_DATA_DIR", engine.ROOT))
    (root / "work").mkdir(parents=True, exist_ok=True)
    if not (root / "config.json").exists():
        (root / "config.json").write_bytes((engine.ROOT / "config.json").read_bytes())
    token_path = root / "work" / "panel-key.txt"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        token_path.chmod(0o600)
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("面板访问密钥文件异常，请检查后重新生成")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(root / "work" / "events.log", encoding="utf-8"),
    ])
    with engine.single_instance(root / "work" / "web.lock"):
        monitor = Monitor(root)
        try:
            print(f"面板运行于 http://{args.host}:{args.port}；访问密钥保存在 {token_path}", flush=True)
            options = proxy_options(os.environ.get("SIGN_PROXY_PEER", "127.0.0.1")) if args.behind_proxy else {}
            serve(create_app(monitor, token, args.behind_proxy), host=args.host, port=args.port, threads=4, **options)
        finally:
            monitor.close()


if __name__ == "__main__":
    main()
