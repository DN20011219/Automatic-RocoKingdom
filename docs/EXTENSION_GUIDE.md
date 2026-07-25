# 扩展开发指南

本文档面向高级用户和开发者，说明如何扩展框架的功能：自定义条件、自定义节点、事件监听、调试技巧等。

---

## 1. 自定义条件类型

框架的条件系统基于注册表模式，添加新条件类型只需 3 步。

### 步骤

1. 创建条件类，继承 `ConditionBase`
2. 实现 `evaluate(context) -> bool` 方法
3. 使用 `@ConditionRegistry.register("type_name")` 装饰器注册

### 完整示例

创建文件 `conditions/my_condition.py`：

```python
"""自定义条件示例：判断计数器是否为偶数。"""

from conditions.base import ConditionBase, ConditionRegistry


@ConditionRegistry.register("even_counter")
class EvenCounterCondition(ConditionBase):
    """判断指定计数器的值是否为偶数。

    YAML 用法：
        conditions:
          - type: even_counter
            counter: "battle_count"
    """

    def evaluate(self, context) -> bool:
        counter_name = self._config.get("counter", "")
        value = context.get_counter(counter_name)
        return value % 2 == 0
```

### 在 YAML 中使用

```yaml
step_check:
  type: condition
  conditions:
    - type: even_counter
      counter: "battle_count"
  on_true: "step_special_action"
  on_false: "step_normal_action"
```

### 确保被加载

在 `conditions/base.py` 的 `_ensure_builtin_loaded()` 方法中添加导入：

```python
@classmethod
def _ensure_builtin_loaded(cls) -> None:
    import conditions.visual_condition
    import conditions.time_condition
    import conditions.counter_condition
    import conditions.composite_condition
    import conditions.my_condition        # 添加这一行
```

或者在你的入口代码中手动导入一次即可触发注册。

---

## 2. 自定义工作流节点

添加新的节点类型同样基于注册表模式。

### 步骤

1. 创建节点类，继承 `NodeBase`
2. 实现 `execute(context) -> (NodeResult, next_step)` 方法
3. 使用 `@NodeRegistry.register("type_name")` 装饰器注册
4. 可选：覆盖 `on_enter()`、`on_exit()`、`reset()` 生命周期方法

### NodeResult 枚举

| 值 | 含义 |
|----|------|
| `SUCCESS` | 执行成功，跳转到下一步 |
| `FAILED` | 执行失败 |
| `RUNNING` | 仍在运行，下次 tick 继续执行同一节点 |
| `TERMINATE` | 请求终止整个工作流 |
| `SKIP` | 跳过此节点 |

### 完整示例

创建文件 `workflow/nodes/screenshot_node.py`：

```python
"""自定义节点示例：保存当前帧截图到文件。"""

import time
from typing import Any, Dict, Optional

from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry


@NodeRegistry.register("screenshot")
class ScreenshotNode(NodeBase):
    """截图节点：将当前帧保存到文件。

    YAML 用法：
        step_save_frame:
          type: screenshot
          save_dir: "debug_frames"
          next: "step_continue"
    """

    @property
    def node_type(self) -> str:
        return "screenshot"

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        import os
        save_dir = self._config.get("save_dir", "debug_frames")
        os.makedirs(save_dir, exist_ok=True)

        filename = f"{save_dir}/frame_{int(time.time())}.png"

        # 这里可以调用 perception adapter 获取当前帧并保存
        # 简化示例：仅记录日志
        import logging
        logging.getLogger(__name__).info("截图已保存: %s", filename)

        return NodeResult.SUCCESS, self._config.get("next")
```

### 在 YAML 中使用

```yaml
step_debug_capture:
  type: screenshot
  save_dir: "debug_frames"
  next: "step_continue"
```

### 确保被加载

在 `workflow/registry.py` 的 `_ensure_builtin_loaded()` 中添加导入：

```python
@classmethod
def _ensure_builtin_loaded(cls) -> None:
    import workflow.nodes.action_node
    import workflow.nodes.perceive_node
    import workflow.nodes.condition_node
    import workflow.nodes.loop_node
    import workflow.nodes.wait_node
    import workflow.nodes.scene_node
    import workflow.nodes.terminate_node
    import workflow.nodes.log_node
    import workflow.nodes.screenshot_node    # 添加这一行
```

---

## 3. 自定义高级动作

### 方式一：在 ActionExecutor 中添加动作格式

编辑 `action/executor.py` 的 `_execute_single_action()` 方法：

```python
def _execute_single_action(self, action: Dict[str, Any], context) -> bool:
    # 新增：drag 拖拽动作
    if "drag" in action:
        from_x, from_y = action["drag"]["from"]
        to_x, to_y = action["drag"]["to"]
        duration_ms = action.get("duration_ms", 300)
        return self._adapter.drag(from_x, from_y, to_x, to_y, duration_ms)

    # ... 原有逻辑 ...
```

YAML 中使用：

```yaml
actions:
  - drag:
      from: [100, 200]
      to: [500, 600]
    duration_ms: 500
```

### 方式二：使用 builtin_actions.py

`action/builtin_actions.py` 提供了更复杂的高级动作封装。你可以在此文件中添加组合动作：

```python
def double_click_target(adapter, target_name: str, context, interval_ms: int = 100):
    """双击视觉目标。"""
    detection = context.find_detection(target_name)
    if detection:
        adapter.click(detection.cx, detection.cy, 50)
        adapter.wait(interval_ms)
        adapter.click(detection.cx, detection.cy, 50)
        return True
    return False
```

---

