# 工作流 DSL 完整参考

本文档是工作流 YAML 文件格式的完整参考手册。工作流文件定义了自动化脚本的全部逻辑。

---

## 1. 文件结构

一个工作流 YAML 文件包含以下顶层字段：

```yaml
name: "工作流名称"              # 必填：工作流标识名
version: "1.0"                  # 可选：版本号
description: "工作流描述"       # 可选：说明文字

variables:                      # 可选：全局变量定义
  key: value

scenes:                         # 可选：场景定义（用于场景状态机）
  scene_name: ...

start: "step_name"              # 可选：起始步骤（默认使用 steps 中的第一个）

global_timeout_sec: 600         # 可选：全局超时（秒），超时后强制终止

steps:                          # 必填：工作流步骤定义
  step_name:
    type: ...
    ...
```

---

## 2. 变量系统

### 定义变量

在 `variables` 段定义全局变量：

```yaml
variables:
  max_retry: 3
  timeout_sec: 300
  target_name: "boss_label"
```

### 引用变量

在任何字符串值中使用 `${var_name}` 语法引用变量：

```yaml
steps:
  step_check:
    type: condition
    conditions:
      - type: counter
        counter: "retry_count"
        operator: "<"
        value: "${max_retry}"    # 解析为 3
```

变量解析规则：
- 仅支持完整字符串替换（`"${var}"` 整体被替换）
- 变量不存在时保留原始字符串
- `log` 节点的消息支持内嵌变量：`"已重试 ${retry_count} 次"`

---

## 3. 节点类型详解

### 3.1 perceive — 视觉感知

等待指定视觉目标出现或消失。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"perceive"` |
| `target` | string | 是 | — | 要检测的目标名称（对应 Spotting 检测器中的模板名） |
| `min_score` | float | 否 | `0.0` | 最低置信度阈值 |
| `mode` | string | 否 | `"found"` | `"found"` = 等待出现，`"absent"` = 等待消失 |
| `timeout_ms` | int | 否 | `0`（无超时） | 超时时间（毫秒） |
| `on_found` | string | 否 | — | 目标找到后跳转的步骤 |
| `on_absent` | string | 否 | — | 目标消失后跳转的步骤（mode=absent 时） |
| `on_timeout` | string | 否 | — | 超时后跳转的步骤 |
| `next` | string | 否 | — | 默认下一步（兜底） |

**示例：**

```yaml
step_wait_menu:
  type: perceive
  target: "main_menu_label"
  min_score: 0.85
  timeout_ms: 10000
  on_found: "step_navigate"
  on_timeout: "step_retry"
```

**行为说明：**
- 每次引擎 tick 检查一次目标是否存在
- 目标满足条件时返回 SUCCESS 并跳转
- 未满足时返回 RUNNING（下次 tick 继续检查）
- 超时后跳转 `on_timeout`

---

### 3.2 action — 动作执行

执行一组键鼠动作序列。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"action"` |
| `actions` | list | 是 | — | 动作列表（见"动作格式参考"） |
| `next` | string | 否 | — | 执行完毕后跳转的步骤 |

**示例：**

```yaml
step_click:
  type: action
  actions:
    - click_target: "start_button"
      fallback_pos: [960, 540]
      min_score: 0.8
    - wait: 1000
    - click: [1200, 700]
    - key: 13
  next: "step_wait_result"
```

---

### 3.3 condition — 条件分支

评估一组条件，根据结果跳转到不同步骤。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"condition"` |
| `conditions` | list | 是 | — | 条件列表（见"条件类型参考"） |
| `logic` | string | 否 | `"AND"` | 多条件逻辑：`"AND"` 全部满足 / `"OR"` 任一满足 |
| `on_true` | string | 否 | — | 条件满足时跳转 |
| `on_false` | string | 否 | — | 条件不满足时跳转 |

**示例：**

```yaml
step_check_done:
  type: condition
  conditions:
    - type: counter
      counter: "quest_done_count"
      operator: ">="
      value: 5
  on_true: "step_end"
  on_false: "step_start"
