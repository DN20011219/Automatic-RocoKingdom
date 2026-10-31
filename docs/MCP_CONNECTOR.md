# MCP 连接器设计文档（RocoKingdom MCP Server）

> 版本：1.0 ｜ 状态：设计定稿 ｜ 模块位置：`mcp_server/`

## 1. 概述

### 1.1 目标

将 Automatic-RocoKingdom 框架的**信息源采集内容**与**操作执行能力**封装为标准
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 服务，使任意 MCP 客户端
（IDE 智能体、Claude Desktop、其他 LLM 应用）中的大模型能够：

1. **读取**：窗口截图、视觉检测结果、RKPP 协议事件、运行时上下文、工作流状态；
2. **操作**：鼠标点击/移动、键盘按键/组合键、批量动作序列、Clicker 原生脚本；
3. **控制**（引擎托管模式）：加载/启动/暂停/恢复/停止 YAML 工作流，读写工作流变量。

### 1.2 非目标

- 不实现向游戏服务器发送协议数据（RKPP 保持只读消费）；
- 不替代 YAML 工作流引擎，MCP 是引擎之上的一个接入层；
- 不提供多租户/鉴权（服务仅绑定本机回环地址，安全边界由配置开关控制）。

### 1.3 设计原则

| 原则 | 说明 |
|---|---|
| 零侵入 | 不修改现有业务逻辑；仅对 `Engine`/`SpottingAdapter` 增加少量只读访问器 |
| 复用优先 | 全部能力复用 `SpottingAdapter` / `ProtocolSource` / `ClickerAdapter` / `Engine` |
| 同步内核 + 异步外壳 | 桥接层全部为同步方法；MCP 工具层用 `asyncio.to_thread` 包装，不阻塞事件循环 |
| 安全默认 | 操作类工具受安全层约束（总开关、序列长度上限、动作日志审计） |
| 结构化响应 | 所有工具返回统一 `{ok, data, error}` JSON 结构，绝不抛裸异常给客户端 |

## 2. 总体架构

```
┌───────────────────────────── MCP Client（大模型） ─────────────────────────────┐
│                    stdio / streamable-http (JSON-RPC)                          │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     ▼
┌──────────────────────────── mcp_server/server.py ──────────────────────────────┐
│  FastMCP 实例，注册 20 个工具（读取 7 + 操作 7 + 引擎控制 6）                     │
│  每个工具 = 参数校验 → SafetyGuard 检查 → asyncio.to_thread(bridge.xxx)          │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     ▼
┌──────────────────────────── mcp_server/bridge.py ──────────────────────────────┐
│  RocoBridge：统一门面（同步接口）                                                 │
│  · 桥接模式：自持 context + event_bus + spotting + protocol + clicker           │
│  · 引擎模式：托管 Engine，全部读写路由到 Engine 子系统                             │
└──────┬─────────────────┬─────────────────┬──────────────────┬──────────────────┘
       ▼                 ▼                 ▼                  ▼
 SpottingAdapter   ProtocolSource     ClickerAdapter       Engine（可选托管）
 （视觉检测/截图）  （RKPP 协议事件）   （键鼠/脚本）        （PDA 循环 + 工作流）
```

### 2.1 双运行模式

由配置项 `mcp.host_engine` 决定，**启动时确定，运行中不切换**：

| | 桥接模式 `host_engine: false` | 引擎托管模式 `host_engine: true` |
|---|---|---|
| 适用场景 | 大模型直接"看 + 操作" | 大模型监督/控制既有工作流 |
| 感知来源 | 桥接层自持 `SpottingAdapter` | `engine.perception` |
| 协议来源 | 桥接层自持 `ProtocolSource` | `engine.protocol_source` |
| 操作执行 | 桥接层自持 `ClickerAdapter` | `engine.action_executor` |
| 上下文 | 桥接层私有 `RuntimeContext` | `engine.context` |
| 工作流控制工具 | 返回 `not_enabled` 错误 | 可用 |

