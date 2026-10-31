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

### 3.9 pet_hunt — 捕猎循环（识别 → 瞄准 → 丢球 → 补回轴线 → 单向扫视）

把「视觉识别精灵 → 移动鼠标瞄准 → 长按左键蓄力丢球 → 补回视角轴线 → 一直朝同一方向
慢速转视角找下一只」整条闭环封装在**一个节点内部**。节点自维持状态机，每个 PDA tick
推进一小步并返回 `RUNNING`，因此无需在 YAML 里用 `loop` + `perceive` + `action`
拼流程，也不会有跨节点的坐标过期问题。

内部状态流转：

```
scan ──发现目标(连续 N 帧确认)──> aim ──> settle ──> throw ──> cooldown ──┐
  ^                                                          (补回轴线)   │
  └──────────────────────────────────────────────────────────────────────┘
  │
  └──未发现目标──> 慢速转视角一小步（默认一直朝同一方向，360° 自己转回来）
```

**目标识别**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `type` | string | — | 固定为 `"pet_hunt"` |
| `target` | string | `"sansan"` | 要捕捉的 YOLO 类别名 |
| `min_score` | float | `0.5` | 最低置信度（子模块 `config.json` 里 YOLO 的 `conf` 是硬下限） |
| `confirm_frames` | int | `2` | 连续命中几帧才开火，防止单帧误检白丢球 |
| `target_select` | string | `"reference"` | 多目标时怎么选：`reference` = 离瞄准基准点最近；`score` = 置信度最高；`largest` = 检测框面积最大（近似离角色最近） |

**关于 `target_select`：**

默认 `reference` 有两个好处：转视角代价最小（最快能丢出球），而且增益标定时
挑的是**离上次瞄准点最近的那只**，所以观测的始终是刚才在瞄的同一只精灵，
不会被旁边的高分目标带偏。截帧区域未知、基准点算不出来时自动退回 `score`。

**瞄准**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `aim_strategy` | string | `"relative"` | `relative` = 纯相对位移转视角（默认）；`planned` = PathPlanner 拟人绝对定位；`direct` = 直接绝对定位 |
| `aim_reference` | string | `"center"` | 瞄准基准点：`center` = 客户区中心；`cursor` = 鼠标光标（仅当游戏真用光标瞄准时） |
| `aim_reference_offset_x` | float | `0` | 基准点微调（实测落点偏左就把 x 调正） |
| `aim_reference_offset_y` | float | `0` | 基准点微调（实测落点偏上就把 y 调正） |
| `aim_gain_x` | float | `-1.0` | 水平比率 = 画面内容位移 ÷ 鼠标位移，**带符号**；由 `tools/calibrate_aim.py` 离线实测后填入 |
| `aim_gain_y` | float | `-1.0` | 垂直比率，同上。两轴符号/量级可能差很多，必须分开存 |
| `aim_max_move_px` | float | `3000` | 单事件位移上限，只在比率配错（接近 0）时兜底 |
| `aim_path_planner` | bool | `false` | `true` = 用 PathPlanner 生成拟人轨迹（加减速+弧线+过冲）执行这段位移 |
| `aim_path_steps` | int | `20` | 仅 `aim_path_planner` 生效：轨迹分段数，越多越平滑 |
| `path_strategy` | string | `"fitts"` | 扰动策略：`fitts` / `sine`（`aim_path_planner` 与 `planned` 都用它） |
| `aim_duration_ms` | int | `350` | 移动耗时：拟人轨迹下是走完整条轨迹的总时长；`planned`/`direct` 是绝对定位耗时 |
| `aim_settle_ms` | int | `120` | 位移后等**相机到位**再丢球；设为 `0` 则同一 tick 内直接丢 |
| `aim_offset_x` | int | `0` | 目标点 X 偏移（正值向右） |
| `aim_offset_y` | int | `0` | 目标点 Y 偏移（正值向下；球总丢到头顶时可调正） |

**关于 `aim_strategy`：**

- **`relative`（默认，推荐）** —— 只发**相对位移**转视角，把目标转到**瞄准基准点**上，
  全程不产生绝对定位事件。鼠标位移 = `−(目标 − 基准点) / 比率`，**一次发完**。
  基准点默认是**客户区中心**：很多游戏（含洛克王国：世界）的投掷是丢向镜头正前方的，
  画面里既没有准星也没有光标，中心是唯一确定已知的瞄准参考。
  比率定义为「画面内容位移 ÷ 鼠标位移」，**带符号、按轴分开**：实测本游戏
  （1920x1080）水平轴约 −1.15（量级接近 1，标准 mouse-look 符号），垂直轴只有
  约 −0.22（鼠标要走误差的 4.5 倍）。两轴差 5 倍，单一比率必然有一轴对不准 ——
  这正是"准星对不到目标上"的根因。
  比率由 `tools/calibrate_aim.py` **离线实测**后填进配置，运行时不改。
