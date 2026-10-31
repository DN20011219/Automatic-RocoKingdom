"""协议条件 — 基于 RKPP 协议信息源的事件判断。"""

from __future__ import annotations

import time
from typing import Any

from conditions.base import ConditionBase, ConditionRegistry
from conditions.counter_condition import OPERATORS


def _extract_field(obj: Any, path: str) -> Any:
    """按点分路径从嵌套字典中提取字段，如 "content.primary_skill.skill_id"。"""
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


@ConditionRegistry.register("protocol")
class ProtocolCondition(ConditionBase):
    """协议条件：判断某个战斗协议事件是否已发生（依赖 RKPP 信息源）。

    事件匹配顺序：
    1. 精确匹配 summary_kind / opcode_name / opencode
    2. 模糊匹配 meaning 包含关键词（忽略大小写）

    配置示例：
        type: protocol
        event: "battle_end"          # 事件标识（见上）
        within_sec: 30               # 可选：事件须在最近 N 秒内收到
        field: "content.damage"      # 可选：提取事件字段进一步比较
        operator: ">="               # 与 field/value 搭配
        value: 100
    """

    def evaluate(self, context: Any) -> bool:
        kind = str(self._config.get("event", "")).strip()
        if not kind:
            return False

        event = context.find_protocol_event(kind)
        if event is None:
            return False

        # 时效性检查
        within_sec = self._config.get("within_sec")
        if within_sec is not None:
            age = time.time() - event.get("_received_at", 0.0)
            if age > float(within_sec):
                return False

        # 字段比较（可选）
        field = self._config.get("field")
        if field is not None:
            expected = context.resolve_value(self._config.get("value"))
            op_str = self._config.get("operator", "==")
            op_func = OPERATORS.get(str(op_str))
            if op_func is None:
                return False

            actual = _extract_field(event, str(field))
            if actual is None:
                return False

            try:
                return bool(op_func(actual, expected))
            except TypeError:
                return False

        return True
