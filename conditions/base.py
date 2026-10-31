"""条件抽象基类 + 注册表。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Type

logger = logging.getLogger(__name__)


class ConditionBase(ABC):
    """条件抽象基类。

    所有条件类型继承此类，实现 evaluate() 方法。
    通过 @ConditionRegistry.register("type_name") 注册。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config

    @abstractmethod
    def evaluate(self, context: Any) -> bool:
        """评估条件是否满足。

        Args:
            context: RuntimeContext 运行时上下文

        Returns:
            True 表示条件满足
        """
        ...


class ConditionRegistry:
    """条件类型注册表。

    通过 @ConditionRegistry.register("type_name") 装饰器注册新条件类型。
    """

    _registry: Dict[str, Type[ConditionBase]] = {}

    @classmethod
    def register(cls, type_name: str):
        """装饰器：将条件类注册到指定类型名。"""
        def decorator(klass: Type[ConditionBase]):
            cls._registry[type_name] = klass
            logger.debug("条件类型已注册: %s -> %s", type_name, klass.__name__)
            return klass
        return decorator

    @classmethod
    def create(cls, type_name: str, config: Dict[str, Any]) -> ConditionBase:
        """根据类型名和配置创建条件实例。"""
        if type_name not in cls._registry:
            # 尝试加载内置条件
            cls._ensure_builtin_loaded()

        if type_name not in cls._registry:
            available = ", ".join(cls._registry.keys()) or "(无)"
            raise KeyError(f"未知条件类型: {type_name}，可用: {available}")

        return cls._registry[type_name](config)

    @classmethod
    def available_types(cls) -> list[str]:
        return list(cls._registry.keys())

    @classmethod
    def _ensure_builtin_loaded(cls) -> None:
        """确保内置条件类型已注册。"""
        import conditions.visual_condition
        import conditions.time_condition
        import conditions.counter_condition
        import conditions.composite_condition
        import conditions.protocol_condition