**为什么不做资源双份**：YOLO 模型加载、协议订阅线程开销大；两种模式互斥使用，
由同一套 `RocoBridge` 代码按模式路由，避免重复初始化。

### 2.2 线程模型

```
主线程        : FastMCP 事件循环（stdio 读 / HTTP ASGI）
线程池        : 所有 bridge 同步调用经 asyncio.to_thread 在此执行
engine 线程   : engine.run_async() 的 daemon 线程（仅引擎模式运行工作流时）
protocol 线程 : ProtocolSource 自带的 /events 订阅线程（各信息源自带）
```

并发约束：
- `RocoBridge` 内部对适配器调用加 `threading.Lock`（防止两个工具调用同时截图/检测）；
- 引擎 `RUNNING`/`PAUSED` 期间，**直接操作类工具被安全层拒绝**（键鼠竞争保护），
  读取类工具正常（数据来自引擎上下文的最新快照）。

## 3. 目录结构与模块职责

```
mcp_server/
├── __init__.py      # 包导出：__version__、create_server、RocoBridge
├── __main__.py      # CLI 入口：参数解析、日志配置、启动 stdio/HTTP 传输
├── config.py        # McpServerConfig 数据类 + load_mcp_config()
├── keys.py          # 键名 → Windows vk_code 映射表
├── safety.py        # SafetyGuard：操作白名单/序列上限/动作日志环形缓冲
├── bridge.py        # RocoBridge：全部能力的同步门面
└── server.py        # create_server()：FastMCP 构建与 20 个工具注册
```

对现有代码的最小改动（只读访问器，不改行为）：

| 文件 | 新增内容 |
|---|---|
| `core/engine.py` | 只读属性：`perception` / `protocol_source` / `action_executor` / `workflow_executor` |
| `perception/adapter.py` | 公开方法 `capture_frame()`（包装私有 `_capture_frame`） |

## 4. 工具接口规范

共 **20 个工具**。所有工具返回统一信封：

```json
{ "ok": true,  "data": <任意 JSON> }
{ "ok": false, "error": "错误描述", "error_code": "简短错误码" }
```

`error_code` 取值：`not_initialized` / `not_enabled` / `blocked_by_safety` /
`engine_running` / `invalid_param` / `window_not_found` / `internal_error`。

### 4.1 读取类工具（7 个）

#### `get_status`
返回系统整体状态。无参数。

```json
{
  "mode": "bridge | engine",
  "window": { "found": true, "keyword": "洛克王国：世界", "hwnd": 123456 },
  "perception": { "available": true, "detectors": ["flat_labels"] },
  "protocol": { "enabled": true, "connected": false, "events": 0 },
  "action": { "available": true, "allow_actions": true },
  "engine": { "enabled": true, "state": "RUNNING", "workflow": "example_daily",
              "step": "battle_loop", "iteration": 3, "workflow_complete": false }
}
```

#### `screenshot(max_width: int = 0)`
截取游戏窗口一帧，返回 MCP `Image` 内容（非信封）。
`max_width > 0` 时按比例缩小（上限受 `mcp.screenshot.max_width` 钳制）；
窗口不存在/在后台（`foreground_only`）时返回错误信封。
内部流程：`capture_frame()` → PIL 缩放 → PNG 编码。

#### `detect(name: str | None = None, min_score: float = 0.0)`
对当前帧执行一次检测。`name` 为空返回全部结果。

```json
{ "ok": true, "data": { "count": 2, "detections": [
    { "name": "confirm", "category": "ui", "score": 0.93,
      "x": 812, "y": 455, "w": 120, "h": 48, "cx": 872, "cy": 479 } ] } }
```

引擎模式下：引擎 `RUNNING` 时直接读 `engine.context.last_detections`（PDA 循环保鲜），
否则手动调用 `engine.perception.detect_frame()` 并刷新上下文。
检测坐标为**屏幕绝对坐标**，可直接用于 `click`。

