"""场景切换节点 — 触发场景状态机转换。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("scene")
class SceneNode(NodeBase):
    """场景切换节点：手动切换到指定场景。

    YAML 配置示例：
        step_goto_battle:
          type: scene
          scene: "battle_field"
          next: "step_battle"
    """

    @property
    def node_type(self) -> str:
        return "scene"

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """切换场景。"""
        scene_name = self._config.get("scene", "")

        if not scene_name:
            logger.warning("场景节点 %s 没有定义 scene", self._step_name)
            return NodeResult.SUCCESS, self._config.get("next")

        # 更新上下文中的当前场景
        old_scene = context.current_scene
        context.current_scene = scene_name

        logger.info("场景切换: %s -> %s", old_scene or "(无)", scene_name)

        return NodeResult.SUCCESS, self._config.get("next")
