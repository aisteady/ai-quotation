"""
线上 Harness（harness.py）
==========================

学习要点
--------
什么是 Harness？
  包在「业务图 / Agent」外面的一层：**守卫 + 审计 + 可选后台循环**。
  业务逻辑仍在 LangGraph；Harness 负责「别跑飞、留痕迹、定时扫积压」。

本文件两类东西：
1. QuotationHarness — 单次 start/resume 的包装与规则
2. HarnessLoop     — 线上定时扫描 pending_human（产品级 loop，不是 Cursor /loop）

图内还有「clarify Loop」：信息不足 → 追问 → 再解析，见 graph/nodes.py。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from config import settings
from store import QuotationStore

logger = logging.getLogger(__name__)


class QuotationHarness:
    """运行时护栏与事件埋点。"""

    def __init__(self, store: QuotationStore) -> None:
        self.store = store
        # 与图内 clarify 共用同一上限
        self.max_clarify_loops = settings.max_clarify_loops

    def guard_clarify_round(self, clarify_round: int) -> tuple[bool, str]:
        """
        追问轮数硬顶。

        返回 (是否允许继续, 错误说明)。
        超过上限应转人工线下处理，避免无限追问。
        """
        if clarify_round > self.max_clarify_loops:
            return (
                False,
                f"已超过最大补全轮数 ({self.max_clarify_loops})，请转人工或重新发起询价。",
            )
        return True, ""

    def assert_no_auto_price(self, unit_price: float | None) -> None:
        """保留接口：本产品主交付是配置方案；正式商务价仍须人审，禁止自动落价。"""
        if unit_price is not None:
            raise RuntimeError("禁止自动写入正式单价；请走配置人审与商务流程")

    def emit(
        self, event_type: str, *, inquiry_id: str | None = None, **payload: Any
    ) -> None:
        """写 harness_events；失败只打日志，不打断主流程。"""
        try:
            self.store.log_harness_event(
                event_type, inquiry_id=inquiry_id, payload=payload
            )
        except Exception as exc:
            logger.warning("harness 事件写入失败: %s", exc)

    def wrap_run(
        self, name: str, fn: Callable[[], Any], *, inquiry_id: str | None = None
    ) -> Any:
        """
        包装一次业务调用：记 start → 执行 → 记 ok/error + 耗时。

        service.start_inquiry / resume_* 都走这里，便于事后复盘。
        """
        self.emit("run_start", inquiry_id=inquiry_id, name=name)
        t0 = time.time()
        try:
            result = fn()
            self.emit(
                "run_ok",
                inquiry_id=inquiry_id,
                name=name,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
            return result
        except Exception as exc:
            self.emit(
                "run_error",
                inquiry_id=inquiry_id,
                name=name,
                error=str(exc),
                elapsed_ms=int((time.time() - t0) * 1000),
            )
            raise


class HarnessLoop:
    """
    线上产品级后台 Loop。

    每隔 HARNESS_LOOP_INTERVAL_SEC 秒：
      查询 status=pending_human 的询价 → 打日志 / 回调 on_pending

    用途示例：积压告警、推送到企业微信、刷新监控指标。
    interval<=0 时不启动（默认开发可关）。
    """

    def __init__(
        self,
        store: QuotationStore,
        harness: QuotationHarness,
        *,
        on_pending: Callable[[list], None] | None = None,
    ) -> None:
        self.store = store
        self.harness = harness
        self.on_pending = on_pending
        # Event：主线程 stop() 时置位，工作线程 wait 醒来退出
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """启动 daemon 线程；已在跑则直接返回 True。"""
        interval = settings.harness_loop_interval_sec
        if interval <= 0:
            logger.info("HarnessLoop 未启用（HARNESS_LOOP_INTERVAL_SEC=0）")
            return False
        if self._thread and self._thread.is_alive():
            return True

        def _run() -> None:
            self.harness.emit("loop_start", interval_sec=interval)
            # wait(interval) 返回 True 表示被 stop 唤醒；False 表示超时→该 tick 了
            while not self._stop.wait(interval):
                try:
                    pending = self.store.list_pending_human(limit=100)
                    self.harness.emit("loop_tick", pending_count=len(pending))
                    if self.on_pending:
                        self.on_pending(pending)
                    else:
                        logger.info("HarnessLoop: 待人审询价 %s 单", len(pending))
                except Exception as exc:
                    logger.exception("HarnessLoop tick 失败: %s", exc)
                    self.harness.emit("loop_error", error=str(exc))

        self._stop.clear()
        # daemon=True：主进程退出时不阻塞（Streamlit 热重载友好）
        self._thread = threading.Thread(
            target=_run, name="quotation-harness-loop", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """请求停止并短暂 join。"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.harness.emit("loop_stop")
