"""感知节点 — 等待视觉目标出现或消失。"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("perceive")
class PerceiveNode(NodeBase):
    """感知节点：等待指定视觉目标出现或消失。

    YAML 配置示例：
        step_start:
          type: perceive
          target: "main_menu_label"
          min_score: 0.85
          mode: found          # found | absent
          timeout_ms: 10000
          on_found: "step_navigate"
          on_timeout: "step_retry"
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._start_time: Optional[float] = None

    @property
    def node_type(self) -> str:
        return "perceive"

    def on_enter(self, context: Any) -> None:
        self._start_time = time.time()

    def reset(self) -> None:
        self._start_time = None

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """检查目标是否满足条件。"""
        target = context.resolve_value(self._config.get("target", ""))
        min_score = context.resolve_value(self._config.get("min_score", 0.0))
        mode = self._config.get("mode", "found")  # found | absent
        timeout_ms = context.resolve_value(self._config.get("timeout_ms", 0))

        # 变量解析后的类型兜底
        try:
            min_score = float(min_score)
        except (TypeError, ValueError):
            min_score = 0.0
        try:
            timeout_ms = int(timeout_ms)
        except (TypeError, ValueError):
            timeout_ms = 0

        if not target:
            logger.warning("感知节点 %s 没有定义 target", self._step_name)
            return NodeResult.SUCCESS, self._config.get("next")

        # 初始化计时
        if self._start_time is None:
            self._start_time = time.time()

        # 检查目标状态
        detection = context.find_detection(target, min_score)

        if mode == "found":
            if detection:
                logger.debug("感知节点 %s: 目标 %s 已找到 (score=%.2f)",
                             self._step_name, target, detection.score)
                return NodeResult.SUCCESS, self._config.get("on_found", self._config.get("next"))
        elif mode == "absent":
            if not detection:
                logger.debug("感知节点 %s: 目标 %s 已消失", self._step_name, target)
                return NodeResult.SUCCESS, self._config.get("on_absent", self._config.get("next"))

        # 超时检查
        if timeout_ms > 0:
            elapsed_ms = (time.time() - self._start_time) * 1000
            if elapsed_ms >= timeout_ms:
                logger.debug("感知节点 %s: 等待超时 (%dms)", self._step_name, timeout_ms)
                return NodeResult.SUCCESS, self._config.get("on_timeout", self._config.get("next"))

        # 仍在等待
        return NodeResult.RUNNING, None
