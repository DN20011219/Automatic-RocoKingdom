"""Automatic-RocoKingdom MCP 连接器。

将框架的信息源（视觉检测、协议事件、运行时上下文、截图）与操作能力
（键鼠控制、脚本执行、工作流引擎控制）封装为 MCP 服务，供大模型调用。

详见设计文档：docs/MCP_CONNECTOR.md
"""

from mcp_server.config import McpServerConfig, load_mcp_config  # noqa: F401

__version__ = "0.1.0"
