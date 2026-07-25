# 架构设计详解

本文档面向开发者，详细说明 Automatic-RocoKingdom 框架的内部架构设计。

---

## 1. 总体架构

框架采用分层架构，核心是一个 PDA (Perceive-Decide-Act) 循环引擎：

```
┌─────────────────────────────────────────────────────────────────┐
│                         main.py (CLI 入口)                       │
└────────────────────────────────┬────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────┐
│                      core/engine.py (PDA 主循环)                 │
│                                                                 │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐       │
│   │  Perceive    │──>│   Decide     │──>│     Act      │       │
│   │  (感知阶段)  │   │  (决策阶段)  │   │  (执行阶段)  │       │
│   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘       │
│          │                  │                   │               │
└──────────┼──────────────────┼───────────────────┼───────────────┘
           │                  │                   │
┌──────────▼──────┐  ┌───────▼────────┐  ┌──────▼──────────┐
│  perception/    │  │  workflow/     │  │   action/       │
│  (感知层)       │  │  (工作流引擎)  │  │   (动作层)      │
│                 │  │  conditions/   │  │                 │
│  SpottingAdapter│  │  scene/        │  │  ClickerAdapter │
│  SceneDetector  │  │                │  │  ActionExecutor │
│  TargetTracker  │  │                │  │                 │
└─────────────────┘  └────────────────┘  └─────────────────┘
           │                  │                   │
┌──────────▼──────────────────▼───────────────────▼───────────────┐
│                    submodules/ (Git 子模块)                       │
│         RocoKingdom-Spotting          RocoKingdom-Clicker        │
│         (视觉检测引擎)                (键鼠控制引擎)             │
└─────────────────────────────────────────────────────────────────┘
```

**横切关注点：**

```
┌────────────────────────────────────────────────────┐
│  core/context.py    — RuntimeContext (数据共享)     │
│  core/events.py     — EventBus (事件通信)          │
│  core/lifecycle.py  — LifecycleManager (状态管理)  │
│  utils/             — 日志、配置、重试工具          │
└────────────────────────────────────────────────────┘
```

---

## 2. 核心引擎 (`core/`)

### 2.1 Engine — PDA 主循环

`core/engine.py` 中的 `Engine` 类是框架的心脏，负责：

1. **初始化** 各子系统（感知、动作、工作流、场景状态机）
2. **驱动** PDA 主循环
3. **协调** 工作流执行器推进步骤

**生命周期方法：**

```python
engine = Engine(config_path="config/global.yaml")
engine.setup()           # 初始化所有子系统
engine.load_workflow("config/workflows/example_daily.yaml")  # 加载工作流
engine.run()             # 阻塞运行 PDA 主循环
# 或
engine.run_async()       # 后台线程运行（非阻塞）
```

**控制接口：**

```python
engine.pause()    # 暂停
engine.resume()   # 恢复
engine.stop()     # 停止
engine.state      # 当前状态 (EngineState 枚举)
```

**主循环伪代码：**

```python
def run(self):
    self.lifecycle.start()
    self.workflow_executor.start()

    while self.lifecycle.should_continue():
        self.lifecycle.wait_if_paused()     # 暂停检查

        self._perceive()                    # 感知：截屏 + 检测
        self._decide()                      # 决策：推进工作流节点

        if self.workflow_executor.is_complete:
            break

        self.lifecycle.interruptible_sleep(loop_interval)  # 帧率控制

    self._shutdown()
```

### 2.2 RuntimeContext — 运行时上下文

`core/context.py` 定义了 `RuntimeContext` 数据类，是所有节点共享的数据容器：

```python
@dataclass
class RuntimeContext:
    variables: Dict[str, Any]          # 工作流变量（YAML variables 段）
    counters: Dict[str, int]           # 计数器（条件判断用）
    current_scene: Optional[str]       # 当前场景名称
    last_detections: List[Detection]   # 最近一帧检测结果
    workflow_name: str                 # 当前工作流名
    step_name: str                     # 当前步骤名
    start_time: float                  # 工作流启动时间戳
    iteration: int                     # 当前循环轮次
    prev_step_name: str                # 上一步骤名
```

**便捷方法：**

