"""转视角标定工具 —— 实测「鼠标相对位移 → 画面实际移动」的真实关系。

为什么需要它：
    pet_hunt 节点的瞄准是 `位移 = (目标 − 客户区中心) / 增益`。这个公式成立的
    前提是「增益是个常数」，但下面任何一条都会让它不成立：
      - 游戏根本不响应相对位移（相机没动）→ 增益无从谈起
      - Windows「提高指针精确度」把相对位移非线性放大 → 增益随幅度变化
      - 相机有平滑/惯性 → 立刻测量会读到半截位移
      - 垂直轴被反转 → 符号错，越瞄越偏
    靠猜是猜不出来的，这个脚本直接量出来。

原理：
    截帧 A → 发一个已知的鼠标相对位移 → 等相机停稳 → 截帧 B，
    用 ORB 特征匹配 + RANSAC 单应矩阵算出**画面中心点**移动了多少像素。
    增益 = 画面位移 / 鼠标位移（带符号，符号反了说明方向与预期相反）。

    注意不要用相位相关（cv2.phaseCorrelate）：它只能测出不超过 ROI 一半的
    平移，而转视角动辄几百像素，超范围时它会返回接近 0 的结果，看起来像
    "相机没动" —— 这个坑实测踩过一次。而且相机偏航是透视变换不是纯平移，
    相位相关的模型本身也不匹配。

用法（游戏必须在**前台**，人物站着别动，周围尽量是静态场景）：
    python tools/calibrate_aim.py
    python tools/calibrate_aim.py --delta 40 --settle 0.8   # 转得太猛测不出时调小
    python tools/calibrate_aim.py --probe-absolute          # 额外测绝对定位是否有反应

注意：脚本会真实移动鼠标（转动游戏视角），跑之前把视角摆到一个你能接受的位置。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import cv2
import numpy as np

from action.adapter import ClickerAdapter
from perception.adapter import SpottingAdapter
from utils.config_loader import load_global_config

# 画面位移小于这个值就认为"相机没动"
STILL_EPSILON = 1.5
# 光流追踪点数的下限，低于它说明区域纹理太少，结论不可信
MIN_FLOW_POINTS = 25
# 逐点位移离散度（MAD）的绝对下限；实际阈值随位移幅度放宽（见 measure_shift）
MIN_FLOW_MAD = 6.0
# 测量区域（占宽/高的比例），按洛克王国：世界 1920x1080 的 HUD 布局手工圈定：
#   左侧 x<0.09 是宠物列表栏，右上是小地图/功能图标，右下是技能键，左下是按钮，
#   中心 x∈[0.36,0.50] 是静止的玩家角色，x∈[0.58,0.73] y≈0.52 是"F 观赏留影"提示框。
# 这些屏幕空间静态块会把测量带偏，必须排除。
# 主测量用左右两条纯世界区域（偏航时角色不动，中心必须排除）；
# 中间横带（含角色）仅作交叉验证 —— 俯仰时角色和世界一起动，那里信号最强。
H_STRIP_FRACTIONS = ((0.10, 0.34), (0.74, 0.93))
V_BAND_FRACTIONS = ((0.10, 0.93), (0.32, 0.88))   # (x 范围, y 范围)

_LK_PARAMS = dict(
    winSize=(21, 21),
    # 金字塔层数决定可追踪的最大位移（约 winSize * 2^maxLevel）。
    # maxLevel=3 只能追 ~168px，而标定 delta=100、比率 ~1.15 就会产生 ~115px 位移，
    # 已经贴近上限；实测 400px 位移直接追丢。放到 6 层（~1300px）留足余量。
    # 粗层只用于初始化，最终精修仍在第 0 层，所以精度不受影响。
    maxLevel=6,
    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
)


def _flow_displacements(gray_a, gray_b, x0, x1, y0, y1):
    """在指定区域内用 LK 光流追踪特征点，返回 Nx2 的逐点位移数组。

    为什么用光流而不是区域对齐：偏航（水平转视角）是**透视旋转**，画面不同
    位置的位移量和方向都不一样，套"整块平移"模型会锁到虚假的对角最优解
    （实测报出过"鼠标纯水平 +100 → (+199,-273)"）。逐点追踪再取中位数
    不需要任何全局运动模型，而且走动的精灵是少数派，会被中位数自动排除 ——
    所以**不需要场景里有静止的精灵**，纯静态背景就能标定。
    """
    region_a = gray_a[y0:y1, x0:x1]
    region_b = gray_b[y0:y1, x0:x1]
    if region_a.size == 0 or region_b.size == 0:
        return None

    points = cv2.goodFeaturesToTrack(region_a, maxCorners=800, qualityLevel=0.005,
                                     minDistance=6, blockSize=7)
    if points is None or len(points) < MIN_FLOW_POINTS:
        return None

    tracked, status, _ = cv2.calcOpticalFlowPyrLK(region_a, region_b, points, None,
                                                  **_LK_PARAMS)
    if tracked is None or status is None:
        return None

    good = status.ravel() == 1
    if int(good.sum()) < MIN_FLOW_POINTS:
        return None
    return (tracked[good] - points[good]).reshape(-1, 2)


def _flow_stats(displacements) -> Optional[dict]:
    """逐点位移 → 中位数 + 离散度（MAD）。"""
    if displacements is None or len(displacements) < MIN_FLOW_POINTS:
        return None
    dx = float(np.median(displacements[:, 0]))
    dy = float(np.median(displacements[:, 1]))
    mad = float(np.median(np.abs(displacements[:, 0] - dx))
                + np.median(np.abs(displacements[:, 1] - dy)))
    return {"dx": dx, "dy": dy, "n": int(len(displacements)), "mad": mad}


def measure_shift(frame_a, frame_b) -> dict:
    """测量 frame_b 相对 frame_a 的画面位移（LK 光流 + 中位数）。

    主测量用**左右两条世界区域条带**：既排除中心静止的第三人称角色
    （偏航时它不动，会把任何覆盖中心的对齐锁死在 0），也排除四周的屏幕空间
    HUD（宠物栏/小地图/技能键）。两条带各算一次，取**离散度更小**的那条 ——
    被走动精灵或特效污染的那条 MAD 会明显更大，自动被排除。

    另外用**中间横带**（含角色，俯仰时角色与世界一起动）做交叉验证，
    结果放在 band_dx/band_dy。

    Returns:
        dict: dx, dy、n（追踪点数）、mad（离散度）、reliable、strip、
              band_dx/band_dy/band_n、diff（全帧平均帧差，用于识别截图冻结）
    """
    gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)
    h, w = gray_a.shape

    (vx0f, vx1f), (vy0f, vy1f) = V_BAND_FRACTIONS
    vx0, vx1 = int(w * vx0f), int(w * vx1f)
    vy0, vy1 = int(h * vy0f), int(h * vy1f)

    best = None
    for f0, f1 in H_STRIP_FRACTIONS:
        x0, x1 = int(w * f0), int(w * f1)
        stats = _flow_stats(_flow_displacements(gray_a, gray_b, x0, x1, vy0, vy1))
        if stats is None:
            continue
        if best is None or stats["mad"] < best["mad"]:
            stats["strip"] = f"{x0}-{x1}"
            best = stats

    band = _flow_stats(_flow_displacements(gray_a, gray_b, vx0, vx1, vy0, vy1))

    diff = float(np.mean(cv2.absdiff(gray_a, gray_b)))
    if best is None:
        return {"dx": 0.0, "dy": 0.0, "n": 0, "mad": 0.0, "reliable": False,
                "strip": None, "band_dx": 0.0, "band_dy": 0.0,
                "band_n": band["n"] if band else 0, "diff": diff}

    magnitude = math.hypot(best["dx"], best["dy"])
    # 位移越大，透视带来的区域内位移差异也越大，所以离散度阈值随幅度放宽
    reliable = best["n"] >= MIN_FLOW_POINTS and best["mad"] <= max(MIN_FLOW_MAD, 0.35 * magnitude)
    return {
        "dx": best["dx"] if reliable else 0.0,
        "dy": best["dy"] if reliable else 0.0,
        "raw_dx": best["dx"], "raw_dy": best["dy"],
        "n": best["n"], "mad": best["mad"], "reliable": reliable,
        "strip": best["strip"], "band": f"{vx0}-{vx1},{vy0}-{vy1}",
        "band_dx": band["dx"] if band else 0.0,
        "band_dy": band["dy"] if band else 0.0,
        "band_n": band["n"] if band else 0,
        "diff": diff,
    }


def format_shift(m: dict) -> str:
    return (f"画面({m['dx']:+7.1f},{m['dy']:+7.1f})  "
            f"追踪 {m['n']} 点 MAD {m['mad']:.1f}  "
            f"横带校验({m['band_dx']:+.0f},{m['band_dy']:+.0f})/{m['band_n']}点  "
            f"帧差 {m['diff']:.1f}")


def summarize_axis(probes, key: str):
    """对同一轴的多次探测取**带符号中位数**，并挑出符号相反的离群点。

    草地这类重复纹理偶尔会让对齐落到混叠的假最优上（实测出现过一次
    鼠标 -100 测出画面 -230，符号和量级都错），中位数 + 符号一致性
    能把这种单点离群挡掉，不让它污染最终建议值。

    Returns:
        (中位数或 None, 离群值列表)
    """
    values = [r[key] for r in probes if r and r.get(key) is not None]
    if not values:
        return None, []
    med = float(np.median(values))
    outliers = [v for v in values if abs(v) > 1e-9 and np.sign(v) != np.sign(med)]
    return med, outliers


def wait_until_still(perception, settle: float, timeout: float = 2.0):
    """等相机停稳：连续两帧几乎不动才算稳，否则一直读到超时。

    游戏相机普遍有平滑插值，发完位移立刻测量会读到半截，增益就被低估了。
    """
    previous = perception.capture_frame()
    deadline = time.time() + timeout
    last = {"dx": 0.0, "dy": 0.0}

    while time.time() < deadline:
        time.sleep(settle / 3.0)
        current = perception.capture_frame()
        if current is None or previous is None:
            return None, last
        last = measure_shift(previous, current)
        if abs(last["dx"]) < STILL_EPSILON and abs(last["dy"]) < STILL_EPSILON:
            return current, last
        previous = current

    return previous, last


def frame_checksum(frame) -> str:
    """帧内容指纹，用来判断两次截图是不是同一张画面。"""
    return hashlib.md5(frame.tobytes()).hexdigest()[:12]


def save_frame(frame, path: Path) -> None:
    """保存帧到磁盘（imwrite 处理不了中文路径，用 imencode + tofile）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", frame)[1].tofile(str(path))


