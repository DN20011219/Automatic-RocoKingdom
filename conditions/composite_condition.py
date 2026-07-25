"""组合条件 — AND/OR/NOT 逻辑组合。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from conditions.base import ConditionBase, ConditionRegistry

logger = logging.getLogger(__name__)


@ConditionRegistry.register("composite")
class CompositeCondition(ConditionBase):
    """组合条件：将多个子条件用逻辑运算符组合。

    配置示例：
        type: composite
        operator: AND       # AND | OR | NOT
        children:
          - type: visual
            target: "victory_label"
            min_score: 0.85
          - type: counter
            counter: "battle_count"
            operator: ">="
            value: 3
    """

    def evaluate(self, context: Any) -> bool:
        op = self._config.get("operator", "AND").upper()
        children_cfg: List[Dict[str, Any]] = self._config.get("children", [])

        if not children_cfg:
            return True

        # 评估所有子条件
        results: List[bool] = []
        for child_cfg in children_cfg:
            child_type = child_cfg.get("type", "")
            try:
                child_condition = ConditionRegistry.create(child_type, child_cfg)
                results.append(child_condition.evaluate(context))
            except Exception as e:
                logger.error("子条件评估失败: type=%s, error=%s", child_type, e)
                results.append(False)

        # 应用逻辑运算符
        if op == "AND":
            return all(results)
        elif op == "OR":
            return any(results)
        elif op == "NOT":
            # NOT 只取第一个子条件的结果取反
            return not results[0] if results else True
        else:
            return all(results)


@ConditionRegistry.register("scene")
class SceneCondition(ConditionBase):
    """场景条件：判断当前是否处于指定场景。

    配置示例：
        type: scene
        scene_name: "battle_field"
    """

    def evaluate(self, context: Any) -> bool:
        scene_name = self._config.get("scene_name", "")
        return context.current_scene == scene_name
