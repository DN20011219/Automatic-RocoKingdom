"""有限状态机实现 — 场景识别与转换。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.context import Detection, RuntimeContext
from core.events import EventBus, EVENT_SCENE_CHANGED
from perception.scene_detector import SceneDetector

logger = logging.getLogger(__name__)


class SceneStateMachine:
    """场景有限状态机。

    根据视觉检测结果自动识别当前场景，并在场景切换时触发事件。

    用法：
        machine = SceneStateMachine(event_bus)
        machine.load_scenes(scenes_config)
        machine.update(detections, context)
    """

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        self._event_bus = event_bus
        self._scene_detector = SceneDetector()
        self._current_scene: Optional[str] = None
        self._scenes_config: Dict[str, dict] = {}
        self._on_enter_callbacks: Dict[str, str] = {}  # scene_name -> step_name

    def load_scenes(self, scenes_config: Dict[str, dict]) -> None:
        """从工作流配置加载场景定义。

        scenes_config 格式：
            {
                "main_menu": {
                    "detect": {"target": "main_menu_label", "min_score": 0.85},
                    "on_enter": "step_navigate"
                },
                ...
            }
        """
        self._scenes_config = scenes_config

        for scene_name, scene_cfg in scenes_config.items():
            detect_cfg = scene_cfg.get("detect", {})
            target = detect_cfg.get("target", "")
            min_score = detect_cfg.get("min_score", 0.8)

            if target:
                self._scene_detector.register_scene(
                    scene_name=scene_name,
                    target_names=[target],
                    min_score=min_score,
                )

            # 记录 on_enter 回调
            on_enter = scene_cfg.get("on_enter")
            if on_enter:
                self._on_enter_callbacks[scene_name] = on_enter

        logger.info("场景状态机已加载 %d 个场景: %s",
                    len(scenes_config), ", ".join(scenes_config.keys()))

    def update(self, detections: List[Detection], context: RuntimeContext) -> None:
        """用新一帧检测结果更新场景状态。"""
        new_scene = self._scene_detector.identify(detections)

        if new_scene and new_scene != self._current_scene:
            old_scene = self._current_scene
            self._current_scene = new_scene
            context.current_scene = new_scene

            logger.info("场景切换: %s -> %s", old_scene or "(无)", new_scene)

            # 触发事件
            if self._event_bus:
                self._event_bus.emit(
                    EVENT_SCENE_CHANGED,
                    old_scene=old_scene,
                    new_scene=new_scene,
                    context=context,
                )

    @property
    def current_scene(self) -> Optional[str]:
        return self._current_scene

    def get_on_enter_step(self, scene_name: str) -> Optional[str]:
        """获取场景的 on_enter 步骤名。"""
        return self._on_enter_callbacks.get(scene_name)

    def is_scene(self, scene_name: str, detections: List[Detection]) -> bool:
        """判断当前检测结果是否匹配指定场景。"""
        return self._scene_detector.is_scene(scene_name, detections)

    def reset(self) -> None:
        """重置状态机。"""
        self._current_scene = None