#### `get_protocol_events(limit: int = 20, keyword: str | None = None)`
返回最近的 RKPP 协议事件（最新在最后）。`keyword` 对 `summary_kind`/`opcode_name`/
`opencode`/`meaning` 做不区分大小写的子串过滤。协议源未连接返回 `ok: true, data: {connected: false, events: []}`。

#### `get_runtime_state`
返回运行时上下文快照：

```json
{ "ok": true, "data": {
    "workflow_name": "...", "step_name": "...", "prev_step_name": "...",
    "current_scene": "...", "iteration": 3, "elapsed_seconds": 125.4,
    "variables": { "...": "..." }, "counters": { "...": 0 } } }
```

#### `list_workflows`
扫描 `config/workflows/*.yaml`，返回 `[{name, path, title}]`（title 取 YAML 内 `name` 字段，解析失败为 `null`）。

#### `get_action_log(limit: int = 20)`
返回安全层记录的最近操作（见 §6）。

### 4.2 操作类工具（7 个）

所有操作类工具先过 `SafetyGuard.check()`；引擎 `RUNNING`/`PAUSED` 时返回
`engine_running` 错误。坐标均为屏幕绝对坐标。

| 工具 | 参数 | 底层调用 |
|---|---|---|
| `click` | `x:int, y:int, hold_ms:int=100` | `ClickerAdapter.click` |
| `move_to` | `x:int, y:int, duration_ms:int=200, humanized:bool=True` | `humanized=True` 走 `move_to_planned`（fitts 拟人），否则 `move_to` |
| `press_key` | `key:str|int, hold_ms:int=50` | `key` 接受键名（`"space"`）或数字 vk_code，键名经 `keys.py` 映射 |
| `press_combo` | `keys:list[str|int], hold_ms:int=50` | `ClickerAdapter.press_combo` |
| `wait` | `duration_ms:int` | `ClickerAdapter.wait`；上限 30000ms |
| `execute_actions` | `actions:list[dict]` | `ClickerAdapter.execute_sequence`；长度受 `max_sequence_len` 限制；元素格式同 Clicker JSON 动作（`click`/`move`/`key`/`combo`/`wait`） |
| `run_script` | `script_name:str` | `ClickerAdapter.load_and_execute_script`；脚本来自 Clicker `data/action_scripts/` |

操作类工具返回：

```json
{ "ok": true, "data": { "action": "click", "detail": {"x": 900, "y": 500} } }
```

### 4.3 引擎控制类工具（6 个，仅引擎模式）

| 工具 | 行为 |
|---|---|
| `load_workflow(path)` | 校验文件存在 → `engine.load_workflow(path)`；引擎已运行时拒绝（先停后载） |
| `start_engine()` | 未加载工作流报错；子系统被上次运行清理过则自动重新 `engine.setup()`；`engine.run_async()` 后台启动 |
| `pause_engine()` | `engine.pause()`；仅 `RUNNING` 有效 |
| `resume_engine()` | `engine.resume()`；仅 `PAUSED` 有效 |
| `stop_engine()` | `engine.stop()`；等待引擎线程退出（最长 5s） |
| `set_variable(name, value)` | 写 `engine.context.set_variable()`；`value` 支持 str/int/float/bool |

状态查询统一走 `get_status` / `get_runtime_state`，不另设工具。

### 4.4 键名映射（`keys.py`）

`resolve_key(key) -> int` 支持：

- 数字：原样视为 vk_code；
- 单字符：`"a"`-`"z"` → `0x41`+，`"0"`-`"9"` → `0x30`+；
- 常用键名（不区分大小写）：`space` 0x20、`enter`/`return` 0x0D、`esc`/`escape` 0x1B、
  `tab` 0x09、`backspace` 0x08、`shift` 0x10、`ctrl` 0x11、`alt` 0x12、
  `up/down/left/right` 0x26-0x28…、`f1`-`f12` 0x70+、`home/end/pgup/pgdn/insert/delete`；
- 未知键名抛 `KeyError`，由工具层转为 `invalid_param` 错误。

## 5. `RocoBridge` 接口定义（`bridge.py`）