```

**多条件 OR 示例：**

```yaml
step_check_either:
  type: condition
  logic: "OR"
  conditions:
    - type: visual
      target: "error_dialog"
      min_score: 0.8
    - type: time
      duration_sec: 300
      mode: elapsed
  on_true: "step_recover"
  on_false: "step_continue"
```

---

### 3.4 loop — 循环执行

重复执行 body 中的动作，直到退出条件满足或达到最大迭代次数。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"loop"` |
| `max_iterations` | int | 否 | `0`（无限） | 最大循环次数 |
| `body` | list | 是 | — | 循环体（内联步骤列表） |
| `exit_condition` | dict | 否 | — | 退出条件（条件类型格式） |
| `on_exit` | string | 否 | — | 退出条件满足时跳转 |
| `on_max_iterations` | string | 否 | — | 达到最大次数时跳转 |
| `next` | string | 否 | — | 兜底下一步 |

**body 中支持的步骤类型：**
- `type: action` — 内联执行动作序列
- `type: perceive` — 内联感知（找到目标时可跳转循环外步骤）

**示例：**

```yaml
step_battle_loop:
  type: loop
  max_iterations: 20
  body:
    - type: perceive
      target: "my_turn_indicator"
      min_score: 0.75
      on_found: "step_use_skill"    # 可跳出循环到外部步骤
    - type: action
      actions:
        - wait: 500
  exit_condition:
    type: visual
    target: "victory_label"
    min_score: 0.85
  on_exit: "step_collect"
  on_max_iterations: "step_collect"
```

---

### 3.5 wait — 等待

纯等待指定时长。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"wait"` |
| `duration_ms` | int | 否 | `1000` | 等待时长（毫秒） |
| `jitter_ms` | int | 否 | `0` | 随机抖动范围（毫秒），实际等待 = duration ± jitter |
| `next` | string | 否 | — | 等待结束后跳转 |

**示例：**

```yaml
step_pause:
  type: wait
  duration_ms: 2000
  jitter_ms: 500       # 实际等待 1500~2500ms（模拟人类行为）
  next: "step_continue"
```

---

### 3.6 scene — 场景切换

手动切换当前场景（更新上下文中的 `current_scene`）。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"scene"` |
| `scene` | string | 是 | — | 目标场景名称 |
| `next` | string | 否 | — | 切换后跳转 |

**示例：**

```yaml
step_enter_battle:
  type: scene
  scene: "battle_field"
  next: "step_battle_loop"
```

---

### 3.7 terminate — 终止工作流

结束工作流执行。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"terminate"` |
| `message` | string | 否 | `"工作流结束"` | 终止时输出的日志消息 |

**示例：**

```yaml
step_end:
  type: terminate
  message: "所有任务已完成！"
```

---

### 3.8 log — 日志输出

输出调试/状态信息到日志。

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | — | 固定为 `"log"` |
| `message` | string | 是 | — | 日志消息（支持 `${var}` 变量替换） |
| `level` | string | 否 | `"info"` | 日志级别：`debug` / `info` / `warning` / `error` |
| `next` | string | 否 | — | 下一步 |

**示例：**

```yaml
step_debug:
  type: log
  message: "第 ${battle_count} 次对战开始"
  level: info
  next: "step_battle"
```

---

## 4. 条件类型参考

条件用于 `condition` 节点的 `conditions` 列表和 `loop` 节点的 `exit_condition`。

### 4.1 visual — 视觉条件

判断画面中是否存在/消失某个视觉目标。

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"visual"` |
| `target` | string | 目标名称 |
| `min_score` | float | 最低置信度（默认 0.0） |
| `mode` | string | `"found"` = 存在（默认）/ `"absent"` = 消失 |

```yaml
- type: visual
  target: "victory_label"
  min_score: 0.85
  mode: found
```

### 4.2 time — 时间条件

