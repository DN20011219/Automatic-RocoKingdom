"""动作序列执行器 — 在工作流层面编排动作执行，支持高级动作（click_target 等）。"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from core.context import Detection, RuntimeContext
from core.events import EventBus, EVENT_ACTION_EXECUTED

logger = logging.getLogger(__name__)


class ActionExecutor:
    """工作流动作执行器。

    在 ClickerAdapter 基础上增加：
    - click_target: 点击视觉目标（从 context.last_detections 获取坐标）
    - 动作执行事件通知
    - 与生命周期管理集成

    用法：
        executor = ActionExecutor(config, event_bus)
        executor.setup()
        executor.execute_actions(actions_list, context)
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self._config = config or {}
        self._event_bus = event_bus
        self._adapter = None  # ClickerAdapter
        self._initialized = False

    def setup(self) -> None:
        """初始化 Clicker 适配器。"""
        from action.adapter import ClickerAdapter

        self._adapter = ClickerAdapter(self._config)
        self._adapter.setup()
        self._initialized = True

    @property
    def is_available(self) -> bool:
        return self._initialized and self._adapter is not None

    def execute_actions(
        self,
        actions: List[Dict[str, Any]],
        context: RuntimeContext,
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> bool:
        """执行动作列表（支持高级动作类型）。

        高级动作类型：
        - click_target: 点击视觉目标
        - click: 直接坐标点击
        - key: 按键
        - wait: 等待
        - move: 移动
        - combo: 组合键
        """
        if not self._initialized:
            logger.error("动作执行器未初始化")
            return False

        for action in actions:
            if stop_event and stop_event.is_set():
                return False

            # 动作级重试（spec 9.2）
            max_retries = action.get("retry", 0) if isinstance(action, dict) else 0
            retry_delay_ms = action.get("retry_delay_ms", 500) if isinstance(action, dict) else 500
            success = False

            for attempt in range(max_retries + 1):
                success = self._execute_single_action(action, context)
                if success:
                    break
                if attempt < max_retries:
                    logger.debug("动作重试 (%d/%d): %s", attempt + 1, max_retries, action)
                    import time
                    time.sleep(retry_delay_ms / 1000.0)

            if not success:
                logger.warning("动作执行失败（已重试 %d 次）: %s", max_retries, action)
                # 非致命错误，继续执行后续动作

            # 触发事件
            if self._event_bus:
                self._event_bus.emit(
                    EVENT_ACTION_EXECUTED,
                    action=action,
                    context=context,
                )

        return True

    def _execute_single_action(
        self,
        action: Dict[str, Any],
        context: RuntimeContext,
    ) -> bool:
        """执行单个动作。"""
        # 高级动作：click_target
        if "click_target" in action:
            return self._execute_click_target(action, context)

        # 高级动作：click 简写格式 [x, y]
        if "click" in action and isinstance(action["click"], (list, tuple)):
            x, y = action["click"]
            hold_ms = action.get("hold_ms", 100)
            return self._adapter.click(x, y, hold_ms)

        # 高级动作：key 简写格式
        if "key" in action and isinstance(action["key"], int):
            hold_ms = action.get("hold_ms", 50)
            return self._adapter.press_key(action["key"], hold_ms)

        # 高级动作：wait 简写格式
        if "wait" in action and isinstance(action["wait"], (int, float)):
            return self._adapter.wait(int(action["wait"]))

        # 标准 Clicker 动作格式
        if "type" in action:
            return self._adapter.execute_action(action)

        logger.warning("无法识别的动作格式: %s", action)
        return False

    def _execute_click_target(
        self,
        action: Dict[str, Any],
        context: RuntimeContext,
    ) -> bool:
        """执行 click_target 动作：点击视觉识别到的目标。"""
        target_name = action["click_target"]
        min_score = action.get("min_score", 0.0)
        fallback_pos = action.get("fallback_pos", None)
        hold_ms = action.get("hold_ms", 100)
        offset_x = action.get("offset_x", 0)
        offset_y = action.get("offset_y", 0)

        # 从上下文中查找目标
        detection = context.find_detection(target_name, min_score)

        if detection:
            # 点击目标中心 + 偏移
            x = detection.cx + offset_x
            y = detection.cy + offset_y
            logger.debug("click_target: %s -> (%d, %d) score=%.2f",
                         target_name, x, y, detection.score)
            return self._adapter.click(x, y, hold_ms)
        elif fallback_pos:
            # 使用备选坐标
            x, y = fallback_pos
            logger.debug("click_target: %s 未找到，使用备选坐标 (%d, %d)",
                         target_name, x, y)
            return self._adapter.click(x, y, hold_ms)
        else:
            logger.warning("click_target: 目标 %s 未找到且无备选坐标", target_name)
            return False

    def cleanup(self) -> None:
        """清理资源。"""
        if self._adapter:
            self._adapter.cleanup()
        self._initialized = False