def run_probe(label, mouse_dx, mouse_dy, perception, clicker, settle,
              expect_x, expect_y, save_dir=None, settings=None):
    """执行一次「发位移 → 测画面移动」的探测。"""
    before = perception.capture_frame()
    if before is None:
        print(f"  [{label}] 截不到画面（游戏不在前台？）")
        return None

    t_move = time.time()
    if not send_move(clicker, mouse_dx, mouse_dy, settings or {}):
        print(f"  [{label}] 位移发送失败")
        return None

    after, residual = wait_until_still(perception, settle)
    if after is None:
        print(f"  [{label}] 等待稳定时截图失败")
        return None

    m = measure_shift(before, after)
    dx, dy = m["dx"], m["dy"]
    gain_x = dx / mouse_dx if mouse_dx else None
    gain_y = dy / mouse_dy if mouse_dy else None

    still = abs(dx) < STILL_EPSILON and abs(dy) < STILL_EPSILON
    if still:
        verdict = "相机没动！"
    elif not m["reliable"]:
        verdict = "特征太少，测量不可信"
    elif ((expect_x is not None and gain_x is not None and gain_x * expect_x < 0)
          or (expect_y is not None and gain_y is not None and gain_y * expect_y < 0)):
        verdict = "方向相反"
    else:
        verdict = "OK"

    def fmt(g):
        return "None" if g is None else f"{g:+.3f}"

    print(f"  [{label}] 鼠标({mouse_dx:+d},{mouse_dy:+d}) → {format_shift(m)}  "
          f"增益 x={fmt(gain_x)} y={fmt(gain_y)}  "
          f"残余={abs(residual['dx']):.1f}/{abs(residual['dy']):.1f}  {verdict}")

    # 帧差为 0 时"相机没动"和"截图没更新"长得一模一样，必须把证据打出来：
    # 校验和相同 = 两帧逐字节一致；同一对象 = 拿到了同一块内存
    same_object = before is after
    print(f"      校验和 {frame_checksum(before)} → {frame_checksum(after)}  "
          f"逐字节相同={m['diff'] < 0.01}  同一对象={same_object}  "
          f"尺寸 {before.shape}→{after.shape}  耗时 {time.time() - t_move:.2f}s")
    if same_object:
        print("      ⚠ 两次截图返回了同一个对象 —— 测量无意义，这是截图层的 bug")

    if m["diff"] < 0.01:
        # 前后帧完全相同有两种截然相反的解释，当场复查一次就能分开：
        # 再截两帧（不动鼠标），若仍然相同 → 截图死了；若在变 → 相机真没动
        live = capture_still_live(perception)
        print(f"      → 原地复查（不动鼠标连截两帧）：{'截图仍在更新，是相机没动' if live else '截图已停止更新'}")

    if save_dir is not None:
        save_frame(before, save_dir / f"{label}_before.png")
        save_frame(after, save_dir / f"{label}_after.png")
        print(f"      已保存 {save_dir / f'{label}_before.png'} / _after.png")

    return {"dx": dx, "dy": dy, "gain_x": gain_x, "gain_y": gain_y,
            "reliable": m["reliable"], "still": still, "diff": m["diff"],
            "n": m["n"], "mad": m["mad"], "same_object": same_object}