## 4. 事件监听

通过 `EventBus` 可以监听框架运行时的各种事件，实现自定义逻辑。

### 基本用法

```python
from core.events import EventBus, EVENT_TARGET_FOUND, EVENT_SCENE_CHANGED

def on_target_found(target_name: str, detection, **kwargs):
    print(f"目标出现: {target_name} at ({detection.cx}, {detection.cy})")

def on_scene_changed(old_scene: str, new_scene: str, **kwargs):
    print(f"场景切换: {old_scene} -> {new_scene}")

# 注册监听器
event_bus = engine.event_bus
event_bus.subscribe(EVENT_TARGET_FOUND, on_target_found)
event_bus.subscribe(EVENT_SCENE_CHANGED, on_scene_changed)
```

### 可用事件列表

| 事件 | 回调参数 | 触发时机 |
|------|---------|---------|
| `on_frame_processed` | `detections`, `context` | 每帧检测完成 |
| `on_target_found` | `target_name`, `detection` | 目标首次出现 |
| `on_target_lost` | `target_name` | 目标消失 |
| `on_scene_changed` | `old_scene`, `new_scene`, `context` | 场景切换 |
| `on_action_executed` | `action`, `context` | 动作执行完成 |
| `on_step_changed` | `prev_step`, `current_step`, `context` | 步骤切换 |
| `on_workflow_started` | `name` | 工作流启动 |
| `on_workflow_complete` | `name` | 工作流结束 |
| `on_error` | `error` | 异常发生 |
| `on_paused` | （无） | 引擎暂停 |
| `on_resumed` | （无） | 引擎恢复 |
| `on_stopped` | （无） | 引擎停止 |

### 实用示例：统计动作执行次数

```python
action_count = 0

def count_actions(**kwargs):
    global action_count
    action_count += 1

event_bus.subscribe("on_action_executed", count_actions)
```

### 实用示例：异常告警

```python
def alert_on_error(error: Exception, **kwargs):
    # 发送到通知系统
    send_notification(f"自动化异常: {error}")

event_bus.subscribe("on_error", alert_on_error)
```

---

## 5. 子模块适配

### 适配器架构

```
主框架 ──> perception/adapter.py ──> RocoKingdom-Spotting (子模块)
主框架 ──> action/adapter.py     ──> RocoKingdom-Clicker  (子模块)
```

主框架**只依赖 Adapter 的接口**，不直接调用子模块内部代码。当子模块 API 变更时，只需修改对应的 Adapter 文件。

### 子模块更新后的适配流程

1. 更新子模块：`git submodule update --remote`
2. 运行框架，观察是否有 ImportError 或 AttributeError
3. 阅读子模块的 changelog，确认 API 变更
4. 修改对应 Adapter 文件中的调用代码
5. 验证功能正常

### Spotting Adapter 关键接口

```python
# perception/adapter.py
class SpottingAdapter:
    def setup(self) -> None: ...
    def detect_frame(self) -> List[Detection]: ...
    def detect_target(self, name: str, min_score: float = 0.0) -> Optional[Detection]: ...
    def is_target_present(self, name: str, min_score: float = 0.0) -> bool: ...
    def cleanup(self) -> None: ...
```

### Clicker Adapter 关键接口

```python
# action/adapter.py
class ClickerAdapter:
    def setup(self) -> None: ...
    def click(self, x: int, y: int, hold_ms: int = 100) -> bool: ...
    def press_key(self, key_code: int, hold_ms: int = 50) -> bool: ...
    def move(self, x: int, y: int) -> bool: ...
    def wait(self, ms: int) -> bool: ...
    def execute_action(self, action_dict: dict) -> bool: ...
    def execute_sequence(self, actions: list) -> bool: ...
    def cleanup(self) -> None: ...
```

---

## 6. 调试技巧

### 6.1 启用详细日志

```bash
python main.py --workflow config/workflows/example_daily.yaml -v
```

`-v` 参数将日志级别设为 DEBUG，输出所有节点执行细节、检测结果、条件评估过程。

### 6.2 日志输出到文件

```bash
python main.py -w config/workflows/example_daily.yaml --log-file logs/debug.log
```

日志文件可用于事后分析执行流程。

### 6.3 在 YAML 中插入调试节点

```yaml
steps:
  step_before_battle:
    type: log
    message: "即将进入战斗，当前计数: ${battle_count}"
    level: debug
    next: "step_battle"
```

`log` 节点不影响执行流程，仅输出信息，适合在开发阶段追踪工作流进度。

### 6.4 使用 perceive 节点验证视觉检测

```yaml
step_debug_detect:
  type: perceive
  target: "my_target"
  min_score: 0.5          # 降低阈值观察是否能检测到
  timeout_ms: 3000
  on_found: "step_found"
  on_timeout: "step_not_found"
```

### 6.5 全局配置调整

在 `config/global.yaml` 中：

```yaml
# 降低循环速度便于观察
loop_interval: 1.0    # 改为 1 秒一次循环

# 启用调试日志
logging:
  level: "DEBUG"
  file: "logs/debug.log"
```

### 6.6 编程方式调试

```python
from core.engine import Engine

engine = Engine(config_path="config/global.yaml")
engine.setup()
engine.load_workflow("config/workflows/example_daily.yaml")

# 手动执行单次感知
engine._perceive()
print("检测结果:", engine.context.last_detections)
print("当前场景:", engine.context.current_scene)

# 手动推进一个 tick
engine._workflow_executor.start()
engine._workflow_executor.tick()
print("当前步骤:", engine._workflow_executor.current_step)
```