| 方法 | 说明 |
|------|------|
| `get_variable(name, default)` | 获取变量值 |
| `set_variable(name, value)` | 设置变量 |
| `increment_counter(name, amount)` | 递增计数器 |
| `get_counter(name)` | 获取计数器值 |
| `elapsed_seconds()` | 已运行秒数 |
| `find_detection(name, min_score)` | 在检测结果中查找目标 |
| `resolve_value(value)` | 解析 `${var_name}` 变量引用 |

**Detection 数据类：**

```python
@dataclass
class Detection:
    name: str          # 目标名称
    category: str      # 检测器分类
    score: float       # 置信度
    x, y, w, h: int   # 边界框
    cx, cy: int        # 中心点（计算属性）
```

### 2.3 EventBus — 事件总线

`core/events.py` 实现了一个简单的同步事件总线，用于模块间解耦通信：

```python
bus = EventBus()
bus.subscribe("on_target_found", my_handler)   # 注册监听
bus.emit("on_target_found", target_name="x")   # 触发事件
bus.unsubscribe("on_target_found", my_handler) # 移除监听
```

**事件类型表：**

| 事件常量 | 触发时机 | 典型用途 |
|---------|---------|---------|
| `EVENT_FRAME_PROCESSED` | 每帧检测完成 | 场景识别、目标追踪 |
| `EVENT_TARGET_FOUND` | 目标首次出现 | 触发工作流步骤 |
| `EVENT_TARGET_LOST` | 目标消失 | 超时/重试逻辑 |
| `EVENT_SCENE_CHANGED` | 场景切换 | 日志、UI 更新 |
| `EVENT_ACTION_EXECUTED` | 动作执行完成 | 计数、状态更新 |
| `EVENT_STEP_CHANGED` | 工作流步骤切换 | 日志、调试 |
| `EVENT_WORKFLOW_STARTED` | 工作流启动 | 初始化通知 |
| `EVENT_WORKFLOW_COMPLETE` | 工作流结束 | 清理、通知 |
| `EVENT_ERROR` | 异常发生 | 容错、告警 |
| `EVENT_PAUSED` | 引擎暂停 | 状态同步 |
| `EVENT_RESUMED` | 引擎恢复 | 状态同步 |
| `EVENT_STOPPED` | 引擎停止 | 资源清理 |

### 2.4 LifecycleManager — 生命周期管理

`core/lifecycle.py` 管理引擎的运行状态转换：

```
         start()
IDLE ──────────────> RUNNING
  ^                    │  │
  │          pause()   │  │  stop()
  │                    ▼  │
  │                  PAUSED│
  │                    │  │
  │         resume()   │  │
  │                    ▼  │
  │                  RUNNING
  │                    │
  │         stop()     │  set_error()
  │                    ▼       │
  └──────────────── STOPPED  ERROR
```

**关键方法：**

| 方法 | 说明 |
|------|------|
| `start()` | IDLE/STOPPED → RUNNING |
| `pause()` | RUNNING → PAUSED |
| `resume()` | PAUSED → RUNNING |
| `stop()` | 任意 → STOPPED |
| `set_error(msg)` | 标记 ERROR 状态 |
| `should_continue()` | 是否应继续循环 |
| `wait_if_paused()` | 暂停时阻塞等待 |
| `interruptible_sleep(sec)` | 可中断的睡眠 |

---

## 3. 感知层 (`perception/`)

### 3.1 SpottingAdapter — 子模块适配器

`perception/adapter.py` 将 RocoKingdom-Spotting 的"持续运行循环"改造为"按需单帧检测"接口：

```python
adapter = SpottingAdapter(config)
adapter.setup()                              # 初始化检测器

detections = adapter.detect_frame()          # 获取当前帧所有检测结果
detection = adapter.detect_target("button")  # 查找特定目标
present = adapter.is_target_present("label") # 判断目标是否存在

adapter.cleanup()                            # 释放资源
```

**实现原理：**
- 通过 `sys.path` 注入子模块路径，直接 import 其内部模块
- 复用 Spotting 的 `Pipeline`、`DetectorRegistry`、窗口捕获等组件
- 每次 `detect_frame()` 执行一次完整的：截屏 → 检测 → 返回结果

### 3.2 SceneDetector — 场景识别器

`perception/scene_detector.py` 根据检测结果判断当前所处场景：

