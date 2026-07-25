"""Clicker 子模块适配器 — 复用 Clicker 的 Interception 驱动和动作执行引擎。

通过 sys.path 注入子模块路径，直接复用其 ActionExecutor 和动作数据类。
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Clicker 子模块路径
CLICKER_DIR = Path(__file__).resolve().parent.parent / "submodules" / "RocoKingdom-Clicker"


class ClickerAdapter:
    """Clicker 键鼠控制适配器。

    封装 Clicker 子模块的 ActionExecutor，提供简洁的动作执行接口。

    用法：
        adapter = ClickerAdapter()
        adapter.setup()
        adapter.click(900, 500)
        adapter.press_key(32)  # 空格
        adapter.execute_raw({"type": "click", "x": 900, "y": 500})
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or {}
        self._executor = None  # Clicker 的 ActionExecutor
        self._script_manager = None  # Clicker 的 ActionScriptManager
        self._initialized = False
        self._move_mouse = self._config.get("move_mouse", True)

    def setup(self) -> None:
        """初始化 Clicker 子系统。"""
        clicker_str = str(CLICKER_DIR)
        if clicker_str not in sys.path:
            sys.path.insert(0, clicker_str)

        try:
            from ActionScript import ActionExecutor, ActionScriptManager

            self._executor = ActionExecutor()

            # 设置 move_mouse 配置
            if hasattr(self._executor, 'clicker') and self._executor.clicker:
                self._executor.clicker.config.move_mouse = self._move_mouse

            # 初始化脚本管理器（用于加载 Clicker 原生 JSON 脚本）
            data_dir = CLICKER_DIR / "data"
            if data_dir.exists():
                self._script_manager = ActionScriptManager(data_dir)

            self._initialized = True
            logger.info("Clicker 适配器初始化完成")

        except ImportError as e:
            logger.error("Clicker 子模块导入失败: %s", e)
            logger.error("请确保子模块已正确克隆: git submodule update --init")
            raise

    @property
    def is_available(self) -> bool:
        return self._initialized and self._executor is not None

    def execute_action(self, action_dict: Dict[str, Any]) -> bool:
        """执行单个动作（JSON dict 格式）。"""
        if not self._initialized:
            return False

        action_obj = self._parse_action(action_dict)
        if action_obj is None:
            return False

        return self._executor.execute_sequence([action_obj])

    def execute_sequence(
        self,
        actions: List[Dict[str, Any]],
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> bool:
        """执行动作序列（JSON dict 列表）。"""
        if not self._initialized:
            return False

        action_objs = []
        for action_dict in actions:
            obj = self._parse_action(action_dict)
            if obj is not None:
                action_objs.append(obj)

        if not action_objs:
            return True

        return self._executor.execute_sequence(action_objs, stop_event, pause_event)

    def click(self, x: int, y: int, hold_ms: int = 100) -> bool:
        """快捷方法：点击指定坐标。"""
        return self.execute_action({
            "type": "click",
            "x": x,
            "y": y,
            "hold_ms": hold_ms,
        })

    def move_to(self, x: int, y: int, duration_ms: int = 100) -> bool:
        """快捷方法：移动鼠标到指定坐标。"""
        return self.execute_action({
            "type": "move",
            "x": x,
            "y": y,
            "duration_ms": duration_ms,
        })

    def press_key(self, vk_code: int, hold_ms: int = 50) -> bool:
        """快捷方法：按下并释放一个键。"""
        return self.execute_action({
            "type": "key",
            "vk_code": vk_code,
            "hold_ms": hold_ms,
        })

    def press_combo(self, vk_codes: List[int], hold_ms: int = 50) -> bool:
        """快捷方法：按下组合键。"""
        return self.execute_action({
            "type": "combo",
            "vk_codes": vk_codes,
            "hold_ms": hold_ms,
        })

    def wait(self, duration_ms: int) -> bool:
        """快捷方法：等待。"""
        return self.execute_action({
            "type": "wait",
            "duration_ms": duration_ms,
        })

    def load_and_execute_script(
        self,
        script_name: str,
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> bool:
        """加载并执行 Clicker 原生 JSON 脚本。"""
        if not self._script_manager:
            logger.error("脚本管理器未初始化")
            return False

        actions = self._script_manager.load_script(script_name)
        if not actions:
            return False

        return self._executor.execute_sequence(actions, stop_event, pause_event)

    def cleanup(self) -> None:
        """清理资源。"""
        self._initialized = False
        logger.info("Clicker 适配器已清理")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _parse_action(self, action_dict: Dict[str, Any]) -> Any:
        """将 JSON dict 转换为 Clicker 的动作对象。"""
        if self._script_manager:
            return self._script_manager._parse_action(action_dict)

        # 回退：直接构造
        from ActionScript import (
            ClickAction, MoveAction, KeyAction,
            ComboAction, WaitAction, LoopAction, TimedAction,
        )

        action_type = action_dict.get("type")
        if action_type == "click":
            return ClickAction(
                action_dict["x"], action_dict["y"],
                action_dict.get("hold_ms", 100),
                action_dict.get("x_jitter_px", 0),
                action_dict.get("y_jitter_px", 0),
                action_dict.get("hold_jitter_ms", 0),
            )
        elif action_type == "move":
            return MoveAction(
                action_dict["x"], action_dict["y"],
                action_dict.get("duration_ms", 100),
                action_dict.get("x_jitter_px", 0),
                action_dict.get("y_jitter_px", 0),
                action_dict.get("duration_jitter_ms", 0),
            )
        elif action_type == "key":
            return KeyAction(
                action_dict["vk_code"],
                action_dict.get("hold_ms", 50),
                action_dict.get("hold_jitter_ms", 0),
            )
        elif action_type == "combo":
            return ComboAction(
                action_dict.get("vk_codes", []),
                action_dict.get("hold_ms", 50),
                action_dict.get("hold_jitter_ms", 0),
            )
        elif action_type == "wait":
            return WaitAction(
                action_dict["duration_ms"],
                action_dict.get("duration_jitter_ms", 0),
            )
        else:
            logger.warning("未知动作类型: %s", action_type)
            return None