```python
class RocoBridge:
    def __init__(self, config: McpServerConfig, global_config_path: str | None): ...

    # ---- 生命周期 ----
    def setup(self) -> None: ...          # 按模式初始化适配器或托管引擎
    def cleanup(self) -> None: ...        # 停止协议源/引擎，清理适配器

    # ---- 状态 ----
    def get_status(self) -> dict: ...
    def get_runtime_state(self) -> dict: ...

    # ---- 感知 ----
    def capture_screenshot_png(self, max_width: int = 0) -> bytes: ...
    def detect(self, name: str | None, min_score: float) -> list[dict]: ...
    def get_protocol_events(self, limit: int, keyword: str | None) -> dict: ...
    def list_workflows(self) -> list[dict]: ...

    # ---- 操作（内部均先过 safety 检查） ----
    def click(self, x: int, y: int, hold_ms: int) -> None: ...
    def move_to(self, x: int, y: int, duration_ms: int, humanized: bool) -> None: ...
    def press_key(self, key: str | int, hold_ms: int) -> None: ...
    def press_combo(self, keys: list, hold_ms: int) -> None: ...
    def wait(self, duration_ms: int) -> None: ...
    def execute_actions(self, actions: list[dict]) -> None: ...
    def run_script(self, script_name: str) -> None: ...
    def get_action_log(self, limit: int) -> list[dict]: ...

    # ---- 引擎控制（仅引擎模式） ----
    def load_workflow(self, path: str) -> None: ...
    def start_engine(self) -> None: ...
    def pause_engine(self) -> None: ...
    def resume_engine(self) -> None: ...
    def stop_engine(self) -> None: ...
    def set_variable(self, name: str, value: Any) -> None: ...
```

失败语义：桥接方法失败时抛 `BridgeError(error_code, message)`，
由 `server.py` 统一捕获转为错误信封，工具函数本身不抛异常。

## 6. 安全层设计（`safety.py`）

```python
@dataclass
class ActionRecord:
    ts: float            # 时间戳
    tool: str            # 工具名
    args: dict           # 参数快照（截断长字段）
    ok: bool
    error: str | None
    duration_ms: float

class SafetyGuard:
    def __init__(self, cfg: SafetyConfig): ...
    @property
    def allow_actions(self) -> bool: ...
    def check_action(self, tool: str) -> None            # 失败抛 BridgeError(blocked_by_safety)
    def check_sequence(self, n: int) -> None             # 序列长度上限
    def record(self, tool: str, args: dict, ok: bool,
               error: str | None, duration_ms: float) -> None
    def recent(self, limit: int) -> list[dict]: ...
```

- `allow_actions: false` 时全部操作类工具返回 `blocked_by_safety`（读取不受影响）；
- `max_sequence_len` 约束 `execute_actions`；
- 动作日志为 `deque(maxlen=200)` 环形缓冲，线程锁保护，`get_action_log` 可审计。

## 7. 引擎托管模式细节

### 7.1 生命周期状态机

```
(未加载) --load_workflow--> (已加载/IDLE) --start_engine--> RUNNING
RUNNING --pause_engine--> PAUSED --resume_engine--> RUNNING
RUNNING/PAUSED --stop_engine--> STOPPED --load_workflow--> 可再次启动
工作流自然结束 --> STOPPED（workflow_complete=true）
```

### 7.2 关键实现点

1. **重启支持**：`engine.run()` 结束时 `_shutdown()` 会清理 perception/protocol，
   `start_engine` 检测到子系统不可用时先重新 `engine.setup()`（幂等）；
2. **完成检测**：`engine.workflow_executor.is_complete` 暴露在 `get_status.engine` 中，
   大模型据此判断任务是否结束；
3. **键鼠竞争**：引擎 `RUNNING`/`PAUSED` 时，`SafetyGuard` 之外的第二道检查直接拒绝
   操作类工具（错误码 `engine_running`），提示先 `pause_engine` 再 `stop_engine`；
