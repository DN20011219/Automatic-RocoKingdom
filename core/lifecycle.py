"""生命周期管理 — 启动/暂停/停止/恢复状态控制。"""

from __future__ import annotations

import logging
import threading
from enum import Enum, auto
from typing import Optional

from core.events import EventBus, EVENT_PAUSED, EVENT_RESUMED, EVENT_STOPPED

logger = logging.getLogger(__name__)


class EngineState(Enum):
    """引擎运行状态。"""

    IDLE = auto()       # 未启动
    RUNNING = auto()    # 运行中
    PAUSED = auto()     # 已暂停
    STOPPED = auto()    # 已停止
    ERROR = auto()      # 异常


class LifecycleManager:
    """管理引擎的生命周期状态转换。

    状态转换图：
        IDLE -> RUNNING -> PAUSED -> RUNNING -> STOPPED
                              \\-> ERROR -> (retry) -> RUNNING
    """

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        self._state = EngineState.IDLE
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始为非暂停状态
        self._stop_event = threading.Event()
        self._event_bus = event_bus
        self._error_message: Optional[str] = None

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == EngineState.RUNNING

    @property
    def is_paused(self) -> bool:
        return self._state == EngineState.PAUSED

    @property
    def is_stopped(self) -> bool:
        return self._state in (EngineState.STOPPED, EngineState.IDLE)

    @property
    def error_message(self) -> Optional[str]:
        return self._error_message

    def start(self) -> bool:
        """从 IDLE/STOPPED 状态启动。"""
        with self._lock:
            if self._state not in (EngineState.IDLE, EngineState.STOPPED):
                logger.warning("无法启动：当前状态为 %s", self._state.name)
                return False
            self._state = EngineState.RUNNING
            self._stop_event.clear()
            self._pause_event.set()
            self._error_message = None
            logger.info("引擎已启动")
            return True

    def pause(self) -> bool:
        """暂停引擎。"""
        with self._lock:
            if self._state != EngineState.RUNNING:
                return False
            self._state = EngineState.PAUSED
            self._pause_event.clear()
            logger.info("引擎已暂停")
            if self._event_bus:
                self._event_bus.emit(EVENT_PAUSED)
            return True

    def resume(self) -> bool:
        """恢复引擎。"""
        with self._lock:
            if self._state != EngineState.PAUSED:
                return False
            self._state = EngineState.RUNNING
            self._pause_event.set()
            logger.info("引擎已恢复")
            if self._event_bus:
                self._event_bus.emit(EVENT_RESUMED)
            return True

    def stop(self) -> bool:
        """停止引擎。"""
        with self._lock:
            if self._state in (EngineState.IDLE, EngineState.STOPPED):
                return False
            self._state = EngineState.STOPPED
            self._stop_event.set()
            self._pause_event.set()  # 确保暂停中的线程能退出
            logger.info("引擎已停止")
            if self._event_bus:
                self._event_bus.emit(EVENT_STOPPED)
            return True

    def set_error(self, message: str) -> None:
        """标记错误状态。"""
        with self._lock:
            self._state = EngineState.ERROR
            self._error_message = message
            logger.error("引擎错误: %s", message)

    def wait_if_paused(self, poll_interval: float = 0.1) -> bool:
        """如果处于暂停状态则阻塞等待，返回 False 表示已停止。"""
        while self._state == EngineState.PAUSED:
            if self._stop_event.is_set():
                return False
            self._pause_event.wait(timeout=poll_interval)
        return self._state == EngineState.RUNNING

    def should_continue(self) -> bool:
        """检查是否应继续执行（未停止且未出错）。"""
        return self._state in (EngineState.RUNNING, EngineState.PAUSED)

    def interruptible_sleep(self, duration: float, poll_interval: float = 0.05) -> bool:
        """支持暂停/停止中断的睡眠。返回 True 表示正常完成，False 表示被中断。"""
        import time
        deadline = time.time() + duration
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if self._state == EngineState.PAUSED:
                if not self.wait_if_paused(poll_interval):
                    return False
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        return not self._stop_event.is_set()
