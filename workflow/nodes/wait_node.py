"""等待节点 — 纯等待（固定/随机时长）。"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("wait")
class WaitNode(NodeBase):
    """等待节点：等待指定时长后继续。

    YAML 配置示例：
        step_wait:
          type: wait
          duration_ms: 2000
          jitter_ms: 500       # 可选随机抖动
          next: "step_next"
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._start_time: Optional[float] = None
        self._target_duration: float = 0.0

    @property
    def node_type(self) -> str:
        return "wait"

    def on_enter(self, context: Any) -> None:
        duration_ms = self._config.get("duration_ms", 1000)
        jitter_ms = self._config.get("jitter_ms", 0)

        if jitter_ms > 0:
            duration_ms += random.randint(-jitter_ms, jitter_ms)

        self._target_duration = max(0, duration_ms) / 1000.0
        self._start_time = time.time()

    def reset(self) -> None:
        self._start_time = None

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """检查等待是否完成。"""
        if self._start_time is None:
            self.on_enter(context)

        elapsed = time.time() - self._start_time
        if elapsed >= self._target_duration:
            return NodeResult.SUCCESS, self._config.get("next")

        return NodeResult.RUNNING, None
