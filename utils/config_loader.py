"""配置加载工具。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "global.yaml"


def load_global_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """加载全局配置文件。

    Args:
        config_path: 配置文件路径，默认使用 config/global.yaml

    Returns:
        配置字典
    """
    path = config_path or DEFAULT_CONFIG_PATH

    if not path.exists():
        logger.warning("配置文件不存在: %s，使用默认配置", path)
        return _default_config()

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info("全局配置已加载: %s", path)
        return data
    except Exception as e:
        logger.error("加载配置文件失败: %s", e)
        return _default_config()


def _default_config() -> Dict[str, Any]:
    """默认配置。"""
    return {
        "loop_interval": 0.1,
        "spotting": {
            "window_keyword": "洛克王国：世界",
            "capture_backend": "screen-client",
            "foreground_only": True,
        },
        "clicker": {
            "move_mouse": True,
        },
        "logging": {
            "level": "INFO",
            "file": None,
        },
    }


def load_workflow_config(workflow_path: Path) -> Dict[str, Any]:
    """加载工作流配置文件。"""
    if not workflow_path.exists():
        raise FileNotFoundError(f"工作流文件不存在: {workflow_path}")

    with workflow_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
