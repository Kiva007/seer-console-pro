"""StallWatchdog（任务完成卡死看门狗）状态机模拟测试。

假时钟 + 假探测函数驱动，无需 pytest / 无 GUI 依赖：
    python seer_console/tests/test_stall_watchdog.py

约定语义（固定窗口，所有等待不随完成事件刷新）：
  兜底等待：进入 run() 即计时，到期无任何完成 → 第1轮重启
  第一轮等待：检测到首个完成时锚定，到期仍有未完成 → 第1轮重启
  第二轮等待：第1轮重启完成即锚定，到期仍有未完成 → 第2轮重启
  末尾轮检测：第2轮重启完成即锚定，到期仍有未完成 → 微信推送，此后只监测
  任意时刻 pending 清空 → all_complete()（完成通知+关闭全部窗口）
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from seer_console.core.watchdog import StallWatchdog


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Env:
    """组装一次看门狗运行环境，记录全部副作用。"""

    def __init__(self, active, *, poll_s, fallback_s, round1_s, round2_s,
                 done_at=None, gone_at=None, stop_after=None):
        self.clock = FakeClock()
        self.restarts = []      # [(round_no, pending列表, 触发时刻)]
        self.pushes = []        # [(pending列表, 触发时刻)]
        self.all_complete = []  # [触发时刻]
        self.logs = []
        self.poll_count = 0
        done_at = done_at or {}
        gone_at = gone_at or {}

        def probe(i):
            if gone_at.get(i) is not None and self.clock.now >= gone_at[i]:
                return "gone"
            if done_at.get(i) is not None and self.clock.now >= done_at[i]:
                return "done"
            return "wait"

        def restart_round(round_no, pending):
            self.restarts.append((round_no, sorted(pending), self.clock.now))
            return True

        def push_unfinished(pending):
            self.pushes.append((sorted(pending), self.clock.now))

        def all_complete():
            self.all_complete.append(self.clock.now)

        def log(msg):
            self.logs.append((self.clock.now, msg))

        def sleep_wait(s):
            # 模拟 Event.wait：睡 poll_s 秒（假时钟直接推进）
            self.clock.now += s
            self.poll_count += 1
            return stop_after is not None and self.poll_count >= stop_after

        self.wd = StallWatchdog(
            poll_s=poll_s, fallback_s=fallback_s, round1_s=round1_s,
            round2_s=round2_s, probe=probe, restart_round=restart_round,
            push_unfinished=push_unfinished, all_complete=all_complete,
            log=log, sleep_wait=sleep_wait, clock=self.clock)
        self.active = list(active)

    def run(self):
        self.wd.run(self.active)


def test_trickle_completes_without_restart():
    """场景A：涓流完成（首个完成→陆续全完成），不重启不推送，只发完成回调。"""
    env = Env([0, 1, 2, 3], poll_s=60, fallback_s=3600, round1_s=600,
              round2_s=1200, done_at={0: 60, 1: 120, 2: 180, 3: 240})
    env.run()
    assert env.all_complete == [240], env.all_complete
    assert env.restarts == [], env.restarts
    assert env.pushes == [], env.pushes


def test_stuck_from_start_full_max_flow():
    """场景B：最长流程——兜底超时→第1轮重启→第2轮重启→推送→推送后补完→完成。"""
    env = Env([0, 1, 2, 3], poll_s=60, fallback_s=600, round1_s=600,
              round2_s=1200,
              done_at={3: 1200, 0: 3060, 1: 3120, 2: 3120})
    env.run()
    # t=600 兜底超时 → 第1轮重启全部；t=1800 第2轮重启剩余；t=3000 推送
    assert env.restarts == [(1, [0, 1, 2, 3], 600), (2, [0, 1, 2], 1800)], env.restarts
    assert env.pushes == [([0, 1, 2], 3000)], env.pushes
    assert env.all_complete == [3120], env.all_complete


def test_first_completion_then_stall_restarts_pending_only():
    """场景C：首个完成后卡死 → 第1轮只重启未完成窗口。"""
    env = Env([0, 1, 2, 3], poll_s=60, fallback_s=600, round1_s=600,
              round2_s=1200,
              done_at={0: 60, 1: 700, 2: 3120, 3: 3180})
    env.run()
    # t=60 acc0 完成（锚定第一轮，deadline=660）；t=660 超时 → 第1轮重启 [1,2,3]
    assert env.restarts[0] == (1, [1, 2, 3], 660), env.restarts
    # 第1轮重启在 t=660，第二轮窗口 deadline=660+1200=1860
    assert env.restarts[1] == (2, [2, 3], 1860), env.restarts
    # 末尾轮 deadline=1860+1200=3060 → 推送 [2,3]
    assert env.pushes == [([2, 3], 3060)], env.pushes
    assert env.all_complete == [3180], env.all_complete


def test_round1_wait_is_not_refreshed_by_later_completions():
    """场景G（核心语义）：第一轮等待期间的新完成不刷新计时。"""
    env = Env([0, 1, 2, 3], poll_s=60, fallback_s=600, round1_s=600,
              round2_s=1200, done_at={0: 60, 1: 120, 2: 540, 3: 1260})
    env.run()
    # t=60 acc0 完成 → deadline=660。acc2 在 t=540 完成【不得】把 deadline 推到 1140。
    assert env.restarts == [(1, [3], 660)], env.restarts
    assert env.all_complete == [1260], env.all_complete


def test_stop_signal_exits_cleanly():
    """场景D：停止信号 → 立即退出，无重启无推送无完成回调。"""
    env = Env([0, 1], poll_s=60, fallback_s=600, round1_s=600, round2_s=1200,
              done_at={0: 60}, stop_after=3)
    env.run()
    assert env.all_complete == [], env.all_complete
    assert env.restarts == [], env.restarts
    assert env.pushes == [], env.pushes


def test_manually_closed_windows_stop_tracking():
    """场景E：手动关闭（gone）→ 停止跟踪；全部 gone 也走完成回调（沿用现有语义）。"""
    env = Env([0, 1], poll_s=60, fallback_s=600, round1_s=600, round2_s=1200,
              gone_at={0: 120, 1: 120})
    env.run()
    assert env.all_complete == [120], env.all_complete
    assert env.restarts == [], env.restarts


def test_single_account_completes_during_fallback():
    """场景F：单号批次，兜底期内完成 → 直接完成回调。"""
    env = Env([0], poll_s=60, fallback_s=600, round1_s=600, round2_s=1200,
              done_at={0: 60})
    env.run()
    assert env.all_complete == [60], env.all_complete
    assert env.restarts == [] and env.pushes == []


def main():
    tests = [
        test_trickle_completes_without_restart,
        test_stuck_from_start_full_max_flow,
        test_first_completion_then_stall_restarts_pending_only,
        test_round1_wait_is_not_refreshed_by_later_completions,
        test_stop_signal_exits_cleanly,
        test_manually_closed_windows_stop_tracking,
        test_single_account_completes_during_fallback,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
