"""工作流执行器 — 节点调度，驱动工作流步骤推进。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.context import RuntimeContext
from core.events import EventBus, EVENT_STEP_CHANGED, EVENT_WORKFLOW_COMPLETE
from core.lifecycle import LifecycleManager
from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


class WorkflowExecutor:
    """工作流执行器：根据工作流定义驱动节点执行。

    每次 tick() 调用执行当前节点的一次迭代：
    - 如果节点返回 SUCCESS，跳转到下一步
    - 如果节点返回 RUNNING，下次 tick 继续执行同一节点
    - 如果节点返回 TERMINATE，工作流结束

    用法：
        executor = WorkflowExecutor(workflow_def, context, ...)
        executor.start()
        while not executor.is_complete:
            executor.tick()
    """

    def __init__(
        self,
        workflow_def: Dict[str, Any],
        context: RuntimeContext,
        perception: Any = None,
        action_executor: Any = None,
        scene_machine: Any = None,
        event_bus: Optional[EventBus] = None,
        lifecycle: Optional[LifecycleManager] = None,
    ) -> None:
        self._workflow_def = workflow_def
        self._context = context
        self._perception = perception
        self._action_executor = action_executor
        self._scene_machine = scene_machine
        self._event_bus = event_bus
        self._lifecycle = lifecycle

        # 步骤定义
        self._steps: Dict[str, Dict[str, Any]] = workflow_def.get("steps", {})

        # 节点实例缓存
        self._nodes: Dict[str, NodeBase] = {}

        # 当前执行状态
        self._current_step: Optional[str] = None
        self._current_node: Optional[NodeBase] = None
        self._is_complete: bool = False
        self._started: bool = False

        # 全局超时保护（spec 9.2）
        self._global_timeout_sec: float = workflow_def.get("global_timeout_sec", 0)
        self._start_time: Optional[float] = None

        # 将动作执行器注入到 context（供节点使用）
        self._context._action_executor = action_executor

        # 确保内置节点类型已注册
        NodeRegistry._ensure_builtin_loaded()

        # 预构建所有节点
        self._build_nodes()

    def _build_nodes(self) -> None:
        """预构建所有步骤的节点实例。"""
        for step_name, step_cfg in self._steps.items():
            step_type = step_cfg.get("type", "action")
            try:
                node = NodeRegistry.create(step_type, step_cfg)
                node.step_name = step_name
                self._nodes[step_name] = node
            except Exception as e:
                logger.error("构建节点失败: step=%s, type=%s, error=%s",
                             step_name, step_type, e)

    def start(self) -> None:
        """启动工作流，进入第一个步骤。"""
        if self._started:
            return

        # 确定起始步骤
        start_step = self._workflow_def.get("start", None)
        if not start_step:
            # 默认使用第一个步骤
            start_step = next(iter(self._steps), None)

        if not start_step:
            logger.error("工作流没有可用步骤")
            self._is_complete = True
            return

        self._started = True
        import time as _time
        self._start_time = _time.time()  # 全局超时计时起点
        self._goto_step(start_step)
        logger.info("工作流启动，起始步骤: %s", start_step)

    def tick(self) -> None:
        """执行一次 tick：推进当前节点。"""
        if self._is_complete or not self._started:
            return

        if self._current_node is None:
            self._is_complete = True
            return

        # 全局超时保护（spec 9.2）
        if self._global_timeout_sec > 0 and self._start_time is not None:
            import time
            elapsed = time.time() - self._start_time
            if elapsed >= self._global_timeout_sec:
                logger.warning("工作流全局超时 (%.0fs)，强制终止", self._global_timeout_sec)
                self._complete()
                return

        # 执行当前节点
        try:
            result, next_step = self._current_node.execute(self._context)
        except Exception as e:
            logger.exception("节点执行异常: step=%s", self._current_step)
            result = NodeResult.FAILED
            next_step = self._current_node._config.get("on_error")

        # 处理结果
        if result == NodeResult.TERMINATE:
            self._complete()
            return

        if result == NodeResult.RUNNING:
            # 节点仍在运行，下次 tick 继续
            return

        if result in (NodeResult.SUCCESS, NodeResult.FAILED, NodeResult.SKIP):
            # 确定下一步
            if next_step is None:
                next_step = self._current_node._config.get("next")

            if next_step:
                self._goto_step(next_step)
            else:
                # 没有下一步，工作流结束
                self._complete()

    def _goto_step(self, step_name: str) -> None:
        """跳转到指定步骤。"""
        if step_name not in self._nodes:
            logger.error("目标步骤不存在: %s", step_name)
            self._complete()
            return

        # 退出当前节点
        if self._current_node:
            self._current_node.on_exit(self._context)

        # 切换
        prev_step = self._current_step
        self._current_step = step_name
        self._current_node = self._nodes[step_name]

        # 更新上下文
        self._context.prev_step_name = prev_step or ""
        self._context.step_name = step_name

        # 重置并进入新节点
        self._current_node.reset()
        self._current_node.on_enter(self._context)

        # 触发事件
        if self._event_bus:
            self._event_bus.emit(
                EVENT_STEP_CHANGED,
                prev_step=prev_step,
                current_step=step_name,
                context=self._context,
            )

        logger.debug("步骤切换: %s -> %s", prev_step or "(start)", step_name)

    def _complete(self) -> None:
        """标记工作流完成。"""
        self._is_complete = True
        if self._event_bus:
            self._event_bus.emit(EVENT_WORKFLOW_COMPLETE, name=self._context.workflow_name)
        logger.info("工作流执行完成: %s", self._context.workflow_name)

    @property
    def is_complete(self) -> bool:
        return self._is_complete

    @property
    def current_step(self) -> Optional[str]:
        return self._current_step
