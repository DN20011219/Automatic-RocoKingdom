"""Spotting 子模块适配器 — 将 Spotting 的持续运行循环改造为按需单帧检测接口。

通过 sys.path 注入子模块路径，直接复用其内部模块：
- capture.window: 窗口查找
- capture.grabber: 截图
- detectors: 检测器注册表
- detectors.base: Detection 数据类
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import replace
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
        # 最近一次 detect_frame 是否真的截到了画面
        # （窗口丢失 / 不在前台 / 截图失败时为 False，此时检测结果为空是"看不到"而非"没有目标"）
        self._last_capture_ok = False

        # 检测框覆盖层（GDI 透明窗口，鼠标穿透 + 不抢前台，不影响 foreground_only 截图）
        self._show_overlay: bool = self._config.get("show_overlay", False)
        self._overlay_show_cursor: bool = self._config.get("overlay_show_cursor", True)
        self._overlay = None
        self._last_rect: tuple = (0, 0, 0, 0)   # 最近一次截帧区域 (x, y, w, h)

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

            # 检测框覆盖层（复用 Spotting 的 OverlayWindow：不抢前台，因此不会破坏截图）
            if self._show_overlay:
                try:
                    from display.overlay import OverlayWindow
                    self._overlay = OverlayWindow()
                    logger.info("检测框覆盖层已启用（置顶 + 鼠标穿透 + 不抢前台）")
                except ImportError as e:
                    logger.warning("覆盖层模块不可用，已跳过: %s", e)
                    self._overlay = None

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
        """执行单帧检测，返回所有检测结果（屏幕绝对坐标）。"""
        if not self._initialized:
            self._last_capture_ok = False
            return []

        frame, ox, oy = self._capture_frame()
        self._last_capture_ok = frame is not None
        if frame is None:
            self._render_overlay(None, [])
            return []

        detections = self._run_detectors(frame, ox, oy)
        self._render_overlay(frame, detections)
        return detections

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

    def capture_frame(self):
        """截取一帧图像（公开接口，供截图等场景使用）。

        Returns:
            BGR 图像帧；窗口不存在或在后台时返回 None。
        """
        frame, _ox, _oy = self._capture_frame()
        return frame

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
    def window_keyword(self) -> str:
        """目标窗口查找关键字。"""
        return self._window_keyword

    @property
    def detector_names(self) -> List[str]:
        """已加载的检测器名称列表。"""
        return list(self._detectors.keys())

    @property
    def is_available(self) -> bool:
        return self._initialized and self._hwnd is not None

    @property
    def last_capture_ok(self) -> bool:
        """最近一次 detect_frame 是否成功截到画面。

        为 False 说明"看不见"（窗口未找到 / 不在前台 / 截图失败），
        此时空的检测结果不能当作"画面里没有目标"来驱动键鼠动作。
        """
        return self._last_capture_ok

    @property
    def last_rect(self) -> tuple:
        """最近一次截帧区域（屏幕坐标 x, y, w, h）。

        丢球是丢向客户区中心的，节点需要它来算瞄准基准点。
        """
        return self._last_rect

    def cleanup(self) -> None:
        """清理资源。"""
        if self._overlay is not None:
            try:
                self._overlay.destroy()
            except Exception as e:
                logger.debug("覆盖层销毁失败: %s", e)
            self._overlay = None

        self._detectors.clear()
        self._initialized = False
        self._last_capture_ok = False
        logger.info("Spotting 适配器已清理")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _capture_frame(self):
        """截取一帧图像。

        Returns:
            (frame, origin_x, origin_y)：origin 为截取区域左上角的屏幕绝对坐标；
            窗口不存在或在后台时返回 (None, 0, 0)。
        """
        from capture.window import (
            find_window_by_keyword,
            get_client_rect_on_screen,
            get_window_rect_on_screen,
            is_foreground,
        )

        # 窗口丢失时尝试重新查找
        if self._hwnd is None:
            self._hwnd = find_window_by_keyword(self._window_keyword)
            if self._hwnd is None:
                return None, 0, 0

        # 前台检查
        if self._foreground_only and not is_foreground(self._hwnd):
            return None, 0, 0

        # 截取区域原点（与 grabber 内部逻辑保持一致）
        if self._capture_backend == "screen-window":
            ox, oy, w, h = get_window_rect_on_screen(self._hwnd)
        else:
            ox, oy, w, h = get_client_rect_on_screen(self._hwnd)

        frame = self._grabber.grab(self._hwnd, (ox, oy, w, h))

        if frame is None or frame.size == 0:
            return None, 0, 0

        # 记下截帧区域，覆盖层需要它来定位窗口并把屏幕坐标换算回帧内坐标
        self._last_rect = (ox, oy, w, h)
        return frame, ox, oy

    def _render_overlay(self, frame, detections: List[Detection]) -> None:
        """把检测结果画到游戏窗口上方的透明覆盖层。

        覆盖层的 render() 要的是**帧内相对坐标**，而 detect_frame 对外给的是
        屏幕绝对坐标，这里减回截帧原点。frame 为 None 表示当前截不到画面，
        此时隐藏覆盖层，免得留下过期的框。
        """
        if self._overlay is None:
            return

        if frame is None or self._hwnd is None:
            self._overlay.hide()
            return

        ox, oy = self._last_rect[0], self._last_rect[1]
        frame_h, frame_w = frame.shape[:2]

        relative = [replace(d, x=d.x - ox, y=d.y - oy) for d in detections]

        # 瞄准基准点（客户区中心）：球就是丢向这里的，画出来才能直观验证瞄没瞄对
        if self._config.get("overlay_show_reference", True):
            relative.append(self._marker(
                "<aim>",
                self._last_rect[0] + self._last_rect[2] / 2.0,
                self._last_rect[1] + self._last_rect[3] / 2.0,
                ox, oy, size=26,
            ))

        # 额外画一个"当前光标位置"标记。排查准不准时只要看它有没有落在
        # 检测框的十字准星上：落在框上=瞄准没问题（该查丢球机制），
        # 没落在框上=瞄准没收敛（该查灵敏度/光标锁定）。
        # 注意这是**截帧那一刻**的光标位置，即本次 tick 瞄准动作之前的状态，
        # 所以要看的永远是下一帧。
        if self._overlay_show_cursor:
            cursor = self._read_cursor()
            if cursor is not None:
                relative.append(self._marker("<cursor>", cursor[0], cursor[1], ox, oy))

        try:
            self._overlay.ensure(self._hwnd, *self._last_rect)
            self._overlay.render(relative, frame_w, frame_h)
        except Exception as e:
            # 覆盖层只是调试辅助，画不出来不该影响主流程
            logger.warning("覆盖层渲染失败，已禁用: %s", e)
            self._overlay = None

    @staticmethod
    def _marker(name: str, screen_x: float, screen_y: float,
                ox: int, oy: int, size: int = 10) -> Detection:
        """造一个只用于覆盖层显示的标记框（屏幕坐标 → 帧内坐标）。

        score=0 + threshold=1.0 触发 is_low_score，画成黄框，与绿色检测框区分。
        """
        return Detection(
            name=name,
            category="debug",
            score=0.0,
            threshold=1.0,
            x=int(screen_x - ox - size / 2),
            y=int(screen_y - oy - size / 2),
            w=size,
            h=size,
        )

    @staticmethod
    def _read_cursor() -> Optional[tuple]:
        """读取当前光标屏幕坐标；失败返回 None。"""
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        try:
            pt = POINT()
            if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return None
            return pt.x, pt.y
        except Exception:
            return None

    def _run_detectors(self, frame, origin_x: int = 0, origin_y: int = 0) -> List[Detection]:
        """在所有检测器上运行推理，合并结果。

        检测器返回的坐标相对截帧左上角，这里加上截取区域原点，
        统一转换为屏幕绝对坐标，供动作层直接点击。
        """
        all_detections: List[Detection] = []

        for name, detector in self._detectors.items():
            try:
                results = detector.detect(frame)
                for r in results:
                    # 将 Spotting 的 Detection 转换为本框架的 Detection（屏幕坐标）
                    all_detections.append(Detection(
                        name=r.name,
                        category=r.category,
                        score=r.score,
                        x=r.x + origin_x,
                        y=r.y + origin_y,
                        w=r.w,
                        h=r.h,
                        threshold=r.threshold,
                    ))
            except Exception as e:
                logger.error("检测器 %s 执行失败: %s", name, e)

        return all_detections

    def _build_detectors(self) -> Dict[str, Any]:
        """根据配置构建检测器实例。

        检测器配置来自两处，主框架的 `spotting.detectors` **按名字覆盖**子模块
        config.json 里的同名条目（未提及的保留）。这样可以只关掉用不上的检测器，
        例如捕猎工作流不需要模板匹配，关掉它能把单帧耗时从 ~290ms 降到 ~56ms。
        """
        from detectors.base import DetectorRegistry

        detectors: Dict[str, Any] = {}

        spotting_cfg = self._load_spotting_config()
        detector_configs: Dict[str, Any] = dict(spotting_cfg.get("detectors", {}))

        overrides = self._config.get("detectors") or {}
        if overrides:
            detector_configs.update(overrides)
            logger.info(
                "主框架配置覆盖检测器: %s",
                ", ".join(
                    f"{name}(enabled={cfg.get('enabled', True)})"
                    for name, cfg in overrides.items()
                ),
            )

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
