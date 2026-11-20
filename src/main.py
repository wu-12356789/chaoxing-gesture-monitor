"""Monitor course gesture activities using an explicit list of user presets."""
import argparse
from contextlib import contextmanager
import getpass
import json
import logging
from pathlib import Path
import re
import time
import unicodedata

import requests
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad
from alerts import Mail

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://mobilelearn.chaoxing.com"
LOG = logging.getLogger("gesture")


def is_university_physics(name):
    if not isinstance(name, str):
        return False
    title = "".join(unicodedata.normalize("NFKC", name).split())
    # Accept volume, level and semester suffixes while excluding distinct courses such as 大学物理实验.
    return re.fullmatch(r"大学物理(?:学)?(?:[A-C]|I{1,3}|IV|[上下册一二三四1-4]|\((?:[上下]册?|[一二三四1-4]|[A-C]|I{1,3}|IV)\))*", title) is not None


class ProtocolError(Exception):
    pass


class ActivityListUnavailable(ProtocolError):
    """The scan could not establish whether there are attendance activities."""


class LoginExpired(ProtocolError):
    pass


class RetryLater(Exception):
    def __init__(self, message, delay=60):
        super().__init__(message)
        self.delay = delay


def read_config(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return validate_config(data)


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError("配置必须为 JSON 对象")
    gestures = data.get("gestures")
    if not isinstance(gestures, list) or not gestures:
        raise ValueError("gestures 必须为非空轨迹列表")
    for code in gestures:
        if not isinstance(code, str) or not re.fullmatch(r"[1-9]{2,9}", code):
            raise ValueError("轨迹须为包含 2～9 个点的字符串，点编号为 1～9")
        if len(set(code)) != len(code):
            raise ValueError("一条轨迹不能重复经过同一个点")
    data["gestures"] = list(dict.fromkeys(gestures))
    for field in ("poll_seconds", "attempt_seconds"):
        value = data.get(field)
        if type(value) not in (int, float) or not 1 <= value <= 86400:
            raise ValueError(f"{field} 必须在 1～86400 秒之间")
    courses = data.get("course_ids", [])
    if not isinstance(courses, list) or any(not isinstance(c, str) for c in courses):
        raise ValueError("course_ids 必须是课程 ID 字符串列表")
    data["course_ids"] = courses
    return data


class State:
    def __init__(self, path):
        self.path = path
        self.data = {"version": 1, "activities": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(self.data, dict) or self.data.get("version") != 1:
            raise ValueError("状态文件版本异常")
        activities = self.data.get("activities")
        if not isinstance(activities, dict):
            raise ValueError("状态文件结构异常")
        for record in activities.values():
            if (not isinstance(record, dict) or not isinstance(record.get("rejected"), list)
                    or record.get("status") not in ("pending", "success", "blocked", "exhausted")):
                raise ValueError("状态记录损坏")

    def record(self, key):
        return self.data["activities"].setdefault(key, {"rejected": [], "status": "pending"})

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


@contextmanager
def single_instance(path):
    """OS lock releases even when the monitor is killed."""
    import os
    handle = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("已有签到程序在运行，请先关闭另一实例") from None
    try:
        yield
    finally:
        handle.close()


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/107.0.0.0 Mobile Safari/537.36"
        self.uid = ""
        self.fid = "-1"
        self.name = ""

    def request(self, method, url, **kwargs):
        try:
            response = self.session.request(method, url, timeout=(10, 20), **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            raise RetryLater("连接失败或超时，稍后重试当前候选") from None
        if response.status_code in (401, 403):
            raise LoginExpired("登录失效或访问被拒绝，请重新启动登录")
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "60")
            delay = int(retry) if retry.isdigit() else 60
            raise RetryLater("服务端限制频率，暂停后重试", max(60, delay))
        if response.status_code >= 500:
            raise RetryLater("服务端暂时不可用")
        if response.status_code != 200:
            raise ProtocolError(f"接口返回 HTTP {response.status_code}")
        if "passport2" in response.url and "passport2" not in url:
            raise LoginExpired("请求跳转至登录页，请重新启动登录")
        return response

    def json(self, method, url, **kwargs):
        response = self.request(method, url, **kwargs)
        try:
            result = response.json()
        except ValueError:
            raise ProtocolError("接口未返回 JSON，可能需要重新登录或完成额外验证") from None
        if not isinstance(result, dict):
            raise ProtocolError("接口数据结构异常")
        return result

    def login(self, account, password):
        encrypted = DES.new(b"u2oh6Vu^", DES.MODE_ECB).encrypt(pad(password.encode(), 8)).hex()
        result = self.json("POST", "https://passport2.chaoxing.com/fanyalogin", data={
            "uname": account, "password": encrypted, "fid": "-1", "t": "true",
            "refer": "https%3A%2F%2Fi.chaoxing.com", "forbidotherlogin": "0", "validate": "",
        })
        if result.get("status") not in (True, 1):
            raise LoginExpired("登录未成功，请检查账号密码及官方登录是否要求验证码")
        cookies = self.session.cookies.get_dict()
        self.uid = cookies.get("_uid", "")
        self.fid = cookies.get("fid", "-1")
        self.name = result.get("name") or ""
        if not self.uid:
            raise LoginExpired("登录响应缺少用户标识")
        if not self.name:
            page = self.request("GET", "https://passport2.chaoxing.com/mooc/accountManage").text
            match = re.search(r'class="fr colorBlue"[^>]*>\s*([^<]+)', page)
            if match:
                self.name = match.group(1).strip()

    def courses(self):
        result = self.json("GET", "https://mooc1-api.chaoxing.com/mycourse/backclazzdata", params={"rss": 1})
        if not isinstance(result.get("channelList"), list):
            raise ProtocolError("课程列表结构异常，请检查登录状态或接口变化")
        courses = {}
        for channel in result["channelList"]:
            content = channel.get("content") or {}
            class_id = content.get("id")
            for course in (content.get("course") or {}).get("data", []):
                if not is_university_physics(course.get("name")):
                    continue
                if class_id is not None and course.get("id") is not None:
                    pair = (str(course["id"]), str(class_id))
                    courses[pair] = {"courseId": pair[0], "classId": pair[1]}
        return list(courses.values())

    def activities(self, course):
        result = self.json("GET", f"{BASE}/v2/apis/active/student/activelist", params={
            **course, "fid": 0, "_": int(time.time() * 1000),
        })
        data = result.get("data")
        flags = [str(result[k]).lower() for k in ("result", "success") if k in result]
        if any(flag in ("0", "false") for flag in flags):
            raise ActivityListUnavailable("学习通返回失败状态，无法判断是否有签到")
        rows = data.get("activeList") if isinstance(data, dict) else None
        manual_list = not isinstance(rows, list)
        if manual_list:
            rows = data if isinstance(data, list) else result.get("activeList")
            if isinstance(data, list) and not data and not any(flag in ("1", "true") for flag in flags):
                raise ActivityListUnavailable("学习通返回空数据，但未确认活动查询成功")
        if not isinstance(rows, list):
            raise ActivityListUnavailable(f"学习通未返回可识别的活动列表（data 类型 {type(data).__name__}）")
        if any(not isinstance(item, dict) or "id" not in item or "status" not in item for item in rows):
            raise ActivityListUnavailable("学习通活动条目缺少可识别的编号或状态")
        activities = []
        for item in rows:
            if str(item.get("status")) != "1" or item.get("id") is None:
                continue
            activity_type = str(item.get("activeType", ""))
            gesture = str(item.get("otherId")) == "3"
            if activity_type != "2" and (activity_type or not gesture):
                continue
            activity = {**course, "activeId": str(item["id"])}
            if manual_list or not gesture:
                activity["manual_only"] = True
            activities.append(activity)
        return activities

    def prepare(self, activity):
        self.request("GET", f"{BASE}/newsign/preSign", params={
            "courseId": activity["courseId"], "classId": activity["classId"],
            "activePrimaryId": activity["activeId"], "uid": self.uid,
            "general": 1, "sys": 1, "ls": 1, "appType": 15, "ut": "s",
        })
        text = self.request("GET", f"{BASE}/pptSign/analysis", params={
            "vs": 1, "DB_STRATEGY": "RANDOM", "aid": activity["activeId"],
        }).text
        match = re.search(r"code='\+'([^']*)", text)
        if match:
            self.request("GET", f"{BASE}/pptSign/analysis2", params={
                "DB_STRATEGY": "RANDOM", "code": match.group(1),
            })

    def check(self, activity, code):
        result = self.json("GET", f"{BASE}/widget/sign/pcStuSignController/checkSignCode", params={
            "activeId": activity["activeId"], "signCode": code,
        })
        if str(result.get("result")) == "1":
            return True
        message = str(result.get("errorMsg") or result.get("msg") or "").strip()
        if message in ("手势不正确", "手势不正确！", "手势码不正确", "签到码不正确"):
            return False
        raise ProtocolError("手势校验未明确通过或拒绝，需检查登录、验证码及活动状态")

    def submit(self, activity, code):
        response = self.request("GET", f"{BASE}/pptSign/stuSignajax", params={
            "activeId": activity["activeId"], "uid": self.uid, "fid": self.fid,
            "name": self.name, "clientip": "", "latitude": "", "longitude": "",
            "appType": 15, "signCode": code,
        }).text.strip()
        if response in ("success", "您已签到过了", "您已签到过了！", "已签到"):
            return
        raise ProtocolError("提交未得到明确成功响应，需在学习通核对；可能要求额外验证")


def process_activity(client, state, activity, config, sleep=time.sleep):
    key = ":".join([client.uid, activity["courseId"], activity["classId"], activity["activeId"]])
    record = state.record(key)
    if record["status"] in ("success", "blocked"):
        return
    pending = [code for code in config["gestures"] if code not in record["rejected"]]
    if not pending:
        return
    LOG.info("发现手势签到，课程 %s，活动 %s", activity["courseId"], activity["activeId"])
    try:
        client.prepare(activity)
        for code in pending:
            sleep(config["attempt_seconds"])
            if not client.check(activity, code):
                record["rejected"].append(code)
                state.save()
                LOG.info("活动 %s：候选 %s 不匹配", activity["activeId"], code)
                continue
            client.submit(activity, code)
            record["status"] = "success"
            state.save()
            LOG.info("活动 %s：签到成功", activity["activeId"])
            return
        record["status"] = "exhausted"
        state.save()
        LOG.warning("活动 %s：预设手势全部不匹配", activity["activeId"])
    except LoginExpired:
        raise
    except ProtocolError as exc:
        record["status"] = "blocked"
        state.save()
        LOG.warning("活动 %s 暂停：%s", activity["activeId"], exc)


def handle_activity(client, state, activity, config, mail, sleep=time.sleep):
    record = state.record(":".join([client.uid, activity["courseId"], activity["classId"], activity["activeId"]]))
    if record["status"] == "success":
        return
    if activity.get("manual_only"):
        reason = "检测到当前程序无法处理的大学物理签到"
    else:
        try:
            process_activity(client, state, activity, config, sleep=sleep)
        except (LoginExpired, RetryLater):
            mail.remind(state, record, activity, "连接或登录异常，无法确认此次签到结果")
            raise
        if record["status"] not in ("blocked", "exhausted"):
            return
        reason = "此次大学物理签到未能确认成功"
    mail.remind(state, record, activity, reason)


def run(args):
    config_path = ROOT / "config.json"
    config = read_config(config_path)
    if args.check_config:
        print("配置有效，候选顺序：" + "、".join(config["gestures"]))
        return
    work = ROOT / "work"
    work.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(work / "events.log", encoding="utf-8"),
    ])
    with single_instance(work / "monitor.lock"):
        state = State(work / "state.json")
        mail = Mail(ROOT)
        client = Client()
        try:
            account = input("学习通账号：").strip()
            password = getpass.getpass("密码（输入隐藏）：")
            client.login(account, password)
            del password
            LOG.info("登录成功。按 Ctrl+C 停止；候选 %s", "、".join(config["gestures"]))
            while True:
                config = read_config(config_path)
                delay = config["poll_seconds"]
                try:
                    courses = client.courses()
                    for course in courses:
                        if config["course_ids"] and course["courseId"] not in config["course_ids"]:
                            continue
                        for activity in client.activities(course):
                            if args.dry_run:
                                LOG.info("只读检测：大学物理课程 %s，签到活动 %s", course["courseId"], activity["activeId"])
                            else:
                                handle_activity(client, state, activity, config, mail)
                    LOG.info("本轮检测结束，返回 %d 门课程", len(courses))
                except RetryLater as exc:
                    LOG.warning("%s", exc)
                    delay = max(delay, exc.delay)
                    if args.once:
                        raise
                if args.once:
                    break
                time.sleep(delay)
        finally:
            client.session.close()


def main():
    parser = argparse.ArgumentParser(description="学习通预设手势签到")
    parser.add_argument("--once", action="store_true", help="检测一轮后退出")
    parser.add_argument("--dry-run", action="store_true", help="只查询活动")
    parser.add_argument("--check-config", action="store_true", help="离线检查配置")
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("\n已停止。")
    except (ValueError, OSError, ProtocolError, RetryLater, EOFError) as exc:
        print(f"程序停止：{exc}")
        return 1
    except requests.RequestException:
        print("程序停止：网络请求失败，请检查连接后重试。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