- **`planned`** —— PathPlanner 生成拟人路径后绝对定位。轨迹更像人，
  但一次绝对跳变本身可能把视角带偏。
- **`direct`** —— 单事件绝对定位，最快也最"机械"。

**一次到位，不复核、不迭代。**

流程就是：检测到目标 → 按比率算出位移 → 单事件发出 → 等相机到位（`aim_settle_ms`）
→ 长按丢球。中间**不看第二帧**。

曾经做过"移动后再检测一次、按观测到的实际位移修正比率、再补一步"的闭环，实测更差：
等一帧要付出 停稳 120ms + 截图 55ms + 推理 10ms，这段时间精灵已经走远，
复核瞄的是它**刚才**的位置，越复核越偏。精度改由离线实测的比率保证。

`aim_settle_ms` 等的是**相机**（平滑插值需要时间，不等就会按半截视角的方向把球丢出去），
不是重新检测 —— 这两件事必须分清。

**位移的发送方式必须和标定时一致。**

`aim_path_planner: false`（默认）是**单事件**发送；`true` 则用 PathPlanner 生成
带加减速/弧线/过冲的拟人轨迹，拆成 `aim_path_steps` 个相对位移微步执行。
PathPlanner 保证各步之和**精确等于**目标位移（最后一步做校正），所以净位移一样，
差别只在轨迹形状和时间分布。

但游戏对"N 个微步"和"1 个大步"的响应**不一定等价** —— 这是踩过的坑：
标定工具用单事件测出比率，节点却拆成 35 个微步执行，测的和跑的不是一件事，
比率直接失效。所以：

- 切换 `aim_path_planner` 之后**必须重新标定**比率；
- `tools/calibrate_aim.py` 默认会**读工作流 YAML**（`--workflow`）里的
  `aim_path_planner` / `aim_path_steps` / `aim_duration_ms` / `path_strategy`，
  用与节点完全相同的方式发位移，从结构上杜绝这种不一致。工具启动时会打印它读到的发法。

扫视（pan）始终是单事件，"慢速"靠 `pan_dx` 小步长 + `pan_interval_ms` 间隔实现。
默认 `sweep_limit_px: 0` —— 一直朝同一方向转，转满 360° 自然回到原处，
不需要为了"看全一圈"来回扫。

**丢球**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `throw_hold_ms` | int | `300` | 长按左键蓄力时长（参考实现用 200~400ms；过长会过充导致球飞过头） |
| `catch_cooldown_ms` | int | `2000` | 丢球后冷却，等捕捉动画播完 |
| `restore_aim_after_throw` | bool | `true` | 冷却结束时把瞄准的相对位移**反向补回**：光标回原位、相机回原轴线（不补则每抓一只相机多偏一点，扫视线越来越斜） |
| `counter_name` | string | `"pets_thrown"` | 投掷次数写入的计数器/变量名，可在 `log` 节点用 `${pets_thrown}` 引用 |

**转视角搜索**（通过 Interception 发送**单事件相对位移**，即 mouse-look）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `pan_dx` | int | `60` | 每次扫视的位移量，越小转得越细 |
| `pan_dy` | int | `0` | 每次扫视的垂直位移量，一般保持 `0` 做纯水平扫视 |
| `pan_interval_ms` | int | `250` | 两次扫视之间的停顿 —— **"慢速"由此实现**，越大扫得越慢 |
| `sweep_limit_px` | int | `0` | `0` = 一直朝同一方向转，360° 自己转回来；`>0` = 单向累计这么多位移后反向（旧行为，搜索范围受限） |
| `vertical_step_px` | int | `0` | 仅在 `sweep_limit_px > 0` 时生效：每次反向时抬升/压低视角，形成"弓"字形搜索 |

