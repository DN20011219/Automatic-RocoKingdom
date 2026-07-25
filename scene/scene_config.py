"""场景定义与转换规则 — 从工作流 YAML 中解析场景配置。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SceneDetectionRule:
    """场景识别规则：哪些目标出现表示处于该场景。"""

    target: str                       # 标志目标名称
    min_score: float = 0.8            # 最低匹配分数
    require_all: bool = False         # 是否要求所有目标都出现
    targets: List[str] = field(default_factory=list)  # 多目标支持


@dataclass
class SceneDefinition:
    """单个场景的完整定义。"""

    name: str
    detection_rule: SceneDetectionRule
    on_enter_step: Optional[str] = None   # 进入场景时跳转的步骤
    on_exit_step: Optional[str] = None    # 离开场景时跳转的步骤
    priority: int = 0                     # 场景优先级（多场景匹配时取高优先级）


class SceneConfig:
    """场景配置管理器 — 解析并存储工作流中的场景定义。

    YAML 格式：
        scenes:
          main_menu:
            detect:
              target: "main_menu_label"
              min_score: 0.85
            on_enter: "step_navigate"
            priority: 10
    """

    def __init__(self) -> None:
        self._scenes: Dict[str, SceneDefinition] = {}

    def load_from_dict(self, scenes_dict: Dict[str, Any]) -> None:
        """从工作流 YAML 的 scenes 段加载场景定义。"""
        self._scenes.clear()

        for scene_name, scene_cfg in scenes_dict.items():
            if not isinstance(scene_cfg, dict):
                logger.warning("场景 '%s' 配置格式错误，跳过", scene_name)
                continue

            detect_cfg = scene_cfg.get("detect", {})
            target = detect_cfg.get("target", "")
            targets = detect_cfg.get("targets", [target] if target else [])
            min_score = detect_cfg.get("min_score", 0.8)
            require_all = detect_cfg.get("require_all", False)

            rule = SceneDetectionRule(
                target=target,
                min_score=min_score,
                require_all=require_all,
                targets=targets,
            )

            definition = SceneDefinition(
                name=scene_name,
                detection_rule=rule,
                on_enter_step=scene_cfg.get("on_enter"),
                on_exit_step=scene_cfg.get("on_exit"),
                priority=scene_cfg.get("priority", 0),
            )

            self._scenes[scene_name] = definition

        logger.info("场景配置已加载: %d 个场景", len(self._scenes))

    def get_scene(self, name: str) -> Optional[SceneDefinition]:
        """获取指定场景定义。"""
        return self._scenes.get(name)

    def get_all_scenes(self) -> Dict[str, SceneDefinition]:
        """获取所有场景定义。"""
        return dict(self._scenes)

    def get_scene_names(self) -> List[str]:
        """获取所有场景名称。"""
        return list(self._scenes.keys())

    @property
    def scene_count(self) -> int:
        return len(self._scenes)
