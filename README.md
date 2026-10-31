# Automatic-RocoKingdom

基于 **PDA (Perceive-Decide-Act) 循环** 驱动的全自动化脚本框架。通过视觉识别感知画面状态，根据工作流定义做出决策，并执行键鼠动作序列 —— 实现"看到什么 → 决定做什么 → 执行操作"的完整自动化闭环。

## 核心能力

- **场景自动识别** — 基于视觉检测结果判断当前所处场景，自动触发对应流程
- **视觉目标追踪** — 持续检测画面中的特定目标（按钮、标签、角色等）
- **YAML 工作流编排** — 用声明式 YAML 定义完整的自动化流程，无需编写代码
- **可扩展条件系统** — 视觉/时间/计数/变量/组合条件，支持自定义扩展
- **循环/分支/中断控制流** — 支持循环、条件分支、超时退出等复杂逻辑
- **容错与恢复** — 动作级重试、步骤级回退、全局超时保护、异常自动捕获

---

## 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 |
| Python | 3.10+ |
| 驱动 | [Interception](https://github.com/oblitum/Interception) 内核级键鼠驱动 |
| 依赖 | 见 `requirements.txt`（PyYAML、OpenCV、NumPy、PyWin32、Pillow） |

---

## 快速开始

### 1. 克隆仓库

```bash
git clone --recursive https://github.com/DN20011219/Automatic-RocoKingdom.git
cd Automatic-RocoKingdom
```

如果已经克隆但缺少子模块：

```bash
git submodule update --init --recursive
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 安装 Interception 驱动

键鼠控制依赖 Interception 内核驱动。请按照子模块文档完成安装：

> 参见 [`submodules/RocoKingdom-Clicker/docs/DRIVER_INSTALLATION.md`](submodules/RocoKingdom-Clicker/docs/DRIVER_INSTALLATION.md)

安装后需要**重启电脑**。

### 4. 放置 YOLO 模型（仅视觉识别精灵时需要）

`submodules/RocoKingdom-Spotting/config.json` 里的 `yolo_pets` 检测器读取
`models/best.pt`，但 `models/` 被子模块 gitignore（模型文件不入库），
所以克隆后需要自己把训练好的模型放进去：

```bash
mkdir -p submodules/RocoKingdom-Spotting/models
cp /path/to/your/best.pt submodules/RocoKingdom-Spotting/models/best.pt
```

重新训练模型后同样需要手动同步。YOLO 检测器还依赖 `ultralytics`（见 `requirements.txt` 中的可选项）。

### 5. 配置全局参数

编辑 `config/global.yaml`，至少修改窗口关键词为你的目标窗口标题：

```yaml
spotting:
  window_keyword: "洛克王国：世界"   # 修改为你的目标窗口标题关键词
```

### 6. 运行

```bash
# 基本用法
python main.py --workflow config/workflows/example_daily.yaml

# 自动捕捉精灵（识别 → 转视角到画面中心 → 长按丢球 → 补回视角轴线 → 单一方向扫视找下一只）
# 默认抓 pipaniao；注意要带 -c config/global_hunt.yaml：它关掉了捕猎用不到的模板
# 检测器，单帧耗时从 294.6ms 降到 55.2ms（3.4fps → 18.1fps），坐标才不会严重滞后
python main.py -w config/workflows/catch_pet.yaml -c config/global_hunt.yaml -v

# 换要抓的精灵，不必改 YAML（--var 可重复，值会自动推断类型）
python main.py -w config/workflows/catch_pet.yaml -c config/global_hunt.yaml --var target_pet=sansan --var max_catches=3 -v

# 启用详细日志
python main.py --workflow config/workflows/example_daily.yaml -v

# 指定配置文件 + 日志输出到文件
python main.py -w config/workflows/example_battle.yaml -c config/global.yaml --log-file logs/run.log
```

运行后按 `Ctrl+C` 可安全停止。

> 游戏窗口必须在前台才截得到图（`foreground_only: true`）。切到终端的那一刻
> 脚本会自动停手（不转视角、不丢球），所以 Alt+Tab 过去按 `Ctrl+C` 是安全的。

开启 `spotting.show_overlay` 后，游戏画面上会叠加检测框（绿色=正常，黄色=低分或光标标记），
覆盖层是透明置顶 + 鼠标穿透 + 不抢前台的，不影响截图也不影响操作。

### 7. 运行测试

```bash
python -m unittest discover -s tests -v
```

---

## 项目结构

```
Automatic-RocoKingdom/
├── main.py                      # 框架入口（CLI）
├── config/
│   ├── global.yaml              # 全局配置（窗口、日志、性能参数）
│   ├── global_hunt.yaml         # 捕猎专用配置（继承 global.yaml，关掉模板检测器提速 5 倍）
│   └── workflows/               # 工作流 YAML 脚本目录
│       ├── example_daily.yaml   # 示例：每日任务自动化
│       ├── example_battle.yaml  # 示例：PVP 对战自动化
│       └── catch_pet.yaml       # 自动捕捉精灵（识别→转视角到中心→丢球→补回轴线→单向360°扫视，默认抓 pipaniao）
│
├── core/                        # 核心引擎
│   ├── engine.py                # 主引擎：PDA 循环调度
│   ├── context.py               # 运行时上下文（变量、状态共享）
│   ├── events.py                # 事件总线（模块间解耦通信）
│   └── lifecycle.py             # 生命周期管理（启动/暂停/停止/恢复）
│
├── workflow/                    # 工作流引擎
│   ├── loader.py                # YAML 工作流加载与校验
│   ├── executor.py              # 工作流执行器（节点调度）
│   ├── registry.py              # 节点类型注册表（可扩展）
│   └── nodes/                   # 工作流节点类型
│       ├── base.py              # NodeBase 抽象基类
│       ├── action_node.py       # 动作节点
│       ├── perceive_node.py     # 感知节点
│       ├── condition_node.py    # 条件分支节点
│       ├── loop_node.py         # 循环节点
│       ├── wait_node.py         # 等待节点
│       ├── scene_node.py        # 场景切换节点
│       ├── terminate_node.py    # 终止节点
│       ├── log_node.py          # 日志节点
│       └── pet_hunt_node.py     # 捕猎节点（识别→瞄准→丢球→转视角闭环）
│
├── perception/                  # 感知层（适配 Spotting）
│   ├── adapter.py               # Spotting 子模块适配器
│   ├── scene_detector.py        # 场景识别器
│   └── target_tracker.py        # 目标追踪器
│
├── action/                      # 动作层（适配 Clicker）
│   ├── adapter.py               # Clicker 子模块适配器
│   ├── executor.py              # 动作序列执行器
│   └── builtin_actions.py       # 内置高级动作
│
├── conditions/                  # 条件系统（可扩展）
│   ├── base.py                  # ConditionBase + ConditionRegistry
│   ├── visual_condition.py      # 视觉条件
│   ├── time_condition.py        # 时间条件
│   ├── counter_condition.py     # 计数/变量条件
│   ├── composite_condition.py   # 组合条件 + 场景条件
│   └── custom_condition.py      # 自定义条件扩展点
│
├── scene/                       # 场景状态机
│   ├── state_machine.py         # 有限状态机实现
│   ├── scene_config.py          # 场景定义与转换规则
│   └── transitions.py           # 转换条件与动作绑定
│
├── utils/                       # 工具层
│   ├── logger.py                # 统一日志
│   ├── config_loader.py         # 配置加载工具
│   └── retry.py                 # 重试/容错工具
│
├── submodules/                  # Git 子模块
│   ├── RocoKingdom-Clicker/     # 键鼠控制（Interception 驱动）
│   └── RocoKingdom-Spotting/    # 视觉识别（模板匹配/SIFT/YOLO）
│
├── docs/                        # 详细文档
│   ├── ARCHITECTURE.md          # 架构设计详解
│   ├── WORKFLOW_DSL.md          # 工作流 DSL 完整参考
│   └── EXTENSION_GUIDE.md       # 扩展开发指南
│
├── tests/                       # 单元测试（python -m unittest discover -s tests）
│   └── test_pet_hunt.py         # 捕猎节点状态机 + 相对位移/长按原语
│
├── tools/                       # 调试与标定工具
│   ├── calibrate_aim.py         # 实测「鼠标位移 → 画面移动」的比率/方向/线性度
│   │                            #   --time-series 区分相机自动回正与低增益
│   │                            #   --during-hold 测蓄力期间能否转视角
│   │                            #   --use-detector 用检测框中心测（需画面里有精灵）
│   ├── verify_model.py          # 校验模型类别名与标注/目录/data.yaml 是否对得上
│   └── relabel_dataset.py       # 修复插入新类别后标注 id 未重编号（默认 dry-run）
│
├── requirements.txt
└── .gitmodules
```

---

## 核心概念：PDA 循环

框架的核心是一个不断循环的 **Perceive-Decide-Act** 引擎：

```
┌─────────────────────────────────────────────────────┐
│                   Engine (PDA Loop)                  │
│                                                     │
│   ┌───────────┐   ┌───────────┐   ┌───────────┐   │
│   │ Perceive  │──>│  Decide   │──>│    Act    │   │
│   │ (感知层)  │   │ (决策层)  │   │  (执行层) │   │
│   └───────────┘   └───────────┘   └───────────┘   │
│        ^                                  │         │
│        └──────────────────────────────────┘         │
│                    (循环/终止)                       │
└─────────────────────────────────────────────────────┘
```

| 阶段 | 职责 | 对应模块 |
|------|------|---------|
| **Perceive** | 截取窗口画面，运行视觉检测，识别当前场景 | `perception/` |
| **Decide** | 根据工作流当前节点 + 条件系统判断下一步 | `workflow/` + `conditions/` |
| **Act** | 执行键鼠动作序列（点击、按键、等待等） | `action/` |

每次循环间隔默认 0.1 秒（可在 `config/global.yaml` 中调整 `loop_interval`）。

---

## 工作流示例

工作流使用 YAML 定义。以下是一个简化示例：

```yaml
name: "每日任务自动化"
version: "1.0"

variables:
  max_retry: 3

steps:
  step_start:
    type: perceive              # 视觉感知：等待目标出现
    target: "main_menu_label"
    timeout_ms: 10000
    on_found: "step_navigate"   # 找到后跳转
    on_timeout: "step_end"      # 超时后跳转

  step_navigate:
    type: action                # 执行动作序列
    actions:
      - click_target: "daily_quest_button"   # 点击视觉目标
        fallback_pos: [900, 500]             # 找不到时的备选坐标
      - wait: 1000                           # 等待 1 秒
    next: "step_end"

  step_end:
    type: terminate
    message: "完成！"
```

更多示例见 `config/workflows/` 目录。完整 DSL 语法参考 [docs/WORKFLOW_DSL.md](docs/WORKFLOW_DSL.md)。

---

## 全局配置

`config/global.yaml` 支持以下配置项：

```yaml
# PDA 循环间隔（秒），越小响应越快但 CPU 占用越高
loop_interval: 0.1

# 视觉识别配置
spotting:
  window_keyword: "洛克王国：世界"    # 目标窗口标题关键词
  capture_backend: "screen-client"   # 截图方式: screen-client | screen-window
  foreground_only: true              # 仅前景窗口

# 键鼠控制配置
clicker:
  move_mouse: true                   # 是否移动鼠标到点击位置

# 日志配置
logging:
  level: "INFO"                      # DEBUG | INFO | WARNING | ERROR
  file: null                         # 日志文件路径（null 表示不写文件）

# 工作流默认配置
workflow:
  default: null                      # 默认工作流路径
```

---

## CLI 参数

```
python main.py --workflow <path> [options]

必选参数：
  --workflow, -w    工作流 YAML 文件路径

可选参数：
  --config, -c      全局配置文件路径（默认: config/global.yaml）
  --verbose, -v     启用 DEBUG 级别详细日志
  --log-file        日志输出文件路径
```

---

## 子模块说明

| 子模块 | 职责 | 仓库 |
|--------|------|------|
| **RocoKingdom-Clicker** | 基于 Interception 驱动的内核级键鼠控制 | [GitHub](https://github.com/DN20011219/RocoKingdom-Clicker) |
| **RocoKingdom-Spotting** | 视觉目标检测（模板匹配/SIFT/YOLO） | [GitHub](https://github.com/DN20011219/RocoKingdom-Spotting) |

主框架通过 **适配器模式** 调用子模块，不直接依赖其内部实现。子模块 API 变更时只需修改对应的 Adapter 文件。

更新子模块到最新版本：

```bash
git submodule update --remote
```

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构设计详解：PDA 引擎、各层职责、数据流、事件系统、容错机制 |
| [docs/WORKFLOW_DSL.md](docs/WORKFLOW_DSL.md) | 工作流 YAML DSL 完整参考：所有节点类型、条件类型、动作格式 |
| [docs/EXTENSION_GUIDE.md](docs/EXTENSION_GUIDE.md) | 扩展开发指南：自定义条件、自定义节点、事件监听、调试技巧 |

---

## License

MIT
