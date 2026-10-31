"""主引擎 — PDA (Perceive-Decide-Act) 循环调度。

引擎负责：
1. 初始化各子系统（感知、动作、工作流）
2. 驱动 PDA 主循环
3. 协调工作流执行器推进步骤
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.context import RuntimeContext
from core.events import (
    EventBus,
    EVENT_ERROR,
    EVENT_FRAME_PROCESSED,
    EVENT_WORKFLOW_COMPLETE,
    EVENT_WORKFLOW_STARTED,
)
from core.lifecycle import EngineState, LifecycleManager

logger = logging.getLogger(__name__)


class Engine:
    """PDA 循环主引擎。

    用法：
        engine = Engine(config_path="config/global.yaml")
        engine.load_workflow("config/workflows/example_daily.yaml")
        engine.run()  # 阻塞运行
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._config_path = Path(config_path) if config_path else None
        self._config: dict = {}

        # 核心组件
        self.event_bus = EventBus()
        self.lifecycle = LifecycleManager(event_bus=self.event_bus)
        self.context = RuntimeContext()

        # 子系统（延迟初始化）
        self._perception = None  # perception.adapter.SpottingAdapter
        self._protocol_source = None  # perception.protocol_source.ProtocolSource
        self._action_executor = None  # action.executor.ActionExecutor
        self._workflow_executor = None  # workflow.executor.WorkflowExecutor
        self._scene_machine = None  # scene.state_machine.SceneStateMachine

        self._loop_interval: float = 0.1  # PDA 循环间隔（秒）

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """初始化所有子系统。"""
        from utils.config_loader import load_global_config

        # 加载全局配置
        if self._config_path:
            self._config = load_global_config(self._config_path)
        else:
            self._config = load_global_config()

        self._loop_interval = self._config.get("loop_interval", 0.1)

        # 初始化感知层
        from perception.adapter import SpottingAdapter
        spotting_config = self._config.get("spotting", {})
        self._perception = SpottingAdapter(spotting_config)
        self._perception.setup()

        # 初始化协议信息源（可选，依赖 RKPP opencode-server）
        from perception.protocol_source import ProtocolSource
        protocol_config = self._config.get("protocol", {})
        self._protocol_source = ProtocolSource(
            protocol_config, self.context, self.event_bus
        )
        self._protocol_source.setup()

        # 初始化动作层
        from action.executor import ActionExecutor
        clicker_config = self._config.get("clicker", {})
        self._action_executor = ActionExecutor(clicker_config, self.event_bus)
        self._action_executor.setup()

        # 初始化场景状态机
        from scene.state_machine import SceneStateMachine
        self._scene_machine = SceneStateMachine(self.event_bus)

        logger.info("引擎初始化完成")

    def load_workflow(self, workflow_path: str,
                      variable_overrides: Optional[Dict[str, Any]] = None) -> None:
        """加载工作流文件。"""
        from workflow.loader import WorkflowLoader
        from workflow.executor import WorkflowExecutor

        loader = WorkflowLoader()
        workflow_def = loader.load(Path(workflow_path))

        self._workflow_executor = WorkflowExecutor(
            workflow_def=workflow_def,
            context=self.context,
            perception=self._perception,
            action_executor=self._action_executor,
            scene_machine=self._scene_machine,
            event_bus=self.event_bus,
            lifecycle=self.lifecycle,
        )

        # 设置工作流变量
        declared = workflow_def.get("variables", {}) or {}
        self.context.variables.update(declared)
        self.context.workflow_name = workflow_def.get("name", "unnamed")

        # 命令行覆盖优先于 YAML（换目标精灵时不必改文件）
        if variable_overrides:
            unknown = [k for k in variable_overrides if k not in declared]
            if unknown:
                logger.warning(
                    "以下 --var 变量在工作流的 variables 里没有声明，可能拼写错了: %s",
                    ", ".join(sorted(unknown)),
                )
            self.context.variables.update(variable_overrides)
            logger.info("命令行覆盖变量: %s",
                        ", ".join(f"{k}={v!r}" for k, v in sorted(variable_overrides.items())))

        # 加载场景定义
        if "scenes" in workflow_def:
            self._scene_machine.load_scenes(workflow_def["scenes"])

        logger.info("工作流已加载: %s", self.context.workflow_name)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动 PDA 主循环（阻塞）。"""
        if not self._workflow_executor:
            raise RuntimeError("请先调用 load_workflow() 加载工作流")

        if not self.lifecycle.start():
            raise RuntimeError("引擎启动失败")

        self.event_bus.emit(EVENT_WORKFLOW_STARTED, name=self.context.workflow_name)
        logger.info("开始执行工作流: %s", self.context.workflow_name)

        try:
            self._workflow_executor.start()

            while self.lifecycle.should_continue():
                tick = time.perf_counter()

                # 暂停检查
                if not self.lifecycle.wait_if_paused():
                    break

                # === PDA 循环 ===
                self._perceive()
                self._decide()
                # Act 由工作流执行器在 decide 阶段内部驱动

                # 检查工作流是否完成
                if self._workflow_executor.is_complete:
                    break

                # 帧率控制
                elapsed = time.perf_counter() - tick
                sleep_time = max(0.0, self._loop_interval - elapsed)
                if sleep_time > 0:
                    if not self.lifecycle.interruptible_sleep(sleep_time):
                        break

        except KeyboardInterrupt:
            logger.info("用户中断 (Ctrl+C)")
        except Exception as e:
            logger.exception("引擎异常")
            self.lifecycle.set_error(str(e))
            self.event_bus.emit(EVENT_ERROR, error=e)
        finally:
            self._shutdown()

    def run_async(self) -> None:
        """在后台线程中启动 PDA 主循环（非阻塞）。"""
        import threading
        thread = threading.Thread(target=self.run, daemon=True, name="pda-engine")
        thread.start()

    # ------------------------------------------------------------------
    # PDA 各阶段
    # ------------------------------------------------------------------

    def _perceive(self) -> None:
        """感知阶段：获取当前帧检测结果。"""
        if self._perception is None:
            return

        detections = self._perception.detect_frame()
        self.context.last_detections = detections
        self.context.frame_seq += 1
        # 区分"画面里没有目标"和"根本没截到画面"，避免节点在看不见时盲动键鼠
        self.context.frame_captured = getattr(self._perception, "last_capture_ok", True)
        # 丢球瞄准的基准点（客户区中心）需要截帧区域
        self.context.frame_rect = getattr(self._perception, "last_rect", (0, 0, 0, 0))

        self.event_bus.emit(
            EVENT_FRAME_PROCESSED,
            detections=detections,
            context=self.context,
        )

        # 场景识别
        if self._scene_machine:
            self._scene_machine.update(detections, self.context)

    def _decide(self) -> None:
        """决策阶段：推进工作流执行器。"""
        if self._workflow_executor is None:
            return

        self._workflow_executor.tick()

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------

    def pause(self) -> None:
        self.lifecycle.pause()

    def resume(self) -> None:
        self.lifecycle.resume()

    def stop(self) -> None:
        self.lifecycle.stop()

    @property
    def state(self) -> EngineState:
        return self.lifecycle.state

    # ---- 子系统只读访问器（供 MCP 等外部接入层使用） ----

    @property
    def perception(self):
        """视觉感知适配器（SpottingAdapter）。"""
        return self._perception

    @property
    def protocol_source(self):
        """协议信息源（ProtocolSource）。"""
        return self._protocol_source

    @property
    def action_executor(self):
        """工作流动作执行器（ActionExecutor）。"""
        return self._action_executor

    @property
    def workflow_executor(self):
        """工作流执行器（WorkflowExecutor，未加载工作流时为 None）。"""
        return self._workflow_executor

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """清理资源。"""
        self.lifecycle.stop()
        self.event_bus.emit(EVENT_WORKFLOW_COMPLETE, name=self.context.workflow_name)

        if self._perception:
            self._perception.cleanup()

        if self._protocol_source:
            self._protocol_source.cleanup()

        if self._action_executor:
            self._action_executor.cleanup()

        logger.info("引擎已关闭")
