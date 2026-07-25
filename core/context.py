"""运行时上下文 — 所有节点通过 Context 共享数据，实现解耦。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Detection:
    """检测结果数据类（与 Spotting 子模块的 Detection 兼容）。"""

    name: str
    category: str
    score: float
    x: int
    y: int
    w: int
    h: int
    threshold: float = 0.0

    @property
    def cx(self) -> int:
        """中心点 x。"""
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        """中心点 y。"""
        return self.y + self.h // 2

    @property
    def is_low_score(self) -> bool:
        if self.threshold <= 0:
            return False
        return self.score < self.threshold * 1.15


@dataclass
class RuntimeContext:
    """工作流运行时上下文，所有节点共享。"""

    # 工作流变量（YAML 中 variables 段定义）
    variables: Dict[str, Any] = field(default_factory=dict)

    # 计数器（用于条件判断）
    counters: Dict[str, int] = field(default_factory=dict)

    # 当前场景名称
    current_scene: Optional[str] = None

    # 最近一帧检测结果
    last_detections: List[Detection] = field(default_factory=list)

    # 当前工作流名称
    workflow_name: str = ""

    # 当前步骤名称
    step_name: str = ""

    # 工作流启动时间戳
    start_time: float = field(default_factory=time.time)

    # 当前循环轮次
    iteration: int = 0

    # 上一步骤名称（用于回退/日志）
    prev_step_name: str = ""

    # ---- 便捷方法 ----

    def get_variable(self, name: str, default: Any = None) -> Any:
        """获取变量值，支持 ${var_name} 格式解析。"""
        return self.variables.get(name, default)

    def set_variable(self, name: str, value: Any) -> None:
        self.variables[name] = value

    def increment_counter(self, name: str, amount: int = 1) -> int:
        """递增计数器并返回新值。"""
        self.counters[name] = self.counters.get(name, 0) + amount
        return self.counters[name]

    def get_counter(self, name: str) -> int:
        return self.counters.get(name, 0)

    def reset_counter(self, name: str) -> None:
        self.counters[name] = 0

    def elapsed_seconds(self) -> float:
        """自工作流启动以来经过的秒数。"""
        return time.time() - self.start_time

    def find_detection(self, name: str, min_score: float = 0.0) -> Optional[Detection]:
        """在最近一帧检测结果中查找指定名称的目标。"""
        for det in self.last_detections:
            if det.name == name and det.score >= min_score:
                return det
        return None

    def find_detections(self, name: str, min_score: float = 0.0) -> List[Detection]:
        """查找所有匹配名称的目标。"""
        return [
            det for det in self.last_detections
            if det.name == name and det.score >= min_score
        ]

    def resolve_value(self, value: Any) -> Any:
        """解析值中的变量引用 ${var_name}。"""
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            var_name = value[2:-1]
            return self.variables.get(var_name, value)
        return value
