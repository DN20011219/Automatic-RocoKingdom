"""场景识别器 — 基于视觉检测结果判断当前所处场景。"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from core.context import Detection

logger = logging.getLogger(__name__)


class SceneDetector:
    """根据检测结果中的特征目标判断当前场景。

    每个场景定义一组"标志目标"，当这些目标被检测到时认为处于该场景。

    用法：
        detector = SceneDetector()
        detector.register_scene("main_menu", ["main_menu_label"], min_score=0.85)
        detector.register_scene("battle", ["battle_ui_label"], min_score=0.80)
        scene = detector.identify(detections)
    """

    def __init__(self) -> None:
        # scene_name -> {"targets": [...], "min_score": float, "require_all": bool}
        self._scenes: Dict[str, dict] = {}

    def register_scene(
        self,
        scene_name: str,
        target_names: List[str],
        min_score: float = 0.8,
        require_all: bool = False,
    ) -> None:
        """注册一个场景的识别规则。

        Args:
            scene_name: 场景名称
            target_names: 标志目标名称列表
            min_score: 最低匹配分数
            require_all: 是否要求所有目标都出现（默认任一即可）
        """
        self._scenes[scene_name] = {
            "targets": target_names,
            "min_score": min_score,
            "require_all": require_all,
        }

    def identify(self, detections: List[Detection]) -> Optional[str]:
        """根据检测结果识别当前场景。

        返回匹配度最高的场景名称，无匹配返回 None。
        """
        best_scene: Optional[str] = None
        best_score: float = 0.0

        for scene_name, rule in self._scenes.items():
            score = self._match_score(detections, rule)
            if score > best_score:
                best_score = score
                best_scene = scene_name

        return best_scene if best_score > 0 else None

    def is_scene(self, scene_name: str, detections: List[Detection]) -> bool:
        """判断当前检测结果是否匹配指定场景。"""
        if scene_name not in self._scenes:
            return False
        rule = self._scenes[scene_name]
        return self._match_score(detections, rule) > 0

    def _match_score(self, detections: List[Detection], rule: dict) -> float:
        """计算检测结果与场景规则的匹配分数。"""
        targets = rule["targets"]
        min_score = rule["min_score"]
        require_all = rule["require_all"]

        matched_scores: List[float] = []
        for target_name in targets:
            # 查找该目标的最高分匹配
            target_dets = [
                d for d in detections
                if d.name == target_name and d.score >= min_score
            ]
            if target_dets:
                matched_scores.append(max(d.score for d in target_dets))

        if require_all:
            # 所有目标都必须出现
            if len(matched_scores) == len(targets):
                return sum(matched_scores) / len(matched_scores)
            return 0.0
        else:
            # 任一目标出现即可，返回平均匹配分
            if matched_scores:
                return sum(matched_scores) / len(matched_scores)
            return 0.0
