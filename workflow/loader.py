"""YAML 工作流加载与校验。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


class WorkflowLoadError(Exception):
    """工作流加载错误。"""
    pass


class WorkflowLoader:
    """YAML 工作流文件加载器。

    负责：
    - 读取 YAML 文件
    - 校验基本结构
    - 返回工作流定义字典

    用法：
        loader = WorkflowLoader()
        workflow_def = loader.load(Path("config/workflows/example.yaml"))
    """

    REQUIRED_FIELDS = ["name", "steps"]
    VALID_NODE_TYPES = [
        "action", "perceive", "condition", "loop",
        "wait", "scene", "terminate", "log",
    ]

    def load(self, path: Path) -> Dict[str, Any]:
        """加载并校验工作流文件。

        Args:
            path: YAML 文件路径

        Returns:
            工作流定义字典

        Raises:
            WorkflowLoadError: 文件不存在或格式错误
        """
        if not path.exists():
            raise WorkflowLoadError(f"工作流文件不存在: {path}")

        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise WorkflowLoadError(f"YAML 解析失败: {e}")

        if not isinstance(data, dict):
            raise WorkflowLoadError("工作流文件必须是 YAML 字典格式")

        # 校验必填字段
        self._validate_required_fields(data, path)

        # 校验步骤定义
        self._validate_steps(data, path)

        logger.info("工作流已加载: %s (%d 个步骤)", data.get("name"), len(data.get("steps", {})))
        return data

    def load_from_string(self, content: str) -> Dict[str, Any]:
        """从字符串加载工作流（用于测试）。"""
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise WorkflowLoadError(f"YAML 解析失败: {e}")

        if not isinstance(data, dict):
            raise WorkflowLoadError("工作流必须是字典格式")

        return data

    def _validate_required_fields(self, data: Dict[str, Any], path: Path) -> None:
        """校验必填字段。"""
        for field in self.REQUIRED_FIELDS:
            if field not in data:
                raise WorkflowLoadError(f"工作流缺少必填字段 '{field}': {path}")

    def _validate_steps(self, data: Dict[str, Any], path: Path) -> None:
        """校验步骤定义。"""
        steps = data.get("steps", {})

        if not isinstance(steps, dict):
            raise WorkflowLoadError(f"'steps' 必须是字典格式: {path}")

        if not steps:
            raise WorkflowLoadError(f"工作流至少需要一个步骤: {path}")

        for step_name, step_cfg in steps.items():
            if not isinstance(step_cfg, dict):
                raise WorkflowLoadError(f"步骤 '{step_name}' 必须是字典格式: {path}")

            step_type = step_cfg.get("type")
            if not step_type:
                raise WorkflowLoadError(f"步骤 '{step_name}' 缺少 'type' 字段: {path}")

            if step_type not in self.VALID_NODE_TYPES:
                logger.warning(
                    "步骤 '%s' 使用了未知类型 '%s'（可用: %s）",
                    step_name, step_type, ", ".join(self.VALID_NODE_TYPES),
                )

        # 校验跳转目标是否存在
        self._validate_references(steps, path)

    def _validate_references(self, steps: Dict[str, Any], path: Path) -> None:
        """校验步骤间的跳转引用是否有效。"""
        step_names = set(steps.keys())
        reference_fields = [
            "next", "on_found", "on_timeout", "on_true", "on_false",
            "on_exit", "on_max_iterations", "on_error", "on_absent",
        ]

        for step_name, step_cfg in steps.items():
            for field in reference_fields:
                target = step_cfg.get(field)
                if target and target not in step_names:
                    logger.warning(
                        "步骤 '%s' 的 '%s' 引用了不存在的步骤 '%s': %s",
                        step_name, field, target, path,
                    )
