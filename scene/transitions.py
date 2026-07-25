"""转换条件与动作绑定 — 场景切换时的条件判断和自动动作。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.context import Detection, RuntimeContext

logger = logging.getLogger(__name__)


@dataclass
class Transition:
    """场景转换定义。"""

    from_scene: Optional[str]          # 源场景（None 表示任意场景）
    to_scene: str                      # 目标场景
    condition_target: str = ""         # 触发转换的目标名称
    condition_min_score: float = 0.8   # 触发转换的最低分数
    on_transition_step: Optional[str] = None  # 转换时跳转的工作流步骤
    priority: int = 0                  # 转换优先级


class TransitionManager:
    """场景转换管理器 — 管理场景间的转换规则和绑定动作。

    用法：
        manager = TransitionManager()
        manager.add_transition(Transition(
            from_scene="main_menu",
            to_scene="quest_panel",
            condition_target="quest_button",
            condition_min_score=0.85,
            on_transition_step="step_navigate",
        ))
        result = manager.check_transitions(detections, context)
    """

    def __init__(self) -> None:
        self._transitions: List[Transition] = []
        self._on_transition_callbacks: List[Callable] = []

    def add_transition(self, transition: Transition) -> None:
        """添加一条转换规则。"""
        self._transitions.append(transition)
        # 按优先级排序（高优先级在前）
        self._transitions.sort(key=lambda t: t.priority, reverse=True)

    def add_callback(self, callback: Callable) -> None:
        """注册转换发生时的回调函数。"""
        self._on_transition_callbacks.append(callback)

    def check_transitions(
        self,
        detections: List[Detection],
        context: RuntimeContext,
    ) -> Optional[Transition]:
        """检查是否有转换条件被满足。

        Args:
            detections: 当前帧检测结果
            context: 运行时上下文

        Returns:
            满足条件的最高优先级转换，或 None
        """
        current_scene = context.current_scene

        for transition in self._transitions:
            # 检查源场景匹配
            if transition.from_scene is not None and transition.from_scene != current_scene:
                continue

            # 检查目标条件
            if not transition.condition_target:
                continue

            # 在检测结果中查找触发目标
            matched = any(
                d.name == transition.condition_target and d.score >= transition.condition_min_score
                for d in detections
            )

            if matched:
                logger.debug(
                    "场景转换触发: %s -> %s (target=%s)",
                    current_scene or "(any)", transition.to_scene,
                    transition.condition_target,
                )
                # 执行回调
                for cb in self._on_transition_callbacks:
                    try:
                        cb(transition, context)
                    except Exception as e:
                        logger.error("转换回调异常: %s", e)

                return transition

        return None

    def load_from_scenes_config(self, scenes_dict: Dict[str, Any]) -> None:
        """从工作流的 scenes 配置自动生成转换规则。

        每个场景的 detect.target 作为"进入该场景"的转换条件。
        """
        self._transitions.clear()

        for scene_name, scene_cfg in scenes_dict.items():
            if not isinstance(scene_cfg, dict):
                continue

            detect_cfg = scene_cfg.get("detect", {})
            target = detect_cfg.get("target", "")
            min_score = detect_cfg.get("min_score", 0.8)
            on_enter = scene_cfg.get("on_enter")

            if target:
                self.add_transition(Transition(
                    from_scene=None,  # 从任意场景都可转入
                    to_scene=scene_name,
                    condition_target=target,
                    condition_min_score=min_score,
                    on_transition_step=on_enter,
                    priority=scene_cfg.get("priority", 0),
                ))

        logger.info("转换规则已加载: %d 条", len(self._transitions))

    def clear(self) -> None:
        """清除所有转换规则。"""
        self._transitions.clear()
        self._on_transition_callbacks.clear()
