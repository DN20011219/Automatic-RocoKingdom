"""工作流节点抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, Optional


class NodeResult(Enum):
    """节点执行结果。"""

    SUCCESS = auto()        # 执行成功
    FAILED = auto()         # 执行失败
    RUNNING = auto()        # 仍在运行（需要下次 tick 继续）
    TERMINATE = auto()      # 请求终止工作流
    SKIP = auto()           # 跳过此节点


@dataclass
class NodeContext:
    """节点执行时的上下文信息。"""

    step_name: str                          # 当前步骤名
    node_config: Dict[str, Any]             # 节点配置（YAML 中的定义）
    next_step: Optional[str] = None         # 下一步骤名（由节点决定）


class NodeBase(ABC):
    """工作流节点抽象基类。

    所有节点类型（action, perceive, condition, loop 等）都继承此类。
    节点通过 execute() 方法执行，返回 NodeResult 和可选的下一步骤名。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Args:
            config: 节点配置字典（来自 YAML 工作流定义）
        """
        self._config = config
        self._step_name: str = ""

    @property
    def step_name(self) -> str:
        return self._step_name

    @step_name.setter
    def step_name(self, value: str) -> None:
        self._step_name = value

    @property
    def node_type(self) -> str:
        """节点类型标识（由子类覆盖）。"""
        return "base"

    @abstractmethod
    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """执行节点逻辑。

        Args:
            context: RuntimeContext 运行时上下文

        Returns:
            (result, next_step): 执行结果和下一步骤名。
            - next_step 为 None 时使用 YAML 中定义的 next 字段
            - next_step 为具体值时跳转到指定步骤
        """
        ...

    def on_enter(self, context: Any) -> None:
        """节点首次进入时的回调（可选覆盖）。"""
        pass

    def on_exit(self, context: Any) -> None:
        """节点退出时的回调（可选覆盖）。"""
        pass

    def reset(self) -> None:
        """重置节点内部状态（用于循环重新进入时）。"""
        pass
