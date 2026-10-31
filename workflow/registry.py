"""节点类型注册表 — 支持 @register 装饰器扩展新节点类型。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Type

from workflow.nodes.base import NodeBase

logger = logging.getLogger(__name__)


class NodeRegistry:
    """工作流节点注册表。

    通过 @NodeRegistry.register("type_name") 装饰器注册新节点类型。
    工作流加载器根据 YAML 中的 type 字段自动实例化对应节点。
    """

    _registry: Dict[str, Type[NodeBase]] = {}

    @classmethod
    def register(cls, type_name: str):
        """装饰器：将节点类注册到指定类型名。"""
        def decorator(klass: Type[NodeBase]):
            cls._registry[type_name] = klass
            logger.debug("节点类型已注册: %s -> %s", type_name, klass.__name__)
            return klass
        return decorator

    @classmethod
    def create(cls, type_name: str, config: Dict[str, Any]) -> NodeBase:
        """根据类型名和配置创建节点实例。"""
        if type_name not in cls._registry:
            available = ", ".join(cls._registry.keys()) or "(无)"
            raise KeyError(f"未知节点类型: {type_name}，可用: {available}")
        return cls._registry[type_name](config)

    @classmethod
    def available_types(cls) -> list[str]:
        return list(cls._registry.keys())

    @classmethod
    def _ensure_builtin_loaded(cls) -> None:
        """确保内置节点类型已注册。"""
        # 导入节点模块触发 @register 装饰器
        import workflow.nodes.action_node
        import workflow.nodes.perceive_node
        import workflow.nodes.condition_node
        import workflow.nodes.loop_node
        import workflow.nodes.wait_node
        import workflow.nodes.scene_node
        import workflow.nodes.terminate_node
        import workflow.nodes.log_node
        import workflow.nodes.pet_hunt_node
