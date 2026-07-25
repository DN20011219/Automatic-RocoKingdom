"""视觉条件 — 目标出现/消失/数量判断。"""

from __future__ import annotations

from typing import Any, Dict

from conditions.base import ConditionBase, ConditionRegistry


@ConditionRegistry.register("visual")
class VisualCondition(ConditionBase):
    """视觉条件：判断视觉目标是否存在。

    配置示例：
        type: visual
        target: "victory_label"
        min_score: 0.85
        mode: found        # found | absent | count_gte | count_lte
        count: 3           # mode=count_gte/count_lte 时使用
    """

    def evaluate(self, context: Any) -> bool:
        target = self._config.get("target", "")
        min_score = self._config.get("min_score", 0.0)
        mode = self._config.get("mode", "found")

        if not target:
            return False

        detections = context.find_detections(target, min_score)

        if mode == "found":
            return len(detections) > 0
        elif mode == "absent":
            return len(detections) == 0
        elif mode == "count_gte":
            count = self._config.get("count", 1)
            return len(detections) >= count
        elif mode == "count_lte":
            count = self._config.get("count", 1)
            return len(detections) <= count
        else:
            return len(detections) > 0