- 每个场景定义一组"识别规则"（哪些目标出现表示在该场景）
- 支持 `min_score` 置信度阈值
- 场景变化时触发 `EVENT_SCENE_CHANGED` 事件

### 3.3 TargetTracker — 目标追踪器

`perception/target_tracker.py` 持续跟踪特定目标的状态：

- **出现** (found)：目标首次被检测到
- **持续** (tracking)：目标持续存在
- **消失** (lost)：目标从画面中消失

---

## 4. 动作层 (`action/`)

### 4.1 ClickerAdapter — 子模块适配器

`action/adapter.py` 封装 RocoKingdom-Clicker 的 Interception 驱动：

```python
adapter = ClickerAdapter(config)
adapter.setup()

adapter.click(x, y, hold_ms=100)       # 鼠标点击
adapter.press_key(key_code, hold_ms=50) # 按键
adapter.move(x, y)                      # 移动鼠标
adapter.wait(ms)                        # 等待
adapter.execute_action(action_dict)     # 执行标准动作字典
adapter.execute_sequence(actions_list)  # 执行动作序列

adapter.cleanup()
```

### 4.2 ActionExecutor — 动作序列执行器

`action/executor.py` 在 ClickerAdapter 基础上增加高级动作支持：

**支持的动作格式：**

| 格式 | 示例 | 说明 |
|------|------|------|
| `click_target` | `{click_target: "btn", fallback_pos: [100,200]}` | 点击视觉目标 |
| `click` | `{click: [500, 300]}` | 直接坐标点击 |
| `key` | `{key: 32}` | 按键（虚拟键码） |
| `wait` | `{wait: 1000}` | 等待（毫秒） |
| 标准格式 | `{type: "click", x: 100, y: 200}` | Clicker 原生格式 |

**动作级重试机制（容错）：**

```yaml
actions:
  - click_target: "rare_button"
    retry: 3              # 失败后最多重试 3 次
    retry_delay_ms: 500   # 每次重试间隔 500ms
```

### 4.3 click_target 工作原理

```
1. 从 context.last_detections 中查找目标名称匹配的 Detection
2. 如果找到 → 点击 Detection 的中心点 (cx, cy) + 偏移
3. 如果未找到 → 使用 fallback_pos 备选坐标
4. 如果都没有 → 返回失败（触发重试或跳过）
```

---

## 5. 工作流引擎 (`workflow/`)

### 5.1 WorkflowLoader — 加载与校验

`workflow/loader.py` 负责：
- 解析 YAML 文件
- 校验必要字段（`name`, `steps`）
- 校验步骤引用完整性（所有 `next`/`on_found` 等引用的步骤必须存在）
- 返回标准化的工作流定义字典

### 5.2 WorkflowExecutor — 节点调度

`workflow/executor.py` 是 tick 驱动的节点调度器：

```python
executor = WorkflowExecutor(workflow_def, context, ...)
executor.start()                    # 进入起始步骤

while not executor.is_complete:
    executor.tick()                 # 执行当前节点一次迭代
```

**tick() 逻辑：**

```
1. 检查全局超时 → 超时则强制终止
2. 执行当前节点 execute(context)
3. 根据返回结果：
   - TERMINATE → 工作流结束
   - RUNNING   → 下次 tick 继续同一节点
   - SUCCESS/FAILED/SKIP → 跳转到下一步骤
4. 异常捕获 → 不中断引擎，跳转 on_error 步骤
```

**全局超时保护：**

```yaml
# 在工作流 YAML 顶层设置
global_timeout_sec: 600   # 10 分钟后强制终止
```

### 5.3 NodeRegistry — 节点注册表

`workflow/registry.py` 实现了插件化的节点类型注册：

```python
@NodeRegistry.register("my_node_type")
class MyNode(NodeBase):
    def execute(self, context):
        ...
        return NodeResult.SUCCESS, "next_step"
```

YAML 中使用 `type: "my_node_type"` 即可自动实例化。

---

## 6. 条件系统 (`conditions/`)

### 6.1 设计模式

