"""循环节点 — 重复执行一组动作直到满足退出条件。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


@NodeRegistry.register("loop")
class LoopNode(NodeBase):
    """循环节点：重复执行 body 中的动作，直到退出条件满足或达到最大迭代次数。

    YAML 配置示例：
        step_battle:
          type: loop
          max_iterations: 10
          body:
            - type: perceive
              target: "enemy_pet"
              timeout_ms: 5000
              on_found: "step_attack"
            - type: action
              actions:
                - key: 32
                - wait: 1500
          exit_condition:
            type: visual
            target: "victory_label"
            min_score: 0.85
          on_exit: "step_collect"
          on_max_iterations: "step_collect"
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self._iteration: int = 0
        self._body_index: int = 0

    @property
    def node_type(self) -> str:
        return "loop"

    def on_enter(self, context: Any) -> None:
        self._iteration = 0
        self._body_index = 0

    def reset(self) -> None:
        self._iteration = 0
        self._body_index = 0

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        """执行循环逻辑。"""
        from conditions.base import ConditionRegistry

        max_iterations = self._config.get("max_iterations", 0)  # 0 = 无限
        body: List[Dict[str, Any]] = self._config.get("body", [])
        exit_condition_cfg = self._config.get("exit_condition", None)

        # 检查退出条件
        if exit_condition_cfg:
            cond_type = exit_condition_cfg.get("type", "")
            try:
                condition = ConditionRegistry.create(cond_type, exit_condition_cfg)
                if condition.evaluate(context):
                    logger.debug("循环节点 %s: 退出条件满足 (iteration=%d)",
                                 self._step_name, self._iteration)
                    return NodeResult.SUCCESS, self._config.get("on_exit", self._config.get("next"))
            except Exception as e:
                logger.error("循环退出条件评估失败: %s", e)

        # 检查最大迭代次数
        if max_iterations > 0 and self._iteration >= max_iterations:
            logger.debug("循环节点 %s: 达到最大迭代次数 %d", self._step_name, max_iterations)
            return NodeResult.SUCCESS, self._config.get("on_max_iterations", self._config.get("next"))

        # 执行 body 中的当前步骤
        if not body:
            self._iteration += 1
            return NodeResult.RUNNING, None

        if self._body_index >= len(body):
            # 一轮 body 执行完毕
            self._iteration += 1
            self._body_index = 0
            context.iteration = self._iteration
            return NodeResult.RUNNING, None

        # 执行当前 body 步骤
        step_cfg = body[self._body_index]
        step_type = step_cfg.get("type", "action")

        # 内联执行简单动作
        if step_type == "action":
            action_executor = getattr(context, "_action_executor", None)
            if action_executor:
                actions = step_cfg.get("actions", [])
                action_executor.execute_actions(actions, context)
            self._body_index += 1
            return NodeResult.RUNNING, None

        elif step_type == "perceive":
            # 内联感知：检查目标
            target = step_cfg.get("target", "")
            min_score = step_cfg.get("min_score", 0.0)
            detection = context.find_detection(target, min_score)
            if detection:
                # 目标找到，跳转到 on_found（可能是循环外的步骤）
                on_found = step_cfg.get("on_found")
                if on_found:
                    return NodeResult.SUCCESS, on_found
                self._body_index += 1
            # 未找到则继续等待（RUNNING）
            return NodeResult.RUNNING, None

        else:
            # 其他类型直接跳过
            self._body_index += 1
            return NodeResult.RUNNING, None
