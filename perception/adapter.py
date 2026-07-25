"""Spotting 子模块适配器 — 将 Spotting 的持续运行循环改造为按需单帧检测接口。

通过 sys.path 注入子模块路径，直接复用其内部模块：
- capture.window: 窗口查找
- capture.grabber: 截图
- detectors: 检测器注册表
- detectors.base: Detection 数据类
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.context import Detection

logger = logging.getLogger(__name__)

# Spotting 子模块路径
SPOTTING_DIR = Path(__file__).resolve().parent.parent / "submodules" / "RocoKingdom-Spotting"


class SpottingAdapter:
    """Spotting 视觉识别适配器。

    将 Spotting 的 Pipeline 拆解为单帧检测接口，供 PDA 引擎按需调用。

    用法：
        adapter = SpottingAdapter(config)
        adapter.setup()
        detections = adapter.detect_frame()
        target = adapter.detect_target("hello")
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or {}
        self._hwnd: Optional[int] = None
        self._grabber = None
        self._detectors: Dict[str, Any] = {}
        self._window_keyword: str = self._config.get("window_keyword", "洛克王国：世界")
        self._capture_backend: str = self._config.get("capture_backend", "screen-client")
        self._foreground_only: bool = self._config.get("foreground_only", True)
        self._spotting_config_path: Optional[str] = self._config.get("config_path", None)
        self._initialized = False

    def setup(self) -> None:
        """初始化 Spotting 子系统：注入路径、创建检测器、查找窗口。"""
        # 注入子模块路径到 sys.path
        spotting_str = str(SPOTTING_DIR)
        if spotting_str not in sys.path:
            sys.path.insert(0, spotting_str)

        try:
            from capture.window import find_window_by_keyword
            from capture.grabber import FrameGrabber
            from detectors.base import DetectorRegistry
            from detectors import _ensure_loaded

            # 加载检测器模块（触发注册）
            _ensure_loaded()

            # 构建检测器
            self._detectors = self._build_detectors()

            # 创建截图器
            self._grabber = FrameGrabber(self._capture_backend)

            # 查找目标窗口
            self._hwnd = find_window_by_keyword(self._window_keyword)
            if self._hwnd is None:
                logger.warning("未找到窗口: %s（将在检测时重试）", self._window_keyword)
            else:
                logger.info("目标窗口已找到: hwnd=%d", self._hwnd)

            self._initialized = True
            logger.info(
                "Spotting 适配器初始化完成 (检测器: %s)",
                ", ".join(self._detectors.keys()) or "无",
            )

        except ImportError as e:
            logger.error("Spotting 子模块导入失败: %s", e)
            logger.error("请确保子模块已正确克隆: git submodule update --init")
            raise

    def detect_frame(self) -> List[Detection]:
        """执行单帧检测，返回所有检测结果。"""
        if not self._initialized:
            return []

        frame = self._capture_frame()
        if frame is None:
            return []

        return self._run_detectors(frame)

    def detect_target(self, name: str, min_score: float = 0.0) -> Optional[Detection]:
        """检测指定名称的目标，返回最高分匹配。"""
        detections = self.detect_frame()
        matches = [
            d for d in detections
            if d.name == name and d.score >= min_score
        ]
        if not matches:
            return None
        return max(matches, key=lambda d: d.score)

    def detect_targets(self, name: str, min_score: float = 0.0) -> List[Detection]:
        """检测指定名称的所有目标。"""
        detections = self.detect_frame()
        return [
            d for d in detections
            if d.name == name and d.score >= min_score
        ]

    def is_target_present(self, name: str, min_score: float = 0.0) -> bool:
        """判断目标是否存在于当前帧。"""
        return self.detect_target(name, min_score) is not None

    def refresh_window(self) -> bool:
        """重新查找目标窗口。"""
        try:
            from capture.window import find_window_by_keyword
            self._hwnd = find_window_by_keyword(self._window_keyword)
            return self._hwnd is not None
        except ImportError:
            return False

    @property
    def window_hwnd(self) -> Optional[int]:
        return self._hwnd

    @property
    def is_available(self) -> bool:
        return self._initialized and self._hwnd is not None

    def cleanup(self) -> None:
        """清理资源。"""
        self._detectors.clear()
        self._initialized = False
        logger.info("Spotting 适配器已清理")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _capture_frame(self):
        """截取一帧图像。"""
        from capture.window import (
            find_window_by_keyword,
            get_client_rect_on_screen,
            is_foreground,
        )

        # 窗口丢失时尝试重新查找
        if self._hwnd is None:
            self._hwnd = find_window_by_keyword(self._window_keyword)
            if self._hwnd is None:
                return None

        # 前台检查
        if self._foreground_only and not is_foreground(self._hwnd):
            return None

        rect = get_client_rect_on_screen(self._hwnd)
        frame = self._grabber.grab(self._hwnd, rect)

        if frame is None or frame.size == 0:
            return None
        return frame

    def _run_detectors(self, frame) -> List[Detection]:
        """在所有检测器上运行推理，合并结果。"""
        all_detections: List[Detection] = []

        for name, detector in self._detectors.items():
            try:
                results = detector.detect(frame)
                for r in results:
                    # 将 Spotting 的 Detection 转换为本框架的 Detection
                    all_detections.append(Detection(
                        name=r.name,
                        category=r.category,
                        score=r.score,
                        x=r.x,
                        y=r.y,
                        w=r.w,
                        h=r.h,
                        threshold=r.threshold,
                    ))
            except Exception as e:
                logger.error("检测器 %s 执行失败: %s", name, e)

        return all_detections

    def _build_detectors(self) -> Dict[str, Any]:
        """根据配置构建检测器实例。"""
        from detectors.base import DetectorRegistry

        detectors: Dict[str, Any] = {}

        # 如果指定了 Spotting 的 config.json，从中加载检测器配置
        spotting_cfg = self._load_spotting_config()
        detector_configs = spotting_cfg.get("detectors", {})

        # 也支持从主框架配置中直接指定
        if not detector_configs and "detectors" in self._config:
            detector_configs = self._config["detectors"]

        for name, det_cfg in detector_configs.items():
            if not det_cfg.get("enabled", True):
                continue
            type_name = det_cfg.get("type")
            if not type_name:
                continue
            params = det_cfg.get("params", {})
            try:
                detectors[name] = DetectorRegistry.create(type_name, params)
                # 预热
                detectors[name].warmup()
                logger.info("检测器已创建: %s (type=%s)", name, type_name)
            except Exception as e:
                logger.error("创建检测器 %s 失败: %s", name, e)

        return detectors

    def _load_spotting_config(self) -> dict:
        """加载 Spotting 的 config.json。"""
        import json

        config_path = None
        if self._spotting_config_path:
            config_path = Path(self._spotting_config_path)
        else:
            # 默认使用子模块自带的 config.json
            config_path = SPOTTING_DIR / "config.json"

        if config_path and config_path.exists():
            try:
                with config_path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("加载 Spotting 配置失败: %s", e)

        return {}