TIME_SERIES_DELAYS = (0.05, 0.15, 0.3, 0.6, 1.2, 2.0)


def time_series_probe(label, mouse_dx, mouse_dy, perception, clicker,
                      hold_left=False, save_dir=None, settings=None):
    """发一次位移后按多个延迟截图，输出位移-时间曲线。

    用来区分两种"延迟截图测不到水平位移"的假说：
      - 曲线先大后归零 → **相机自动回正**：转了一下又自己转回去
      - 曲线一直很小但稳定非零 → 增益极小但持续存在
      - 全程为 0 → 该输入路径根本不转视角

    hold_left=True 时全程按住左键（验证"蓄力期间才能水平瞄准"）。
    """
    before = perception.capture_frame()
    if before is None:
        print(f"  [{label}] 截不到画面")
        return None

    if hold_left and not clicker.left_down():
        print(f"  [{label}] 左键按下失败")
        return None

    samples = []
    try:
        t0 = time.perf_counter()
        if not send_move(clicker, mouse_dx, mouse_dy, settings or {}):
            print(f"  [{label}] 位移发送失败")
            return None

        for delay in TIME_SERIES_DELAYS:
            while time.perf_counter() - t0 < delay:
                pass                      # 忙等：sleep 精度不够，时间轴必须准
            actual = time.perf_counter() - t0
            frame = perception.capture_frame()
            if frame is None:
                print(f"  t={actual:4.2f}s  截图失败")
                samples.append((actual, None))
                continue
            m = measure_shift(before, frame)
            samples.append((actual, m))
            tag = "" if m["reliable"] else "  （无平移解释）"
            print(f"  t={actual:4.2f}s  位移 ({m['dx']:+7.1f},{m['dy']:+7.1f})  "
                  f"追踪 {m['n']}点 MAD {m['mad']:.1f}  帧差 {m['diff']:5.1f}{tag}")
            if save_dir is not None:
                save_frame(frame, save_dir / f"{label}_t{actual:.2f}s.png")
    finally:
        if hold_left:
            clicker.left_up()

    reliable = [(t, m) for t, m in samples if m and m["reliable"]]
    if not reliable:
        print("  → 全程没有可信的平移：该输入路径不转视角，或位移太小")
        return samples

    peak_t, peak_m = max(reliable, key=lambda tm: abs(tm[1]["dx"]) + abs(tm[1]["dy"]))
    last_t, last_m = reliable[-1]
    peak = abs(peak_m["dx"]) + abs(peak_m["dy"])
    last = abs(last_m["dx"]) + abs(last_m["dy"])
    print(f"  → 峰值 {peak:.0f}px @ t={peak_t:.2f}s；末值 {last:.0f}px @ t={last_t:.2f}s")
    if peak > 10 and last < peak * 0.3:
        print("  ⇒ **相机自动回正**：位移出现后又归零。瞄准必须在移动后立刻丢球，不能等。")
    elif peak > 3:
        print("  ⇒ 位移持续存在：增益小但稳定，按峰值/鼠标位移填增益即可。")
    return samples