4. **停止等待**：`stop_engine` 调 `engine.stop()` 后 join 引擎线程（超时 5s），
   返回时保证状态收敛为 `STOPPED`。

## 8. 配置规范（`config/global.yaml` 新增 `mcp` 段）

```yaml
mcp:
  enabled: true
  server_name: "roco-kingdom"
  transport: "http"            # stdio | http
  host: "127.0.0.1"            # http 监听地址（仅回环）
  port: 8766
  host_engine: false           # true = 引擎托管模式
  log_level: "INFO"
  log_file: null               # 建议 http 模式落盘，避免控制台刷屏
  safety:
    allow_actions: true        # false = 只读服务
    max_sequence_len: 50       # execute_actions 上限
  screenshot:
    max_width: 1280            # screenshot 缩放上限（0 = 不缩放）
    format: "png"              # png | jpeg
    jpeg_quality: 80
```

`load_mcp_config()` 合并策略：代码内默认值 ← `global.yaml` 的 `mcp` 段 ←
`__main__.py` 的 CLI 参数（`--transport`/`--host`/`--port` 优先级最高）。

## 9. 客户端接入

### 9.1 stdio（本地进程）

```json
{
  "mcpServers": {
    "roco-kingdom": {
      "command": "python",
      "args": ["-m", "mcp_server", "--config", "config/global.yaml"],
      "cwd": "<项目根目录>"
    }
  }
}
```

### 9.2 streamable-http

先启动服务：`python -m mcp_server --transport http --port 8766`

```json
{
  "mcpServers": {
    "roco-kingdom": { "url": "http://127.0.0.1:8766/mcp" }
  }
}
```

## 10. 日志与可观测性

- **stdio 模式**：日志只能写 `stderr` 或文件（stdout 是 JSON-RPC 通道，禁止污染）。
  `__main__.py` 自行配置 `logging`（StreamHandler(stderr)），**不复用** `utils.logger.setup_logging`
  （其默认 handler 为 stdout）；
- **http 模式**：可写控制台/文件，由 `mcp.log_file` 控制；
- 每次工具调用记录 DEBUG 日志（工具名 + 耗时），操作类记录 INFO（含安全层结论）。

## 11. 依赖

`requirements.txt` 新增：

```
mcp>=1.8,<2       # 官方 Python SDK（FastMCP，含 streamable-http 传输）
                  # 注意：2.0 移除了 fastmcp 模块，必须锁定 1.x 系列
```

`Pillow`/`PyYAML` 等已存在，无需新增其他依赖。

## 12. 实施计划

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | 设计文档（本文档） | 评审通过 |
| 2 | `mcp_server/` 骨架：全部接口签名 + `NotImplementedError` | 可导入、无语法错误 |
| 3 | 实现 `config.py` / `keys.py` | 单测：配置合并、键名解析 |
| 4 | 实现 `safety.py` | 拒绝路径全覆盖 |
| 5 | 实现 `bridge.py`（桥接模式） | M1 读取 + M2 操作可用 |
| 6 | 实现 `bridge.py`（引擎模式）+ `Engine` 访问器 | M3 控制可用 |
| 7 | 实现 `server.py` 工具注册 + `__main__.py` 双传输 | Inspector 联调 |
| 8 | 收尾：`requirements.txt`、`global.yaml`、`mcp.json` 示例 | 客户端可直接接入 |

## 13. 风险与约束

| 风险 | 应对 |
|---|---|
| 大模型误操作键鼠 | `safety.allow_actions` 总开关；动作日志审计；建议人盯屏运行 |
| 引擎与直接操作并发抢键鼠 | `engine_running` 拒绝策略（§7.2-3） |
| 截图过大撑爆上下文 | `screenshot.max_width` 默认缩放至 1280 |
| stdio 日志污染协议通道 | 强制 stderr/文件（§10） |
| HTTP 服务被本机其他进程访问 | 仅绑定 `127.0.0.1`；不开放公网 |
| 前台检查导致截图失败 | 游戏窗口须在前景（`foreground_only`），错误信封明确提示 |
