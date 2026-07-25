"""动作节点 — 执行键鼠动作序列。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("action")
class ActionNode(NodeBase):
    """动作节点：执行一组键鼠动作。

    YAML 配置示例：
        step_navigate:
          type: action
          actions:
            - click_target: "daily_quest_button"
              fallback_pos: [900, 500]
            - wait: 1000
            - click: [1200, 700]
          next: "step_battle"
    """

    @property
    def node_type(self) -> str:
        return "action"

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """执行动作序列。"""
        actions: List[Dict[str, Any]] = self._config.get("actions", [])

        if not actions:
            logger.warning("动作节点 %s 没有定义 actions", self._step_name)
            return NodeResult.SUCCESS, self._config.get("next")

        # 获取动作执行器（从 context 的引擎引用中获取）
        action_executor = getattr(context, "_action_executor", None)
        if action_executor is None:
            # 回退：尝试从全局获取
            logger.error("动作执行器不可用")
            return NodeResult.FAILED, self._config.get("on_error")

        success = action_executor.execute_actions(actions, context)

        if success:
            return NodeResult.SUCCESS, self._config.get("next")
        else:
            return NodeResult.FAILED, self._config.get("on_error", self._config.get("next"))
