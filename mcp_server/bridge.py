"""RocoBridge — MCP 服务桥接层（同步门面）。

统一封装框架的全部能力，供 ``server.py`` 的 MCP 工具经
``asyncio.to_thread`` 调用。所有方法为同步方法；失败时抛出
:class:`BridgeError`，由工具层转换为统一错误信封。

双模式（由 ``config.host_engine`` 决定，启动时固定）：
- 桥接模式：自持 RuntimeContext + EventBus + SpottingAdapter
  + ProtocolSource + ClickerAdapter；
- 引擎模式：托管 :class:`core.engine.Engine`，全部读写路由到引擎子系统，
  并额外提供工作流控制能力。

详见设计文档 §5 / §7。
"""

from __future__ import annotations

import io
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import cv2
from PIL import Image

from core.context import RuntimeContext
from core.events import EventBus
from core.lifecycle import EngineState
from mcp_server.config import McpServerConfig
from mcp_server.keys import resolve_key
from mcp_server.safety import SafetyGuard

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = PROJECT_ROOT / "config" / "workflows"

# wait 工具单次最大等待时长（毫秒）
_WAIT_MAX_MS = 30000

# stop_engine 等待引擎线程退出的超时（秒）
_ENGINE_STOP_TIMEOUT = 5.0


class BridgeError(Exception):
    """桥接层统一异常，携带错误码供工具层构造错误信封。"""

    def __init__(self, error_code: str, message: str) -> None:
        """初始化。

        Args:
            error_code: 简短错误码（见设计文档 §4 错误码表）。
            message: 人类可读的错误描述。
        """
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class RocoBridge:
    """MCP 桥接门面。

    用法：
        bridge = RocoBridge(config, global_config_path)
        bridge.setup()
        status = bridge.get_status()
        bridge.click(900, 500, 100)
        bridge.cleanup()
    """

    def __init__(
        self,
        config: McpServerConfig,
        global_config_path: Optional[str] = None,
    ) -> None:
        """初始化（不做任何重活，重活在 :meth:`setup`）。

        Args:
            config: MCP 服务器配置。
            global_config_path: 框架全局配置路径（传给 Engine / config_loader）。
        """
        self._config = config
        self._global_config_path = global_config_path

        # 通用组件（两种模式都有）
        self._safety = SafetyGuard(config.safety)
        self._lock = threading.Lock()  # 适配器调用互斥
        self._initialized = False
        self._global_cfg_cache: Optional[Dict[str, Any]] = None

        # 桥接模式组件（host_engine=False 时创建）
        self._context: Optional[RuntimeContext] = None
        self._event_bus: Optional[EventBus] = None
        self._perception = None       # SpottingAdapter
        self._protocol_source = None  # ProtocolSource
        self._clicker = None          # ClickerAdapter

        # 引擎模式组件（host_engine=True 时创建）
        self._engine = None           # core.engine.Engine

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        """运行模式：``"bridge"`` 或 ``"engine"``。"""
        return "engine" if self._config.host_engine else "bridge"

    def setup(self) -> None:
        """按模式初始化子系统。

        - 桥接模式：创建 context/event_bus，初始化 SpottingAdapter、
          ProtocolSource、ClickerAdapter；
        - 引擎模式：创建 Engine 并 ``setup()``（不启动主循环）。

        Raises:
            BridgeError: 子系统初始化失败（error_code=not_initialized）。
        """
        if self._initialized:
            return

        if self._config.host_engine:
            from core.engine import Engine

            self._engine = Engine(config_path=self._global_config_path)
            try:
                self._engine.setup()
            except Exception as e:
                self._engine = None
                raise BridgeError("not_initialized", f"托管引擎初始化失败: {e}")
            logger.info("引擎托管模式：Engine 子系统初始化完成")
        else:
            self._event_bus = EventBus()
            self._context = RuntimeContext()
            global_cfg = self._get_global_config()
            try:
                from action.adapter import ClickerAdapter
                from perception.adapter import SpottingAdapter
                from perception.protocol_source import ProtocolSource

                self._perception = SpottingAdapter(global_cfg.get("spotting", {}))
                self._perception.setup()

                self._protocol_source = ProtocolSource(
                    global_cfg.get("protocol", {}), self._context, self._event_bus
                )
                self._protocol_source.setup()

                self._clicker = ClickerAdapter(global_cfg.get("clicker", {}))
                self._clicker.setup()
            except Exception as e:
                raise BridgeError("not_initialized", f"桥接模式子系统初始化失败: {e}")
            logger.info("桥接模式：感知/协议/操作子系统初始化完成")

        self._initialized = True

    def cleanup(self) -> None:
        """清理全部资源：停止引擎、协议源，清理适配器。幂等。"""
        if not self._initialized:
            return

        try:
            if self._engine is not None:
                if self._engine_running():
                    self._engine.stop()
                # 引擎从未运行时 run() 的 finally 不会执行，手动清理协议源
                protocol = self._engine.protocol_source
                if protocol is not None:
                    protocol.cleanup()
            else:
                if self._protocol_source is not None:
                    self._protocol_source.cleanup()
                if self._perception is not None:
                    self._perception.cleanup()
                if self._clicker is not None:
                    self._clicker.cleanup()
        finally:
            self._initialized = False
            logger.info("RocoBridge 已清理")

    # ------------------------------------------------------------------
    # 状态与读取
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """返回系统整体状态（窗口/感知/协议/操作/引擎）。

        Returns:
            状态字典，结构见设计文档 §4.1 ``get_status``。
        """
        if not self._initialized:
            raise BridgeError("not_initialized", "桥接层未初始化，请先调用 setup()")

        perception = self._active_perception
        status: Dict[str, Any] = {
            "mode": self.mode,
            "window": {
                "found": perception.window_hwnd is not None,
                "hwnd": perception.window_hwnd,
                "keyword": perception.window_keyword,
            },
            "perception": {
                "available": perception.is_available,
                "detectors": perception.detector_names,
            },
        }

        protocol = self._active_protocol_source
        status["protocol"] = protocol.stats() if protocol else {"enabled": False}

        clicker = self._active_clicker
        status["action"] = {
            "available": clicker is not None and clicker.is_available,
            "allow_actions": self._safety.allow_actions,
        }

        if self._config.host_engine and self._engine is not None:
            ctx = self._engine.context
            executor = self._engine.workflow_executor
            status["engine"] = {
                "enabled": True,
                "state": self._engine.state.name,
                "workflow": ctx.workflow_name or None,
                "step": ctx.step_name or None,
                "iteration": ctx.iteration,
                "workflow_complete": bool(executor and executor.is_complete),
            }
        else:
            status["engine"] = {"enabled": False}

        return status

    def get_runtime_state(self) -> Dict[str, Any]:
        """返回运行时上下文快照（变量/计数器/场景/步骤等）。

        Returns:
            状态字典，结构见设计文档 §4.1 ``get_runtime_state``。
        """
        context = self._active_context
        return {
            "workflow_name": context.workflow_name,
            "step_name": context.step_name,
            "prev_step_name": context.prev_step_name,
            "current_scene": context.current_scene,
            "iteration": context.iteration,
            "elapsed_seconds": round(context.elapsed_seconds(), 1),
            "variables": dict(context.variables),
            "counters": dict(context.counters),
        }

    def capture_screenshot_png(self, max_width: int = 0) -> bytes:
        """截取游戏窗口一帧并编码为图像字节流。

        Args:
            max_width: 输出最大宽度；0 表示使用配置默认值。

        Returns:
            PNG/JPEG 编码后的字节流（格式由配置决定）。

        Raises:
            BridgeError: 窗口不存在/在后台/截图失败（error_code=window_not_found）。
        """
        with self._lock:
            frame = self._active_perception.capture_frame()

        if frame is None:
            raise BridgeError(
                "window_not_found",
                "截图失败：游戏窗口不存在或不在前台（请将游戏窗口置于前台后重试）",
            )

        # 帧为 BGR ndarray，转换为 PIL RGB 图像
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        cfg = self._config.screenshot
        limit = max_width if max_width and max_width > 0 else cfg.max_width
        if limit and image.width > limit:
            scale = limit / image.width
            image = image.resize((limit, max(1, int(image.height * scale))))

        buffer = io.BytesIO()
        if cfg.format == "jpeg":
            image.convert("RGB").save(buffer, "JPEG", quality=cfg.jpeg_quality)
        else:
            image.save(buffer, "PNG")
        return buffer.getvalue()

    def detect(
        self,
        name: Optional[str] = None,
        min_score: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """对当前帧执行一次视觉检测。

        引擎运行中直接读引擎上下文快照；否则手动检测并刷新上下文。

        Args:
            name: 目标名过滤；None 返回全部。
            min_score: 最低置信度。

        Returns:
            检测结果 dict 列表（含 cx/cy 中心点，屏幕绝对坐标）。
        """
        with self._lock:
            if self._engine_running():
                detections = list(self._active_context.last_detections)
            else:
                detections = self._active_perception.detect_frame()
                self._active_context.last_detections = detections
                self._active_context.frame_seq += 1
                self._active_context.frame_captured = getattr(
                    self._active_perception, "last_capture_ok", True
                )
                self._active_context.frame_rect = getattr(
                    self._active_perception, "last_rect", (0, 0, 0, 0)
                )

        results: List[Dict[str, Any]] = []
        for det in detections:
            if name and det.name != name:
                continue
            if det.score < min_score:
                continue
            results.append({
                "name": det.name,
                "category": det.category,
                "score": round(float(det.score), 3),
                "x": det.x, "y": det.y, "w": det.w, "h": det.h,
                "cx": det.cx, "cy": det.cy,
            })
        return results

    def get_protocol_events(
        self,
        limit: int = 20,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        """查询最近的 RKPP 协议事件。

        Args:
            limit: 最多返回条数（最新在最后）。
            keyword: 子串过滤（不区分大小写）。

        Returns:
            ``{"connected": bool, "count": int, "events": [...]}``。
        """
        source = self._active_protocol_source
        if source is None:
            return {"connected": False, "count": 0, "events": []}

        # 带关键词时需要先取全量历史再过滤
        fetch = 200 if keyword else max(1, limit)
        events = source.recent_events(fetch)

        if keyword:
            needle = keyword.lower()
            fields = ("summary_kind", "opcode_name", "opencode",
                      "meaning", "summary_text")
            events = [
                e for e in events
                if any(needle in str(e.get(f, "")).lower() for f in fields)
            ]

        events = events[-max(0, limit):]
        return {"connected": source.connected, "count": len(events), "events": events}

    def list_workflows(self) -> List[Dict[str, Any]]:
        """扫描 ``config/workflows/*.yaml``，返回可用工作流列表。

        Returns:
            ``[{"name": 文件名, "path": 路径, "title": YAML 内 name}]``。
        """
        import yaml

        results: List[Dict[str, Any]] = []
        if not WORKFLOWS_DIR.exists():
            return results

        for path in sorted(WORKFLOWS_DIR.glob("*.yaml")):
            title = None
            try:
                with path.open("r", encoding="utf-8") as f:
                    title = (yaml.safe_load(f) or {}).get("name")
            except Exception:
                pass
            results.append({"name": path.stem, "path": str(path), "title": title})
        return results

    def get_action_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """返回最近的操作审计日志（委托安全层）。"""
        return self._safety.recent(limit)

    # ------------------------------------------------------------------
    # 操作（全部先过安全检查与引擎竞争检查）
    # ------------------------------------------------------------------

    def click(self, x: int, y: int, hold_ms: int = 100) -> None:
        """点击屏幕绝对坐标。

        Raises:
            BridgeError: 被安全层拒绝/引擎占用/执行失败。
        """
        self._run_action(
            "click",
            {"x": x, "y": y, "hold_ms": hold_ms},
            lambda: self._active_clicker.click(x, y, hold_ms),
        )

    def move_to(
        self,
        x: int,
        y: int,
        duration_ms: int = 200,
        humanized: bool = True,
    ) -> None:
        """移动鼠标到屏幕绝对坐标。

        Args:
            humanized: True 使用 PathPlanner 拟人路径，否则直接移动。
        """
        clicker = self._active_clicker
        if humanized:
            fn = lambda: clicker.move_to_planned(x, y, duration_ms)  # noqa: E731
        else:
            fn = lambda: clicker.move_to(x, y, duration_ms)  # noqa: E731
        self._run_action(
            "move_to",
            {"x": x, "y": y, "duration_ms": duration_ms, "humanized": humanized},
            fn,
        )

    def press_key(self, key: Union[str, int], hold_ms: int = 50) -> None:
        """按下并释放一个键。

        Args:
            key: 键名（如 "space"）或 vk_code 整数。

        Raises:
            BridgeError: 键名无法识别（error_code=invalid_param）。
        """
        try:
            vk_code = resolve_key(key)
        except KeyError as e:
            raise BridgeError("invalid_param", str(e))
        self._run_action(
            "press_key",
            {"key": key, "vk_code": vk_code, "hold_ms": hold_ms},
            lambda: self._active_clicker.press_key(vk_code, hold_ms),
        )

    def press_combo(self, keys: List[Union[str, int]], hold_ms: int = 50) -> None:
        """按下组合键（如 ["ctrl", "a"]）。"""
        if not keys:
            raise BridgeError("invalid_param", "keys 不能为空")
        try:
            vk_codes = [resolve_key(k) for k in keys]
        except KeyError as e:
            raise BridgeError("invalid_param", str(e))
        self._run_action(
            "press_combo",
            {"keys": keys, "vk_codes": vk_codes, "hold_ms": hold_ms},
            lambda: self._active_clicker.press_combo(vk_codes, hold_ms),
        )

    def wait(self, duration_ms: int) -> None:
        """等待。上限 30000ms，超限抛 invalid_param。"""
        if duration_ms <= 0:
            raise BridgeError("invalid_param", "duration_ms 必须为正数")
        if duration_ms > _WAIT_MAX_MS:
            raise BridgeError(
                "invalid_param",
                f"等待时长 {duration_ms}ms 超过上限 {_WAIT_MAX_MS}ms",
            )
        self._run_action(
            "wait",
            {"duration_ms": duration_ms},
            lambda: self._active_clicker.wait(duration_ms),
        )

    def execute_actions(self, actions: List[Dict[str, Any]]) -> None:
        """执行 Clicker JSON 格式动作序列。

        Args:
            actions: 动作列表，元素含 ``type`` 字段
                （click/move/key/combo/wait）。

        Raises:
            BridgeError: 序列超长/元素非法/被拒绝。
        """
        if not isinstance(actions, list) or not actions:
            raise BridgeError("invalid_param", "actions 必须为非空列表")
        self._safety.check_sequence(len(actions))
        for i, action in enumerate(actions):
            if not isinstance(action, dict) or "type" not in action:
                raise BridgeError(
                    "invalid_param",
                    f"actions[{i}] 格式非法：必须为含 type 字段的对象",
                )
        self._run_action(
            "execute_actions",
            {"count": len(actions), "actions": actions},
            lambda: self._active_clicker.execute_sequence(actions),
        )

    def run_script(self, script_name: str) -> None:
        """执行 Clicker 原生 JSON 脚本（data/action_scripts/ 下）。"""
        self._run_action(
            "run_script",
            {"script_name": script_name},
            lambda: self._active_clicker.load_and_execute_script(script_name),
        )

    # ------------------------------------------------------------------
    # 引擎控制（仅引擎模式；桥接模式抛 not_enabled）
    # ------------------------------------------------------------------

    def load_workflow(self, path: str) -> None:
        """加载工作流文件到托管引擎。

        Raises:
            BridgeError: 桥接模式（not_enabled）/文件不存在（invalid_param）
                /引擎运行中（engine_running）。
        """
        self._ensure_engine_mode("load_workflow")
        if self._engine_running():
            raise BridgeError(
                "engine_running",
                "引擎运行中，请先调用 stop_engine 再加载工作流",
            )

        wf_path = Path(path)
        if not wf_path.is_absolute():
            wf_path = PROJECT_ROOT / wf_path
        if not wf_path.exists():
            raise BridgeError("invalid_param", f"工作流文件不存在: {path}")

        try:
            self._engine.load_workflow(str(wf_path))
        except Exception as e:
            raise BridgeError("internal_error", f"工作流加载失败: {e}")

    def start_engine(self) -> None:
        """后台启动托管引擎执行已加载的工作流。

        子系统被上次运行清理过时自动重新初始化（幂等）。
        """
        self._ensure_engine_mode("start_engine")
        if self._engine_running():
            raise BridgeError("engine_running", "引擎已在运行中")
        if self._engine.workflow_executor is None:
            raise BridgeError(
                "invalid_param",
                "尚未加载工作流，请先调用 load_workflow",
            )

        # 上次 run() 结束时 _shutdown 会清理感知/协议子系统，重启前重新初始化
        perception = self._engine.perception
        if perception is None or not perception.is_available:
            try:
                self._engine.setup()
            except Exception as e:
                raise BridgeError("not_initialized", f"引擎重新初始化失败: {e}")

        self._engine.run_async()
        logger.info("托管引擎已后台启动: %s", self._engine.context.workflow_name)

    def pause_engine(self) -> None:
        """暂停托管引擎（仅 RUNNING 有效）。"""
        self._ensure_engine_mode("pause_engine")
        if not self._engine.pause():
            raise BridgeError(
                "invalid_param",
                f"暂停失败：当前引擎状态为 {self._engine.state.name}（仅 RUNNING 可暂停）",
            )

    def resume_engine(self) -> None:
        """恢复托管引擎（仅 PAUSED 有效）。"""
        self._ensure_engine_mode("resume_engine")
        if not self._engine.resume():
            raise BridgeError(
                "invalid_param",
                f"恢复失败：当前引擎状态为 {self._engine.state.name}（仅 PAUSED 可恢复）",
            )

    def stop_engine(self) -> None:
        """停止托管引擎并等待线程退出（最长 5s）。"""
        self._ensure_engine_mode("stop_engine")
        self._engine.stop()

        deadline = time.time() + _ENGINE_STOP_TIMEOUT
        while time.time() < deadline and self._engine_running():
            time.sleep(0.1)

        if self._engine_running():
            logger.warning("引擎线程未在 %.1fs 内退出", _ENGINE_STOP_TIMEOUT)

    def set_variable(self, name: str, value: Any) -> None:
        """写入工作流变量（引擎上下文）。"""
        self._ensure_engine_mode("set_variable")
        self._engine.context.set_variable(name, value)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @property
    def _active_context(self) -> RuntimeContext:
        """当前生效的运行时上下文（引擎模式优先用引擎上下文）。"""
        if self._config.host_engine and self._engine is not None:
            return self._engine.context
        if self._context is None:
            raise BridgeError("not_initialized", "运行时上下文未初始化")
        return self._context

    @property
    def _active_perception(self):
        """当前生效的感知适配器（引擎模式返回引擎的）。"""
        perception = (
            self._engine.perception
            if self._config.host_engine and self._engine is not None
            else self._perception
        )
        if perception is None:
            raise BridgeError("not_initialized", "感知适配器未初始化")
        return perception

    @property
    def _active_protocol_source(self):
        """当前生效的协议信息源（引擎模式返回引擎的）。"""
        if self._config.host_engine and self._engine is not None:
            return self._engine.protocol_source
        return self._protocol_source

    @property
    def _active_clicker(self):
        """当前生效的键鼠适配器（引擎模式返回引擎 ActionExecutor 的内部适配器）。"""
        if self._config.host_engine and self._engine is not None:
            executor = self._engine.action_executor
            clicker = executor.adapter if executor is not None else None
        else:
            clicker = self._clicker
        if clicker is None:
            raise BridgeError("not_initialized", "键鼠适配器未初始化")
        return clicker

    def _ensure_engine_mode(self, tool: str) -> None:
        """引擎模式守卫：桥接模式调用引擎工具时抛 not_enabled。"""
        if not self._config.host_engine:
            raise BridgeError(
                "not_enabled",
                f"{tool} 仅在引擎托管模式（mcp.host_engine=true）下可用",
            )
        if self._engine is None:
            raise BridgeError("not_initialized", "托管引擎未初始化")

    def _ensure_engine_not_occupying(self) -> None:
        """键鼠竞争守卫：引擎 RUNNING/PAUSED 时拒绝直接操作。"""
        if self._engine_running():
            raise BridgeError(
                "engine_running",
                "托管引擎正在运行工作流，直接键鼠操作被拒绝（键鼠竞争保护）；"
                "如需手动操作请先 stop_engine",
            )

    def _engine_running(self) -> bool:
        """托管引擎是否处于 RUNNING/PAUSED。"""
        if self._engine is None:
            return False
        return self._engine.state in (EngineState.RUNNING, EngineState.PAUSED)

    def _run_action(
        self,
        tool: str,
        args: Dict[str, Any],
        fn: Callable[[], bool],
    ) -> None:
        """执行一次操作：安全检查 → 互斥执行 → 审计记录。

        Args:
            tool: 工具名。
            args: 参数快照（写入审计日志）。
            fn: 实际操作，返回是否成功。

        Raises:
            BridgeError: 被安全层拒绝或执行失败。
        """
        self._safety.check_action(tool)
        self._ensure_engine_not_occupying()

        start = time.perf_counter()
        ok = False
        error: Optional[str] = None
        try:
            with self._lock:
                ok = bool(fn())
            if not ok:
                error = f"操作 {tool} 执行失败（底层适配器返回 false）"
        except Exception as e:  # noqa: BLE001
            error = str(e)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            self._safety.record(tool, args, ok, error, duration_ms)
            logger.info(
                "MCP 操作: %s args=%s ok=%s%s",
                tool, args, ok, f" error={error}" if error else "",
            )

        if error:
            raise BridgeError("internal_error", error)

    def _get_global_config(self) -> Dict[str, Any]:
        """加载框架全局配置（复用 utils.config_loader，带缓存）。"""
        if self._global_cfg_cache is None:
            from utils.config_loader import load_global_config

            path = Path(self._global_config_path) if self._global_config_path else None
            self._global_cfg_cache = load_global_config(path)
        return self._global_cfg_cache
