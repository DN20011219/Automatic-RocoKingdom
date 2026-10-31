"""Clicker 子模块适配器 — 复用 Clicker 的 Interception 驱动和动作执行引擎。

通过 sys.path 注入子模块路径，直接复用其 ActionExecutor 和动作数据类。
"""

from __future__ import annotations

import ctypes
import logging
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Clicker 子模块路径
CLICKER_DIR = Path(__file__).resolve().parent.parent / "submodules" / "RocoKingdom-Clicker"


# ─── SendInput 数据结构（用户模式回退，Interception 驱动未安装时使用） ───
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KeybdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeybdInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _InputUnion)]


# 鼠标事件标志
_MF_MOVE = 0x0001
_MF_LEFTDOWN = 0x0002
_MF_LEFTUP = 0x0004
_MF_ABSOLUTE = 0x8000
_MF_VIRTUALDESK = 0x4000
# 键盘事件标志
_KF_KEYUP = 0x0002


def _send_inputs(inputs: List[_Input]) -> None:
    """批量发送 SendInput 事件。"""
    n = len(inputs)
    arr = (_Input * n)(*inputs)
    ctypes.windll.user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_Input))


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

        # Interception dll 随子模块 third 目录自带，但 Clicker 的 ActionExecutor
        # 只搜索“同目录 + CWD”，这里把 dll 目录注册进进程搜索路径
        import platform
        arch = "x64" if platform.architecture()[0] == "64bit" else "x86"
        dll_dir = CLICKER_DIR / "third" / "Interception" / "library" / arch
        if not (dll_dir / "interception.dll").exists():
            dll_dir = CLICKER_DIR / "third" / "Interception" / "library" / "x64"
        if (dll_dir / "interception.dll").exists():
            ctypes.windll.kernel32.SetDllDirectoryW(str(dll_dir))

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

        # Interception 驱动不可用时回退到 SendInput 用户模式
        if not self._interception_available:
            return self._execute_action_user_mode(action_dict)

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

        # 用户模式：逐个动作走 SendInput 回退路径
        if not self._interception_available:
            for action_dict in actions:
                if stop_event and stop_event.is_set():
                    return False
                if not self._execute_action_user_mode(action_dict):
                    return False
            return True

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

    def move_to_planned(
        self,
        x: int,
        y: int,
        duration_ms: int = 300,
        strategy: str = "fitts",
        num_steps: int = 20,
    ) -> bool:
        """使用 PathPlanner 路径规划器拟人移动鼠标到目标坐标。

        生成一条从当前鼠标位置到 (x, y) 的拟人化路径，
        通过 Interception 驱动逐步发送相对移动事件。

        Args:
            x: 目标屏幕坐标 X。
            y: 目标屏幕坐标 Y。
            duration_ms: 移动总耗时（毫秒）。
            strategy: 路径扰动策略 'fitts'（拟人化）或 'sine'（正弦弧线）。
            num_steps: 路径分段数（越多越平滑，默认 20）。
        """
        if not self._initialized:
            return False

        # 用户模式无相对移动注入能力，回退到绝对坐标移动
        if not self._interception_available:
            self._si_move(x, y)
            return True

        try:
            from PathPlanner import PathPlanner
        except ImportError:
            logger.warning("PathPlanner 导入失败，回退到直接移动")
            return self.move_to(x, y, duration_ms)

        # 获取当前鼠标位置
        cur_x, cur_y = self._get_cursor_pos()
        total_dx = x - cur_x
        total_dy = y - cur_y

        dist = math.sqrt(total_dx * total_dx + total_dy * total_dy)
        if dist < 3.0:
            # 距离极短，无需移动
            return True

        # 生成线性插值的 base move 事件
        step_interval = duration_ms / 1000.0 / num_steps
        t = 0.0
        original_moves: List[Dict[str, Any]] = []
        for i in range(num_steps):
            t += step_interval
            original_moves.append({
                "t": t,
                "type": "move",
                "x": total_dx // num_steps,
                "y": total_dy // num_steps,
            })

        # 使用 PathPlanner 生成扰动路径
        planner = PathPlanner(strategy=strategy)
        planned_moves = planner.generate_path(total_dx, total_dy, original_moves)

        if not planned_moves:
            return self.move_to(x, y, duration_ms)

        # 通过 Interception 逐步执行相对移动
        return self._execute_relative_path(planned_moves, step_interval)

    def move_relative(self, dx: int, dy: int, duration_ms: int = 0, steps: int = 1) -> bool:
        """相对位移移动鼠标 —— 用于 3D 游戏转视角（mouse-look）。

        与 move_to / move_to_planned 的绝对定位不同，这里发送的是相对位移事件，
        游戏将其解释为视角转动，屏幕上的鼠标指针也会同步偏移。

        **默认单事件发送**（steps=1），这是刻意的设计：游戏对"一个大位移事件"
        和"N 个微位移事件"的响应并不等价（存在每事件最小响应量与合并策略），
        把位移拆碎会让实际转视角幅度与标定值对不上。慢速扫视应当靠
        "小位移 + 事件之间 sleep"实现，而不是把一个位移拆成微步。

        Args:
            dx: 水平位移量（正=右，负=左）。
            dy: 垂直位移量（正=下，负=上）。
            duration_ms: 完成整段位移的总耗时（毫秒）。
            steps: 拆分的子位移数量；0 表示按每 ~10ms 一步自动推算。
        """
        if not self._initialized:
            return False

        dx = int(dx)
        dy = int(dy)
        if dx == 0 and dy == 0:
            return True

        duration_ms = max(int(duration_ms), 0)
        if steps <= 0:
            steps = max(1, duration_ms // 10) if duration_ms > 0 else 1

        # 整数均分 + 余数分摊，保证累计位移精确等于 (dx, dy)
        # divmod 对负数向下取整且余数恒为 [0, steps)，前 rem 步各补 1 即可精确对齐
        base_x, rem_x = divmod(dx, steps)
        base_y, rem_y = divmod(dy, steps)
        step_interval = (duration_ms / 1000.0) / steps if steps > 0 else 0.0

        use_driver = self._interception_available
        stroke_cls = None
        if use_driver:
            # 必须与 ActionExecutor 使用同一个类定义，否则 ctypes 数组初始化会报类型冲突
            try:
                from ActionScript import InterceptionMouseStroke as stroke_cls
            except ImportError:
                try:
                    from InterceptionCore import InterceptionMouseStroke as stroke_cls
                except ImportError:
                    use_driver = False

        for i in range(steps):
            sx = base_x + (1 if i < rem_x else 0)
            sy = base_y + (1 if i < rem_y else 0)

            if use_driver:
                stroke = stroke_cls()
                stroke.state = 0
                stroke.flags = 0  # 相对移动
                stroke.rolling = 0
                stroke.x = int(sx)
                stroke.y = int(sy)
                stroke.information = 0
                self._executor._send_mouse_stroke(stroke)
            else:
                self._si_move_relative(int(sx), int(sy))

            if step_interval > 0 and i < steps - 1:
                time.sleep(step_interval)

        return True

    def hold_left(self, hold_ms: int = 1200) -> bool:
        """在当前鼠标位置长按左键（按下 → 保持 → 松开），全程不移动鼠标。

        与 click(x, y, hold_ms) 的区别：click 会先把指针绝对定位到 (x, y)，
        在 3D 游戏里这次定位本身就会转动视角；本方法只按下/松开，
        适合"先瞄准、再蓄力丢球"的两段式操作。

        Args:
            hold_ms: 按住时长（毫秒），即蓄力时间。
        """
        if not self._initialized:
            return False

        hold_ms = max(int(hold_ms), 1)

        if not self._interception_available:
            self._si_left_down()
            time.sleep(hold_ms / 1000.0)
            self._si_left_up()
            return True

        try:
            from ActionScript import InterceptionMouseStroke
        except ImportError:
            from InterceptionCore import InterceptionMouseStroke

        down = InterceptionMouseStroke()
        down.state = 0x001  # 左键按下
        down.flags = 0
        down.rolling = 0
        down.x = 0
        down.y = 0
        down.information = 0

        up = InterceptionMouseStroke()
        up.state = 0x002  # 左键松开
        up.flags = 0
        up.rolling = 0
        up.x = 0
        up.y = 0
        up.information = 0

        executor = self._executor
        if not executor._send_mouse_stroke(down):
            logger.error("长按左键失败：无法发送按下事件")
            return False

        time.sleep(hold_ms / 1000.0)

        if not executor._send_mouse_stroke(up):
            # 松手失败会让左键卡在按下状态（游戏里持续蓄力 / 桌面上一直拖拽），必须显式报错
            logger.error("长按左键失败：无法发送松开事件，左键可能仍处于按下状态")
            return False
        return True

    def left_down(self) -> bool:
        """左键按下但不松开 —— 用于"按住期间还要做别的事"（如蓄力时转视角）。

        必须与 left_up 配对调用；调用方异常退出时要保证 finally 里松开，
        否则左键会一直卡在按下状态。
        """
        if not self._initialized:
            return False

        if not self._interception_available:
            self._si_left_down()
            return True

        try:
            from ActionScript import InterceptionMouseStroke
        except ImportError:
            from InterceptionCore import InterceptionMouseStroke

        stroke = InterceptionMouseStroke()
        stroke.state = 0x001  # 左键按下
        stroke.flags = 0
        stroke.rolling = 0
        stroke.x = 0
        stroke.y = 0
        stroke.information = 0
        return bool(self._executor._send_mouse_stroke(stroke))

    def left_up(self) -> bool:
        """左键松开，与 left_down 配对。"""
        if not self._initialized:
            return False

        if not self._interception_available:
            self._si_left_up()
            return True

        try:
            from ActionScript import InterceptionMouseStroke
        except ImportError:
            from InterceptionCore import InterceptionMouseStroke

        stroke = InterceptionMouseStroke()
        stroke.state = 0x002  # 左键松开
        stroke.flags = 0
        stroke.rolling = 0
        stroke.x = 0
        stroke.y = 0
        stroke.information = 0
        return bool(self._executor._send_mouse_stroke(stroke))

    def move_relative_planned(
        self,
        dx: int,
        dy: int,
        duration_ms: int = 200,
        strategy: str = "fitts",
        num_steps: int = 20,
    ) -> bool:
        """用 PathPlanner 生成拟人轨迹，执行一段**相对位移**。

        与 move_to_planned 的区别：那个要先读当前光标位置再算"到绝对坐标的差量"，
        在隐藏/锁定光标的 3D 游戏里读到的值是假的；这个直接接受相对量，
        不依赖任何绝对坐标，所以适用于"转视角"这种纯相对操作。

        PathPlanner 保证扰动后的各步位移之和**精确等于** (dx, dy)（最后一步校正），
        因此净位移与单事件版本一致，差别只在轨迹形状与时间分布。

        注意：游戏对"N 个微步"和"1 个大步"的响应不一定等价，所以用本方法时
        必须用**同样的分段方式**重新标定比率（tools/calibrate_aim.py 会读工作流
        配置自动对齐），否则测出来的比率对不上实际执行。

        Args:
            dx: 水平相对位移（正=右）。
            dy: 垂直相对位移（正=下）。
            duration_ms: 走完整条轨迹的总耗时。
            strategy: PathPlanner 扰动策略，'fitts'（加减速+过冲+微抖）| 'sine'（正弦弧线）。
            num_steps: 轨迹分段数，越多越平滑。
        """
        if not self._initialized:
            return False

        dx = int(dx)
        dy = int(dy)
        if dx == 0 and dy == 0:
            return True

        if math.sqrt(dx * dx + dy * dy) < 3.0:
            # 位移过短，规划没有意义，直接单事件发出
            return self.move_relative(dx, dy, 0, steps=1)

        if not self._interception_available:
            # 用户模式没有分段相对注入的价值，退回等分的相对移动
            return self.move_relative(dx, dy, duration_ms, steps=num_steps)

        try:
            from PathPlanner import PathPlanner
        except ImportError:
            logger.warning("PathPlanner 导入失败，退回单事件相对位移")
            return self.move_relative(dx, dy, 0, steps=1)

        num_steps = max(2, int(num_steps))
        duration_ms = max(int(duration_ms), num_steps)
        step_interval = duration_ms / 1000.0 / num_steps

        original_moves: List[Dict[str, Any]] = []
        for i in range(num_steps):
            original_moves.append({
                "t": step_interval * (i + 1),
                "type": "move",
                "x": dx // num_steps,
                "y": dy // num_steps,
            })

        planner = PathPlanner(strategy=strategy)
        planned_moves = planner.generate_path(dx, dy, original_moves)
        if not planned_moves:
            return self.move_relative(dx, dy, 0, steps=1)

        return self._execute_relative_path(planned_moves, step_interval)

    def _execute_relative_path(
        self,
        moves: List[Dict[str, Any]],
        step_interval: float,
    ) -> bool:
        """通过 Interception 驱动执行一系列相对鼠标移动。"""
        executor = self._executor
        if executor is None or not hasattr(executor, '_send_mouse_stroke'):
            # 回退：直接用绝对移动
            last_x = sum(m.get("x", 0) for m in moves)
            last_y = sum(m.get("y", 0) for m in moves)
            cur_x, cur_y = self._get_cursor_pos()
            return self.move_to(cur_x + last_x, cur_y + last_y, int(step_interval * len(moves) * 1000))

        try:
            # 必须与 ActionExecutor 使用同一个类定义，否则 ctypes 数组初始化会报类型冲突
            from ActionScript import InterceptionMouseStroke
        except ImportError:
            from InterceptionCore import InterceptionMouseStroke

        for move in moves:
            stroke = InterceptionMouseStroke()
            stroke.state = 0
            stroke.flags = 0  # 相对移动
            stroke.rolling = 0
            stroke.x = int(move.get("x", 0))
            stroke.y = int(move.get("y", 0))
            stroke.information = 0
            executor._send_mouse_stroke(stroke)
            time.sleep(step_interval)

        return True

    @staticmethod
    def cursor_pos() -> tuple:
        """当前鼠标的屏幕绝对坐标（公开接口）。

        相对位移瞄准需要它来算「光标 → 目标」的差量。
        注意：游戏若隐藏或锁定光标，返回值可能不更新，此时应改用
        aim_strategy: "planned" / "direct" 的绝对定位方式。
        """
        return ClickerAdapter._get_cursor_pos()

    @staticmethod
    def _get_cursor_pos() -> tuple:
        """获取当前鼠标屏幕坐标。"""
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    # ------------------------------------------------------------------
    # 用户模式回退（Interception 驱动未安装时用 SendInput 注入）
    # ------------------------------------------------------------------

    @property
    def _interception_available(self) -> bool:
        """Interception 驱动是否可用。"""
        return self._executor is not None and getattr(self._executor, "_lib", None) is not None

    def _execute_action_user_mode(self, action_dict: Dict[str, Any]) -> bool:
        """用户模式下用 SendInput 执行基础动作（屏幕绝对坐标）。"""
        action_type = action_dict.get("type")

        if action_type == "click":
            self._si_click(
                action_dict["x"], action_dict["y"],
                action_dict.get("hold_ms", 100),
            )
            return True
        if action_type == "move":
            self._si_move(action_dict["x"], action_dict["y"])
            return True
        if action_type == "key":
            self._si_key(action_dict["vk_code"], action_dict.get("hold_ms", 50))
            return True
        if action_type == "combo":
            self._si_combo(action_dict.get("vk_codes", []), action_dict.get("hold_ms", 50))
            return True
        if action_type == "wait":
            time.sleep(action_dict.get("duration_ms", 0) / 1000.0)
            return True

        logger.warning("用户模式不支持的动作类型: %s", action_type)
        return False

    def _si_move(self, x: int, y: int) -> None:
        """SendInput 绝对坐标移动（虚拟桌面归一化，支持多显示器）。"""
        user32 = ctypes.windll.user32
        vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        vw = max(user32.GetSystemMetrics(78), 2) - 1  # SM_CXVIRTUALSCREEN
        vh = max(user32.GetSystemMetrics(79), 2) - 1  # SM_CYVIRTUALSCREEN

        inp = _Input()
        inp.type = _INPUT_MOUSE
        inp.u.mi.dx = int((x - vx) * 65535 / vw)
        inp.u.mi.dy = int((y - vy) * 65535 / vh)
        inp.u.mi.dwFlags = _MF_MOVE | _MF_ABSOLUTE | _MF_VIRTUALDESK
        _send_inputs([inp])

    def _si_move_relative(self, dx: int, dy: int) -> None:
        """SendInput 相对位移移动（不带 ABSOLUTE 标志，等价于 mouse-look 输入）。"""
        inp = _Input()
        inp.type = _INPUT_MOUSE
        inp.u.mi.dx = int(dx)
        inp.u.mi.dy = int(dy)
        inp.u.mi.dwFlags = _MF_MOVE
        _send_inputs([inp])

    def _si_left_down(self) -> None:
        """SendInput 左键按下。"""
        inp = _Input()
        inp.type = _INPUT_MOUSE
        inp.u.mi.dwFlags = _MF_LEFTDOWN
        _send_inputs([inp])

    def _si_left_up(self) -> None:
        """SendInput 左键松开。"""
        inp = _Input()
        inp.type = _INPUT_MOUSE
        inp.u.mi.dwFlags = _MF_LEFTUP
        _send_inputs([inp])

    def _si_click(self, x: int, y: int, hold_ms: int) -> None:
        """SendInput 移动 + 左键按下/释放。"""
        self._si_move(x, y)
        time.sleep(0.02)

        down = _Input()
        down.type = _INPUT_MOUSE
        down.u.mi.dwFlags = _MF_LEFTDOWN
        up = _Input()
        up.type = _INPUT_MOUSE
        up.u.mi.dwFlags = _MF_LEFTUP

        _send_inputs([down])
        time.sleep(max(hold_ms, 1) / 1000.0)
        _send_inputs([up])

    def _si_key(self, vk_code: int, hold_ms: int) -> None:
        """SendInput 按键按下/释放。"""
        down = _Input()
        down.type = _INPUT_KEYBOARD
        down.u.ki.wVk = vk_code
        up = _Input()
        up.type = _INPUT_KEYBOARD
        up.u.ki.wVk = vk_code
        up.u.ki.dwFlags = _KF_KEYUP

        _send_inputs([down])
        time.sleep(max(hold_ms, 1) / 1000.0)
        _send_inputs([up])

    def _si_combo(self, vk_codes: List[int], hold_ms: int) -> None:
        """SendInput 组合键：顺序按下、逆序释放。"""
        downs = []
        for vk in vk_codes:
            inp = _Input()
            inp.type = _INPUT_KEYBOARD
            inp.u.ki.wVk = vk
            downs.append(inp)
        ups = []
        for vk in reversed(vk_codes):
            inp = _Input()
            inp.type = _INPUT_KEYBOARD
            inp.u.ki.wVk = vk
            inp.u.ki.dwFlags = _KF_KEYUP
            ups.append(inp)

        _send_inputs(downs)
        time.sleep(max(hold_ms, 1) / 1000.0)
        _send_inputs(ups)

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
