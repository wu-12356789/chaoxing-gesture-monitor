"""Private SMTP configuration and activity reminders."""
from email.message import EmailMessage
import json
import logging
import os
from pathlib import Path
import re
import smtplib
import ssl
import tempfile
import time

LOG = logging.getLogger("gesture")


def email_address(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
        raise ValueError("请输入有效邮箱地址")
    return value


class Mail:
    def __init__(self, root):
        self.path = Path(root) / "work" / "mail.json"

    def settings(self):
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def recipient(self):
        target = self.path.with_name("mail-recipient.txt")
        return self.settings().get("recipient") or (target.read_text(encoding="utf-8").strip() if target.exists() else "")

    def configure(self, sender, recipient, password):
        sender, recipient = email_address(sender), email_address(recipient)
        if not sender.lower().endswith("@qq.com"):
            raise ValueError("发信邮箱需使用 QQ 邮箱")
        if not isinstance(password, str) or not password.strip():
            raise ValueError("请填写发信邮箱的 SMTP 授权码")
        settings = dict(sender=sender, recipient=recipient, password=password.strip())
        # Verify the route before replacing working credentials.
        self.send(settings, "大学物理提醒测试", "这是一封测试邮件。收到后，说明大学物理签到提醒可以投递到此邮箱。")
        with tempfile.NamedTemporaryFile(dir=self.path.parent, mode="w", encoding="utf-8", delete=False) as f:
            try:
                json.dump(settings, f)
                f.close()
                os.replace(f.name, self.path)
            finally:
                Path(f.name).unlink(missing_ok=True)

    @staticmethod
    def send(settings, subject, body):
        if not settings:
            raise ValueError("请先配置邮件提醒")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings["sender"]
        message["To"] = settings["recipient"]
        message.set_content(body)
        try:
            with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=8, context=ssl.create_default_context()) as smtp:
                smtp.login(settings["sender"], settings["password"])
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException, UnicodeError):
            raise ValueError("邮件未发出，请核对 SMTP 授权码、邮箱地址及服务器网络") from None

    def remind(self, state, record, activity, reason):
        now = time.time()
        if record.get("mail_sent") or record.get("mail_retry_after", 0) > now:
            return
        try:
            self.send(self.settings(), "大学物理签到需要你处理",
                      f"{reason}。请现在打开学习通，进入大学物理课程核对签到状态，尚未签到时请手动完成。\n活动编号 {activity['activeId']}")
        except (OSError, ValueError) as exc:
            record["mail_retry_after"] = now + 60
            LOG.warning("手动签到邮件未发出，活动有效期间将重试：%s", exc)
        else:
            record["mail_sent"] = True
            LOG.info("手动签到提醒已交给邮件服务器，活动 %s", activity["activeId"])
        state.save()
