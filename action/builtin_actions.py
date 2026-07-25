"""内置高级动作 — 扩展工作流中可用的动作类型。

这些动作在标准 Clicker 动作之上，结合视觉识别结果执行更智能的操作。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.context import Detection, RuntimeContext

logger = logging.getLogger(__name__)


class BuiltinActions:
    """内置高级动作集合。

    提供比原始 click/key/move 更高级的动作语义：
    - click_target: 点击视觉目标
    - move_to_target: 移动到视觉目标
    - wait_for_target: 等待目标出现
    - drag_target: 拖拽目标（预留）
    """

    @staticmethod
    def resolve_click_target(
        action: Dict[str, Any],
        context: RuntimeContext,
    ) -> Optional[Dict[str, Any]]:
        """将 click_target 动作解析为标准 click 动作。

        Returns:
            标准 click 动作 dict，或 None（目标未找到且无 fallback）。
        """
        target_name = action["click_target"]
        min_score = action.get("min_score", 0.0)
        fallback_pos = action.get("fallback_pos", None)
        hold_ms = action.get("hold_ms", 100)
        offset_x = action.get("offset_x", 0)
        offset_y = action.get("offset_y", 0)

        detection = context.find_detection(target_name, min_score)

        if detection:
            return {
                "type": "click",
                "x": detection.cx + offset_x,
                "y": detection.cy + offset_y,
                "hold_ms": hold_ms,
            }
        elif fallback_pos:
            return {
                "type": "click",
                "x": fallback_pos[0],
                "y": fallback_pos[1],
                "hold_ms": hold_ms,
            }
        return None

    @staticmethod
    def resolve_move_to_target(
        action: Dict[str, Any],
        context: RuntimeContext,
    ) -> Optional[Dict[str, Any]]:
        """将 move_to_target 动作解析为标准 move 动作。"""
        target_name = action["move_to_target"]
        min_score = action.get("min_score", 0.0)
        fallback_pos = action.get("fallback_pos", None)
        duration_ms = action.get("duration_ms", 100)
        offset_x = action.get("offset_x", 0)
        offset_y = action.get("offset_y", 0)

        detection = context.find_detection(target_name, min_score)

        if detection:
            return {
                "type": "move",
                "x": detection.cx + offset_x,
                "y": detection.cy + offset_y,
                "duration_ms": duration_ms,
            }
        elif fallback_pos:
            return {
                "type": "move",
                "x": fallback_pos[0],
                "y": fallback_pos[1],
                "duration_ms": duration_ms,
            }
        return None