def detector_probe(label, mouse_dx, mouse_dy, perception, clicker, settle,
                   settings=None, min_score=0.4, save_dir=None):
    """用**检测框中心位移**测比率，而不是区域对齐。

    为什么需要这个模式：偏航（水平转视角）是**透视旋转**而不是平移 ——
    画面边缘的点位移大、中心的小，还带垂直分量。任何基于平移模型的区域对齐
    （相位相关 / 暴力搜索 / ECC）在这种帧对上都会锁到虚假的对角最优解，
    实测出现过"鼠标纯水平 +100 → 报出 (+199,-273)"这种荒谬结果。
    俯仰接近纯平移，所以垂直轴区域对齐是准的，水平轴不是。

    追踪一个**点特征**（精灵的检测框中心）就没有这个问题：不管相机怎么转，
    那个点的位移就是我们要的量。而且这正是节点瞄准时用的同一个信号，
    标定与执行天然一致（参考项目 RocoPilot 的 _calibrate_sensitivity 同理）。

    代价：精灵自己会走，带来随机噪声。用多次探测取中位数压制。
    """
    before = perception.capture_frame()
    if before is None:
        print(f"  [{label}] 截不到画面")
        return None

    dets_before = [d for d in perception.detect_frame() if d.score >= min_score]
    if not dets_before:
        print(f"  [{label}] 画面里没有检测到目标，无法用检测框法标定（需要精灵在画面里）")
        return None

    # 选最靠近画面中心的那只：位移后最不容易出画
    rect = perception.last_rect
    cx0 = rect[0] + rect[2] / 2.0
    cy0 = rect[1] + rect[3] / 2.0
    target = min(dets_before, key=lambda d: (d.cx - cx0) ** 2 + (d.cy - cy0) ** 2)
    before_pos = (target.cx, target.cy)

    t_move = time.time()
    if not send_move(clicker, mouse_dx, mouse_dy, settings or {}):
        print(f"  [{label}] 位移发送失败")
        return None

    # 等相机停稳（区域对齐法的 wait_until_still 在这里不适用：它按平移判稳，
    # 而画面里精灵一直在动，永远"不稳"）
    time.sleep(max(settle, 0.15))
    after = perception.capture_frame()
    if after is None:
        print(f"  [{label}] 位移后截图失败")
        return None

    dets_after = [d for d in perception.detect_frame() if d.score >= min_score]
    if not dets_after:
        print(f"  [{label}] 位移后目标丢失（可能转出画面了），本次作废")
        return None

    # 匹配同一只：取离"预期新位置"最近的检出
    matched = min(dets_after, key=lambda d: (d.cx - before_pos[0]) ** 2
                                            + (d.cy - before_pos[1]) ** 2)
    shift_x = matched.cx - before_pos[0]
    shift_y = matched.cy - before_pos[1]
    gain_x = shift_x / mouse_dx if mouse_dx else None
    gain_y = shift_y / mouse_dy if mouse_dy else None

    def fmt(g):
        return "None" if g is None else f"{g:+.3f}"

    print(f"  [{label}] 鼠标({mouse_dx:+d},{mouse_dy:+d}) → "
          f"{target.name} 中心 ({before_pos[0]:.0f},{before_pos[1]:.0f}) → "
          f"({matched.cx:.0f},{matched.cy:.0f})  位移({shift_x:+.0f},{shift_y:+.0f})  "
          f"比率 x={fmt(gain_x)} y={fmt(gain_y)}  "
          f"conf {target.score:.2f}→{matched.score:.2f}  耗时 {time.time() - t_move:.2f}s")

    if save_dir is not None:
        save_frame(before, save_dir / f"{label}_det_before.png")
        save_frame(after, save_dir / f"{label}_det_after.png")

    return {"dx": shift_x, "dy": shift_y, "gain_x": gain_x, "gain_y": gain_y,
            "reliable": True, "still": abs(shift_x) < 1 and abs(shift_y) < 1,
            "diff": 0.0, "n": 0, "mad": 0.0}


def read_aim_settings(workflow_path: Path) -> dict:
    """从工作流 YAML 读出 pet_hunt 节点的瞄准设置。

    这是本工具最重要的设计：**标定必须用和节点执行时完全相同的发法**。
    之前踩过坑 —— 工具用单事件测出比率，节点却把位移拆成 20~35 个微步执行，
    游戏对两者响应不等价，测的和跑的不是一回事，比率直接失效。
    读同一份配置就从结构上杜绝了这种不一致。
    """
    settings = {
        "path_planner": False, "path_steps": 20, "duration_ms": 350,
        "path_strategy": "fitts", "gain_x": -1.0, "gain_y": -1.0,
    }
    if not workflow_path.exists():
        print(f"  [警告] 工作流不存在: {workflow_path}，用默认单事件方式标定")
        return settings

    import yaml
    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
    variables = data.get("variables", {}) or {}

    step = None
    for candidate in (data.get("steps") or {}).values():
        if isinstance(candidate, dict) and candidate.get("type") == "pet_hunt":
            step = candidate
            break
    if step is None:
        print(f"  [警告] {workflow_path.name} 里没有 pet_hunt 节点，用默认单事件方式标定")
        return settings

    def resolve(key, default):
        value = step.get(key, default)
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            value = variables.get(value[2:-1], default)
        return default if value is None else value

    settings["path_planner"] = bool(resolve("aim_path_planner", False))
    settings["path_steps"] = int(resolve("aim_path_steps", 20))
    settings["duration_ms"] = int(resolve("aim_duration_ms", 350))
    settings["path_strategy"] = str(resolve("path_strategy", "fitts"))
    settings["gain_x"] = float(resolve("aim_gain_x", -1.0))
    settings["gain_y"] = float(resolve("aim_gain_y", -1.0))
    return settings


