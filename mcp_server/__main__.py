"""MCP 服务器 CLI 入口。

用法：
    python -m mcp_server                                  # 按 global.yaml 的 mcp 段运行
    python -m mcp_server --config config/global.yaml
    python -m mcp_server --transport stdio                # stdio 传输（供进程式客户端拉起）
    python -m mcp_server --transport http --port 8766     # streamable-http 传输

注意：日志一律写 stderr / 文件，绝不写 stdout（stdio 模式为 JSON-RPC 通道）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

# 确保项目根目录在 sys.path 中（以 -m 方式运行时通常已满足，双保险）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        参数对象：--config / --transport / --host / --port / --host-engine。
    """
    parser = argparse.ArgumentParser(
        description="Automatic-RocoKingdom MCP 连接器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="框架全局配置文件路径（默认: config/global.yaml）",
    )
    parser.add_argument(
        "--transport", "-t",
        type=str,
        choices=["stdio", "http"],
        default=None,
        help="传输方式，覆盖配置文件中的 mcp.transport",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="http 模式监听地址（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=None,
        help="http 模式端口，覆盖配置文件中的 mcp.port",
    )
    parser.add_argument(
        "--host-engine",
        action="store_true",
        default=False,
        help="强制启用引擎托管模式",
    )
    return parser.parse_args()


def setup_mcp_logging(level: str, log_file: Optional[str]) -> None:
    """配置日志（仅 stderr + 可选文件，禁止 stdout）。

    Args:
        level: 日志级别名（INFO/DEBUG/...）。
        log_file: 可选日志文件路径。
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # 抑制第三方库噪音
    for noisy in ("urllib3", "PIL", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    """入口：加载配置 → 初始化桥接层 → 构建服务 → 运行传输。

    Returns:
        进程退出码。
    """
    args = parse_args()
    logger = logging.getLogger("mcp_server")

    from mcp_server.config import load_mcp_config

    config = load_mcp_config(args.config)

    # CLI 参数优先于配置文件
    if args.transport:
        config.transport = args.transport.lower()
    if args.host:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    if args.host_engine:
        config.host_engine = True

    setup_mcp_logging(config.log_level, config.log_file)

    if not config.enabled:
        logger.error("MCP 服务已被禁用 (mcp.enabled = false)")
        return 1

    from mcp_server.bridge import RocoBridge
    from mcp_server.server import create_server

    bridge = RocoBridge(config, global_config_path=args.config)
    try:
        bridge.setup()
    except Exception as e:  # noqa: BLE001
        logger.error("桥接层初始化失败: %s", e)
        return 1

    server = create_server(config, bridge)

    logger.info(
        "MCP 服务启动: name=%s transport=%s mode=%s",
        config.server_name, config.transport, bridge.mode,
    )

    exit_code = 0
    try:
        if config.transport == "stdio":
            server.run(transport="stdio")
        else:
            logger.info("监听地址: http://%s:%d/mcp", config.host, config.port)
            server.run(transport="streamable-http")
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:  # noqa: BLE001
        logger.exception("MCP 服务异常退出: %s", e)
        exit_code = 1
    finally:
        bridge.cleanup()
        logger.info("MCP 服务已退出")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
