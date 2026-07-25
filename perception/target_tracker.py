"""目标追踪器 — 持续跟踪特定目标，记录出现/消失状态。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.context import Detection

logger = logging.getLogger(__name__)


@dataclass
class TrackedTarget:
    """被追踪的目标状态。"""

    name: str
    min_score: float = 0.0
    is_present: bool = False
    last_seen_time: float = 0.0
    first_seen_time: float = 0.0
    last_detection: Optional[Detection] = None
    appear_count: int = 0       # 累计出现次数（从消失到出现算一次）
    continuous_frames: int = 0  # 连续出现帧数


class TargetTracker:
    """目标追踪器，跟踪多个目标的出现/消失状态。

    用法：
        tracker = TargetTracker()
        tracker.track("hello", min_score=0.85)
        tracker.update(detections)
        if tracker.is_present("hello"):
            det = tracker.get_detection("hello")
    """

    def __init__(self) -> None:
        self._targets: Dict[str, TrackedTarget] = {}

    def track(self, name: str, min_score: float = 0.0) -> None:
        """开始追踪一个目标。"""
        if name not in self._targets:
            self._targets[name] = TrackedTarget(name=name, min_score=min_score)

    def untrack(self, name: str) -> None:
        """停止追踪一个目标。"""
        self._targets.pop(name, None)

    def update(self, detections: List[Detection]) -> None:
        """用新一帧的检测结果更新所有追踪目标的状态。"""
        now = time.time()

        for name, tracked in self._targets.items():
            # 在当前帧中查找该目标
            matches = [
                d for d in detections
                if d.name == name and d.score >= tracked.min_score
            ]

            if matches:
                best = max(matches, key=lambda d: d.score)
                if not tracked.is_present:
                    # 目标刚出现
                    tracked.is_present = True
                    tracked.first_seen_time = now
                    tracked.appear_count += 1
                    tracked.continuous_frames = 0
                tracked.last_seen_time = now
                tracked.last_detection = best
                tracked.continuous_frames += 1
            else:
                if tracked.is_present:
                    # 目标刚消失
                    tracked.is_present = False
                    tracked.continuous_frames = 0
                    logger.debug("目标消失: %s", name)

    def is_present(self, name: str) -> bool:
        """目标当前是否存在。"""
        tracked = self._targets.get(name)
        return tracked.is_present if tracked else False

    def is_absent(self, name: str) -> bool:
        """目标当前是否不存在。"""
        return not self.is_present(name)

    def get_detection(self, name: str) -> Optional[Detection]:
        """获取目标最近一次的检测结果。"""
        tracked = self._targets.get(name)
        if tracked and tracked.is_present:
            return tracked.last_detection
        return None

    def get_absent_duration(self, name: str) -> float:
        """获取目标已消失的时长（秒）。如果仍在出现则返回 0。"""
        tracked = self._targets.get(name)
        if not tracked or tracked.is_present:
            return 0.0
        if tracked.last_seen_time == 0:
            return 0.0
        return time.time() - tracked.last_seen_time

    def get_present_duration(self, name: str) -> float:
        """获取目标当前已持续出现的时长（秒）。"""
        tracked = self._targets.get(name)
        if not tracked or not tracked.is_present:
            return 0.0
        return time.time() - tracked.first_seen_time

    def reset(self) -> None:
        """重置所有追踪状态。"""
        self._targets.clear()
