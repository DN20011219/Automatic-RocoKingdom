"""Automatic-RocoKingdom 全自动化脚本框架入口。

用法：
    python main.py --workflow config/workflows/example_daily.yaml
    python main.py --workflow config/workflows/example_daily.yaml --config config/global.yaml
    python main.py --workflow config/workflows/example_daily.yaml -v  # 详细日志
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automatic-RocoKingdom 全自动化脚本框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workflow", "-w",
        type=str,
        required=True,
        help="工作流 YAML 文件路径",
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="全局配置文件路径（默认: config/global.yaml）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志（DEBUG 级别）",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="日志输出文件路径",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # 配置日志
    from utils.logger import setup_logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_file = Path(args.log_file) if args.log_file else None
    setup_logging(level=log_level, log_file=log_file)

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Automatic-RocoKingdom 全自动化脚本框架")
    logger.info("=" * 60)

    # 检查子模块
    submodules_dir = PROJECT_ROOT / "submodules"
    clicker_dir = submodules_dir / "RocoKingdom-Clicker"
    spotting_dir = submodules_dir / "RocoKingdom-Spotting"

    if not clicker_dir.exists() or not (clicker_dir / "InterceptionCore.py").exists():
        logger.error("RocoKingdom-Clicker 子模块未正确克隆！")
        logger.error("请运行: git submodule update --init --recursive")
        return 1

    if not spotting_dir.exists() or not (spotting_dir / "pipeline.py").exists():
        logger.error("RocoKingdom-Spotting 子模块未正确克隆！")
        logger.error("请运行: git submodule update --init --recursive")
        return 1

    # 创建并配置引擎
    from core.engine import Engine

    config_path = args.config
    engine = Engine(config_path=config_path)

    try:
        # 初始化引擎
        logger.info("正在初始化引擎...")
        engine.setup()

        # 加载工作流
        workflow_path = Path(args.workflow)
        if not workflow_path.exists():
            logger.error("工作流文件不存在: %s", workflow_path)
            return 1

        logger.info("正在加载工作流: %s", workflow_path)
        engine.load_workflow(str(workflow_path))

        # 运行
        logger.info("开始执行... (Ctrl+C 停止)")
        engine.run()

        logger.info("执行完毕")
        return 0

    except KeyboardInterrupt:
        logger.info("用户中断")
        return 0
    except Exception as e:
        logger.exception("致命错误: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