**终止条件与跳转**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_catches` | int | `0` | 投掷多少次后结束，`0` = 无限循环（靠 Ctrl+C 停止）→ `next` |
| `duration_sec` | float | `0` | 最长运行秒数，`0` = 不限时 → `on_timeout` |
| `scene_target` | string | — | 可选场景守卫：该视觉目标消失即退出 → `on_absent` |
| `scene_min_score` | float | `0.8` | 场景守卫的最低置信度 |
| `next` | string | — | 达到 `max_catches` 后的下一步 |
| `on_timeout` | string | `next` | 达到 `duration_sec` 后的下一步 |
| `on_absent` | string | `next` | 场景守卫目标消失后的下一步 |
| `on_error` | string | — | 动作执行器不可用时的下一步 |

**安全行为：**

- 截不到游戏画面时（窗口未找到 / 不在前台，见 `RuntimeContext.frame_captured`），
  节点**不会转视角、也不会按左键**，只每 5 秒告警一次。否则"看不到"会被误判成
  "画面里没有精灵"，导致脚本一直甩动你正在使用的真实鼠标、把左键点到别的窗口上。
- 键鼠动作失败（驱动抽风、拿不到截帧区域等）时不会立刻重试，而是退避
  `_FAIL_BACKOFF_SEC`（0.5s）。直接回扫描的话，下一帧又会选中同一个目标、又失败，
  持续性故障下就是以满帧率空转刷日志。

**示例**（完整可运行版本见 `config/workflows/catch_pet.yaml`）：

```yaml
variables:
  target_pet: "sansan"

steps:
  step_hunt:
    type: pet_hunt
    target: "${target_pet}"
    min_score: 0.5
    confirm_frames: 2
    throw_hold_ms: 300         # 长按蓄力 0.3 秒丢球
    catch_cooldown_ms: 2000
    restore_aim_after_throw: true   # 冷却结束把瞄准位移反向补回，恢复视角轴线
    aim_gain_x: -1.15          # 实测比率：只用于首步逼近，之后自动在线标定
    aim_gain_y: -0.22
    aim_step_px: 40            # 后续每步限幅
    aim_tolerance_px: 6        # 收敛阈值
    pan_dx: 60                 # 每次扫视转 60（单事件）
    pan_interval_ms: 250       # 每 250ms 扫一次 → 慢速扫视
    sweep_limit_px: 0          # 0 = 一直朝同一方向转，360° 自己转回来
    max_catches: 0             # 无限循环
    next: "step_summary"

  step_summary:
    type: log
    message: "捕猎结束，共投掷 ${pets_thrown} 次精灵球"
    next: "step_end"

  step_end:
    type: terminate
```

**前置条件：** `submodules/RocoKingdom-Spotting/models/best.pt` 必须存在
（`models/` 被子模块 gitignore，重新训练后需手动同步），且模型类别名要包含 `target` 的值。

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

### 4.7 protocol — 协议条件（RKPP 信息源）

判断某个战斗协议事件是否已发生。依赖 RKPP 协议信息源（见 `config/global.yaml` 的 `protocol` 段），
事件匹配顺序：先精确匹配 `summary_kind` / `opcode_name` / `opencode`，再对 `meaning` 做包含匹配（忽略大小写）。
协议源不可用时条件始终为 `false`，建议与视觉条件用 `OR` 组合做回退。

| 参数 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"protocol"` |
| `event` | string | 事件标识（`summary_kind` / `opcode_name` / `opencode` 精确，或 `meaning` 包含） |
| `within_sec` | float | 可选：事件须在最近 N 秒内收到 |
| `field` | string | 可选：点分路径提取事件字段，如 `content.damage` |
| `operator` | string | 与 `field` 搭配的比较运算符（默认 `==`） |
| `value` | any | 与 `field` 搭配的期望值（支持 `${var}` 变量引用） |

```yaml
- type: protocol
  event: "battle_end"          # 战斗结束事件已发生（30 秒内）
  within_sec: 30

- type: protocol
  event: "action_resolve"      # 行动结算伤害 >= 200
  field: "content.damage"
  operator: ">="
  value: 200
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

### move_to_target — PathPlanner 拟人移动到视觉目标

使用路径规划器生成拟人化鼠标路径，将鼠标从当前位置自然移动到目标。

```yaml
- move_to_target: "target_name"   # 目标名称（必填）
  min_score: 0.75                 # 最低置信度（默认 0.0）
  fallback_pos: [500, 300]        # 目标未找到时的备选坐标
  duration_ms: 300                # 移动总耗时（默认 300ms）
  strategy: "fitts"               # 路径策略："fitts"（拟人化）/ "sine"（正弦弧线）
  offset_x: 0                     # X 偏移
  offset_y: 0                     # Y 偏移
```

**策略说明：**
- `fitts`：模拟真实人类鼠标行为（加减速 + 过冲 + 微抖动 + 弧线偏移）
- `sine`：正弦垂直扰动（平滑弧线，适合简单场景）

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
