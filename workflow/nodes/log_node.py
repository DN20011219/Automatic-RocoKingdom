"""日志节点 — 输出调试信息。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("log")
class LogNode(NodeBase):
    """日志节点：输出调试/状态信息。

    YAML 配置示例：
        step_log:
          type: log
          message: "进入战斗阶段"
          level: info          # debug | info | warning | error
          next: "step_battle"
    """

    @property
    def node_type(self) -> str:
        return "log"

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """输出日志。"""
        message = self._config.get("message", "")
        level = self._config.get("level", "info").lower()

        # 支持变量替换
        message = self._resolve_message(message, context)

        if level == "debug":
            logger.debug("[workflow] %s", message)
        elif level == "warning":
            logger.warning("[workflow] %s", message)
        elif level == "error":
            logger.error("[workflow] %s", message)
        else:
            logger.info("[workflow] %s", message)

        return NodeResult.SUCCESS, self._config.get("next")

    def _resolve_message(self, message: str, context: Any) -> str:
        """解析消息中的变量引用。"""
        import re
        pattern = r"\$\{(\w+)\}"

        def replacer(match):
            var_name = match.group(1)
            value = context.get_variable(var_name)
            if value is not None:
                return str(value)
            # 也尝试计数器
            counter_val = context.get_counter(var_name)
            if counter_val != 0:
                return str(counter_val)
            return match.group(0)

        return re.sub(pattern, replacer, message)
