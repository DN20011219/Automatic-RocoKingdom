"""pet_hunt 节点与 ClickerAdapter 新增原语的离线单元测试。

不需要游戏窗口、Interception 驱动或 YOLO 模型：
- 节点测试用假的 ClickerAdapter 记录调用，用 RuntimeContext 喂造好的检测结果
- 适配器测试走 SendInput 用户模式回退路径，并拦截 _send_inputs 只校验发出的事件

运行：
    python -m unittest discover -s tests -v
    python tests/test_pet_hunt.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2
import numpy as np

from core.context import Detection, RuntimeContext  # noqa: E402
from workflow.nodes import pet_hunt_node  # noqa: E402
from workflow.nodes.base import NodeResult  # noqa: E402


def make_detection(name: str = "sansan", cx: int = 800, cy: int = 450,
                   score: float = 0.9, size: int = 40) -> Detection:
    """造一个中心点在 (cx, cy) 的检测结果。"""
    return Detection(
        name=name,
        category="3d_object",
        score=score,
        x=cx - size // 2,
        y=cy - size // 2,
        w=size,
        h=size,
    )


class FakeClickerAdapter:
    """记录调用而不产生真实键鼠输入的假适配器。

    维护两个虚拟状态：
      - `cursor`：相对位移累加、绝对定位覆盖，供 aim_reference=cursor 模式使用
      - `offset`：**相机响应**。move_relative 发出 (dx,dy) 后，世界内容在画面上
        移动 (world_gain_x*dx, world_gain_y*dy)。闭环瞄准的测试靠它才能收敛；
        默认 world_gain=(0,0) 表示"世界不响应"，用于测试预算用尽等路径。

    `pet(wx, wy)` 返回世界坐标 (wx,wy) 对应的**当前屏幕坐标**检测。
    `gain` 是旧的光标跟随系数，保留给 cursor 模式的欠冲测试。
    """

    def __init__(self, cursor: Tuple[int, int] = (0, 0), gain: float = 1.0,
                 world_gain: Tuple[float, float] = (0.0, 0.0)) -> None:
        self.calls: List[Tuple[str, tuple]] = []
        self.cursor: Tuple[int, int] = cursor
        self.gain = gain
        self.world_gain = world_gain
        self.offset: Tuple[float, float] = (0.0, 0.0)
        self.move_result = True
        self.hold_result = True
        self.cursor_readable = True

    def pet(self, wx: int, wy: int, **kwargs) -> Detection:
        """世界坐标 → 当前屏幕坐标的检测（含相机累计响应）。"""
        return make_detection(cx=int(wx + self.offset[0]),
                              cy=int(wy + self.offset[1]), **kwargs)

    def cursor_pos(self):
        if not self.cursor_readable:
            raise OSError("GetCursorPos 不可用")
        return self.cursor

    def move_to_planned(self, x, y, duration_ms=300, strategy="fitts", num_steps=20):
        self.calls.append(("move_to_planned", (x, y, duration_ms, strategy)))
        if self.move_result:
            self.cursor = (x, y)
        return self.move_result

    def move_to(self, x, y, duration_ms=100):
        self.calls.append(("move_to", (x, y, duration_ms)))
        if self.move_result:
            self.cursor = (x, y)
        return self.move_result

    def move_relative(self, dx, dy, duration_ms=200, steps=0):
        self.calls.append(("move_relative", (dx, dy, duration_ms)))
        if self.move_result:
            self.cursor = (self.cursor[0] + int(dx * self.gain),
                           self.cursor[1] + int(dy * self.gain))
            self.offset = (self.offset[0] + self.world_gain[0] * dx,
                           self.offset[1] + self.world_gain[1] * dy)
        return self.move_result

    def move_relative_planned(self, dx, dy, duration_ms=200, strategy="fitts", num_steps=20):
        """拟人轨迹版：记录调用，但净位移与单事件版一致（PathPlanner 保证总和不变）。"""
        self.calls.append(("move_relative_planned", (dx, dy, duration_ms, strategy, num_steps)))
        if self.move_result:
            self.cursor = (self.cursor[0] + int(dx * self.gain),
                           self.cursor[1] + int(dy * self.gain))
            self.offset = (self.offset[0] + self.world_gain[0] * dx,
                           self.offset[1] + self.world_gain[1] * dy)
        return self.move_result

    def hold_left(self, hold_ms=1200):
        self.calls.append(("hold_left", (hold_ms,)))
        return self.hold_result

    # ---- 断言辅助 ----

    def names(self) -> List[str]:
        return [c[0] for c in self.calls]

    def of(self, kind: str) -> List[tuple]:
        return [c[1] for c in self.calls if c[0] == kind]


class FakeActionExecutor:
    """节点通过 context._action_executor.adapter 取适配器。"""

    def __init__(self, adapter: FakeClickerAdapter) -> None:
        self.adapter = adapter


class NodeTestBase(unittest.TestCase):
    """构建节点 + 上下文的公共脚手架。"""

    def build(self, config: Dict[str, Any], variables: Optional[Dict[str, Any]] = None,
              cursor: Tuple[int, int] = (0, 0), gain: float = 1.0,
              world_gain: Tuple[float, float] = (0.0, 0.0),
              frame_rect: Tuple[int, int, int, int] = (0, 0, 1920, 1080)):
        node = pet_hunt_node.PetHuntNode(config)
        node.step_name = "step_hunt"

        context = RuntimeContext()
        context.variables.update(variables or {})
        # 默认 1920x1080 客户区 → 瞄准基准点 (960, 540)
        context.frame_rect = frame_rect
        self.adapter = FakeClickerAdapter(cursor=cursor, gain=gain, world_gain=world_gain)
        context._action_executor = FakeActionExecutor(self.adapter)

        node.reset()
        node.on_enter(context)
        return node, context

    def tick(self, node, context, detections: Optional[List[Detection]] = None,
             frame_captured: bool = True):
        """模拟一次 PDA tick：引擎先感知（帧序号自增 + 写入 last_detections），再驱动节点。"""
        context.frame_seq += 1
        context.last_detections = detections or []
        context.frame_captured = frame_captured
        return node.execute(context)

    def ticks(self, node, context, frames: List[Optional[List[Detection]]],
              frame_captured: bool = True):
        """连续跑多帧，返回每次的结果。"""
        return [self.tick(node, context, frame, frame_captured) for frame in frames]


class TestScanAndPan(NodeTestBase):
    """阶段：识别不到目标时慢速转视角搜索。"""

    def test_pans_when_no_target(self):
        node, ctx = self.build({"target": "sansan", "pan_dx": 60, "pan_interval_ms": 0})

        results = self.ticks(node, ctx, [None, None, None])

        self.assertTrue(all(r[0] == NodeResult.RUNNING for r in results))
        # 单事件发送：duration 恒为 0，"慢速"靠 pan_interval_ms 控制节奏
        self.assertEqual(self.adapter.of("move_relative"), [(60, 0, 0)] * 3)
        self.assertEqual(self.adapter.of("hold_left"), [])

    def test_sweep_reverses_at_limit(self):
        node, ctx = self.build({"pan_dx": 100, "sweep_limit_px": 250})

        self.ticks(node, ctx, [None] * 5)

        dx_sequence = [args[0] for args in self.adapter.of("move_relative")]
        # 累计 100/200/300 到达 250 上限后反向，反向从下一步生效
        self.assertEqual(dx_sequence, [100, 100, 100, -100, -100])

    def test_vertical_step_applied_on_reversal(self):
        node, ctx = self.build({"pan_dx": 100, "pan_dy": 0, "sweep_limit_px": 250,
                                "vertical_step_px": -30})

        self.ticks(node, ctx, [None] * 3)

        dy_sequence = [args[1] for args in self.adapter.of("move_relative")]
        # 第 3 步累计 300 到达上限 → 反向的同时抬升视角
        self.assertEqual(dy_sequence, [0, 0, -30])

    def test_pan_interval_throttles_panning(self):
        node, ctx = self.build({"pan_dx": 50, "pan_interval_ms": 10_000})

        self.ticks(node, ctx, [None] * 4)

        # 间隔远未到，只允许转一次视角
        self.assertEqual(len(self.adapter.of("move_relative")), 1)

    def test_default_sweep_is_one_direction(self):
        """sweep_limit_px 默认 0：一直朝同一方向转，转满 360° 自然回到原处。"""
        node, ctx = self.build({"pan_dx": 100, "pan_interval_ms": 0})

        self.ticks(node, ctx, [None] * 30)

        dx_sequence = [args[0] for args in self.adapter.of("move_relative")]
        self.assertEqual(len(dx_sequence), 30)
        self.assertEqual(set(dx_sequence), {100})   # 从不反向

    def test_sweep_limit_zero_ignores_vertical_step(self):
        """单向搜索没有"边界"，vertical_step_px 不应再产生抬升。"""
        node, ctx = self.build({"pan_dx": 100, "pan_interval_ms": 0,
                                "sweep_limit_px": 0, "vertical_step_px": -30})

        self.ticks(node, ctx, [None] * 10)

        self.assertEqual({args[1] for args in self.adapter.of("move_relative")}, {0})


class TestPerceptionGuard(NodeTestBase):
    """感知不可用（截不到画面）时的安全行为。

    截不到画面通常意味着游戏窗口没找到或不在前台，此时 last_detections 为空
    并不代表"画面里没有精灵"，绝不能据此甩动真实鼠标或按下左键。
    """

    def test_no_panning_when_frame_not_captured(self):
        node, ctx = self.build({"pan_dx": 60, "pan_interval_ms": 0})

        results = self.ticks(node, ctx, [None] * 4, frame_captured=False)

        self.assertTrue(all(r[0] == NodeResult.RUNNING for r in results))
        self.assertEqual(self.adapter.names(), [])

    def test_panning_resumes_after_frame_returns(self):
        node, ctx = self.build({"pan_dx": 60, "pan_interval_ms": 0})

        self.tick(node, ctx, None, frame_captured=False)
        self.assertEqual(self.adapter.names(), [])

        self.tick(node, ctx, None, frame_captured=True)
        self.assertEqual(self.adapter.of("move_relative"), [(60, 0, 0)])

    def test_throw_blocked_when_frame_lost_after_aim(self):
        """复核删了，但"看不见就不按左键"这条安全守卫保留 —— 否则按键会落到别的窗口。"""
        node, ctx = self.build({"confirm_frames": 1, "aim_settle_ms": 500,
                                "aim_max_steps": 1})

        self.tick(node, ctx, [make_detection()])           # 修正 → observe
        node._observe_ready_at = 0.0                       # 相机停稳时间到
        self.tick(node, ctx, [make_detection()])           # observe → 预算尽 → settle
        node._wait_until = 0.0
        self.tick(node, ctx, [make_detection()], frame_captured=False)   # 丢球前画面已丢

        self.assertEqual(self.adapter.of("hold_left"), [])
        self.assertEqual(node._throws, 0)
        self.assertEqual(node._state, pet_hunt_node._COOLDOWN)   # 退避，不空转


class TestAimAndThrow(NodeTestBase):
    """一次到位瞄准 → 丢球 的主流程（无复核、无迭代）。

    aim_settle_ms=0 时，一个 tick 内就串完 确认 → 位移 → 丢球。
    """

    BASE = {"confirm_frames": 1, "aim_settle_ms": 0, "catch_cooldown_ms": 0,
            "aim_duration_ms": 350}

    def test_full_cycle_one_shot(self):
        """检测到就位移过去然后扔球，全程只看一帧。"""
        node, ctx = self.build({**self.BASE, "confirm_frames": 2,
                                "throw_hold_ms": 300})
        pet = [make_detection(cx=800, cy=450, score=0.9)]

        self.tick(node, ctx, pet)                          # 确认帧 1，不动作
        self.assertEqual(self.adapter.names(), [])

        self.tick(node, ctx, pet)                          # 确认帧 2 → 位移 + 丢球
        self.assertEqual(self.adapter.names(), ["move_relative", "hold_left"])
        # 目标 (800,450)，基准点 (960,540)，比率 -1 → 位移 = 误差 = (-160,-90)
        self.assertEqual(self.adapter.of("move_relative"), [(-160, -90, 0)])
        self.assertEqual(self.adapter.of("hold_left"), [(300,)])
        self.assertEqual(ctx.get_counter("pets_thrown"), 1)
        self.assertEqual(ctx.get_variable("pets_thrown"), 1)

    def test_exactly_one_move_per_throw(self):
        """一次发现 = 恰好一次位移 + 一次丢球，绝不迭代多步。"""
        node, ctx = self.build({**self.BASE, "catch_cooldown_ms": 100_000})
        pet = [make_detection(cx=1160, cy=640)]

        for _ in range(6):
            self.tick(node, ctx, pet)

        self.assertEqual(len(self.adapter.of("move_relative")), 1)
        self.assertEqual(len(self.adapter.of("hold_left")), 1)
        self.assertEqual(node._throws, 1)

    def test_settle_waits_for_camera_before_throw(self):
        """aim_settle_ms > 0 时：位移与丢球分成两个 tick，中间等相机到位。"""
        node, ctx = self.build({**self.BASE, "aim_settle_ms": 500})

        self.tick(node, ctx, [make_detection(cx=1160, cy=540)])
        self.assertEqual(self.adapter.names(), ["move_relative"])   # 只位移，不丢球

        node._wait_until = 0.0                                     # 相机到位
        self.tick(node, ctx, [make_detection(cx=1160, cy=540)])
        self.assertEqual(self.adapter.of("hold_left"), [(300,)])

    def test_full_cycle_with_planned_strategy(self):
        """planned/direct 是绝对定位逃生舱。"""
        node, ctx = self.build({**self.BASE, "aim_strategy": "planned",
                                "path_strategy": "sine"})

        self.tick(node, ctx, [make_detection(cx=800, cy=450, score=0.9)])

        self.assertEqual(self.adapter.of("move_to_planned"), [(800, 450, 350, "sine")])
        self.assertEqual(self.adapter.of("hold_left"), [(300,)])

    def test_min_score_filters_weak_detection(self):
        node, ctx = self.build({"min_score": 0.8, "confirm_frames": 1})

        self.ticks(node, ctx, [[make_detection(score=0.55)]] * 3)

        self.assertEqual(self.adapter.of("hold_left"), [])
        # 未命中 → 一直在转视角找
        self.assertEqual(len(self.adapter.of("move_relative")), 3)

    def test_single_frame_false_positive_does_not_throw(self):
        node, ctx = self.build({"confirm_frames": 3, "aim_settle_ms": 0, "catch_cooldown_ms": 0})

        self.ticks(node, ctx, [[make_detection()], None, None, None])

        self.assertEqual(self.adapter.of("hold_left"), [])

    def test_aim_offset_applied(self):
        node, ctx = self.build({**self.BASE, "aim_offset_x": -5, "aim_offset_y": 20})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])

        # 目标点 (795,470) − 基准点 (960,540)，比率 -1 → 位移等于误差
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (-165, -70))

    def test_direct_aim_strategy(self):
        node, ctx = self.build({**self.BASE, "aim_strategy": "direct"})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])

        self.assertEqual(self.adapter.names(), ["move_to", "hold_left"])

    def test_move_failure_backs_off_then_retries(self):
        node, ctx = self.build({**self.BASE, "catch_cooldown_ms": 100_000})
        self.adapter.move_result = False

        # 移动失败时绝不能盲丢球
        self.tick(node, ctx, [make_detection()])
        self.assertEqual(self.adapter.of("hold_left"), [])
        self.assertEqual(node._throws, 0)
        self.assertEqual(node._state, pet_hunt_node._COOLDOWN)   # 进退避而不是立刻重试
        self.assertEqual(len(self.adapter.of("move_relative")), 1)

        # 退避期间不重试
        self.tick(node, ctx, [make_detection()])
        self.assertEqual(len(self.adapter.of("move_relative")), 1)

        # 退避结束、移动恢复 → 修正一步 → 观测 → 预算用尽 → 丢球
        self.adapter.move_result = True
        node._wait_until = 0.0
        self.tick(node, ctx, [make_detection()])
        self.tick(node, ctx, [make_detection()])
        self.assertEqual(len(self.adapter.of("hold_left")), 1)
        self.assertEqual(node._throws, 1)

    def test_hold_failure_does_not_count_as_throw(self):
        node, ctx = self.build({**self.BASE})
        self.adapter.hold_result = False

        self.tick(node, ctx, [make_detection()])
        self.tick(node, ctx, [make_detection()])

        self.assertEqual(node._throws, 0)
        self.assertEqual(ctx.get_counter("pets_thrown"), 0)
        self.assertEqual(node._state, pet_hunt_node._COOLDOWN)


class TestThrowBehavior(NodeTestBase):
    """丢球时机：位移完、相机到位就丢，不做"目标还在吗"的复核。"""

    BASE = {"confirm_frames": 1, "aim_settle_ms": 0, "catch_cooldown_ms": 0,
            "aim_duration_ms": 350}

    def test_throws_even_when_target_vanishes_at_throw_time(self):
        node, ctx = self.build({**self.BASE, "aim_settle_ms": 500})

        self.tick(node, ctx, [make_detection(cx=1160, cy=540)])   # 位移 → 等相机
        node._wait_until = 0.0                                    # 相机到位
        self.tick(node, ctx, None)                                # 丢球瞬间目标已消失

        self.assertEqual(len(self.adapter.of("hold_left")), 1)   # 照样丢，不复核
        self.assertEqual(node._throws, 1)

    def test_removed_loop_keys_are_ignored(self):
        """旧配置里残留的闭环参数不应影响行为，也不应报错。"""
        node, ctx = self.build({**self.BASE, "recheck_before_throw": True,
                                "aim_max_steps": 9, "aim_tolerance_px": 1,
                                "aim_step_px": 40, "aim_gain_auto": True})

        self.tick(node, ctx, [make_detection(cx=1160, cy=540)])

        self.assertEqual(len(self.adapter.of("move_relative")), 1)
        self.assertEqual(len(self.adapter.of("hold_left")), 1)


class TestRestoreAimAxis(NodeTestBase):
    """丢完球把瞄准位移反向补回：光标回原位、相机回原轴线。

    瞄准会永久改变相机朝向（偏航 + 俯仰），不补的话每抓一只就多偏一点，
    水平扫视沿的线越来越斜。补回放在冷却结束、重新扫视之前。
    """

    BASE = {"confirm_frames": 1, "aim_settle_ms": 0, "catch_cooldown_ms": 100_000,
            "aim_duration_ms": 350, "pan_interval_ms": 0}

    def test_restore_after_cooldown_before_pan(self):
        node, ctx = self.build({**self.BASE})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])
        # 瞄准 (-160,-90) 后丢球；冷却还没结束，先不补
        self.assertEqual(self.adapter.of("move_relative"), [(-160, -90, 0)])
        self.assertEqual(self.adapter.of("hold_left"), [(300,)])

        node._wait_until = 0.0                      # 捕捉动画播完
        self.tick(node, ctx, [])                    # 冷却结束 → 补回 → 扫视

        self.assertEqual(self.adapter.of("move_relative"),
                         [(-160, -90, 0), (160, 90, 0), (60, 0, 0)])

    def test_cursor_returns_to_origin(self):
        """反向补回用的是同一套相对位移，所以光标精确回到瞄准前的位置。"""
        node, ctx = self.build({**self.BASE})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])
        self.assertEqual(self.adapter.cursor, (-160, -90))

        node._restore_aim_axis(self.adapter, ctx)

        self.assertEqual(self.adapter.cursor, (0, 0))

    def test_restore_only_once_per_cycle(self):
        """补回后必须清零，否则下一帧扫描（没找到目标）会反复反补。"""
        node, ctx = self.build({**self.BASE})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])
        node._wait_until = 0.0
        self.ticks(node, ctx, [None, None, None])

        restores = [args for args in self.adapter.of("move_relative") if args[:2] == (160, 90)]
        self.assertEqual(len(restores), 1)

    def test_restore_can_be_disabled(self):
        node, ctx = self.build({**self.BASE, "restore_aim_after_throw": False})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])
        node._wait_until = 0.0
        self.tick(node, ctx, [])

        self.assertEqual(self.adapter.of("move_relative"),
                         [(-160, -90, 0), (60, 0, 0)])   # 只有瞄准和扫视

    def test_restore_skipped_when_frame_lost(self):
        """看不见游戏画面时不注入输入 —— 与转视角、丢球同一条安全原则。"""
        node, ctx = self.build({**self.BASE})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])
        node._wait_until = 0.0
        self.tick(node, ctx, [], frame_captured=False)

        self.assertEqual(self.adapter.of("move_relative"), [(-160, -90, 0)])
        self.assertEqual(node._aim_dx, 0)               # 不补，但也不能留到下一回合

    def test_failed_throw_still_restores_axis(self):
        """没丢出去（长按失败）相机也已经转了，轴线同样要补回。"""
        node, ctx = self.build({**self.BASE})
        self.adapter.hold_result = False

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])
        self.assertEqual(node._throws, 0)
        node._wait_until = 0.0
        self.tick(node, ctx, [])

        self.assertEqual(self.adapter.of("move_relative"),
                         [(-160, -90, 0), (160, 90, 0), (60, 0, 0)])


class TestRelativeAim(NodeTestBase):
    """默认的纯相对位移转视角：一次到位，位移 = −(目标 − 基准点) / 比率。

    默认 frame_rect=(0,0,1920,1080) → 瞄准基准点 (960, 540)。
    """

    BASE = {"confirm_frames": 1, "aim_settle_ms": 0, "catch_cooldown_ms": 0,
            "aim_duration_ms": 350}

    def test_delta_is_target_minus_center(self):
        node, ctx = self.build({**self.BASE})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])

        self.assertEqual(self.adapter.of("move_relative")[0][:2], (-160, -90))
        # settle=0 → 同一 tick 内位移完就丢球
        self.assertEqual(self.adapter.of("hold_left"), [(300,)])

    def test_delta_divided_by_ratio(self):
        """比率 = 画面内容位移 ÷ 鼠标位移（带符号）；|比率| 2 时位移应减半。"""
        node, ctx = self.build({**self.BASE, "aim_gain_x": -2.0})

        self.tick(node, ctx, [make_detection(cx=1160, cy=540)])

        # 误差 (200, 0)，mouse = -err/ratio = -200/-2 = (100, 0)
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (100, 0))

    def test_measured_game_ratios(self):
        """用实测比率（x=-1.15, y=-0.22）：垂直误差要被放大 4.5 倍。"""
        node, ctx = self.build({**self.BASE, "aim_gain_x": -1.15, "aim_gain_y": -0.22})

        self.tick(node, ctx, [make_detection(cx=1060, cy=562)])

        # err = (100, 22) → mouse = (-100/-1.15, -22/-0.22) = (87, 100)
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (87, 100))

    def test_no_absolute_positioning_at_all(self):
        """整轮捕猎（含转视角）都只用相对位移，适配隐藏/锁定光标的游戏。"""
        node, ctx = self.build({**self.BASE})

        self.ticks(node, ctx, [None, None, [make_detection()], [make_detection()]])

        self.assertEqual(self.adapter.of("move_to_planned"), [])
        self.assertEqual(self.adapter.of("move_to"), [])
        self.assertTrue(self.adapter.of("move_relative"))

    def test_zero_error_sends_zero_move(self):
        """目标正好在基准点上：发 (0,0) 位移（适配器会当空操作），然后直接丢球。"""
        node, ctx = self.build({**self.BASE})

        self.tick(node, ctx, [make_detection(cx=960, cy=540)])

        self.assertEqual(self.adapter.of("move_relative"), [(0, 0, 0)])
        self.assertEqual(len(self.adapter.of("hold_left")), 1)

    def test_signed_ratio_flips_direction(self):
        """比率的符号决定鼠标往哪边动 —— 两轴符号可能相反，必须按轴存。"""
        # 默认 gain_y = -1.0（内容逆着鼠标动）：目标在基准点下方 100 → 鼠标向下 100
        node, ctx = self.build({**self.BASE})
        self.tick(node, ctx, [make_detection(cx=960, cy=640)])
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (0, 100))

        # gain_y = +1.0（内容跟着鼠标同向动）：鼠标要反向
        node, ctx = self.build({**self.BASE, "aim_gain_y": 1.0})
        self.tick(node, ctx, [make_detection(cx=960, cy=640)])
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (0, -100))

    def test_cap_limits_absurd_move(self):
        """比率配得离谱（接近 0）时，位移必须被 aim_max_move_px 截断。"""
        node, ctx = self.build({**self.BASE, "aim_gain_x": 0.01,
                                "aim_max_move_px": 3000})

        self.tick(node, ctx, [make_detection(cx=1160, cy=540)])

        # err 200 → 比率算出 -20000 → 截断到 -3000
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (-3000, 0))

    def test_zero_ratio_does_not_move(self):
        """比率配成 0 时必须退化成不动，而不是除出天文数字。"""
        node, ctx = self.build({**self.BASE, "aim_gain_x": 0.0, "aim_gain_y": 0.0})

        self.tick(node, ctx, [make_detection(cx=1160, cy=540)])

        self.assertEqual(self.adapter.of("move_relative"), [(0, 0, 0)])

    def test_reference_offset_shifts_aim_point(self):
        """落点整体偏上时，用 offset 把基准点往下压。"""
        node, ctx = self.build({**self.BASE, "aim_reference_offset_y": 40})

        self.tick(node, ctx, [make_detection(cx=960, cy=580)])

        # 基准点变成 (960, 580)，目标正好在上面 → 误差 0 → 位移 (0,0)
        self.assertEqual(self.adapter.of("move_relative"), [(0, 0, 0)])

    def test_missing_frame_rect_fails_safe(self):
        """拿不到截帧区域就不知道基准点在哪，绝不能瞎转视角或盲丢球。"""
        node, ctx = self.build({**self.BASE}, frame_rect=(0, 0, 0, 0))

        result, _ = self.tick(node, ctx, [make_detection()])

        self.assertEqual(result, NodeResult.RUNNING)
        self.assertEqual(self.adapter.of("move_relative"), [])
        self.assertEqual(self.adapter.of("hold_left"), [])
        self.assertEqual(node._throws, 0)
        # 进退避冷却而不是立刻回扫描，避免满帧率空转
        self.assertEqual(node._state, pet_hunt_node._COOLDOWN)

    def test_persistent_aim_failure_does_not_spin(self):
        """瞄准持续失败时必须退避，否则下一帧又选中同一目标、又失败，无限刷。"""
        node, ctx = self.build({**self.BASE})
        self.adapter.move_result = False
        pet = [make_detection(cx=1160, cy=540)]

        for _ in range(10):
            self.tick(node, ctx, pet)

        # 10 个 tick 只应尝试 1 次（其余都在退避等待里）
        self.assertEqual(len(self.adapter.of("move_relative")), 1)

    def test_cursor_reference_mode_still_supported(self):
        """aim_reference=cursor 是给"真用光标瞄准"的游戏留的逃生舱。"""
        node, ctx = self.build({**self.BASE, "aim_reference": "cursor"}, cursor=(100, 50))

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])

        # 基准点 = 光标 (100,50)，误差 (700,400)，比率 -1 → 位移 (700,400)
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (700, 400))

    def test_path_planner_branch_when_enabled(self):
        """aim_path_planner=true 时走拟人轨迹，且不再发单事件位移。"""
        node, ctx = self.build({**self.BASE, "aim_path_planner": True,
                                "aim_path_steps": 12, "path_strategy": "sine",
                                "aim_duration_ms": 350})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])

        self.assertEqual(self.adapter.of("move_relative_planned"),
                         [(-160, -90, 350, "sine", 12)])
        self.assertEqual(self.adapter.of("move_relative"), [])

    def test_single_event_by_default(self):
        """默认必须是单事件 —— 比率是按单事件标定的，发法不一致比率就失效。"""
        node, ctx = self.build({**self.BASE})

        self.tick(node, ctx, [make_detection(cx=800, cy=450)])

        self.assertEqual(self.adapter.of("move_relative"), [(-160, -90, 0)])
        self.assertEqual(self.adapter.of("move_relative_planned"), [])

    def test_gains_come_from_config_only(self):
        """比率只从配置读取，运行时不改 —— 在线标定已移除（等一帧的功夫精灵就走了）。"""
        node, ctx = self.build({**self.BASE, "aim_gain_x": -1.15, "aim_gain_y": -0.22})

        for _ in range(4):
            self.tick(node, ctx, [make_detection(cx=1160, cy=640)])

        self.assertEqual(node._gain_x, -1.15)
        self.assertEqual(node._gain_y, -0.22)


class TestTargetSelection(NodeTestBase):
    """多目标时的选择策略。默认 reference = 离瞄准基准点（客户区中心）最近。"""

    AIM = {"confirm_frames": 1, "aim_settle_ms": 0, "catch_cooldown_ms": 0,
           "aim_duration_ms": 350}

    def test_prefers_closest_to_reference_over_highest_score(self):
        """近处的低分目标优先于远处的高分目标 —— 转视角代价最小。"""
        node, ctx = self.build({**self.AIM})
        frame = [
            make_detection(cx=200, cy=200, score=0.95),    # 远但置信度高
            make_detection(cx=900, cy=540, score=0.60),    # 离基准点近（误差 60 > 容差 15）
        ]

        self.tick(node, ctx, frame)

        self.assertEqual(self.adapter.of("move_relative")[0][:2], (900 - 960, 540 - 540))

    def test_score_mode_picks_highest(self):
        node, ctx = self.build({**self.AIM, "target_select": "score"})
        frame = [
            make_detection(cx=200, cy=200, score=0.95),
            make_detection(cx=950, cy=540, score=0.60),
        ]

        self.tick(node, ctx, frame)

        # score 模式选中远处高分那只：位移 = 误差 (200-960, 200-540)
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (-760, -340))

    def test_largest_mode_picks_biggest_box(self):
        """largest = 检测框面积最大，近似"离角色最近"。"""
        node, ctx = self.build({**self.AIM, "target_select": "largest"})
        frame = [
            make_detection(cx=950, cy=540, score=0.95, size=30),
            make_detection(cx=400, cy=400, score=0.60, size=200),
        ]

        self.tick(node, ctx, frame)

        # largest 模式选中大框那只 (400,400)：位移 = 误差 (400-960, 400-540)
        self.assertEqual(self.adapter.of("move_relative")[0][:2], (-560, -140))

    def test_select_target_falls_back_to_score_without_reference(self):
        """截帧区域未知时基准点算不出来，退回置信度保证还有目标可选。"""
        node, ctx = self.build({}, frame_rect=(0, 0, 0, 0))
        dets = [make_detection(cx=950, cy=540, score=0.60),
                make_detection(cx=300, cy=300, score=0.99)]

        chosen = node._select_target(ctx, self.adapter, dets)

        self.assertEqual(chosen.score, 0.99)

    def test_select_target_returns_none_for_empty(self):
        node, ctx = self.build({})

        self.assertIsNone(node._select_target(ctx, self.adapter, []))


class TestExitConditions(NodeTestBase):
    """终止条件：投掷上限、时长上限、场景守卫。"""

    def test_max_catches_completes(self):
        node, ctx = self.build({
            "confirm_frames": 1, "aim_settle_ms": 0, "catch_cooldown_ms": 0,
            "aim_max_steps": 1, "max_catches": 2, "next": "step_summary",
        })
        pet = [make_detection()]

        result = next_step = None
        for _ in range(12):
            result, next_step = self.tick(node, ctx, pet)
            if result == NodeResult.SUCCESS:
                break

        self.assertEqual(result, NodeResult.SUCCESS)
        self.assertEqual(next_step, "step_summary")
        self.assertEqual(len(self.adapter.of("hold_left")), 2)

    def test_duration_limit_times_out(self):
        node, ctx = self.build({"duration_sec": 30, "on_timeout": "step_summary"})

        with patch.object(pet_hunt_node, "time") as fake_time:
            fake_time.time.side_effect = [0.0, 100.0]   # on_enter 起点 / execute 当前时刻
            node.on_enter(ctx)
            result, next_step = node.execute(ctx)

        self.assertEqual(result, NodeResult.SUCCESS)
        self.assertEqual(next_step, "step_summary")

    def test_scene_guard_exits_on_absent(self):
        node, ctx = self.build({"scene_target": "wild_scene_label",
                                "scene_min_score": 0.8, "on_absent": "step_lost"})

        result, next_step = self.tick(node, ctx, [make_detection()])

        self.assertEqual(result, NodeResult.SUCCESS)
        self.assertEqual(next_step, "step_lost")
        self.assertEqual(self.adapter.names(), [])

    def test_scene_guard_keeps_hunting_when_present(self):
        node, ctx = self.build({"scene_target": "wild_scene_label", "confirm_frames": 1,
                                "aim_settle_ms": 0, "catch_cooldown_ms": 100_000})
        frame = [make_detection(), make_detection(name="wild_scene_label", score=0.9)]

        self.tick(node, ctx, frame)      # 位移 → 丢球

        self.assertEqual(len(self.adapter.of("hold_left")), 1)

    def test_missing_action_executor_fails(self):
        node = pet_hunt_node.PetHuntNode({"target": "sansan"})
        node.step_name = "step_hunt"
        node.reset()
        node.on_enter(RuntimeContext())

        result, _ = node.execute(RuntimeContext())

        self.assertEqual(result, NodeResult.FAILED)


class TestVariableResolution(NodeTestBase):
    """YAML 变量（${var}）应被解析成实际值。"""

    def test_variables_resolve_from_context(self):
        node, ctx = self.build(
            {
                "target": "${target_pet}",
                "min_score": "${pet_min_score}",
                "throw_hold_ms": "${throw_hold_ms}",
                "confirm_frames": 1,
                "aim_settle_ms": 0,
                "catch_cooldown_ms": 100_000,
            },
            variables={"target_pet": "sansan", "pet_min_score": 0.6, "throw_hold_ms": 900},
        )

        # 低于 0.6 的 sansan 不应触发
        self.tick(node, ctx, [make_detection(score=0.55)])
        self.assertEqual(self.adapter.of("hold_left"), [])

        self.tick(node, ctx, [make_detection(score=0.75)])   # 位移 → 丢球
        self.assertEqual(self.adapter.of("hold_left"), [(900,)])

    def test_unresolvable_numeric_variable_falls_back(self):
        node, ctx = self.build({"throw_hold_ms": "${not_defined}", "confirm_frames": 1,
                                "aim_settle_ms": 0, "catch_cooldown_ms": 100_000})

        self.tick(node, ctx, [make_detection()])

        # 变量缺失时 resolve_value 原样返回字符串，节点应回退到默认 300
        self.assertEqual(self.adapter.of("hold_left"), [(300,)])

    def test_reset_clears_state(self):
        node, ctx = self.build({"confirm_frames": 1, "aim_settle_ms": 0,
                                "catch_cooldown_ms": 0, "max_catches": 1, "next": "step_end"})

        self.tick(node, ctx, [make_detection()])
        self.tick(node, ctx, [make_detection()])

        node.reset()
        node.on_enter(ctx)
        self.assertEqual(node._throws, 0)
        self.assertEqual(node._state, pet_hunt_node._SCAN)

        result, _ = self.tick(node, ctx, [make_detection()])
        self.assertEqual(result, NodeResult.RUNNING)


class TestClickerAdapterPrimitives(unittest.TestCase):
    """ClickerAdapter.move_relative / hold_left 的用户模式回退路径。"""

    def setUp(self):
        from action.adapter import ClickerAdapter

        self.adapter = ClickerAdapter()
        # 不跑 setup()：避免依赖子模块与 Interception 驱动
        self.adapter._initialized = True
        self.adapter._executor = None          # → _interception_available == False

        self.sent: List[Any] = []
        patcher = patch("action.adapter._send_inputs", side_effect=lambda inputs: self.sent.extend(inputs))
        patcher.start()
        self.addCleanup(patcher.stop)

        # 只替换 adapter 模块内的 time 引用，避免污染全局 time.sleep
        time_patcher = patch("action.adapter.time")
        fake_time = time_patcher.start()
        self.fake_sleep = fake_time.sleep
        self.addCleanup(time_patcher.stop)

    def flags_and_deltas(self):
        return [(inp.u.mi.dwFlags, inp.u.mi.dx, inp.u.mi.dy) for inp in self.sent]

    def total_delta(self):
        return (sum(inp.u.mi.dx for inp in self.sent),
                sum(inp.u.mi.dy for inp in self.sent))

    def test_move_relative_positive_remainder(self):
        self.assertTrue(self.adapter.move_relative(7, 5, duration_ms=0, steps=3))
        self.assertEqual(self.total_delta(), (7, 5))
        self.assertEqual(len(self.sent), 3)

    def test_move_relative_negative_remainder(self):
        """负位移的余数分摊必须精确 —— 曾因 divmod 向下取整算错过。"""
        self.assertTrue(self.adapter.move_relative(-7, -5, duration_ms=0, steps=3))
        self.assertEqual(self.total_delta(), (-7, -5))

    def test_move_relative_mixed_sign(self):
        self.assertTrue(self.adapter.move_relative(-11, 4, duration_ms=0, steps=4))
        self.assertEqual(self.total_delta(), (-11, 4))

    def test_move_relative_uses_relative_flag(self):
        """必须是相对位移（无 ABSOLUTE 标志），否则游戏不会当成转视角。"""
        from action.adapter import _MF_ABSOLUTE, _MF_MOVE

        self.adapter.move_relative(10, 0, duration_ms=0, steps=2)

        for flags, _dx, _dy in self.flags_and_deltas():
            self.assertEqual(flags, _MF_MOVE)
            self.assertFalse(flags & _MF_ABSOLUTE)

    def test_move_relative_zero_is_noop(self):
        self.assertTrue(self.adapter.move_relative(0, 0, duration_ms=100))
        self.assertEqual(self.sent, [])

    def test_move_relative_default_is_single_event(self):
        """默认必须单事件发送：游戏对"一个大位移"和"N 个微位移"响应不等价。"""
        self.adapter.move_relative(100, 0, duration_ms=200)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.total_delta(), (100, 0))

    def test_move_relative_can_split_when_explicitly_asked(self):
        self.adapter.move_relative(100, 0, duration_ms=200, steps=20)
        self.assertEqual(len(self.sent), 20)
        self.assertEqual(self.total_delta(), (100, 0))

    def test_hold_left_sends_down_then_up(self):
        from action.adapter import _MF_LEFTDOWN, _MF_LEFTUP

        self.assertTrue(self.adapter.hold_left(1200))

        flags = [inp.u.mi.dwFlags for inp in self.sent]
        self.assertEqual(flags, [_MF_LEFTDOWN, _MF_LEFTUP])
        # 全程不移动鼠标
        self.assertTrue(all(inp.u.mi.dx == 0 and inp.u.mi.dy == 0 for inp in self.sent))
        self.fake_sleep.assert_called_once_with(1.2)

    def test_hold_left_minimum_hold(self):
        self.adapter.hold_left(0)
        self.fake_sleep.assert_called_once_with(0.001)

    def test_primitives_require_initialization(self):
        self.adapter._initialized = False
        self.assertFalse(self.adapter.move_relative(10, 0))
        self.assertFalse(self.adapter.hold_left(100))
        self.assertEqual(self.sent, [])

    def test_cursor_pos_delegates_to_internal(self):
        """cursor_pos() 是相对瞄准算差量的输入，必须拿到真实光标位置。"""
        from action.adapter import ClickerAdapter

        with patch.object(ClickerAdapter, "_get_cursor_pos", return_value=(123, 456)):
            self.assertEqual(self.adapter.cursor_pos(), (123, 456))

    def test_cursor_pos_returns_integers(self):
        """ctypes POINT 结构体能正常读出屏幕坐标（只读，不产生输入）。"""
        x, y = self.adapter.cursor_pos()
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)


class TestClickerAdapterDriverPath(unittest.TestCase):
    """ClickerAdapter 的 Interception 驱动路径。

    装了驱动的机器（本机即是）走的是这条路径，而不是 SendInput 回退，
    因此必须单独校验发出去的 stroke 字段。用假的 stroke 类替换 ActionScript
    模块，避免测试依赖子模块与真实驱动。
    """

    class FakeStroke:
        def __init__(self):
            self.state = 0
            self.flags = -1
            self.rolling = -1
            self.x = 0
            self.y = 0
            self.information = -1

    class FakeExecutor:
        _lib = object()   # 让 _interception_available 判定为 True

        def __init__(self):
            self.strokes = []

        def _send_mouse_stroke(self, stroke):
            self.strokes.append(stroke)
            return True

    def setUp(self):
        import types

        from action.adapter import ClickerAdapter

        self.executor = self.FakeExecutor()
        self.adapter = ClickerAdapter()
        self.adapter._initialized = True
        self.adapter._executor = self.executor
        self.assertTrue(self.adapter._interception_available)

        # 拦截 SendInput，驱动路径下它一次都不该被调用
        self.send_inputs = []
        patcher = patch("action.adapter._send_inputs",
                        side_effect=lambda inputs: self.send_inputs.extend(inputs))
        patcher.start()
        self.addCleanup(patcher.stop)

        # 用假 stroke 类顶替子模块的 ActionScript，保持测试离线可跑
        fake_module = types.ModuleType("ActionScript")
        fake_module.InterceptionMouseStroke = self.FakeStroke
        patcher = patch.dict(sys.modules, {"ActionScript": fake_module})
        patcher.start()
        self.addCleanup(patcher.stop)

        time_patcher = patch("action.adapter.time")
        self.fake_sleep = time_patcher.start().sleep
        self.addCleanup(time_patcher.stop)

    def test_move_relative_uses_driver_not_sendinput(self):
        self.adapter.move_relative(-7, 5, duration_ms=0, steps=3)

        self.assertEqual(self.send_inputs, [])
        self.assertEqual(len(self.executor.strokes), 3)
        self.assertEqual((sum(s.x for s in self.executor.strokes),
                          sum(s.y for s in self.executor.strokes)), (-7, 5))

    def test_move_relative_strokes_are_relative_moves(self):
        """flags 必须为 0（相对移动），state 为 0（无按键变化）。"""
        self.adapter.move_relative(30, 0, duration_ms=0, steps=3)

        for stroke in self.executor.strokes:
            self.assertEqual(stroke.flags, 0)
            self.assertEqual(stroke.state, 0)
            self.assertEqual(stroke.rolling, 0)
            self.assertEqual(stroke.information, 0)

    def test_move_relative_zero_is_noop(self):
        self.adapter.move_relative(0, 0, duration_ms=100)
        self.assertEqual(self.executor.strokes, [])

    def test_hold_left_sends_down_then_up(self):
        self.adapter.hold_left(1200)

        self.assertEqual(self.send_inputs, [])
        states = [s.state for s in self.executor.strokes]
        self.assertEqual(states, [0x001, 0x002])      # 按下 → 松开
        for stroke in self.executor.strokes:
            self.assertEqual(stroke.flags, 0)
            self.assertEqual((stroke.x, stroke.y), (0, 0))   # 全程不移动
        self.fake_sleep.assert_called_once_with(1.2)

    def test_hold_left_aborts_when_press_fails(self):
        """按下失败就不该再发松手事件，否则会留下悬空的按键状态。"""
        self.executor._send_mouse_stroke = lambda stroke: False

        self.assertFalse(self.adapter.hold_left(500))
        self.assertEqual(len(self.executor.strokes), 0)
        self.fake_sleep.assert_not_called()

    def test_hold_left_reports_failure_when_release_fails(self):
        """松手失败必须上报 —— 否则左键会卡在按下状态而调用方毫不知情。"""
        sent = []

        def send(stroke):
            sent.append(stroke.state)
            return stroke.state != 0x002      # 按下成功，松开失败

        self.executor._send_mouse_stroke = send

        self.assertFalse(self.adapter.hold_left(500))
        self.assertEqual(sent, [0x001, 0x002])
        self.fake_sleep.assert_called_once_with(0.5)


class TestPlannedRelativeMove(unittest.TestCase):
    """ClickerAdapter.move_relative_planned：PathPlanner 拟人轨迹的相对位移。

    用子模块里**真实的** PathPlanner（不是 mock），因为要保证的核心性质是
    "扰动后各步位移之和精确等于目标位移" —— 这正是比率标定能沿用的前提。
    """

    class FakeStroke:
        def __init__(self):
            self.state = 0
            self.flags = -1
            self.rolling = -1
            self.x = 0
            self.y = 0
            self.information = -1

    class FakeExecutor:
        _lib = object()

        def __init__(self):
            self.strokes = []

        def _send_mouse_stroke(self, stroke):
            self.strokes.append(stroke)
            return True

    def setUp(self):
        import types

        from action.adapter import ClickerAdapter

        # 让真实的 PathPlanner 可导入
        clicker_dir = str(PROJECT_ROOT / "submodules" / "RocoKingdom-Clicker")
        if clicker_dir not in sys.path:
            sys.path.insert(0, clicker_dir)

        self.executor = self.FakeExecutor()
        self.adapter = ClickerAdapter()
        self.adapter._initialized = True
        self.adapter._executor = self.executor
        self.assertTrue(self.adapter._interception_available)

        fake_module = types.ModuleType("ActionScript")
        fake_module.InterceptionMouseStroke = self.FakeStroke
        patcher = patch.dict(sys.modules, {"ActionScript": fake_module})
        patcher.start()
        self.addCleanup(patcher.stop)

        time_patcher = patch("action.adapter.time")
        time_patcher.start()
        self.addCleanup(time_patcher.stop)

    def totals(self):
        return (sum(s.x for s in self.executor.strokes),
                sum(s.y for s in self.executor.strokes))

    def test_total_displacement_is_preserved(self):
        """核心性质：拟人扰动（加减速+弧线+过冲）之后，净位移必须精确不变。"""
        for dx, dy, steps in ((300, -120, 20), (-209, 727, 20), (45, 8, 8)):
            self.executor.strokes.clear()

            self.assertTrue(self.adapter.move_relative_planned(
                dx, dy, duration_ms=50, strategy="fitts", num_steps=steps))

            self.assertEqual(self.totals(), (dx, dy),
                             f"({dx},{dy}) 的净位移被 PathPlanner 改变了")
            self.assertEqual(len(self.executor.strokes), steps)

    def test_both_strategies_preserve_displacement(self):
        for strategy in ("fitts", "sine"):
            self.executor.strokes.clear()

            self.adapter.move_relative_planned(250, -60, duration_ms=40,
                                               strategy=strategy, num_steps=16)

            self.assertEqual(self.totals(), (250, -60), f"{strategy} 策略改变了净位移")

    def test_all_strokes_are_relative_moves(self):
        self.adapter.move_relative_planned(200, 0, duration_ms=40, num_steps=10)

        for stroke in self.executor.strokes:
            self.assertEqual(stroke.flags, 0)      # 相对移动
            self.assertEqual(stroke.state, 0)      # 不夹带按键

    def test_short_move_falls_back_to_single_event(self):
        """位移过短时规划没有意义，直接单事件发出。"""
        self.adapter.move_relative_planned(1, 1, duration_ms=100, num_steps=20)

        self.assertEqual(len(self.executor.strokes), 1)
        self.assertEqual(self.totals(), (1, 1))

    def test_zero_move_is_noop(self):
        self.assertTrue(self.adapter.move_relative_planned(0, 0))
        self.assertEqual(self.executor.strokes, [])

    def test_falls_back_when_pathplanner_unavailable(self):
        """PathPlanner 导入失败时退回单事件，绝不能因此不动。"""
        with patch.dict(sys.modules, {"PathPlanner": None}):
            self.adapter.move_relative_planned(300, -120, duration_ms=50, num_steps=20)

        self.assertEqual(len(self.executor.strokes), 1)
        self.assertEqual(self.totals(), (300, -120))

    def test_requires_initialization(self):
        self.adapter._initialized = False
        self.assertFalse(self.adapter.move_relative_planned(100, 0))
        self.assertEqual(self.executor.strokes, [])


class TestFrameCapturedFlag(unittest.TestCase):
    """frame_captured 标志的传导链路：SpottingAdapter → Engine → RuntimeContext。"""

    def test_context_defaults_to_not_captured(self):
        self.assertFalse(RuntimeContext().frame_captured)

    def test_adapter_reports_capture_failure(self):
        from perception.adapter import SpottingAdapter

        adapter = SpottingAdapter({})
        self.assertFalse(adapter.last_capture_ok)

        # 未 setup（或窗口丢失/不在前台）时检测返回空，且必须标记为"没截到画面"
        self.assertEqual(adapter.detect_frame(), [])
        self.assertFalse(adapter.last_capture_ok)

    def test_engine_propagates_flag_to_context(self):
        from core.engine import Engine

        class FakePerception:
            def __init__(self, detections, capture_ok):
                self._detections = detections
                self.last_capture_ok = capture_ok

            def detect_frame(self):
                return self._detections

        engine = Engine()
        engine._scene_machine = None

        engine._perception = FakePerception([], capture_ok=False)
        engine._perceive()
        self.assertFalse(engine.context.frame_captured)
        self.assertEqual(engine.context.frame_seq, 1)

        engine._perception = FakePerception([make_detection()], capture_ok=True)
        engine._perceive()
        self.assertTrue(engine.context.frame_captured)
        self.assertEqual(len(engine.context.last_detections), 1)
        # 帧序号必须单调自增：节点靠它判断"是不是瞄准后的新帧"
        self.assertEqual(engine.context.frame_seq, 2)


class TestDetectionOverlay(unittest.TestCase):
    """检测框覆盖层：屏幕绝对坐标 → 帧内相对坐标的换算，以及光标标记。"""

    class FakeOverlay:
        def __init__(self):
            self.ensure_calls: List[tuple] = []
            self.render_calls: List[tuple] = []
            self.hidden = 0
            self.destroyed = 0
            self.raise_on_render = False

        def ensure(self, hwnd, x, y, w, h):
            self.ensure_calls.append((hwnd, x, y, w, h))

        def render(self, detections, frame_w, frame_h):
            if self.raise_on_render:
                raise RuntimeError("GDI 炸了")
            self.render_calls.append((list(detections), frame_w, frame_h))

        def hide(self):
            self.hidden += 1

        def destroy(self):
            self.destroyed += 1

    class FakeFrame:
        shape = (1080, 1920, 3)

    ORIGIN = (100, 50)      # 截帧区域左上角的屏幕坐标

    def make_adapter(self, show_cursor: bool = True):
        from perception.adapter import SpottingAdapter

        adapter = SpottingAdapter({"show_overlay": True, "overlay_show_cursor": show_cursor})
        adapter._overlay = self.FakeOverlay()
        adapter._hwnd = 12345
        adapter._last_rect = (self.ORIGIN[0], self.ORIGIN[1], 1920, 1080)
        return adapter

    def rendered(self, adapter) -> List[Detection]:
        return adapter._overlay.render_calls[-1][0]

    def test_coordinates_converted_to_frame_relative(self):
        """覆盖层要帧内坐标，detect_frame 给的是屏幕坐标，必须减回原点。"""
        adapter = self.make_adapter(show_cursor=False)
        det = make_detection(cx=1100, cy=1050)     # 屏幕绝对坐标

        adapter._render_overlay(self.FakeFrame(), [det])

        drawn = self.rendered(adapter)[0]
        self.assertEqual(drawn.cx, 1100 - self.ORIGIN[0])
        self.assertEqual(drawn.cy, 1050 - self.ORIGIN[1])
        self.assertEqual(drawn.name, "sansan")

    def test_ensure_receives_client_rect(self):
        adapter = self.make_adapter(show_cursor=False)

        adapter._render_overlay(self.FakeFrame(), [])

        self.assertEqual(adapter._overlay.ensure_calls, [(12345, 100, 50, 1920, 1080)])
        self.assertEqual(adapter._overlay.render_calls[-1][1:], (1920, 1080))

    def test_cursor_marker_appended(self):
        adapter = self.make_adapter(show_cursor=True)

        with patch("perception.adapter.SpottingAdapter._read_cursor",
                   return_value=(300, 200)):
            adapter._render_overlay(self.FakeFrame(), [make_detection()])

        drawn = self.rendered(adapter)
        # 顺序：检测框 → <aim> 基准点 → <cursor> 光标
        self.assertEqual([d.name for d in drawn], ["sansan", "<aim>", "<cursor>"])
        marker = drawn[-1]
        self.assertEqual((marker.cx, marker.cy), (300 - 100, 200 - 50))
        # 必须是低分色（黄框），才能和绿色检测框区分开
        self.assertTrue(marker.is_low_score)

    def test_aim_marker_at_client_center(self):
        """<aim> 标记 = 客户区中心，球就是丢向这里的。"""
        adapter = self.make_adapter(show_cursor=False)

        adapter._render_overlay(self.FakeFrame(), [])

        drawn = self.rendered(adapter)
        self.assertEqual([d.name for d in drawn], ["<aim>"])
        aim = drawn[0]
        # 帧内坐标 = 客户区尺寸的一半（1920/2, 1080/2），与截帧原点无关
        self.assertEqual((aim.cx, aim.cy), (960, 540))

    def test_aim_marker_can_be_disabled(self):
        from perception.adapter import SpottingAdapter

        adapter = SpottingAdapter({"show_overlay": True, "overlay_show_cursor": False,
                                    "overlay_show_reference": False})
        adapter._overlay = self.FakeOverlay()
        adapter._hwnd = 12345
        adapter._last_rect = (100, 50, 1920, 1080)

        adapter._render_overlay(self.FakeFrame(), [make_detection()])

        self.assertEqual([d.name for d in self.rendered(adapter)], ["sansan"])

    def test_cursor_marker_can_be_disabled(self):
        adapter = self.make_adapter(show_cursor=False)

        adapter._render_overlay(self.FakeFrame(), [make_detection()])

        # 关掉光标标记后仍保留 <aim> 基准点标记
        self.assertEqual([d.name for d in self.rendered(adapter)], ["sansan", "<aim>"])

    def test_hidden_when_no_frame(self):
        """截不到画面时要隐藏覆盖层，否则会留下过期的框。"""
        adapter = self.make_adapter()

        adapter._render_overlay(None, [])

        self.assertEqual(adapter._overlay.hidden, 1)
        self.assertEqual(adapter._overlay.render_calls, [])

    def test_render_failure_disables_overlay_without_raising(self):
        """覆盖层只是调试辅助，画不出来不能拖垮主流程。"""
        adapter = self.make_adapter()
        adapter._overlay.raise_on_render = True

        adapter._render_overlay(self.FakeFrame(), [make_detection()])   # 不应抛异常
        self.assertIsNone(adapter._overlay)

        adapter._overlay = self.FakeOverlay()
        adapter._render_overlay(self.FakeFrame(), [])                  # 重新装上也还能用

    def test_noop_when_overlay_disabled(self):
        from perception.adapter import SpottingAdapter

        adapter = SpottingAdapter({})          # show_overlay 默认关闭
        self.assertIsNone(adapter._overlay)
        adapter._render_overlay(self.FakeFrame(), [make_detection()])   # 不应抛异常

    def test_cleanup_destroys_overlay(self):
        adapter = self.make_adapter()
        overlay = adapter._overlay

        adapter.cleanup()

        self.assertEqual(overlay.destroyed, 1)
        self.assertIsNone(adapter._overlay)


class TestConfigExtends(unittest.TestCase):
    """config_loader 的 extends 继承与深度合并。

    捕猎要用 config/global_hunt.yaml 关掉 234ms 的模板检测器，
    而 enter_game.yaml 又离不开它，所以靠继承派生出两份配置。
    """

    def write(self, directory: str, name: str, content: str) -> Path:
        path = Path(directory) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_deep_merge_dicts_recursively(self):
        from utils.config_loader import _deep_merge

        merged = _deep_merge(
            {"a": 1, "spotting": {"window_keyword": "K", "foreground_only": True}},
            {"spotting": {"foreground_only": False}},
        )

        self.assertEqual(merged, {
            "a": 1,
            "spotting": {"window_keyword": "K", "foreground_only": False},
        })

    def test_deep_merge_replaces_lists_wholesale(self):
        """列表整体替换而非拼接，否则派生配置里的 templates 会和基础的混在一起。"""
        from utils.config_loader import _deep_merge

        merged = _deep_merge({"items": [1, 2, 3]}, {"items": [9]})

        self.assertEqual(merged["items"], [9])

    def test_deep_merge_does_not_mutate_inputs(self):
        from utils.config_loader import _deep_merge

        base = {"spotting": {"a": 1}}
        _deep_merge(base, {"spotting": {"b": 2}})

        self.assertEqual(base, {"spotting": {"a": 1}})

    def test_extends_inherits_and_overrides(self):
        import tempfile

        from utils.config_loader import load_global_config

        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, "base.yaml",
                       'loop_interval: 0.1\n'
                       'spotting:\n'
                       '  window_keyword: "K"\n'
                       '  foreground_only: true\n')
            child = self.write(tmp, "child.yaml",
                               'extends: "base.yaml"\n'
                               'loop_interval: 0.02\n'
                               'spotting:\n'
                               '  detectors:\n'
                               '    flat_labels:\n'
                               '      enabled: false\n')

            cfg = load_global_config(child)

        self.assertEqual(cfg["loop_interval"], 0.02)                    # 覆盖
        self.assertEqual(cfg["spotting"]["window_keyword"], "K")         # 继承
        self.assertTrue(cfg["spotting"]["foreground_only"])              # 继承
        self.assertFalse(cfg["spotting"]["detectors"]["flat_labels"]["enabled"])
        self.assertNotIn("extends", cfg)                                 # extends 本身要被消费掉

    def test_extends_cycle_falls_back_to_defaults(self):
        """互相继承必须被检测到并安全回退，不能死循环。"""
        import tempfile

        from utils.config_loader import load_global_config

        with tempfile.TemporaryDirectory() as tmp:
            a = self.write(tmp, "a.yaml", 'extends: "b.yaml"\nloop_interval: 0.5\n')
            self.write(tmp, "b.yaml", 'extends: "a.yaml"\nloop_interval: 0.9\n')

            cfg = load_global_config(a)

        self.assertEqual(cfg["loop_interval"], 0.1)   # _default_config 的值
        self.assertNotIn("extends", cfg)

    def test_global_hunt_config_is_valid(self):
        """真实的 global_hunt.yaml：关掉模板检测器，其余从 global.yaml 继承。"""
        from utils.config_loader import load_global_config

        cfg = load_global_config(PROJECT_ROOT / "config" / "global_hunt.yaml")

        self.assertEqual(cfg["loop_interval"], 0.02)
        self.assertFalse(cfg["spotting"]["detectors"]["flat_labels"]["enabled"])
        # 以下都应是继承来的，证明继承链没断
        self.assertEqual(cfg["spotting"]["window_keyword"], "洛克王国：世界")
        self.assertTrue(cfg["spotting"]["foreground_only"])
        self.assertTrue(cfg["spotting"]["show_overlay"])
        self.assertTrue(cfg["spotting"]["overlay_show_cursor"])
        self.assertEqual(cfg["clicker"]["move_mouse"], True)


class TestCatchPetWorkflowIntegration(NodeTestBase):
    """用真实的 config/workflows/catch_pet.yaml 驱动 WorkflowExecutor。

    适配器是假的，因此不会产生真实键鼠输入，也不需要游戏窗口和模型。
    """

    def setUp(self):
        import yaml

        path = PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml"
        self.definition = yaml.safe_load(path.read_text(encoding="utf-8"))

        from workflow.executor import WorkflowExecutor

        self.context = RuntimeContext()
        self.context.variables.update(self.definition["variables"])
        self.context.workflow_name = self.definition["name"]
        # 真实运行里由 Engine._perceive 填入；这里手工模拟 1920x1080 客户区
        self.context.frame_rect = (0, 0, 1920, 1080)
        self.adapter = FakeClickerAdapter()

        # WorkflowExecutor 会把 action_executor 注入 context，必须从构造参数传入
        self.executor = WorkflowExecutor(
            workflow_def=self.definition,
            context=self.context,
            action_executor=FakeActionExecutor(self.adapter),
        )
        self.executor.start()

    def test_starts_at_hunt_step(self):
        self.assertEqual(self.executor.current_step, "step_hunt")

    def feed(self, detections, captured: bool = True):
        """模拟引擎采了一帧：帧序号自增 + 写入检测结果。"""
        self.context.frame_seq += 1
        self.context.last_detections = detections
        self.context.frame_captured = captured

    def test_blind_then_scan_then_throw(self):
        import time

        # 1) 截不到画面：绝不能产生任何键鼠输入
        for _ in range(5):
            self.feed([], captured=False)
            self.executor.tick()
        self.assertEqual(self.adapter.names(), [])

        # 2) 画面正常但没有精灵：慢速转视角（单事件位移，节奏由 pan_interval_ms 控制）
        self.feed([])
        self.executor.tick()
        self.assertEqual(self.adapter.of("move_relative"), [(
            self.context.variables["pan_dx"],
            self.context.variables["pan_dy"],
            0,                       # 单事件发送 → duration 恒为 0
        )])

        # 3) 目标出现：确认帧数未满之前不该有任何瞄准动作
        # 检测框的名字必须取自 YAML 的 target_pet，否则改了默认目标这个测试就静默失效；
        # 确认帧数 / 稳定等待也从 YAML 读，改配置不必改测试
        v = self.context.variables
        target_name = v["target_pet"]
        pet = [make_detection(cx=1200, cy=700, score=0.9, name=target_name)]
        pan_count = len(self.adapter.of("move_relative"))
        confirm = max(1, int(v["confirm_frames"]))

        for _ in range(confirm - 1):
            self.feed(pet)
            self.executor.tick()
        self.assertEqual(len(self.adapter.of("move_relative")), pan_count)
        self.assertEqual(self.adapter.of("hold_left"), [])

        # 4) 确认通过 → 按比率算位移，走 YAML 里配置的发送方式
        self.feed(pet)
        self.executor.tick()
        exp_dx = round(-(1200 - 960) / v["aim_gain_x"])
        exp_dy = round(-(700 - 540) / v["aim_gain_y"])
        if v["aim_path_planner"]:
            planned = self.adapter.of("move_relative_planned")
            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0], (exp_dx, exp_dy, v["aim_duration_ms"],
                                          v["path_strategy"], v["aim_path_steps"]))
            self.assertEqual(self.adapter.of("move_relative")[pan_count:], [])
        else:
            self.assertEqual(self.adapter.of("move_relative")[pan_count:],
                             [(exp_dx, exp_dy, 0)])
        # 全程不产生任何绝对定位事件
        self.assertEqual(self.adapter.of("move_to_planned"), [])
        self.assertEqual(self.adapter.of("move_to"), [])

        # 5) aim_settle_ms=0 → 转完视角当帧就丢球；>0 → 等相机到位后的下一 tick 丢
        if v["aim_settle_ms"] > 0:
            self.assertEqual(self.adapter.of("hold_left"), [])
            time.sleep(v["aim_settle_ms"] / 1000.0 + 0.03)
            self.feed(pet)
            self.executor.tick()
        self.assertEqual(self.adapter.of("hold_left"), [(v["throw_hold_ms"],)])
        self.assertEqual(self.context.get_counter("pets_thrown"), 1)
        self.assertEqual(self.context.get_variable("pets_thrown"), 1)

        # 6) 冷却期内不应再开火
        self.feed(pet)
        self.executor.tick()
        self.assertEqual(len(self.adapter.of("hold_left")), 1)

    def test_summary_step_reports_throw_count(self):
        """捕猎结束后的 log 节点应能引用 ${pets_thrown}。"""
        self.context.set_variable("pets_thrown", 7)

        from workflow.nodes.log_node import LogNode

        node = LogNode(self.definition["steps"]["step_summary"])
        node.step_name = "step_summary"
        result, next_step = node.execute(self.context)

        self.assertEqual(result, NodeResult.SUCCESS)
        self.assertEqual(next_step, "step_end")


class TestCalibrateAimTool(unittest.TestCase):
    """tools/calibrate_aim.py 的画面位移测量。

    第一版用相位相关，实测翻车了：相机确实转了，它却报 0.1px —— 因为相位相关
    只能测出不超过 ROI 一半的平移，而转视角动辄几百像素。改用 ORB + 单应矩阵后，
    这里专门留一条大位移回归测试钉住这个坑。
    """

    @staticmethod
    def texture(seed: int = 7, size: Tuple[int, int] = (400, 600)):
        """造一张既有空间结构、又有尖锐角点的纹理，模拟真实游戏画面。

        两个极端都不行：
          - 稀疏色块：大片空白区域无法追踪，且错轴偶然对齐可能胜出
          - 密集随机矩形：近似白噪声，任何错位的代价都差不多
          - 纯平滑分形噪声：goodFeaturesToTrack 找不到角点，光流无点可追
        所以用「分形平滑底 + 随机矩形色块」的组合：底色提供连续结构，
        矩形的边角提供 LK 光流需要的可追踪特征。
        """
        rng = np.random.default_rng(seed)
        h, w = size
        field = np.zeros((h, w), dtype=np.float64)
        for scale in (8, 16, 32, 64):
            small = rng.random((max(2, h // scale), max(2, w // scale)))
            field += cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
        field -= field.min()
        field /= max(field.max(), 1e-9)
        img = cv2.cvtColor((field * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)

        for _ in range(max(60, w * h // 4000)):
            x = int(rng.integers(0, max(1, w - 12)))
            y = int(rng.integers(0, max(1, h - 12)))
            bw = int(rng.integers(4, 12))
            bh = int(rng.integers(4, 12))
            color = tuple(int(c) for c in rng.integers(0, 255, 3))
            cv2.rectangle(img, (x, y), (x + bw, y + bh), color, -1)
        return img

    @staticmethod
    def shift_image(img, dx: int, dy: int):
        h, w = img.shape[:2]
        matrix = np.float32([[1, 0, dx], [0, 1, dy]])
        return cv2.warpAffine(img, matrix, (w, h), borderMode=cv2.BORDER_REPLICATE)

    def setUp(self):
        sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        import calibrate_aim
        self.tool = calibrate_aim

    def test_detects_known_horizontal_shift(self):
        base = self.texture()
        moved = self.shift_image(base, 40, 0)      # 内容整体右移 40px

        m = self.tool.measure_shift(base, moved)

        self.assertAlmostEqual(m["dx"], 40, delta=3)
        self.assertLess(abs(m["dy"]), 3)
        self.assertTrue(m["reliable"])
        # 钉住符号：内容右移 → dx 为正
        self.assertGreater(m["dx"], 0, f"符号约定变了：内容右移却得到 dx={m['dx']:.1f}")

    def test_detects_known_vertical_shift(self):
        base = self.texture()
        moved = self.shift_image(base, 0, -25)     # 内容整体上移 25px

        m = self.tool.measure_shift(base, moved)

        self.assertAlmostEqual(m["dy"], -25, delta=3)
        self.assertLess(abs(m["dx"]), 3)

    def test_moderate_shift_is_measured_exactly(self):
        """光流法的可靠工作区间：几十像素的位移能精确测出。"""
        base = self.texture(seed=11, size=(1080, 4000))
        moved = self.shift_image(base, 60, 0)

        m = self.tool.measure_shift(base, moved)

        self.assertAlmostEqual(m["dx"], 60, delta=2)
        self.assertLess(abs(m["dy"]), 2)
        self.assertTrue(m["reliable"])
        self.assertLess(m["mad"], 2.0)

    def test_large_shift_is_flagged_unreliable_not_guessed(self):
        """超出光流追踪范围时必须报不可信，而不是给一个看似合理的错数字。

        LK 在重复纹理上遇到几百像素的位移会误匹配（实测 MAD 从 0 飙到 80）。
        标定工具宁可说"测不了"也不能给出错误的比率 —— 错误的比率会让瞄准
        系统性偏差，而且看起来像是"参数没调好"，极难排查。
        """
        base = self.texture(seed=11, size=(1080, 4000))
        moved = self.shift_image(base, 400, 0)

        m = self.tool.measure_shift(base, moved)

        self.assertFalse(m["reliable"])
        self.assertEqual((m["dx"], m["dy"]), (0.0, 0.0))   # 不可信时归零，不外流假值
        self.assertGreater(m["mad"], self.tool.MIN_FLOW_MAD)

    def test_zero_shift_reads_as_still(self):
        base = self.texture()

        m = self.tool.measure_shift(base, base.copy())

        self.assertLess(abs(m["dx"]), self.tool.STILL_EPSILON)
        self.assertLess(abs(m["dy"]), self.tool.STILL_EPSILON)

    def test_shift_is_antisymmetric(self):
        """正反两次位移应给出符号相反、量级相同的结果，否则增益算不可信。"""
        base = self.texture()
        shifted = self.shift_image(base, 30, 0)

        forward = self.tool.measure_shift(base, shifted)
        backward = self.tool.measure_shift(shifted, base)

        self.assertAlmostEqual(forward["dx"], -backward["dx"], delta=4)

    def test_robust_to_local_edge_changes(self):
        """局部变化（边缘 UI、路过的动画）不该把整体测量带偏。"""
        base = self.texture()
        moved = self.shift_image(base, 20, 0)
        moved[:80, :80] = 0            # 左上角涂黑，模拟 UI/特效变化

        m = self.tool.measure_shift(base, moved)

        self.assertAlmostEqual(m["dx"], 20, delta=5)

    def test_frame_diff_flags_identical_frames(self):
        """帧差用于区分"相机没动"和"截图返回了缓存帧"。"""
        base = self.texture()

        identical = self.tool.measure_shift(base, base.copy())
        changed = self.tool.measure_shift(base, self.shift_image(base, 60, 0))

        self.assertLess(identical["diff"], 0.01)
        self.assertGreater(changed["diff"], 1.0)

    def test_uniform_frame_reports_unreliable(self):
        """纯色画面没有可追踪的特征点，必须报 reliable=False 而不是给出一个假的 0。"""
        flat = np.full((400, 600, 3), 128, np.uint8)

        m = self.tool.measure_shift(flat, flat.copy())

        self.assertFalse(m["reliable"])
        self.assertLess(m["n"], self.tool.MIN_FLOW_POINTS)

    def test_ignores_stationary_center_object(self):
        """第三人称陷阱：画面中心一大块静止（玩家角色），只有边缘世界在转。

        中心对齐的方法（ORB/相位相关/ECC）会被这块静止区域锁死在 0 位移，
        边缘条带才是正确的对齐区域。
        """
        base = self.texture(seed=3, size=(720, 1280))
        moved = self.shift_image(base, 60, 0)

        h, w = base.shape[:2]
        y0, y1 = h // 4, h * 3 // 4
        x0, x1 = w // 3, w * 2 // 3
        moved[y0:y1, x0:x1] = base[y0:y1, x0:x1]   # 中心还原成未移动

        m = self.tool.measure_shift(base, moved)

        self.assertAlmostEqual(m["dx"], 60, delta=6)
        self.assertTrue(m["reliable"])

    def test_picks_the_uncorrupted_strip(self):
        """一条边带被局部变化污染时，应采用另一条干净边带的结论。"""
        base = self.texture(seed=5, size=(720, 1280))
        moved = self.shift_image(base, 40, 0)

        h, w = base.shape[:2]
        moved[:, : int(w * 0.36)] = 0          # 整个左边带涂黑

        m = self.tool.measure_shift(base, moved)

        self.assertAlmostEqual(m["dx"], 40, delta=6)
        self.assertTrue(m["strip"].startswith(str(int(w * 0.74))))

    def test_read_aim_settings_matches_workflow(self):
        """标定工具必须读到工作流里的真实发法，否则测的和跑的不一致。"""
        settings = self.tool.read_aim_settings(
            PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml")

        import yaml
        variables = yaml.safe_load(
            (PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml")
            .read_text(encoding="utf-8"))["variables"]

        # 逐项对齐 YAML，避免配置改了而工具读的是旧值
        self.assertEqual(settings["path_planner"], bool(variables["aim_path_planner"]))
        self.assertEqual(settings["path_steps"], variables["aim_path_steps"])
        self.assertEqual(settings["path_strategy"], variables["path_strategy"])
        self.assertEqual(settings["duration_ms"], variables["aim_duration_ms"])
        self.assertAlmostEqual(settings["gain_x"], variables["aim_gain_x"])
        self.assertAlmostEqual(settings["gain_y"], variables["aim_gain_y"])

    def test_read_aim_settings_missing_file_falls_back(self):
        settings = self.tool.read_aim_settings(
            PROJECT_ROOT / "config" / "workflows" / "does_not_exist.yaml")

        self.assertFalse(settings["path_planner"])

    def test_send_move_dispatches_like_the_node(self):
        """工具发位移的方式必须和节点一致：拟人轨迹 or 单事件。"""
        fake = FakeClickerAdapter()

        self.tool.send_move(fake, 100, 0, {"path_planner": True, "path_steps": 7,
                                           "duration_ms": 200, "path_strategy": "sine"})
        self.assertEqual(fake.of("move_relative_planned"), [(100, 0, 200, "sine", 7)])
        self.assertEqual(fake.of("move_relative"), [])

        self.tool.send_move(fake, 50, 0, {"path_planner": False})
        self.assertEqual(fake.of("move_relative"), [(50, 0, 0)])

        # settings 为空时也必须能跑（默认单事件）
        self.tool.send_move(fake, 30, 0, {})
        self.assertEqual(fake.of("move_relative")[-1], (30, 0, 0))


class TestDetectorProbe(unittest.TestCase):
    """tools/calibrate_aim.py 的检测框法标定。

    水平轴（偏航）是透视旋转不是平移，区域对齐法会给出虚假的对角位移，
    所以必须靠追踪检测框中心来测 —— 这也正是节点瞄准时用的同一个信号。
    """

    class FakeDet:
        def __init__(self, cx, cy, name="sansan", score=0.9):
            self.cx, self.cy, self.name, self.score = cx, cy, name, score

    class FakePerception:
        last_rect = (0, 0, 1920, 1080)

        def __init__(self, detections_per_call):
            self.queue = list(detections_per_call)
            self.captures = 0

        def capture_frame(self):
            self.captures += 1
            return np.zeros((8, 8, 3), dtype=np.uint8)

        def detect_frame(self):
            return self.queue.pop(0) if self.queue else []

    def setUp(self):
        sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        import calibrate_aim
        self.tool = calibrate_aim

    def probe(self, before, after, dx=100, dy=0):
        perception = self.FakePerception([before, after])
        clicker = FakeClickerAdapter()
        result = self.tool.detector_probe("t", dx, dy, perception, clicker, settle=0.0)
        return result, clicker

    def test_ratio_from_box_center_shift(self):
        result, clicker = self.probe([self.FakeDet(1200, 700)],
                                     [self.FakeDet(1090, 700)])

        # 鼠标 +100 → 检测中心 -110 → 比率 -1.10
        self.assertAlmostEqual(result["gain_x"], -1.10, places=6)
        self.assertIsNone(result["gain_y"])
        self.assertEqual(clicker.of("move_relative"), [(100, 0, 0)])

    def test_vertical_ratio(self):
        result, _ = self.probe([self.FakeDet(960, 600)], [self.FakeDet(960, 579)],
                               dx=0, dy=100)

        self.assertAlmostEqual(result["gain_y"], -0.21, places=6)

    def test_picks_detection_nearest_center(self):
        """多只精灵时选最靠近画面中心的那只，位移后最不容易转出画面。"""
        before = [self.FakeDet(200, 200, score=0.99), self.FakeDet(950, 540, score=0.6)]
        after = [self.FakeDet(200, 200, score=0.99), self.FakeDet(840, 540, score=0.6)]

        result, _ = self.probe(before, after)

        # 选中的是 (950,540) 那只 → 位移 -110 → 比率 -1.10（而不是边缘那只的 0）
        self.assertAlmostEqual(result["gain_x"], -1.10, places=6)

    def test_no_detection_returns_none(self):
        result, clicker = self.probe([], [self.FakeDet(960, 540)])

        self.assertIsNone(result)
        self.assertEqual(clicker.of("move_relative"), [])   # 没目标就不该动鼠标

    def test_target_lost_after_move_returns_none(self):
        result, clicker = self.probe([self.FakeDet(1200, 700)], [])

        self.assertIsNone(result)
        self.assertEqual(len(clicker.of("move_relative")), 1)   # 位移已经发出去了

    def test_uses_configured_send_mode(self):
        """检测框法也必须走工作流配置的发法，否则测跑不一致。"""
        perception = self.FakePerception([[self.FakeDet(1200, 700)],
                                          [self.FakeDet(1090, 700)]])
        clicker = FakeClickerAdapter()

        self.tool.detector_probe("t", 100, 0, perception, clicker, settle=0.0,
                                 settings={"path_planner": True, "path_steps": 9,
                                           "duration_ms": 120, "path_strategy": "sine"})

        self.assertEqual(clicker.of("move_relative_planned"), [(100, 0, 120, "sine", 9)])
        self.assertEqual(clicker.of("move_relative"), [])


class TestRelabelDatasetTool(unittest.TestCase):
    """tools/relabel_dataset.py 的 data.yaml 解析。

    类别名列表是整个重编号的依据，解析错了会把标注改到错误的 id 上，
    所以两种常见写法都要能正确解析。
    """

    def setUp(self):
        sys.path.insert(0, str(PROJECT_ROOT / "tools"))
        import relabel_dataset
        self.tool = relabel_dataset

    def parse(self, text: str):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.yaml"
            path.write_text(text, encoding="utf-8")
            return self.tool.read_names(path)

    def test_parses_quoted_list(self):
        names = self.parse("train: images\nnc: 6\nnames: ['beise', 'cloversketch', "
                           "'gjy', 'qjk', 'sansan', 'yunxing']\n")
        self.assertEqual(names, ["beise", "cloversketch", "gjy", "qjk", "sansan", "yunxing"])

    def test_parses_unquoted_list(self):
        names = self.parse("names: [beise, cloversketch, sansan]\n")
        self.assertEqual(names, ["beise", "cloversketch", "sansan"])

    def test_real_dataset_folders_are_all_declared(self):
        """不变量：每个标注目录名都必须在 data.yaml 的 names 里。

        不钉具体索引（插入新类别就会变），只钉这条 —— 目录名不在类别表里，
        relabel 工具就无法推断该目录的正确 id，也正是上次错位事故的形状。
        """
        dataset = Path(r"C:\Users\17676\Desktop\开发中\RocoKingdom-Spotting"
                       r"\datasets\yolo_dataset")
        if not dataset.is_dir():
            self.skipTest("独立训练仓库不在本机")

        names = self.tool.read_names(dataset / "data.yaml")
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)), "data.yaml 里有重名类别")

        folders = {p.name for p in (dataset / "labels").iterdir() if p.is_dir()}
        self.assertTrue(folders)
        self.assertEqual(folders - set(names), set(),
                         "有标注目录名不在 data.yaml 的 names 里，relabel 无法确定其 id")

    def test_missing_names_raises(self):
        with self.assertRaises(ValueError):
            self.parse("train: images\nnc: 6\n")


class TestVarOverride(unittest.TestCase):
    """main.py 的 --var KEY=VALUE 覆盖：换目标精灵不必改 YAML。"""

    def setUp(self):
        import main
        self.main = main

    def test_coerce_types(self):
        """YAML 变量是有类型的，命令行只能给字符串，必须做最小类型推断。"""
        coerce = self.main.coerce_var_value
        self.assertEqual(coerce("pipaniao"), "pipaniao")
        self.assertEqual(coerce("sansan"), "sansan")
        self.assertIsInstance(coerce("3"), int)
        self.assertEqual(coerce("3"), 3)
        self.assertIsInstance(coerce("-1.15"), float)
        self.assertAlmostEqual(coerce("-1.15"), -1.15)
        self.assertIs(coerce("true"), True)
        self.assertIs(coerce("False"), False)
        self.assertIsNone(coerce("null"))
        # 不能被误判成数字的字符串
        self.assertEqual(coerce("0.5x"), "0.5x")

    def test_parse_multiple_overrides(self):
        result = self.main.parse_var_overrides(
            ["target_pet=pipaniao", "throw_hold_ms=250", "max_catches=3"])

        self.assertEqual(result, {"target_pet": "pipaniao",
                                  "throw_hold_ms": 250, "max_catches": 3})

    def test_parse_handles_equals_in_value(self):
        result = self.main.parse_var_overrides(["msg=a=b"])
        self.assertEqual(result, {"msg": "a=b"})

    def test_parse_rejects_bad_format(self):
        with self.assertRaises(ValueError):
            self.main.parse_var_overrides(["target_pet"])
        with self.assertRaises(ValueError):
            self.main.parse_var_overrides(["=pipaniao"])

    def test_parse_none_is_empty(self):
        self.assertEqual(self.main.parse_var_overrides(None), {})

    def test_engine_applies_overrides_over_yaml(self):
        from core.engine import Engine

        engine = Engine()
        engine.load_workflow(
            str(PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml"),
            variable_overrides={"target_pet": "sansan", "throw_hold_ms": 250},
        )

        # YAML 里默认是 pipaniao，被命令行覆盖成 sansan
        self.assertEqual(engine.context.get_variable("target_pet"), "sansan")
        self.assertEqual(engine.context.get_variable("throw_hold_ms"), 250)
        # 没覆盖的变量保持 YAML 里的值
        self.assertEqual(engine.context.get_variable("aim_gain_y"), -0.21)

    def test_engine_without_overrides_keeps_yaml_values(self):
        from core.engine import Engine

        engine = Engine()
        engine.load_workflow(
            str(PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml"))

        self.assertEqual(engine.context.get_variable("target_pet"), "pipaniao")

    def test_unknown_override_key_is_warned_not_fatal(self):
        """拼错变量名不该静默生效，也不该让整个流程崩掉。"""
        import io
        import logging

        from core.engine import Engine

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger("core.engine")
        logger.addHandler(handler)
        try:
            engine = Engine()
            engine.load_workflow(
                str(PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml"),
                variable_overrides={"target_pett": "sansan"},   # 故意拼错
            )
        finally:
            logger.removeHandler(handler)

        self.assertIn("target_pett", stream.getvalue())
        # 拼错的键不会覆盖到正确的键上
        self.assertEqual(engine.context.get_variable("target_pet"), "pipaniao")


class TestRegistration(unittest.TestCase):
    """节点类型注册与工作流加载。"""

    def test_pet_hunt_is_registered(self):
        from workflow.registry import NodeRegistry

        NodeRegistry._ensure_builtin_loaded()
        self.assertIn("pet_hunt", NodeRegistry.available_types())

        node = NodeRegistry.create("pet_hunt", {"target": "sansan"})
        self.assertIsInstance(node, pet_hunt_node.PetHuntNode)
        self.assertEqual(node.node_type, "pet_hunt")

    def test_valid_node_types_includes_pet_hunt(self):
        from workflow.loader import WorkflowLoader

        self.assertIn("pet_hunt", WorkflowLoader.VALID_NODE_TYPES)

    def test_catch_pet_workflow_loads(self):
        from workflow.loader import WorkflowLoader

        path = PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml"
        definition = WorkflowLoader().load(path)

        self.assertEqual(definition["start"], "step_hunt")
        self.assertEqual(definition["steps"]["step_hunt"]["type"], "pet_hunt")
        # 所有跳转目标都必须存在（loader 只告警，这里硬性校验）
        step_names = set(definition["steps"])
        for field in ("next", "on_timeout", "on_error", "on_absent"):
            target = definition["steps"]["step_hunt"].get(field)
            if target:
                self.assertIn(target, step_names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
