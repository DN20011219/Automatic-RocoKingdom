"""时间条件 — 超时/定时判断。"""

from __future__ import annotations

from typing import Any, Dict

from conditions.base import ConditionBase, ConditionRegistry


@ConditionRegistry.register("time")
class TimeCondition(ConditionBase):
    """时间条件：判断是否经过/剩余指定时间。

    配置示例：
        type: time
        duration_sec: 300
        mode: elapsed      # elapsed | remaining
    """

    def evaluate(self, context: Any) -> bool:
        duration_sec = self._config.get("duration_sec", 0)
        mode = self._config.get("mode", "elapsed")

        # 支持变量引用
        duration_sec = context.resolve_value(duration_sec)
        if isinstance(duration_sec, str):
            try:
                duration_sec = float(duration_sec)
            except ValueError:
                return False

        elapsed = context.elapsed_seconds()

        if mode == "elapsed":
            # 已经过的时间 >= 指定时间
            return elapsed >= duration_sec
        elif mode == "remaining":
            # 剩余时间 <= 0（即已超时）
            return elapsed >= duration_sec
        else:
            return elapsed >= duration_sec