基于工作流运行时间的条件。

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"time"` |
| `duration_sec` | float | 时间阈值（秒） |
| `mode` | string | `"elapsed"` = 已运行超过（默认）/ `"remaining"` = 剩余时间不足 |

```yaml
- type: time
  duration_sec: 300
  mode: elapsed       # 运行超过 5 分钟时为 true
```

### 4.3 counter — 计数器条件

比较计数器的值。

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"counter"` |
| `counter` | string | 计数器名称 |
| `operator` | string | 比较运算符 |
| `value` | any | 比较值（支持 `${var}` 引用） |

```yaml
- type: counter
  counter: "battle_count"
  operator: ">="
  value: "${max_battles}"
```

### 4.4 variable — 变量条件

比较工作流变量的值。

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"variable"` |
| `variable` | string | 变量名称 |
| `operator` | string | 比较运算符 |
| `value` | any | 比较值 |

```yaml
- type: variable
  variable: "current_mode"
  operator: "=="
  value: "hard"
```

### 4.5 composite — 组合条件

将多个条件用逻辑运算符组合。

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"composite"` |
| `operator` | string | `"AND"` / `"OR"` / `"NOT"` |
| `conditions` | list | 子条件列表 |

```yaml
- type: composite
  operator: "AND"
  conditions:
    - type: counter
      counter: "retry_count"
      operator: "<"
      value: 3
    - type: visual
      target: "retry_button"
      min_score: 0.7
```

### 4.6 scene — 场景条件

判断当前是否处于指定场景。

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"scene"` |
| `scene` | string | 场景名称 |

```yaml
- type: scene
  scene: "battle_field"
```

### 支持的运算符

所有使用 `operator` 字段的条件支持以下比较运算符：

| 运算符 | 含义 |
|--------|------|
| `==` | 等于 |
| `!=` | 不等于 |
| `>` | 大于 |
| `>=` | 大于等于 |
| `<` | 小于 |
| `<=` | 小于等于 |

---

## 5. 场景定义

在 `scenes` 段定义场景识别规则（可选，用于场景状态机）：

```yaml
scenes:
  main_menu:                      # 场景名称
    detect:
      target: "main_menu_label"   # 检测目标名
      min_score: 0.85             # 最低置信度
    on_enter: "step_navigate"     # 进入此场景时跳转的步骤

  battle_field:
    detect:
      target: "battle_ui_label"
      min_score: 0.80
    on_enter: "step_battle"
```

**字段说明：**

| 字段 | 说明 |
|------|------|
| `detect.target` | 当该目标被检测到时，认为处于此场景 |
| `detect.min_score` | 检测置信度阈值 |
| `on_enter` | 首次进入此场景时自动跳转的工作流步骤 |

---

## 6. 动作格式参考

`action` 节点的 `actions` 列表支持以下动作格式：

### click_target — 点击视觉目标

```yaml
- click_target: "button_name"     # 目标名称（必填）
  min_score: 0.8                  # 最低置信度（默认 0.0）
  fallback_pos: [960, 540]        # 目标未找到时的备选坐标
  hold_ms: 100                    # 按住时长（默认 100ms）
  offset_x: 0                     # X 偏移
  offset_y: 0                     # Y 偏移
  retry: 3                        # 失败重试次数（默认 0）
  retry_delay_ms: 500             # 重试间隔（默认 500ms）
```

### click — 坐标点击

```yaml
- click: [500, 300]               # [x, y] 坐标
  hold_ms: 100                    # 按住时长（默认 100ms）
```

### key — 按键

```yaml
- key: 32                         # 虚拟键码（此处为空格键）
  hold_ms: 50                     # 按住时长（默认 50ms）
```

常用键码：空格=32, 回车=13, ESC=27, Tab=9

### wait — 等待

```yaml
- wait: 1000                      # 等待毫秒数
```

### 标准 Clicker 格式

兼容 RocoKingdom-Clicker 的原生动作字典格式：

```yaml
- type: "click"
  x: 500
  y: 300
  hold_ms: 100