```python
# 抽象基类
class ConditionBase(ABC):
    def __init__(self, config: Dict[str, Any]): ...
    @abstractmethod
    def evaluate(self, context: RuntimeContext) -> bool: ...

# 注册表
class ConditionRegistry:
    @classmethod
    def register(cls, type_name: str): ...   # 装饰器注册
    @classmethod
    def create(cls, type_name: str, config: Dict) -> ConditionBase: ...
```

### 6.2 内置条件类型

| 类型 | 参数 | 说明 |
|------|------|------|
| `visual` | `target`, `min_score`, `mode`(found/absent) | 视觉目标存在/消失 |
| `time` | `duration_sec`, `mode`(elapsed/remaining) | 时间条件 |
| `counter` | `counter`, `operator`, `value` | 计数器比较 |
| `variable` | `variable`, `operator`, `value` | 变量比较 |
| `composite` | `operator`(AND/OR/NOT), `conditions[]` | 组合条件 |
| `scene` | `scene` | 当前场景匹配 |

**支持的运算符：** `==`, `!=`, `>`, `>=`, `<`, `<=`

---

## 7. 场景状态机 (`scene/`)

### 7.1 组件

| 文件 | 类 | 职责 |
|------|---|------|
| `state_machine.py` | `SceneStateMachine` | 有限状态机主体，管理当前场景、驱动转换 |
| `scene_config.py` | `SceneConfig`, `SceneDefinition` | 场景定义与检测规则数据类 |
| `transitions.py` | `TransitionManager`, `Transition` | 转换规则管理与回调绑定 |

### 7.2 工作流程

```
1. 从 YAML 的 scenes 段加载场景定义
2. 每帧 detect_frame() 后调用 state_machine.update(detections, context)
3. 对每个场景的检测规则进行匹配
4. 如果匹配到新场景 → 触发转换 → 执行 on_enter 绑定的步骤
5. 发出 EVENT_SCENE_CHANGED 事件
```

### 7.3 YAML 中的场景定义

```yaml
scenes:
  main_menu:
    detect:
      target: "main_menu_label"   # 检测目标名
      min_score: 0.85             # 最低置信度
    on_enter: "step_navigate"     # 进入场景时跳转的步骤
```

---

## 8. 容错机制

框架在多个层级提供容错保护：

| 层级 | 机制 | 配置方式 |
|------|------|---------|
| **动作级** | 单个动作失败后重试 | `retry: N`, `retry_delay_ms: M` |
| **步骤级** | 步骤超时/失败跳转 fallback | `on_timeout`, `on_error` 字段 |
| **工作流级** | 全局超时强制终止 | `global_timeout_sec: N` |
| **引擎级** | 所有节点 try-catch，异常不中断引擎 | 自动 |
| **场景级** | 检测到意外场景时执行恢复流程 | 场景状态机 |

---

## 9. 数据流：一帧的完整路径

```
┌────────────────────────────────────────────────────────────────────┐
│ 1. Engine._perceive()                                              │
│    └─> SpottingAdapter.detect_frame()                              │
│        ├─> 截取窗口画面 (WindowCapture)                            │
│        ├─> 运行所有启用的检测器 (Template/SIFT/YOLO)               │
│        └─> 返回 List[Detection]                                    │
│                                                                    │
│ 2. context.last_detections = detections                            │
│    └─> EventBus.emit(EVENT_FRAME_PROCESSED)                        │
│                                                                    │
│ 3. SceneStateMachine.update(detections)                            │
│    └─> 匹配场景规则 → 可能触发场景转换                             │
│                                                                    │
│ 4. Engine._decide()                                                │
│    └─> WorkflowExecutor.tick()                                     │
│        └─> 当前节点.execute(context)                               │
│            ├─ perceive_node: 检查 context 中是否有目标             │
│            ├─ condition_node: 评估条件列表                         │
│            └─ action_node: 调用 ActionExecutor                     │
│                                                                    │
│ 5. ActionExecutor.execute_actions(actions, context)                │
│    └─> 对每个动作：                                                │
│        ├─ click_target: 从 context 查找目标坐标 → ClickerAdapter   │
│        ├─ click/key/wait: 直接调用 ClickerAdapter                  │
│        └─ 失败时执行重试循环                                       │
│                                                                    │
│ 6. ClickerAdapter → Interception 驱动 → 内核级键鼠事件注入         │
└────────────────────────────────────────────────────────────────────┘
```
