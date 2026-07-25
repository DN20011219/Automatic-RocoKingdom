"""终止节点 — 结束工作流。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("terminate")
class TerminateNode(NodeBase):
    """终止节点：结束工作流执行。

    YAML 配置示例：
        step_end:
          type: terminate
          message: "每日任务完成"
    """

    @property
    def node_type(self) -> str:
        return "terminate"

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """终止工作流。"""
        message = self._config.get("message", "工作流结束")
        logger.info("工作流终止: %s", message)
        return NodeResult.TERMINATE, None