def send_move(clicker, dx: int, dy: int, settings: dict) -> bool:
    """按工作流配置的方式发一次位移 —— 与节点执行路径保持一致。"""
    if (settings or {}).get("path_planner"):
        return clicker.move_relative_planned(
            dx, dy,
            duration_ms=settings.get("duration_ms", 350),
            strategy=settings.get("path_strategy", "fitts"),
            num_steps=settings.get("path_steps", 20),
        )
    return clicker.move_relative(dx, dy, 0, steps=1)


def capture_still_live(perception, interval: float = 0.3) -> bool:
    """不动鼠标连截两帧，判断截图管线是否还在更新。

    返回 False 表示两帧逐字节相同 —— 截图拿到的是不再变化的表面，
    此时"相机没动"的结论不成立，因为根本看不到相机动没动。
    """
    first = perception.capture_frame()
    if first is None:
        return False
    time.sleep(interval)
    second = perception.capture_frame()
    if second is None:
        return False
    return frame_checksum(first) != frame_checksum(second)


def preflight_capture(perception, samples: int = 5, interval: float = 0.25) -> bool:
    """截图活性自检：**不动鼠标**连拍几帧，看画面是否在变化。

    这一步必须在发任何位移之前做，因为它能区分两种表现完全相同、
    但处理方式截然相反的故障：
      - 画面在变 → 截图正常，后面测出"相机没动"就是真的输入没生效
      - 画面全同 → 截图层拿到的是 DWM 里不再更新的表面（窗口被遮挡、
        游戏暂停渲染、或抓错了区域），此时任何测量结果都是垃圾
    """
    print(f"\n=== 截图活性自检（不动鼠标，连拍 {samples} 帧，间隔 {interval}s）===")
    frames = []
    for i in range(samples):
        frame = perception.capture_frame()
        if frame is None:
            print(f"  第 {i + 1} 帧: 截图失败（窗口不在前台？）")
            return False
        frames.append(frame)
        print(f"  第 {i + 1} 帧: 校验和 {frame_checksum(frame)}  尺寸 {frame.shape}")
        if i < samples - 1:
            time.sleep(interval)

    checksums = {frame_checksum(f) for f in frames}
    diffs = [float(np.mean(cv2.absdiff(frames[i], frames[i + 1])))
             for i in range(len(frames) - 1)]
    print(f"  不同画面数: {len(checksums)}/{samples}   相邻帧差: "
          f"{', '.join(f'{d:.2f}' for d in diffs)}")

    if len(checksums) == 1:
        print("  ✗ 所有帧逐字节完全相同 —— 截图没有更新。可能原因：")
        print("      - 游戏窗口被其他窗口遮挡（DWM 保留的是最后一张表面）")
        print("      - 游戏暂停渲染 / 卡在加载界面")
        print("      - 抓到的是错误区域（检查上面的截帧区域是否与游戏画面对得上）")
        print("    这种情况下后面的所有测量都无意义，请先解决截图问题。")
        return False

    print("  ✓ 截图在正常更新")
    return True


def wait_for_frame(perception, timeout: float = 30.0):
    """等一帧能截到的画面。

    前台检查开着（foreground_only），而确认提示是在终端里按的回车 —— 按完
    焦点还在终端上，必须留出时间让用户点回游戏窗口，否则第一次截图必然失败。
    """
    print(f"\n等待游戏窗口进入前台（{timeout:.0f}s 内点击游戏画面即可）...")
    deadline = time.time() + timeout
    warned = False

    while time.time() < deadline:
        frame = perception.capture_frame()
        if frame is not None:
            print("  已截到画面")
            return frame
        if not warned:
            print(f"  还截不到（窗口未找到: {perception.window_hwnd is None}，"
                  f"或不在前台）—— 请点击游戏窗口")
            warned = True
        time.sleep(0.5)

    return None


