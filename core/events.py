"""事件总线 — 模块间解耦通信。

支持同步事件分发，所有监听器按注册顺序调用。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

# 事件类型常量
EVENT_FRAME_PROCESSED = "on_frame_processed"
EVENT_TARGET_FOUND = "on_target_found"
EVENT_TARGET_LOST = "on_target_lost"
EVENT_SCENE_CHANGED = "on_scene_changed"
EVENT_ACTION_EXECUTED = "on_action_executed"
EVENT_WORKFLOW_COMPLETE = "on_workflow_complete"
EVENT_WORKFLOW_STARTED = "on_workflow_started"
EVENT_STEP_CHANGED = "on_step_changed"
EVENT_ERROR = "on_error"
EVENT_PAUSED = "on_paused"
EVENT_RESUMED = "on_resumed"
EVENT_STOPPED = "on_stopped"


class EventBus:
    """简单同步事件总线。

    用法：
        bus = EventBus()
        bus.subscribe("on_target_found", my_handler)
        bus.emit("on_target_found", target_name="hello", detection=det)
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable[..., Any]]] = defaultdict(list)

    def subscribe(self, event: str, handler: Callable[..., Any]) -> None:
        """注册事件监听器。"""
        self._handlers[event].append(handler)
        logger.debug("事件监听器已注册: %s -> %s", event, handler.__name__)

    def unsubscribe(self, event: str, handler: Callable[..., Any]) -> None:
        """移除事件监听器。"""
        if event in self._handlers:
            self._handlers[event] = [
                h for h in self._handlers[event] if h is not handler
            ]

    def emit(self, event: str, **kwargs: Any) -> None:
        """触发事件，按注册顺序调用所有监听器。"""
        handlers = self._handlers.get(event, [])
        for handler in handlers:
            try:
                handler(**kwargs)
            except Exception as e:
                logger.error(
                    "事件处理器异常: event=%s, handler=%s, error=%s",
                    event, handler.__name__, e,
                )

    def clear(self) -> None:
        """清除所有监听器。"""
        self._handlers.clear()

    def clear_event(self, event: str) -> None:
        """清除指定事件的所有监听器。"""
        self._handlers.pop(event, None)
