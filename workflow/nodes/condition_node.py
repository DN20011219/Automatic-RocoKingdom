"""条件分支节点 — 根据条件判断跳转到不同步骤。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("condition")
class ConditionNode(NodeBase):
    """条件分支节点：评估一组条件，根据结果跳转。

    YAML 配置示例：
        step_check:
          type: condition
          conditions:
            - type: counter
              counter: "quest_done_count"
              operator: ">="
              value: 5
          on_true: "step_end"
          on_false: "step_start"
    """

    @property
    def node_type(self) -> str:
        return "condition"

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """评估条件并决定跳转。"""
        from conditions.base import ConditionRegistry

        conditions_config: List[Dict[str, Any]] = self._config.get("conditions", [])
        logic = self._config.get("logic", "AND")  # AND | OR

        if not conditions_config:
            logger.warning("条件节点 %s 没有定义 conditions", self._step_name)
            return NodeResult.SUCCESS, self._config.get("on_true", self._config.get("next"))

        # 评估所有条件
        results: List[bool] = []
        for cond_cfg in conditions_config:
            cond_type = cond_cfg.get("type", "")
            try:
                condition = ConditionRegistry.create(cond_type, cond_cfg)
                result = condition.evaluate(context)
                results.append(result)
            except Exception as e:
                logger.error("条件评估失败: type=%s, error=%s", cond_type, e)
                results.append(False)

        # 根据逻辑运算符合并结果
        if logic.upper() == "OR":
            final_result = any(results)
        else:  # AND
            final_result = all(results)

        if final_result:
            return NodeResult.SUCCESS, self._config.get("on_true", self._config.get("next"))
        else:
            return NodeResult.SUCCESS, self._config.get("on_false", self._config.get("next"))
