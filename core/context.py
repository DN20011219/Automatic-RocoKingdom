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

    # 最近一帧是否真的截到了画面
    # False = 窗口未找到/不在前台/截图失败，此时 last_detections 为空是"看不见"而非"没有目标"
    frame_captured: bool = False

    # 最近一帧的截取区域（屏幕坐标 x, y, w, h）
    # 丢球瞄准的基准点（客户区中心）由它算出；(0,0,0,0) 表示还没截到过画面
    frame_rect: tuple = (0, 0, 0, 0)

    # 感知帧序号，每次 _perceive 自增
    # 节点用它判断"手里的检测结果是不是瞄准动作之后采的"：
    # 同一帧内做复核没有意义（相机还没动，误差当然没变）
    frame_seq: int = 0

    # 最近协议事件（RKPP）：key 为 summary_kind / opcode_name / opencode
    last_protocol_events: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # 最近一条协议事件快照（含 _received_at 本地接收时间戳）
    last_protocol_event: Optional[Dict[str, Any]] = None

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

    def get_protocol_event(self, kind: str) -> Optional[Dict[str, Any]]:
        """按 summary_kind / opcode_name / opencode 精确查找最近的协议事件。"""
        return self.last_protocol_events.get(kind)

    def find_protocol_event(self, keyword: str) -> Optional[Dict[str, Any]]:
        """按关键词模糊查找协议事件（先精确匹配，再 meaning 包含匹配）。"""
        event = self.get_protocol_event(keyword)
        if event is not None:
            return event

        needle = str(keyword).lower()
        best: Optional[Dict[str, Any]] = None
        for candidate in self.last_protocol_events.values():
            if needle not in str(candidate.get("meaning", "")).lower():
                continue
            if best is None or candidate.get("_received_at", 0.0) > best.get("_received_at", 0.0):
                best = candidate
        return best

    def protocol_event_age_sec(self, kind: str) -> Optional[float]:
        """返回指定协议事件距今秒数，未找到返回 None。"""
        event = self.find_protocol_event(kind)
        if event is None:
            return None
        return time.time() - event.get("_received_at", 0.0)

    def resolve_value(self, value: Any) -> Any:
        """解析值中的变量引用 ${var_name}。"""
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            var_name = value[2:-1]
            return self.variables.get(var_name, value)
        return value
