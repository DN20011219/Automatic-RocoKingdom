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

    支持 `extends: "global.yaml"` 继承：先加载被继承的文件，再用本文件的内容
    深度合并覆盖（dict 逐键合并，其余类型整体替换）。这样派生配置只需写出
    和基础配置不同的部分，不必整份复制。路径相对**本文件所在目录**解析。

    Args:
        config_path: 配置文件路径，默认使用 config/global.yaml

    Returns:
        配置字典
    """
    path = config_path or DEFAULT_CONFIG_PATH

    try:
        return _load_with_extends(Path(path), seen=[])
    except Exception as e:
        logger.error("加载配置文件失败: %s", e)
        return _default_config()


# extends 链的最大深度，防止配置互相继承造成死循环
_MAX_EXTENDS_DEPTH = 5


def _load_with_extends(path: Path, seen: list) -> Dict[str, Any]:
    """加载配置并递归解析 extends 链。"""
    if not path.exists():
        logger.warning("配置文件不存在: %s，使用默认配置", path)
        return _default_config()

    resolved = path.resolve()
    if resolved in seen:
        raise ValueError(f"配置继承出现循环: {' -> '.join(str(p) for p in seen + [resolved])}")
    if len(seen) >= _MAX_EXTENDS_DEPTH:
        raise ValueError(f"配置继承层级超过 {_MAX_EXTENDS_DEPTH} 层: {resolved}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 YAML 字典格式: {path}")

    parent_name = data.pop("extends", None)
    if parent_name:
        parent_path = Path(parent_name)
        if not parent_path.is_absolute():
            parent_path = path.parent / parent_path
        parent_data = _load_with_extends(parent_path, seen + [resolved])
        data = _deep_merge(parent_data, data)
        logger.info("全局配置已加载: %s（继承自 %s）", path, parent_path.name)
    else:
        logger.info("全局配置已加载: %s", path)

    return data


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置：两边都是 dict 时逐键合并，否则 override 整体替换。

    列表也整体替换（不做拼接），这样派生配置里写 templates 就是完整覆盖，
    不会和基础配置的列表混在一起。
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


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
