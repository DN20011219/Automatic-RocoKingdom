"""MCP 服务器配置 — 数据类定义与加载合并逻辑。

配置优先级：代码默认值 ← global.yaml 的 `mcp` 段 ← CLI 参数（__main__.py 覆盖）。
详见设计文档 §8。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SafetyConfig:
    """安全层配置。"""

    # 操作类工具总开关；false 时服务只读
    allow_actions: bool = True

    # execute_actions 单次动作序列长度上限
    max_sequence_len: int = 50


@dataclass
class ScreenshotConfig:
    """截图输出配置。"""

    # 输出最大宽度（像素），超宽按比例缩放；0 表示不缩放
    max_width: int = 1280

    # 编码格式：png | jpeg
    format: str = "png"

    # jpeg 质量（仅 format=jpeg 生效）
    jpeg_quality: int = 80


@dataclass
class McpServerConfig:
    """MCP 服务器完整配置。"""

    # 服务总开关
    enabled: bool = True

    # 服务名（MCP server name）
    server_name: str = "roco-kingdom"

    # 传输方式：stdio | http
    transport: str = "http"

    # http 模式监听地址（仅回环，不开放公网）
    host: str = "127.0.0.1"

    # http 模式端口
    port: int = 8766

    # 引擎托管模式：true 时托管工作流引擎，启用引擎控制类工具
    host_engine: bool = False

    # 日志级别
    log_level: str = "INFO"

    # 日志文件路径；None 表示仅输出到 stderr（不写 stdout，避免污染 stdio 通道）
    log_file: Optional[str] = None

    # 子配置
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)


def load_mcp_config(
    global_config_path: Optional[str] = None,
) -> McpServerConfig:
    """加载 MCP 配置。

    从项目全局配置（config/global.yaml 或指定路径）读取 `mcp` 段，
    与代码默认值合并生成 :class:`McpServerConfig`。

    Args:
        global_config_path: 全局配置文件路径；None 时使用默认路径。

    Returns:
        合并后的配置对象。文件缺失或 `mcp` 段缺失时返回纯默认配置。
    """
    from pathlib import Path

    from utils.config_loader import load_global_config

    path = Path(global_config_path) if global_config_path else None
    raw = load_global_config(path)

    mcp_raw = raw.get("mcp") or {}
    if not isinstance(mcp_raw, dict):
        mcp_raw = {}

    safety_raw = mcp_raw.get("safety") or {}
    screenshot_raw = mcp_raw.get("screenshot") or {}
    defaults = McpServerConfig()

    return McpServerConfig(
        enabled=bool(mcp_raw.get("enabled", defaults.enabled)),
        server_name=str(mcp_raw.get("server_name", defaults.server_name)),
        transport=str(mcp_raw.get("transport", defaults.transport)).lower(),
        host=str(mcp_raw.get("host", defaults.host)),
        port=int(mcp_raw.get("port", defaults.port)),
        host_engine=bool(mcp_raw.get("host_engine", defaults.host_engine)),
        log_level=str(mcp_raw.get("log_level", defaults.log_level)).upper(),
        log_file=mcp_raw.get("log_file", defaults.log_file),
        safety=SafetyConfig(
            allow_actions=bool(safety_raw.get(
                "allow_actions", defaults.safety.allow_actions)),
            max_sequence_len=int(safety_raw.get(
                "max_sequence_len", defaults.safety.max_sequence_len)),
        ),
        screenshot=ScreenshotConfig(
            max_width=int(screenshot_raw.get(
                "max_width", defaults.screenshot.max_width)),
            format=str(screenshot_raw.get(
                "format", defaults.screenshot.format)).lower(),
            jpeg_quality=int(screenshot_raw.get(
                "jpeg_quality", defaults.screenshot.jpeg_quality)),
        ),
    )