- type: "key"
  key_code: 32
  hold_ms: 50

- type: "move"
  x: 500
  y: 300
```

### 通用重试字段

所有动作字典都支持以下重试字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `retry` | int | `0` | 失败后最大重试次数 |
| `retry_delay_ms` | int | `500` | 每次重试的间隔（毫秒） |

---

## 7. 完整示例解读

### example_daily.yaml — 每日任务

```yaml
name: "每日任务自动化"
version: "1.0"
description: "自动完成每日任务流程（示例）"

# 定义可复用变量
variables:
  max_retry: 3          # 最大重试次数
  timeout_sec: 300      # 全局超时（未在此使用）

# 场景定义：当检测到特定 UI 标签时自动识别场景
scenes:
  main_menu:
    detect:
      target: "main_menu_label"
      min_score: 0.85
    on_enter: "step_navigate"     # 进入主菜单时自动跳转
  battle_field:
    detect:
      target: "battle_ui_label"
      min_score: 0.80
    on_enter: "step_battle"

# 起始步骤
start: "step_start"

steps:
  # 第 1 步：等待主菜单出现
  step_start:
    type: perceive
    target: "main_menu_label"     # 等待这个视觉目标
    min_score: 0.85
    timeout_ms: 10000             # 最多等 10 秒
    on_found: "step_navigate"     # 找到 → 继续
    on_timeout: "step_retry"      # 超时 → 重试逻辑

  # 第 2 步：导航到每日任务
  step_navigate:
    type: action
    actions:
      - click_target: "daily_quest_button"   # 点击"每日任务"按钮
        fallback_pos: [900, 500]             # 找不到就点这个坐标
        min_score: 0.8
      - wait: 1000                           # 等 1 秒让界面加载
      - click: [1200, 700]                   # 点击确认坐标
    next: "step_battle"

  # 第 3 步：战斗循环
  step_battle:
    type: loop
    max_iterations: 10              # 最多循环 10 次
    body:
      - type: perceive              # 等待敌人出现
        target: "enemy_pet"
        min_score: 0.7
        timeout_ms: 5000
        on_found: "step_attack"     # 找到敌人 → 跳到攻击步骤
      - type: action                # 没找到就按空格跳过
        actions:
          - key: 32
          - wait: 1500
    exit_condition:                 # 胜利时退出循环
      type: visual
      target: "victory_label"
      min_score: 0.85
    on_exit: "step_collect"
    on_max_iterations: "step_collect"

  # 第 4 步：释放技能
  step_attack:
    type: action
    actions:
      - click_target: "skill_button_1"
        fallback_pos: [400, 800]
      - wait: 800
      - click_target: "skill_button_2"
        fallback_pos: [500, 800]
      - wait: 2000                  # 等待技能动画
    next: "step_battle"             # 回到战斗循环

  # 第 5 步：领取奖励
  step_collect:
    type: action
    actions:
      - click_target: "reward_button"
        fallback_pos: [960, 600]
      - wait: 1000
    next: "step_check_continue"

  # 第 6 步：检查是否继续
  step_check_continue:
    type: condition
    conditions:
      - type: counter
        counter: "quest_done_count"
        operator: ">="
        value: 5                    # 完成 5 次后结束
    on_true: "step_end"
    on_false: "step_start"          # 否则从头开始

  # 重试逻辑
  step_retry:
    type: condition
    conditions:
      - type: counter
        counter: "retry_count"
        operator: "<"
        value: "${max_retry}"       # 引用变量：最多重试 3 次
    on_true: "step_start"
    on_false: "step_end"

  # 结束
  step_end:
    type: terminate
    message: "每日任务完成！"
```

### example_battle.yaml — PVP 对战

参见 `config/workflows/example_battle.yaml`，演示了：
- 匹配等待（perceive + 长超时）
- 回合制战斗循环（loop + 内联 perceive）
- 多技能释放序列
- 对战计数与条件退出
- 场景定义（lobby / battle / result）
