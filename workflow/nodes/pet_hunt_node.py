"""捕猎节点 — 「识别精灵 → 瞄准 → 长按丢球 → 慢速转视角搜索」的闭环状态机。

与普通节点"一次 tick 做一件事就跳走"不同，本节点在自身内部维持一个状态机，
每个 PDA tick 推进一小步并返回 RUNNING，因此：

- 每次判定都使用引擎刚采集的**最新一帧**检测结果（不会用到过期坐标）
- 扫视靠"小步长 + 事件间隔"实现慢速平滑（位移本身一律单事件发送，见 _pan_view）
- Ctrl+C / 引擎 stop 依然能在 tick 边界及时生效（等待用时间戳而非阻塞 sleep）
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core.context import Detection
from workflow.nodes.base import NodeBase, NodeResult
from workflow.registry import NodeRegistry

logger = logging.getLogger(__name__)


# 状态机状态
_SCAN = "scan"        # 扫视寻找目标（找不到就转视角）
_AIM = "aim"          # 一次位移把目标转到基准点
_SETTLE = "settle"    # 等相机到位（不是重新检测），然后丢球
_THROW = "throw"      # 长按左键蓄力丢球
_COOLDOWN = "cooldown"  # 丢球后等待捕捉动画

# 键鼠动作失败后的退避时长（秒）。直接回扫描会让下一帧又选中同一个目标、
# 又失败，在持续性故障（驱动抽风、拿不到截帧区域）下以满帧率空转刷日志
_FAIL_BACKOFF_SEC = 0.5


@NodeRegistry.register("pet_hunt")
class PetHuntNode(NodeBase):
    """捕猎节点：持续搜索指定精灵并循环投掷精灵球。

    YAML 配置示例：
        step_hunt:
          type: pet_hunt
          target: "sansan"          # YOLO 类别名
          min_score: 0.5
          throw_hold_ms: 300        # 蓄力时长
          pan_dx: 60                # 每步转视角的水平位移
          pan_interval_ms: 250      # 两次扫视之间的停顿（"慢速"由此实现）
          sweep_limit_px: 0         # 0 = 一直朝同一方向转，360° 自己转回来
          restore_aim_after_throw: true   # 丢完球把瞄准位移反向补回，恢复视角轴线
          max_catches: 0            # 0 = 无限循环
          next: "step_done"

    瞄准是**一次到位、不复核**：检测到目标 → 按标定比率算出位移 → 单事件发出 →
    等相机到位（aim_settle_ms，不是重新检测）→ 长按丢球。

    为什么不复核：等"移动后再看一帧"要付出 停稳 120ms + 截图 55ms + 推理 10ms，
    这段时间精灵已经走远，复核瞄的是它**刚才**的位置，越复核越偏。
    精度靠 tools/calibrate_aim.py 离线实测的比率保证，不靠运行时迭代。
    """

    # 单个 tick 内最多连续推进多少个状态（防御性上限，避免意外死循环）
    _MAX_STEPS_PER_TICK = 8

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)

        self._state: str = _SCAN
        self._wait_until: float = 0.0        # 当前状态的等待截止时间戳
        self._hit_frames: int = 0            # 目标连续命中帧数（防抖）
        self._pending: Optional[Detection] = None   # 待攻击的目标
        self._aimed_at: Optional[tuple] = None      # 上次瞄准的屏幕坐标
        self._throws: int = 0
        # 本回合瞄准发出的净位移（鼠标坐标增量）。丢完球后反向补回，
        # 让光标和相机都回到瞄准前的位置 —— 否则每抓一只相机就多偏一点。
        self._aim_dx: int = 0
        self._aim_dy: int = 0
        self._swept_px: int = 0              # 当前方向已累计的水平位移
        self._pan_dir: int = 1               # 1 = 向右扫，-1 = 向左扫
        self._last_pan_time: float = 0.0
        self._last_blind_warn: float = 0.0
        self._started_at: Optional[float] = None
        self._deadline: Optional[float] = None
        self._adapter = None
        # 转视角比率 = 「画面内容位移 ÷ 鼠标位移」，**带符号**、按轴分开存。
        # 由 tools/calibrate_aim.py 离线实测后填进配置；不做在线自标定 ——
        # 那需要"移动后再看一帧"，而等这一帧的功夫精灵已经走远了。
        self._gain_x: float = -1.0
        self._gain_y: float = -1.0
        # 解析过 ${var} 引用的配置快照，每次 execute 刷新
        self._cfg: Dict[str, Any] = dict(config)

    @property
    def node_type(self) -> str:
        return "pet_hunt"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_enter(self, context: Any) -> None:
        self._refresh_config(context)
        self._started_at = time.time()
        duration_sec = self._num("duration_sec", 0)
        self._deadline = (self._started_at + duration_sec) if duration_sec > 0 else None

        self._gain_x = self._num("aim_gain_x", -1.0)
        self._gain_y = self._num("aim_gain_y", -1.0)

        target = self._str("target", "sansan")
        logger.info(
            "捕猎节点 %s 启动：目标=%s min_score=%.2f 蓄力=%dms 冷却=%dms 基准=%s "
            "比率(x=%.4f, y=%.4f)",
            self._step_name, target,
            self._num("min_score", 0.5),
            int(self._num("throw_hold_ms", 300)),
            int(self._num("catch_cooldown_ms", 2000)),
            self._str("aim_reference", "center"),
            self._gain_x, self._gain_y,
        )

    def reset(self) -> None:
        self._state = _SCAN
        self._wait_until = 0.0
        self._hit_frames = 0
        self._pending = None
        self._aimed_at = None
        self._throws = 0
        self._aim_dx = 0
        self._aim_dy = 0
        self._swept_px = 0
        self._pan_dir = 1
        self._last_pan_time = 0.0
        self._last_blind_warn = 0.0
        self._started_at = None
        self._deadline = None
        self._cfg = dict(self._config)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def execute(self, context: Any) -> tuple[NodeResult, Optional[str]]:
        self._refresh_config(context)

        adapter = self._get_adapter(context)
        if adapter is None:
            logger.error("捕猎节点 %s：动作执行器不可用", self._step_name)
            return NodeResult.FAILED, self._config.get("on_error")

        now = time.time()

        # ---- 全局守卫：时间上限 ----
        if self._deadline is not None and now >= self._deadline:
            self._restore_aim_axis(adapter, context)
            logger.info("捕猎节点 %s：达到时长上限，共投掷 %d 次", self._step_name, self._throws)
            return NodeResult.SUCCESS, self._config.get("on_timeout", self._config.get("next"))

        # ---- 全局守卫：场景丢失 ----
        scene_target = self._str("scene_target", "")
        if scene_target:
            scene_det = context.find_detection(scene_target, self._num("scene_min_score", 0.8))
            if scene_det is None:
                self._restore_aim_axis(adapter, context)
                logger.warning("捕猎节点 %s：场景标识 %s 消失，退出捕猎", self._step_name, scene_target)
                return NodeResult.SUCCESS, self._config.get("on_absent", self._config.get("next"))

        # ---- 全局守卫：投掷次数达标 ----
        max_catches = int(self._num("max_catches", 0))
        if max_catches > 0 and self._throws >= max_catches:
            self._restore_aim_axis(adapter, context)
            logger.info("捕猎节点 %s：已完成 %d 次投掷", self._step_name, self._throws)
            return NodeResult.SUCCESS, self._config.get("next")

        # ---- 状态机推进：本 tick 内尽量多走几步，遇到需要等待的状态才让出 ----
        for _ in range(self._MAX_STEPS_PER_TICK):
            if not self._advance(context, adapter, time.time()):
                break

        return NodeResult.RUNNING, None

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    def _advance(self, context: Any, adapter: Any, now: float) -> bool:
        """推进一步状态机。

        Returns:
            True  — 本 tick 内可以继续推进下一状态；
            False — 需要让出，等下一个 PDA tick（等待中 / 已发出阻塞输入 / 需要新帧）。
        """
        if self._state == _SCAN:
            return self._do_scan(context, adapter, now)
        if self._state == _AIM:
            return self._do_aim(context, adapter, now)
        if self._state == _SETTLE:
            return self._do_settle(context, adapter, now)
        if self._state == _THROW:
            return self._do_throw(context, adapter, now)
        if self._state == _COOLDOWN:
            return self._do_cooldown(context, adapter, now)

        logger.error("捕猎节点 %s：未知状态 %s，重置为扫描", self._step_name, self._state)
        self._state = _SCAN
        return True

    def _do_scan(self, context: Any, adapter: Any, now: float) -> bool:
        """扫描目标；找不到就慢速转视角继续找。"""
        target = self._str("target", "sansan")
        min_score = self._num("min_score", 0.5)
        confirm_frames = max(1, int(self._num("confirm_frames", 2)))

        detections: List[Detection] = context.find_detections(target, min_score)

        if detections:
            self._pending = self._select_target(context, adapter, detections)
            self._hit_frames += 1

            if self._hit_frames >= confirm_frames:
                ref = self._aim_reference(context, adapter)
                distance = None
                if ref is not None:
                    distance = ((self._pending.cx - ref[0]) ** 2
                                + (self._pending.cy - ref[1]) ** 2) ** 0.5
                logger.info(
                    "发现 %s（连续 %d 帧确认，%d 个候选）：中心=(%d, %d) 置信度=%.2f 离基准点=%s",
                    target, self._hit_frames, len(detections),
                    self._pending.cx, self._pending.cy, self._pending.score,
                    f"{distance:.0f}px" if distance is not None else "未知",
                )
                # 新一轮瞄准
                self._state = _AIM
                return True  # 立刻进入瞄准，不等下一个 tick

            return False  # 还要再确认几帧，让引擎采下一帧

        # 目标丢失：清零防抖计数，转视角搜索
        self._hit_frames = 0
        self._pending = None

        # 感知不可用时绝不能盲转视角 —— 截不到画面往往意味着游戏窗口不在前台，
        # 此时甩动鼠标会直接作用在用户当前正在操作的桌面上
        if not getattr(context, "frame_captured", True):
            if now - self._last_blind_warn >= 5.0:
                logger.warning(
                    "捕猎节点 %s：截不到游戏画面（窗口未找到或不在前台），暂停搜索",
                    self._step_name,
                )
                self._last_blind_warn = now
            return False

        pan_interval = self._num("pan_interval_ms", 0) / 1000.0
        if pan_interval > 0 and (now - self._last_pan_time) < pan_interval:
            return False

        self._pan_view(adapter)
        return False  # 视角已变，必须等新帧才能判断

    def _do_aim(self, context: Any, adapter: Any, now: float) -> bool:
        """把鼠标移动到目标位置。

        默认走 relative：只发相对位移，全程不产生绝对定位事件，
        与转视角用的是同一套输入语义，因此在 mouse-look 游戏里最稳。
        """
        det = self._pending
        if det is None:
            self._state = _SCAN
            return True

        x = det.cx + int(self._num("aim_offset_x", 0))
        y = det.cy + int(self._num("aim_offset_y", 0))
        duration_ms = int(self._num("aim_duration_ms", 350))
        strategy = self._str("aim_strategy", "relative")

        if strategy == "relative":
            ok = self._aim_relative(context, adapter, x, y, duration_ms)
        elif strategy == "direct":
            ok = adapter.move_to(x, y, duration_ms)
        else:
            # planned 走 PathPlanner 拟人路径；path_strategy 是扰动策略名
            ok = adapter.move_to_planned(x, y, duration_ms, self._str("path_strategy", "fitts"))

        if not ok:
            self._fail_to_scan(now, f"移动鼠标到 ({x}, {y}) 失败")
            return False  # 让出本 tick：下一帧重新定位，不拿过期坐标反复重试

        self._aimed_at = (x, y)
        # 位移发出后相机还在平滑插值，必须等它到位再松手，否则球是按"半截视角"
        # 的方向飞出去的（对齐 RocoPilot 的 pet_aim_camera_delay）。
        # 注意这里等的是**相机**，不是重新检测 —— 不再看第二帧，因为看一帧的功夫
        # （停稳 120ms + 截图 55ms + 推理 10ms）精灵已经走远了，瞄的是它刚才的位置。
        settle_ms = self._num("aim_settle_ms", 120)
        self._wait_until = now + settle_ms / 1000.0
        self._state = _SETTLE
        return settle_ms <= 0

    def _aim_reference(self, context: Any, adapter: Any) -> Optional[tuple]:
        """瞄准基准点（屏幕坐标）。

        这个游戏的球是丢向**客户区中心 / 镜头正前方**的，所以默认基准就是
        截帧区域的中心 —— 它是确定性的已知量，不依赖任何会被游戏隐藏或
        锁定的输入状态。

        aim_reference="cursor" 是给"真的用光标瞄准"的游戏留的逃生舱：那种
        游戏里光标位置才代表瞄准点。
        """
        if self._str("aim_reference", "center") == "cursor":
            try:
                return adapter.cursor_pos()
            except Exception as e:
                logger.error("读取光标位置失败，无法相对瞄准: %s", e)
                return None

        rect = getattr(context, "frame_rect", (0, 0, 0, 0)) or (0, 0, 0, 0)
        if rect[2] <= 0 or rect[3] <= 0:
            logger.error("捕猎节点 %s：拿不到截帧区域，无法确定瞄准基准", self._step_name)
            return None

        ox, oy, w, h = rect
        return (
            ox + w / 2.0 + self._num("aim_reference_offset_x", 0),
            oy + h / 2.0 + self._num("aim_reference_offset_y", 0),
        )

    def _expectation_point(self, context: Any, adapter: Any) -> Optional[tuple]:
        """我们认为目标此刻应该在哪 —— 多候选时靠它挑出"正在瞄的那一只"。

        已经瞄准过就看上次瞄准点：这样始终追踪同一只精灵，不会在多只之间来回跳。
        还没瞄准过就看基准点：离基准点最近的目标转视角代价最小、最快能丢出去。
        """
        if self._aimed_at is not None:
            return self._aimed_at
        return self._aim_reference(context, adapter)

    def _select_target(self, context: Any, adapter: Any,
                       detections: List[Detection]) -> Optional[Detection]:
        """从多个候选里挑一个目标。

        默认 reference：离**期望点**最近的那只。扫描阶段期望点就是瞄准基准点，
        复核阶段则是上次瞄准的位置 —— 扫描和复核必须用同一套规则，否则会出现
        "瞄着 A 却拿 B 算误差"，触发一堆无意义的重瞄。

        另外两种：score = 置信度最高（旧行为）；largest = 检测框面积最大（近似"离角色最近"）。
        """
        if not detections:
            return None

        mode = self._str("target_select", "reference")
        if mode == "score":
            return max(detections, key=lambda d: d.score)
        if mode == "largest":
            return max(detections, key=lambda d: d.w * d.h)

        point = self._expectation_point(context, adapter)
        if point is None:
            # 基准点不可用（截帧区域未知）时退回置信度，保证还有目标可选
            return max(detections, key=lambda d: d.score)

        px, py = point
        return min(detections, key=lambda d: (d.cx - px) ** 2 + (d.cy - py) ** 2)

    def _aim_relative(self, context: Any, adapter: Any, x: int, y: int,
                      duration_ms: int) -> bool:
        """一次相对位移把目标转到瞄准基准点（客户区中心）上。

        位移 = −(目标 − 基准点) / 比率，**一次发完、不看第二帧**：
        等"移动后再检测一次"要付出 停稳 120ms + 截图 55ms + 推理 10ms，
        这段时间精灵已经走远，瞄的反而是它刚才的位置，越复核越偏。

        发送方式由 aim_path_planner 决定：
          - False（默认）：**单事件**。比率必须是单事件标定出来的。
          - True：PathPlanner 拟人轨迹，拆成 aim_path_steps 个微步。
            PathPlanner 保证各步之和精确等于目标位移，但游戏对"N 个微步"和
            "1 个大步"的响应不一定等价，所以**比率必须用同样的分段方式重新标定**
            （tools/calibrate_aim.py 会读工作流配置自动对齐）。
        """
        ref = self._aim_reference(context, adapter)
        if ref is None:
            return False

        ref_x, ref_y = ref
        err_x = x - ref_x
        err_y = y - ref_y

        # 限幅只在比率配置离谱时兜底（比如填了接近 0 的值会算出天文数字）
        cap = self._num("aim_max_move_px", 3000)
        dx = int(round(self._clamp(self._err_to_mouse(err_x, self._gain_x), cap)))
        dy = int(round(self._clamp(self._err_to_mouse(err_y, self._gain_y), cap)))

        planned = bool(self._cfg.get("aim_path_planner", False))
        logger.debug(
            "瞄准：%s 目标(%d,%d) 基准(%.0f,%.0f) 误差(%.0f,%.0f) "
            "比率(x=%.4f,y=%.4f) → 位移(%d,%d) 方式=%s",
            self._str("target", "sansan"), x, y, ref_x, ref_y, err_x, err_y,
            self._gain_x, self._gain_y, dx, dy,
            f"拟人轨迹 {int(self._num('aim_path_steps', 20))} 步" if planned else "单事件",
        )

        if planned:
            return adapter.move_relative_planned(
                dx, dy,
                duration_ms=duration_ms,
                strategy=self._str("path_strategy", "fitts"),
                num_steps=int(self._num("aim_path_steps", 20)),
            )

        # 只有单事件路径记净位移：拟人轨迹是 N 个微步，游戏对微步的响应与单事件
        # 不等价（见 aim_path_planner 的说明），用一条反向单事件反补反而对不准。
        if adapter.move_relative(dx, dy, 0, steps=1):
            self._aim_dx += dx
            self._aim_dy += dy
            return True
        return False

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        return max(-limit, min(limit, value))

    @staticmethod
    def _err_to_mouse(err: float, gain: float) -> float:
        """把画面误差换算成需要发出的鼠标位移。

        gain 的定义是「画面内容位移 ÷ 鼠标位移」，**带符号**：
          - 标准 mouse-look：鼠标右移 → 内容左移 → gain 为负
          - 实测本游戏水平轴却是正的（内容跟着鼠标同向移动），垂直轴是负的

        所以必须按轴分别存符号，一个正数增益 + 一个 invert 开关表达不了。
        要让目标落到基准点上，内容需要位移 −err，于是 mouse = −err / gain。
        """
        if abs(gain) < 1e-3:
            return 0.0
        return -err / gain

    def _do_settle(self, context: Any, adapter: Any, now: float) -> bool:
        """瞄准后的稳定等待。"""
        if now < self._wait_until:
            return False
        self._state = _THROW
        return True

    def _do_throw(self, context: Any, adapter: Any, now: float) -> bool:
        """长按左键蓄力投掷。

        瞄准完就丢，不做复核。复核原本是为了"目标跑了就别浪费球"，但它同时是
        重瞄环的入口：增益没标定准时误差永远超容差，于是一次投掷要连着重瞄
        max_reaim 次，外层再套"丢不中 → 冷却 → 又发现同一只"的循环，
        整体表现就是永不收敛的死循环。删掉它换来的是流程可预测。
        """
        # 截不到画面就别按左键：这时按键会落到用户当前正在用的那个窗口上。
        # 注意这是安全守卫，不是精度复核 —— 不重新检测、不判断目标、不重瞄，
        # 只是"看不见就不动手"，和转视角的盲操作守卫同一条原则。
        if not getattr(context, "frame_captured", True):
            self._fail_to_scan(now, "截不到游戏画面，取消本次丢球")
            return False

        hold_ms = int(self._num("throw_hold_ms", 300))
        if not adapter.hold_left(hold_ms):
            self._fail_to_scan(now, "长按左键失败")
            return False  # 输入异常，让出本 tick 等下一帧重新判断

        self._throws += 1
        counter_name = self._str("counter_name", "pets_thrown")
        context.increment_counter(counter_name)
        context.set_variable(counter_name, self._throws)
        logger.info(
            "第 %d 次丢球完成（蓄力 %dms，比率 x=%+.4f y=%+.4f）",
            self._throws, hold_ms, self._gain_x, self._gain_y,
        )

        self._wait_until = time.time() + self._num("catch_cooldown_ms", 2000) / 1000.0
        self._state = _COOLDOWN
        return False

    def _do_cooldown(self, context: Any, adapter: Any, now: float) -> bool:
        """丢球后冷却，等捕捉动画播完；冷却结束时把瞄准位移补回去。"""
        if now < self._wait_until:
            return False
        self._restore_aim_axis(adapter, context)
        self._hit_frames = 0
        self._pending = None
        self._aimed_at = None
        self._state = _SCAN
        return True

    def _fail_to_scan(self, now: float, reason: str) -> None:
        """动作失败后回扫描，但先短暂退避。

        直接回 SCAN 的话，下一帧又会选中同一个目标、又失败，以满帧率空转刷日志
        （驱动抽风、拿不到截帧区域等持续性故障下就是死循环）。借用 COOLDOWN
        状态退避一下，把重试频率压到可观测的水平。
        """
        logger.warning("捕猎节点 %s：%s，%.1fs 后重新扫描",
                       self._step_name, reason, _FAIL_BACKOFF_SEC)
        self._hit_frames = 0
        self._pending = None
        self._aimed_at = None
        self._wait_until = now + _FAIL_BACKOFF_SEC
        self._state = _COOLDOWN

    # ------------------------------------------------------------------
    # 恢复视角轴线
    # ------------------------------------------------------------------

    def _restore_aim_axis(self, adapter: Any, context: Any) -> None:
        """把瞄准时发出的相对位移反向补回，让光标回到原位、相机回到原轴线。

        瞄准是「转视角把目标拽到画面中心」，它会永久改变相机朝向（水平偏航 +
        垂直俯仰）。不补回来的话，每抓一只相机就多偏一点：水平扫视走的是一条
        越来越斜的线，表现就是"自动转视角丢了原本的轴线"；光标也会越飘越偏，
        最后贴到屏幕边缘。

        补回放在冷却结束时（捕捉动画播完、重新扫视之前），避免在投掷动画期间
        再转一次视角。补回用的是和瞄准完全相同的单事件相对位移，所以净位移抵消。
        """
        dx, dy = self._aim_dx, self._aim_dy
        self._aim_dx = 0
        self._aim_dy = 0
        if dx == 0 and dy == 0:
            return
        if not bool(self._cfg.get("restore_aim_after_throw", True)):
            return
        # 与转视角同一条原则：看不见游戏画面时绝不注入输入
        if not getattr(context, "frame_captured", True):
            logger.debug("截不到游戏画面，跳过视角轴线恢复（位移 %d, %d）", dx, dy)
            return
        if adapter.move_relative(-dx, -dy, 0, steps=1):
            logger.debug("恢复视角轴线：反补瞄准位移 (%d, %d)", -dx, -dy)
        else:
            logger.warning("恢复视角轴线失败：反补位移 (%d, %d)", -dx, -dy)

    # ------------------------------------------------------------------
    # 转视角
    # ------------------------------------------------------------------

    def _pan_view(self, adapter: Any) -> None:
        """慢速转视角搜索下一只精灵：默认一直朝一个方向转，360° 自己转回来。

        每次都是**单事件**位移（同 _aim_relative 的理由：拆成微步会让实际幅度
        与标定值对不上）。"慢速"由 pan_interval_ms 控制事件间隔实现 ——
        小步长 + 步间停顿，而不是把一个位移切碎。

        sweep_limit_px > 0 时才左右来回扫（旧行为），并在反向时按
        vertical_step_px 抬升/压低视角形成"弓"字形。
        """
        pan_dx = int(self._num("pan_dx", 60))
        pan_dy = int(self._num("pan_dy", 0))
        sweep_limit = int(self._num("sweep_limit_px", 0))
        vertical_step = int(self._num("vertical_step_px", 0))

        dx = pan_dx * self._pan_dir

        # 到达扫视边界：反向，并按配置抬升/压低一点视角，形成"弓"字形搜索
        extra_dy = 0
        if sweep_limit > 0:
            self._swept_px += abs(dx)
            if self._swept_px >= sweep_limit:
                self._pan_dir = -self._pan_dir
                self._swept_px = 0
                if vertical_step:
                    extra_dy = vertical_step
                logger.debug("扫视到边界，反向搜索（累计投掷 %d 次）", self._throws)

        adapter.move_relative(dx, pan_dy + extra_dy, 0, steps=1)
        self._last_pan_time = time.time()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _refresh_config(self, context: Any) -> None:
        """刷新配置快照，解析 ${var} 变量引用。"""
        resolve = getattr(context, "resolve_value", None)
        if resolve is None:
            self._cfg = dict(self._config)
            return
        self._cfg = {key: resolve(value) for key, value in self._config.items()}

    def _get_adapter(self, context: Any):
        """取得底层 ClickerAdapter（需要 move_relative / hold_left 等原语）。"""
        if self._adapter is not None:
            return self._adapter

        action_executor = getattr(context, "_action_executor", None)
        if action_executor is None:
            return None
        self._adapter = getattr(action_executor, "adapter", None)
        return self._adapter

    def _str(self, key: str, default: str) -> str:
        value = self._cfg.get(key, default)
        return str(value) if value is not None else default

    def _num(self, key: str, default: float) -> float:
        """读取数值配置；变量解析失败或非数值时回退到默认值。"""
        try:
            return float(self._cfg.get(key, default))
        except (TypeError, ValueError):
            logger.debug("配置项 %s 的值 %r 不是数值，使用默认值 %s", key, self._cfg.get(key), default)
            return float(default)

