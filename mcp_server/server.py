"""MCP 服务构建 — FastMCP 实例与全部工具注册。

工具清单（共 20 个，详见设计文档 §4）：
- 读取类：get_status / screenshot / detect / get_protocol_events /
  get_runtime_state / list_workflows / get_action_log
- 操作类：click / move_to / press_key / press_combo / wait /
  execute_actions / run_script
- 引擎控制类：load_workflow / start_engine / pause_engine /
  resume_engine / stop_engine / set_variable

统一响应信封：``{"ok": bool, "data": ...}`` 或
``{"ok": false, "error": ..., "error_code": ...}``。
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Dict, List, Union

from mcp_server.bridge import BridgeError, RocoBridge
from mcp_server.config import McpServerConfig

logger = logging.getLogger(__name__)

_INSTRUCTIONS = """\
洛克王国自动化框架的 MCP 接口。

感知：用 screenshot 查看游戏画面，用 detect 获取 UI 元素检测框
（返回的 cx/cy 为屏幕绝对坐标，可直接传给 click）。
协议：get_protocol_events 查看游戏协议事件（战斗进度等）。
操作：click/press_key 等直接控制键鼠；引擎运行工作流期间操作类工具被禁用。
工作流（引擎托管模式）：list_workflows → load_workflow → start_engine，
之后用 get_status 跟踪进度，可 pause/resume/stop。
所有工具返回 {"ok", "data"} 或 {"ok", "error", "error_code"} 结构。
"""


def _ok(data: Any) -> Dict[str, Any]:
    """构造成功信封。"""
    return {"ok": True, "data": data}


def _err(error_code: str, message: str) -> Dict[str, Any]:
    """构造失败信封。"""
    return {"ok": False, "error": message, "error_code": error_code}


def _guard(fn):
    """统一异常守卫：BridgeError → 错误信封，其余异常 → internal_error。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except BridgeError as e:
            return _err(e.error_code, e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("工具 %s 内部异常", fn.__name__)
            return _err("internal_error", str(e))

    return wrapper


def create_server(config: McpServerConfig, bridge: RocoBridge):
    """构建 FastMCP 服务并注册全部工具。

    Args:
        config: MCP 服务器配置。
        bridge: 已完成 ``setup()`` 的桥接层实例。

    Returns:
        ``mcp.server.fastmcp.FastMCP`` 实例（由 __main__.py 选择传输运行）。
    """
    from mcp.server.fastmcp import FastMCP, Image

    mcp = FastMCP(
        name=config.server_name,
        instructions=_INSTRUCTIONS,
        host=config.host,
        port=config.port,
    )

    # ==================================================================
    # 读取类工具
    # ==================================================================

    @mcp.tool()
    @_guard
    async def get_status() -> Dict[str, Any]:
        """获取系统整体状态：游戏窗口、感知、协议源、操作开关、引擎状态。"""
        return _ok(await asyncio.to_thread(bridge.get_status))

    @mcp.tool()
    @_guard
    async def screenshot(max_width: int = 0) -> Any:
        """截取游戏窗口当前画面（游戏窗口须在前台）。

        Args:
            max_width: 输出最大宽度像素，0 表示按服务端配置缩放。

        Returns:
            图像；失败时返回错误信封。
        """
        data = await asyncio.to_thread(bridge.capture_screenshot_png, max_width)
        fmt = "jpeg" if config.screenshot.format == "jpeg" else "png"
        return Image(data=data, format=fmt)

    @mcp.tool()
    @_guard
    async def detect(name: str = "", min_score: float = 0.0) -> Dict[str, Any]:
        """对当前帧执行视觉检测，返回 UI 元素列表。

        Args:
            name: 目标名过滤（如 "confirm"），空串返回全部。
            min_score: 最低置信度（0-1）。

        Returns:
            detections 列表，元素含 name/score/x/y/w/h/cx/cy，
            cx/cy 为中心点的屏幕绝对坐标，可直接用于 click。
        """
        results = await asyncio.to_thread(
            bridge.detect, name or None, min_score
        )
        return _ok({"count": len(results), "detections": results})

    @mcp.tool()
    @_guard
    async def get_protocol_events(
        limit: int = 20, keyword: str = ""
    ) -> Dict[str, Any]:
        """查询最近的游戏协议事件（RKPP 解析的战斗/场景事件流）。

        Args:
            limit: 最多返回条数（最新在最后）。
            keyword: 关键词过滤（匹配事件类型/含义，不区分大小写）。
        """
        return _ok(await asyncio.to_thread(
            bridge.get_protocol_events, max(1, limit), keyword or None
        ))

    @mcp.tool()
    @_guard
    async def get_runtime_state() -> Dict[str, Any]:
        """获取运行时上下文：工作流变量、计数器、当前场景与步骤。"""
        return _ok(await asyncio.to_thread(bridge.get_runtime_state))

    @mcp.tool()
    @_guard
    async def list_workflows() -> Dict[str, Any]:
        """列出 config/workflows/ 目录下可用的 YAML 工作流。"""
        return _ok(await asyncio.to_thread(bridge.list_workflows))

    @mcp.tool()
    @_guard
    async def get_action_log(limit: int = 20) -> Dict[str, Any]:
        """查询最近的操作审计日志（时间、工具、参数、结果）。"""
        return _ok(await asyncio.to_thread(bridge.get_action_log, max(1, limit)))

    # ==================================================================
    # 操作类工具
    # ==================================================================

    @mcp.tool()
    @_guard
    async def click(x: int, y: int, hold_ms: int = 100) -> Dict[str, Any]:
        """鼠标左键点击屏幕绝对坐标（坐标可由 detect 的 cx/cy 获得）。

        Args:
            x: 屏幕 X 坐标。
            y: 屏幕 Y 坐标。
            hold_ms: 按下保持时长（毫秒）。
        """
        await asyncio.to_thread(bridge.click, x, y, hold_ms)
        return _ok({"action": "click", "x": x, "y": y})

    @mcp.tool()
    @_guard
    async def move_to(
        x: int, y: int, duration_ms: int = 200, humanized: bool = True
    ) -> Dict[str, Any]:
        """移动鼠标到屏幕绝对坐标（不点击）。

        Args:
            x: 屏幕 X 坐标。
            y: 屏幕 Y 坐标。
            duration_ms: 移动耗时（毫秒）。
            humanized: 是否使用拟人化路径。
        """
        await asyncio.to_thread(bridge.move_to, x, y, duration_ms, humanized)
        return _ok({"action": "move_to", "x": x, "y": y})

    @mcp.tool()
    @_guard
    async def press_key(key: Union[str, int], hold_ms: int = 50) -> Dict[str, Any]:
        """按下并释放一个键。

        Args:
            key: 键名（如 "space"/"enter"/"esc"/"f1"/"a"）或 vk_code 数字。
            hold_ms: 按下保持时长（毫秒）。
        """
        await asyncio.to_thread(bridge.press_key, key, hold_ms)
        return _ok({"action": "press_key", "key": key})

    @mcp.tool()
    @_guard
    async def press_combo(
        keys: List[Union[str, int]], hold_ms: int = 50
    ) -> Dict[str, Any]:
        """按下组合键，如 ["ctrl", "a"]。

        Args:
            keys: 键名/vk_code 列表，按顺序按下、逆序释放。
            hold_ms: 保持时长（毫秒）。
        """
        await asyncio.to_thread(bridge.press_combo, keys, hold_ms)
        return _ok({"action": "press_combo", "keys": keys})

    @mcp.tool()
    @_guard
    async def wait(duration_ms: int) -> Dict[str, Any]:
        """等待指定毫秒数（上限 30000）。"""
        await asyncio.to_thread(bridge.wait, duration_ms)
        return _ok({"action": "wait", "duration_ms": duration_ms})

    @mcp.tool()
    @_guard
    async def execute_actions(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量执行动作序列。每个元素为含 type 字段的对象：
        {"type":"click","x":..,"y":..} / {"type":"move","x":..,"y":..} /
        {"type":"key","vk_code":..} / {"type":"combo","vk_codes":[..]} /
        {"type":"wait","duration_ms":..}

        Args:
            actions: 动作列表（长度受服务端安全配置限制）。
        """
        await asyncio.to_thread(bridge.execute_actions, actions)
        return _ok({"action": "execute_actions", "count": len(actions)})

    @mcp.tool()
    @_guard
    async def run_script(script_name: str) -> Dict[str, Any]:
        """执行 Clicker 原生 JSON 动作脚本（位于 Clicker 子模块
        data/action_scripts/，如 "click"/"精灵全选"）。

        Args:
            script_name: 脚本名（不含 .json 后缀）。
        """
        await asyncio.to_thread(bridge.run_script, script_name)
        return _ok({"action": "run_script", "script_name": script_name})

    # ==================================================================
    # 引擎控制类工具（仅引擎托管模式）
    # ==================================================================

    @mcp.tool()
    @_guard
    async def load_workflow(path: str) -> Dict[str, Any]:
        """加载 YAML 工作流到托管引擎（引擎须未运行）。

        Args:
            path: 工作流文件路径（相对路径按项目根目录解析，
                可用 list_workflows 获取可选值）。
        """
        await asyncio.to_thread(bridge.load_workflow, path)
        return _ok({"action": "load_workflow", "path": path})

    @mcp.tool()
    @_guard
    async def start_engine() -> Dict[str, Any]:
        """后台启动托管引擎执行已加载的工作流。"""
        await asyncio.to_thread(bridge.start_engine)
        return _ok({"action": "start_engine"})

    @mcp.tool()
    @_guard
    async def pause_engine() -> Dict[str, Any]:
        """暂停托管引擎（仅运行中有效）。"""
        await asyncio.to_thread(bridge.pause_engine)
        return _ok({"action": "pause_engine"})

    @mcp.tool()
    @_guard
    async def resume_engine() -> Dict[str, Any]:
        """恢复已暂停的托管引擎。"""
        await asyncio.to_thread(bridge.resume_engine)
        return _ok({"action": "resume_engine"})

    @mcp.tool()
    @_guard
    async def stop_engine() -> Dict[str, Any]:
        """停止托管引擎并等待其退出。"""
        await asyncio.to_thread(bridge.stop_engine)
        return _ok({"action": "stop_engine"})

    @mcp.tool()
    @_guard
    async def set_variable(
        name: str, value: Union[str, int, float, bool]
    ) -> Dict[str, Any]:
        """设置工作流变量（供工作流 YAML 中 ${name} 引用）。

        Args:
            name: 变量名。
            value: 变量值（字符串/数字/布尔）。
        """
        await asyncio.to_thread(bridge.set_variable, name, value)
        return _ok({"action": "set_variable", "name": name, "value": value})

    return mcp