def main():
    parser = argparse.ArgumentParser(description="标定转视角：鼠标相对位移与画面移动的换算关系")
    parser.add_argument("--delta", type=int, default=60,
                        help="探测用的鼠标位移量（默认 60）。光流法只在画面位移"
                             "约 100px 以内可靠（实测 60px 完美、150px 开始误匹配），"
                             "而画面位移 = delta × 比率，所以比率 ~1.15 时 60 对应 ~69px，正好。"
                             "超过会被 MAD 判为不可信而不是给出错数字")
    parser.add_argument("--settle", type=float, default=0.5, help="每次位移后的等待秒数（默认 0.5）")
    parser.add_argument("--config", default=None, help="全局配置路径（默认 config/global.yaml）")
    parser.add_argument("--probe-absolute", action="store_true",
                        help="额外探测绝对定位（move_to）是否能让相机移动")
    parser.add_argument("--save-frames", action="store_true",
                        help="把每次探测的前后两帧存到 logs/calib_frames/，用于人工核对")
    parser.add_argument("--time-series", action="store_true",
                        help="水平轴时间序列探测：区分「相机自动回正」和「增益极小但持续」")
    parser.add_argument("--during-hold", action="store_true",
                        help="额外探测：按住左键蓄力期间发水平位移，验证瞄准是否必须在蓄力时做")
    parser.add_argument("--yes", action="store_true", help="跳过确认提示")
    parser.add_argument("--quiet", action="store_true", help="不打印适配器的 INFO 日志")
    parser.add_argument("--workflow", default=None,
                        help="pet_hunt 工作流路径（默认 config/workflows/catch_pet.yaml）。"
                             "工具会读它的 aim_path_planner / aim_path_steps / aim_duration_ms / "
                             "path_strategy，用和节点完全相同的方式发位移，"
                             "避免「单事件标定、微步执行」这种测跑不一致")
    parser.add_argument("--use-detector", action="store_true",
                        help="用 YOLO 检测框中心的位移测比率（需要画面里有精灵）。"
                             "水平轴必须用这个：偏航是透视旋转，区域对齐会给出虚假的对角位移")
    parser.add_argument("--repeat", type=int, default=3,
                        help="配合 --use-detector：每个方向重复几次取中位数，压制精灵自行走动的噪声")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config) if args.config else PROJECT_ROOT / "config" / "global.yaml"
    cfg = load_global_config(config_path)
    spotting_cfg = dict(cfg.get("spotting", {}))
    spotting_cfg["show_overlay"] = False       # 覆盖层会画到画面上，污染测量
    if args.use_detector:
        # 检测框法需要 YOLO，保留子模块 config.json 里的检测器
        spotting_cfg.pop("detectors", None)
    else:
        # 区域对齐法只需要截图 —— 关掉全部检测器，省掉模型加载和 GPU 初始化。
        # 注意只 pop 主框架的 detectors 是没用的，子模块 config.json 里的照样会加载，
        # 必须用覆盖机制显式关掉
        spotting_cfg["detectors"] = {
            "yolo_pets": {"enabled": False},
            "flat_labels": {"enabled": False},
        }

    perception = SpottingAdapter(spotting_cfg)
    try:
        perception.setup()                     # 返回 None；子模块导入失败会抛 ImportError
    except ImportError as e:
        print(f"\nSpotting 子模块导入失败: {e}")
        print("请确认子模块已克隆: git submodule update --init")
        return 1

    clicker = ClickerAdapter()
    clicker.setup()                            # 同样返回 None，可用性看 is_available
    if not clicker.is_available:
        print("\n动作层不可用：无法发送鼠标位移，标定没法进行")
        perception.cleanup()
        return 1

    workflow_path = (Path(args.workflow) if args.workflow
                     else PROJECT_ROOT / "config" / "workflows" / "catch_pet.yaml")
    settings = read_aim_settings(workflow_path)

    print(f"\n窗口: {perception.window_hwnd or '未找到'}  截帧区域: {perception.last_rect}")
    print(f"鼠标后端: {'Interception 驱动' if clicker._interception_available else 'SendInput 用户模式'}")
    print(f"位移量: {args.delta}px   等待: {args.settle}s")
    print(f"发法（读自 {workflow_path.name}）: "
          + (f"PathPlanner 拟人轨迹 {settings['path_steps']} 步 / "
             f"{settings['duration_ms']}ms / {settings['path_strategy']}"
             if settings["path_planner"] else "单事件（steps=1）"))
    print(f"当前配置里的比率: x={settings['gain_x']:+.4f}  y={settings['gain_y']:+.4f}\n")

    if not args.yes:
        print("即将真实移动鼠标转动游戏视角。请确保：")
        print("  1. 游戏窗口在前台（本工具需要截图）")
        print("  2. 人物站着不动，周围是静态场景（避免草地/水面动画干扰）")
        print("  3. 视角位置你能接受被转走")
        try:
            input("回车开始，Ctrl+C 取消...")
        except (KeyboardInterrupt, EOFError):
            print("\n已取消")
            clicker.cleanup()
            perception.cleanup()
            return 0

    probe = wait_for_frame(perception)
    if probe is None:
        print("\n超时：始终截不到游戏画面。请确认")
        print(f"  - 游戏窗口标题包含 config 里的 window_keyword: {spotting_cfg.get('window_keyword')!r}")
        print("  - 游戏处于前台（foreground_only=true 时后台窗口截不到）")
        clicker.cleanup()
        perception.cleanup()
        return 1

    # 必须先确认截图在更新，否则"相机没动"和"截图冻结"分不开，结论会完全相反
    if not preflight_capture(perception):
        clicker.cleanup()
        perception.cleanup()
        return 1

    save_dir = (PROJECT_ROOT / "logs" / "calib_frames") if args.save_frames else None
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n前后帧将保存到: {save_dir}")

    print("\n=== 静止基线（不发任何位移，只测画面自身抖动）===")
    time.sleep(args.settle)
    second = perception.capture_frame()
    if second is None:
        print("截不到画面")
        return 1
    bm = measure_shift(probe, second)
    print(f"  自发漂移 ({bm['dx']:+.1f},{bm['dy']:+.1f})  "
          f"追踪 {bm['n']}点 MAD {bm['mad']:.1f}  帧差 {bm['diff']:.2f}")

    if bm["diff"] < 0.01:
        print("  ⚠ 两帧逐像素完全相同 —— 截图可能在返回缓存帧，测量无从谈起")
    noise = max(abs(bm["dx"]), abs(bm["dy"]))
    if bm["reliable"] and noise > 5:
        print(f"  ⚠ 场景本身在动（漂移 {noise:.1f}px），测量会被污染，换个静态场景再试")
    if not bm["reliable"]:
        print("  （静止基线找不到平移解释是正常的 —— 没动鼠标本来就不该有平移）")

    delta = args.delta
    small = max(20, delta // 4)

    if args.use_detector:
        print(f"\n=== 检测框法标定（每方向重复 {args.repeat} 次取中位数）===")
        probes = {}
        for label, mdx, mdy in (("右", delta, 0), ("左", -delta, 0),
                                ("下", 0, delta), ("上", 0, -delta)):
            samples = []
            for i in range(max(1, args.repeat)):
                r = detector_probe(f"{label}{i + 1}", mdx, mdy, perception, clicker,
                                   args.settle, settings=settings, save_dir=save_dir)
                if r is not None:
                    samples.append(r)
                time.sleep(0.2)
            if samples:
                gx = [s["gain_x"] for s in samples if s["gain_x"] is not None]
                gy = [s["gain_y"] for s in samples if s["gain_y"] is not None]
                probes[label] = {
                    "gain_x": float(np.median(gx)) if gx else None,
                    "gain_y": float(np.median(gy)) if gy else None,
                    "reliable": True, "still": False, "diff": 1.0,
                    "n": len(samples),
                    "spread_x": (max(gx) - min(gx)) if len(gx) > 1 else 0.0,
                    "spread_y": (max(gy) - min(gy)) if len(gy) > 1 else 0.0,
                }
                p = probes[label]
                gx_txt = "-" if p["gain_x"] is None else f"{p['gain_x']:+.3f}"
                gy_txt = "-" if p["gain_y"] is None else f"{p['gain_y']:+.3f}"
                print(f"  ⇒ [{label}] {p['n']} 次中位数: "
                      f"x={gx_txt} (极差 {p['spread_x']:.3f})  "
                      f"y={gy_txt} (极差 {p['spread_y']:.3f})")

        print("\n" + "=" * 68)
        print("结论（检测框法）")
        print("=" * 68)
        hx = [probes[k]["gain_x"] for k in ("右", "左")
              if k in probes and probes[k]["gain_x"] is not None]
        vy = [probes[k]["gain_y"] for k in ("下", "上")
              if k in probes and probes[k]["gain_y"] is not None]
        if hx:
            print(f"水平比率 {float(np.median(hx)):+.4f}  （样本 {', '.join(f'{g:+.3f}' for g in hx)}）")
            print(f"  → aim_gain_x 建议填 {float(np.median(hx)):+.4f}")
        else:
            print("水平比率：没有有效样本（画面里需要有一只精灵，且位移后不能转出画面）")
        if vy:
            print(f"垂直比率 {float(np.median(vy)):+.4f}  （样本 {', '.join(f'{g:+.3f}' for g in vy)}）")
            print(f"  → aim_gain_y 建议填 {float(np.median(vy)):+.4f}")
        else:
            print("垂直比率：没有有效样本")
        for k, p in probes.items():
            if max(p["spread_x"], p["spread_y"]) > 0.3:
                print(f"  ⚠ [{k}] 多次采样极差偏大（{max(p['spread_x'], p['spread_y']):.3f}）"
                      f"—— 精灵在走动，换个静止的精灵或加大 --repeat")
        print("\n把上面这段输出贴给我，或直接填进 catch_pet.yaml 的 aim_gain_x / aim_gain_y。")
        clicker.cleanup()
        perception.cleanup()
        return 0

    print("\n=== 水平轴 ===")
    # 标准 mouse-look 下增益两轴都应为负（内容逆着鼠标动），所以四次探测
    # 的期望符号都是 -1。之前给"左/上"传了 +1，导致把正确结果误报成"方向相反"。
    right = run_probe("右", delta, 0, perception, clicker, args.settle,
                      expect_x=-1, expect_y=None, save_dir=save_dir, settings=settings)
    left = run_probe("左", -delta, 0, perception, clicker, args.settle,
                     expect_x=-1, expect_y=None, save_dir=save_dir, settings=settings)

    print("\n=== 垂直轴 ===")
    down = run_probe("下", 0, delta, perception, clicker, args.settle,
                     expect_x=None, expect_y=-1, save_dir=save_dir, settings=settings)
    up = run_probe("上", 0, -delta, perception, clicker, args.settle,
                   expect_x=None, expect_y=-1, save_dir=save_dir, settings=settings)

    print("\n=== 线性度（小幅度 vs 大幅度，判断是否有鼠标加速）===")
    small_right = run_probe(f"右小{small}", small, 0, perception, clicker, args.settle,
                            expect_x=-1, expect_y=None, save_dir=save_dir, settings=settings)

    if args.time_series:
        print("\n=== 水平轴时间序列（区分自动回正 / 低增益持续）===")
        time_series_probe("时序", delta, 0, perception, clicker,
                          save_dir=save_dir, settings=settings)

    if args.during_hold:
        print("\n=== 按住左键蓄力期间的水平时间序列 ===")
        time_series_probe("蓄力中", delta, 0, perception, clicker,
                          hold_left=True, save_dir=save_dir, settings=settings)

    if args.probe_absolute:
        print("\n=== 绝对定位探测（游戏是否响应 move_to）===")
        before = perception.capture_frame()
        rect = perception.last_rect
        center_x = rect[0] + rect[2] // 2
        center_y = rect[1] + rect[3] // 2
        clicker.move_to(center_x + delta, center_y, duration_ms=200)
        after, _ = wait_until_still(perception, args.settle)
        if before is not None and after is not None:
            am = measure_shift(before, after)
            moved = abs(am["dx"]) > STILL_EPSILON or abs(am["dy"]) > STILL_EPSILON
            print(f"  move_to(+{delta},0) → {format_shift(am)}  "
                  f"{'相机有响应' if moved else '相机没响应'}")

    # ─── 结论 ───
    print("\n" + "=" * 68)
    print("结论")
    print("=" * 68)

    horizontal = [r for r in (right, left) if r and not r["still"] and r["reliable"]]
    vertical = [r for r in (down, up) if r and not r["still"] and r["reliable"]]
    measured = [r for r in (right, left, down, up, small_right) if r]

    if not horizontal and not vertical:
        frozen = [r for r in measured if r["diff"] < 0.01]
        changed = [r for r in measured if r["diff"] > 1.0]

        if len(frozen) == len(measured) and measured:
            print("所有探测的前后两帧都**逐字节完全相同** —— 发了位移但画面一点没变。")
            print("注意：截图活性自检是通过的（不动鼠标时画面在变），所以这不是截图冻结，")
            print("而是**鼠标位移根本没有作用到游戏相机上**。可能原因：")
            print("  - 游戏用 Raw Input / DirectInput 读鼠标，不吃注入的相对位移")
            print("  - 需要按住右键或中键拖动才转视角")
            print("  - 位移被游戏内的某个 UI 拦截（比如鼠标移到画面上唤出了光标）")
            print(f"  当前鼠标后端: {'Interception 驱动' if clicker._interception_available else 'SendInput 用户模式'}")
            print("  → 加 --probe-absolute 看绝对定位有没有反应，能进一步区分")
        elif changed:
            print("画面**确实变了**（帧差 {:.1f}~{:.1f}），但边缘条带找不到平移解释。".format(
                min(r["diff"] for r in changed), max(r["diff"] for r in changed)))
            print("常见原因，按可能性排序：")
            print("  - **相机自动回正**：转了一下又自己转回去，延迟截图时已经归位。")
            print("    用时间序列探测确认：")
            print("      python tools/calibrate_aim.py --time-series")
            print("  - 转得太猛，前后两帧几乎没有重叠区域 → 用更小的位移重测：")
            print(f"      python tools/calibrate_aim.py --delta {max(10, args.delta // 8)}")
            print("  - 变化来自走动的精灵/UI 弹窗而非相机 → 换静态场景、等精灵走开再测")
        else:
            print("画面变化很小且找不到一致的变换，无法判断。")
            print("  加 --save-frames 重跑，把 logs/calib_frames/ 里的前后帧发我看看。")
    else:
        gx, gx_out = summarize_axis(horizontal, "gain_x")
        gy, gy_out = summarize_axis(vertical, "gain_y")

        if gx is not None:
            print(f"水平增益 {gx:+.4f} 画面px/鼠标px（{len(horizontal)} 次探测的带符号中位数）")
            if gx_out:
                print(f"  ⚠ 剔除符号相反的离群样本 {', '.join(f'{v:+.3f}' for v in gx_out)}"
                      f" —— 草地这类重复纹理偶尔会让对齐落到混叠的假最优上")
            if gx > 0:
                print("  ⚠ 符号为正：内容跟着鼠标同向移动，与标准 mouse-look 相反")
            if small_right and small_right.get("gain_x") is not None:
                ratio = abs(small_right["gain_x"]) / abs(gx) if gx else 0.0
                if ratio < 0.7 or ratio > 1.4:
                    print(f"  ⚠ 小幅度增益是大幅度的 {ratio:.2f} 倍 —— 存在鼠标加速，"
                          f"固定增益不可能对准；建议关掉 Windows「提高指针精确度」后重测")
                else:
                    print(f"  线性度 OK（小/大增益比 {ratio:.2f}）")
            print(f"  → aim_gain_x 建议填 {gx:+.4f}")

        if gy is not None:
            print(f"垂直增益 {gy:+.4f} 画面px/鼠标px（{len(vertical)} 次探测的带符号中位数）")
            if gy_out:
                print(f"  ⚠ 剔除符号相反的离群样本 {', '.join(f'{v:+.3f}' for v in gy_out)}")
            if gx is not None and abs(abs(gy) - abs(gx)) / max(abs(gx), 1e-6) > 0.25:
                print(f"  ⚠ 水平/垂直增益差 {abs(abs(gy) - abs(gx)) / max(abs(gx), 1e-6) * 100:.0f}%，"
                      f"必须按轴分别配置（aim_gain_x / aim_gain_y）")
            print(f"  → aim_gain_y 建议填 {gy:+.4f}")
        else:
            print("垂直轴没有可测响应 —— 可能相机俯仰被锁定，或垂直位移被游戏忽略")

    print("\n注意：对齐区域按本游戏 1920x1080 的 HUD 布局圈定（水平用左右两条世界区域、")
    print("垂直用中间横带）。换分辨率或 HUD 变化后若结果异常，改 H_STRIP_FRACTIONS /")
    print("V_BAND_FRACTIONS，或加 --save-frames 看保存的帧确认区域里有没有 UI。")
    print("\n把上面这段输出贴给我，我按实测值改配置/代码。")

    clicker.cleanup()
    perception.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
