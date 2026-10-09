"""微信推送通知（pushplus）"""

import threading
import urllib.request
import urllib.parse
import json
from datetime import datetime


class Notifier:
    """微信推送通知服务"""

    PUSHPLUS_URL = "http://www.pushplus.plus/send"

    def __init__(self, token: str = ""):
        self._token = token
        self._lock = threading.Lock()

    @property
    def token(self) -> str:
        return self._token

    @token.setter
    def token(self, value: str) -> None:
        self._token = value

    def push(self, title: str, content: str) -> bool:
        """推送一条微信消息（pushplus）"""
        if not self._token:
            return False

        with self._lock:
            try:
                data = {
                    "token": self._token,
                    "title": title,
                    "content": content,
                    "template": "txt",
                }
                json_data = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(
                    self.PUSHPLUS_URL,
                    data=json_data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    return result.get("code") == 200
            except Exception:
                return False

    def notify_login_timeout(self, index: int, client_type: str) -> None:
        """登录超时通知"""
        title = f"{client_type}登录超时"
        content = f"序号[{index+1}]{client_type}_登录超时！！"
        self.push(title, content)

    def notify_expiring(self, warning_type: str, account_info: str, days: int) -> None:
        """临期通知"""
        title = f"{warning_type}临期账号"
        content = f"{account_info}|{warning_type}剩余天数：{days}天"
        self.push(title, content)

    def notify_all_complete(self, mode: str) -> None:
        """全部完成通知"""
        title = f"{mode}完成通知"
        content = "运行正常"
        self.push(title, content)

    def notify_stall_unfinished(self, mode: str, account_detail: str) -> None:
        """卡死重启2轮后仍未完成的账号提醒（每批次一次，由看门狗保证）"""
        title = f"{mode}任务未完成"
        content = f"卡死重启2轮后仍未完成：{account_detail}"
        self.push(title, content)

    def notify_error(self, title: str, detail: str) -> None:
        """通用错误通知"""
        timestamp = datetime.now().strftime("%m-%d %H:%M:%S")
        self.push(title, f"[{timestamp}] {detail}")
