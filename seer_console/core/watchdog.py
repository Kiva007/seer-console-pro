"""任务完成卡死看门狗：任务完成检查阶段（阶段2）的固定窗口状态机。

设计约定（与用户逐条确认）：
- 所有等待均为固定窗口，不随任务完成事件刷新：
    兜底等待   进入即计时；到期无任何完成 → 第1轮重启（任务未正常进入）
    第一轮等待 锚定「首个完成」；到期仍有未完成 → 第1轮重启（其余窗口没跟上）
    第二轮等待 锚定「第1轮重启完成」；到期仍有未完成 → 第2轮重启（给从头跑的任务留足时间）
    末尾轮检测 锚定「第2轮重启完成」；到期仍有未完成 → 推送微信提醒，此后只监测
- 重启只针对当时仍未完成的窗口；微信推送每批次只发一次。
- 任意时刻 pending 清空（含推送后监测期）→ all_complete()（完成通知+关闭全部窗口）。
- 窗口被手动关闭（probe 返回 "gone"）→ 停止跟踪（沿用既有语义，不计入推送清单）。

依赖全部注入（probe/restart_round/push_unfinished/all_complete/log/sleep_wait/clock），
便于假时钟模拟测试（tests/test_stall_watchdog.py）。
"""

import time

# probe(i) 返回值
GONE = "gone"    # 窗口已关闭 → 停止跟踪
DONE = "done"    # 任务完成 → 移出待完成
WAIT = "wait"    # 仍在执行，继续等

# 内部阶段
_FALLBACK = "兜底等待"
_ROUND1 = "第一轮等待"
_ROUND2 = "第二轮等待"
_FINAL = "末尾轮检测"
_MONITOR = "推送后监测"


class StallWatchdog:
    """固定窗口看门狗。构造参数：

    poll_s          轮询间隔（秒）——探测/到期判定的粒度
    fallback_s      兜底等待时长：进入 run() 即计时，到期无任何完成 → 第1轮重启
    round1_s        第一轮等待时长：锚定首个完成，到期仍有未完成 → 第1轮重启
    round2_s        第二轮等待/末尾轮时长：锚定对应重启完成时刻
    probe(i)        -> GONE / DONE / WAIT（异常由调用方包装为 WAIT）
    restart_round(round_no, pending) -> bool  # False = 收到停止信号，看门狗退出
    push_unfinished(pending)          # 末尾轮到期：推送未完成账号（微信）
    all_complete()                    # 全部完成：完成通知 + 关闭全部窗口
    log(msg)                          # 日志
    sleep_wait(s) -> bool             # Event.wait 语义：True = 停止信号
    clock() -> float                  # 可注入假时钟，默认 time.time
    account_seq(i)                    # 日志序号映射，默认使用下标 + 1
    """

    def __init__(self, *, poll_s, fallback_s, round1_s, round2_s,
                 probe, restart_round, push_unfinished, all_complete,
                 log, sleep_wait, clock=time.time, account_seq=None):
        self.poll_s = poll_s
        self.fallback_s = fallback_s
        self.round1_s = round1_s
        self.round2_s = round2_s
        self.probe = probe
        self.restart_round = restart_round
        self.push_unfinished = push_unfinished
        self.all_complete = all_complete
        self.log = log
        self.sleep_wait = sleep_wait
        self.clock = clock
        self.account_seq = account_seq if account_seq is not None else lambda i: i + 1

    def run(self, active):
        pending = set(active)
        phase = _FALLBACK
        deadline = self.clock() + self.fallback_s
        self.log(f"进入任务完成检查（兜底{self._min(self.fallback_s)}/"
                 f"轮1 {self._min(self.round1_s)}/"
                 f"轮2 {self._min(self.round2_s)}分钟，固定窗口不刷新）")
        while True:
            if self.sleep_wait(self.poll_s):
                self.log("已收到停止信号，退出")
                return
            now = self.clock()

            for i in sorted(pending):
                state = self.probe(i)
                if state == GONE:
                    self.log(f"序号[{self.account_seq(i)}]窗口已关闭，停止跟踪")
                    pending.discard(i)
                elif state == DONE:
                    self.log(f"序号[{self.account_seq(i)}]任务完成")
                    pending.discard(i)
                    if phase == _FALLBACK:
                        # 首个完成 → 锚定第一轮等待（此后新完成不再刷新）
                        phase = _ROUND1
                        deadline = now + self.round1_s
                        self.log(f"首个任务完成，{self._min(self.round1_s)}分钟内"
                                 f"未全部完成将执行第1轮重启")

            if not pending:
                self.all_complete()
                return
            if phase == _MONITOR:
                continue  # 已推送：只监测完成，不再重启
            if now < deadline:
                continue

            # 固定窗口到期
            if phase == _FALLBACK:
                self.log(f"兜底{self._min(self.fallback_s)}分钟内无任何任务完成，"
                         f"执行第1轮重启")
                if not self._do_restart(1, pending):
                    return
                phase = _ROUND2
            elif phase == _ROUND1:
                self.log(f"第一轮等待{self._min(self.round1_s)}分钟超时，"
                         f"仍有{len(pending)}个未完成，执行第1轮重启")
                if not self._do_restart(1, pending):
                    return
                phase = _ROUND2
            elif phase == _ROUND2:
                self.log(f"第二轮等待{self._min(self.round2_s)}分钟超时，"
                         f"仍有{len(pending)}个未完成，执行第2轮重启")
                if not self._do_restart(2, pending):
                    return
                phase = _FINAL
            else:  # _FINAL
                self.log(f"末尾轮{self._min(self.round2_s)}分钟检测超时，"
                         f"推送未完成账号（此后只监测不再重启）")
                self.push_unfinished(sorted(pending))
                phase = _MONITOR

            if phase in (_ROUND2, _FINAL):
                # 重启刚完成：以完成时刻锚定下一窗口
                deadline = self.clock() + self.round2_s

    def _do_restart(self, round_no, pending):
        ok = self.restart_round(round_no, sorted(pending))
        if not ok:
            self.log("已收到停止信号，不再重启")
        return ok

    @staticmethod
    def _min(seconds):
        m = seconds / 60
        return int(m) if m == int(m) else round(m, 1)
