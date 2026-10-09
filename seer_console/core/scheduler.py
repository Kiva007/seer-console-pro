"""定时任务调度器：启动日常、启动巅峰、自检重启"""

import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Callable
from dataclasses import dataclass


@dataclass
class ScheduledTask:
    """定时任务定义"""
    name: str
    target_hour: int = 0
    target_minute: int = 0
    enabled: bool = False
    callback: Optional[Callable] = None
    last_run: Optional[datetime] = None


class Scheduler:
    """定时任务管理器（运行在主线程的定时器线程中）"""

    def __init__(self):
        self._tasks: dict[str, ScheduledTask] = {}
        self._timer_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._on_tick: Optional[Callable] = None  # 每秒tick回调（更新UI）

    def add_task(self, name: str, hour: int, minute: int,
                 enabled: bool, callback: Callable) -> None:
        """添加或更新定时任务"""
        with self._lock:
            if name in self._tasks:
                task = self._tasks[name]
                task.target_hour = hour
                task.target_minute = minute
                task.enabled = enabled
                task.callback = callback
            else:
                self._tasks[name] = ScheduledTask(
                    name=name,
                    target_hour=hour,
                    target_minute=minute,
                    enabled=enabled,
                    callback=callback,
                )

    def remove_task(self, name: str) -> None:
        """移除定时任务"""
        with self._lock:
            self._tasks.pop(name, None)

    def set_enabled(self, name: str, enabled: bool) -> None:
        """启用/禁用定时任务"""
        with self._lock:
            if name in self._tasks:
                self._tasks[name].enabled = enabled

    def start(self, on_tick: Optional[Callable] = None) -> None:
        """启动定时器线程（每秒检查一次）"""
        if self._running:
            return
        self._running = True
        self._on_tick = on_tick
        self._timer_thread = threading.Thread(target=self._loop, daemon=True)
        self._timer_thread.start()

    def stop(self) -> None:
        """停止定时器"""
        self._running = False

    def _loop(self) -> None:
        """定时器主循环：每秒检查是否到达预设时间"""
        while self._running:
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            current_second = now.second

            # 仅在整分钟触发（秒=0时检查）
            if current_second == 0:
                with self._lock:
                    for task in self._tasks.values():
                        if not task.enabled or task.callback is None:
                            continue
                        if (task.target_hour == current_hour and
                                task.target_minute == current_minute):
                            # 避免同一分钟内重复触发
                            if (task.last_run is None or
                                    (now - task.last_run).total_seconds() > 60):
                                task.last_run = now
                                # 在独立线程中执行回调，避免阻塞定时器
                                threading.Thread(
                                    target=task.callback,
                                    daemon=True,
                                ).start()

            # 调用UI tick（用于更新剩余时间显示等）
            if self._on_tick:
                try:
                    self._on_tick(current_hour, current_minute, current_second)
                except Exception:
                    pass

            time.sleep(1)

    def get_remaining_seconds(self, name: str) -> Optional[int]:
        """获取距离下次执行的剩余秒数"""
        with self._lock:
            task = self._tasks.get(name)
            if not task or not task.enabled:
                return None

        now = datetime.now()
        target = now.replace(
            hour=task.target_hour,
            minute=task.target_minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return int((target - now).total_seconds())

    def get_remaining_text(self, name: str) -> str:
        """获取友好的剩余时间文本"""
        secs = self.get_remaining_seconds(name)
        if secs is None:
            return "--"
        if secs <= 60:
            return f"{secs}秒"
        if secs <= 3600:
            mins = secs // 60
            secs_rem = secs % 60
            return f"{mins}分{secs_rem}秒"
        hours = secs // 3600
        mins = (secs % 3600) // 60
        return f"{hours}时{mins}分"
