"""验证 YOLO 模型的类别名是否和标注/目录对得上。

为什么需要它：
    在 data.yaml 里插入新类别后，旧目录的标注 id 不会自动重编号，训练出的模型
    类别名会整体错位一格。2026-10-05 就踩过：`sansan` 目录的标注仍是 id 3，
    而 data.yaml 里 3=qjk、4=sansan，结果模型的 sansan 类**零训练样本**，
    永远不输出 "sansan"，真 sansan 全以 "qjk" 名义输出 —— 按名字匹配的工作流
    一只都打不到，而且从日志上完全看不出原因（只是"一直没发现目标"）。

    这个工具做三件事：
      1. 打印模型的类别表，和 data.yaml 对比
      2. 统计每个类别的**训练框数量**，零样本的类直接标红
      3. 拿各目录的真实图片跑推理，确认输出的名字 == 目录名

用法：
    python tools/verify_model.py
    python tools/verify_model.py --target pipaniao      # 额外确认要抓的类别可用
    python tools/verify_model.py --model PATH --samples 5
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import cv2
import numpy as np

DEFAULT_MODEL = PROJECT_ROOT / "submodules" / "RocoKingdom-Spotting" / "models" / "best.pt"
DEFAULT_DATASET = Path(
    r"C:\Users\17676\Desktop\开发中\RocoKingdom-Spotting\datasets\yolo_dataset")


def imread_unicode(path: Path):
    """cv2.imread 处理不了中文路径，用 fromfile + imdecode 绕开。"""
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def read_data_yaml_names(data_yaml: Path):
    if not data_yaml.exists():
        return None
    for line in data_yaml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("names:"):
            items = [p.strip().strip("'\"")
                     for p in stripped[len("names:"):].strip().strip("[]").split(",")]
            return [i for i in items if i]
    return None


def count_label_classes(labels_dir: Path) -> collections.Counter:
    """统计标注文件里每个 class id 出现的框数。"""
    counter = collections.Counter()
    if not labels_dir.is_dir():
        return counter
    for label_file in labels_dir.rglob("*.txt"):
        for line in label_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 5:
                counter[parts[0]] += 1
    return counter


def main() -> int:
    parser = argparse.ArgumentParser(description="验证模型类别名与标注/目录是否一致")
    parser.add_argument("--model", default=str(DEFAULT_MODEL), help="模型路径")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="数据集根目录")
    parser.add_argument("--target", default=None,
                        help="额外确认这个类别名可用（工作流 target_pet 要填的值）")
    parser.add_argument("--samples", type=int, default=3, help="每个目录抽几张图推理")
    parser.add_argument("--conf", type=float, default=0.4, help="推理置信度阈值")
    args = parser.parse_args()

    model_path = Path(args.model)
    dataset = Path(args.dataset)
    if not model_path.exists():
        print(f"模型不存在: {model_path}")
        return 1

    from ultralytics import YOLO

    model = YOLO(str(model_path))
    names = model.names
    print("=" * 74)
    print(f"模型: {model_path}")
    print(f"类别表: {names}")

    problems = []

    # ---- 1. 和 data.yaml 对比 ----
    yaml_names = read_data_yaml_names(dataset / "data.yaml")
    print("\n--- 1. 与 data.yaml 的类别表对比 ---")
    if yaml_names is None:
        print(f"  找不到 {dataset / 'data.yaml'}，跳过")
    elif list(yaml_names) == [names[i] for i in sorted(names)]:
        print("  ✓ 一致")
    else:
        print(f"  ✗ 不一致（模型是旧的，data.yaml 是即将训练用的）")
        print(f"    data.yaml: {yaml_names}")
        print(f"    模型:      {[names[i] for i in sorted(names)]}")
        problems.append("模型类别表与 data.yaml 不一致（若正在重训，以 data.yaml 为准）")

    # 判断"标注 id 该是多少"必须以 data.yaml 为准 —— 它决定正在训练/即将训练的模型。
    # 拿旧模型的类别表去比会漏报：插入新类别后旧模型整体错位一格，
    # 用旧表对比反而处处"✓"，把真正的编号错误掩盖掉。
    authoritative = yaml_names or [names[i] for i in sorted(names)]
    expected_id = {n: i for i, n in enumerate(authoritative)}
    source = "data.yaml" if yaml_names else "模型类别表（data.yaml 缺失）"
    print(f"\n  以下检查以 {source} 为准: {authoritative}")

    # ---- 2. 每个类别的训练框数量（零样本 = 该类学不到任何东西）----
    print("\n--- 2. 各类别的训练框数量（按标注里实际出现的 id）---")
    counts = count_label_classes(dataset / "labels")
    if not counts:
        print(f"  {dataset / 'labels'} 里没有标注，跳过")
    else:
        for idx, label in enumerate(authoritative):
            n = counts.get(str(idx), 0)
            flag = "✓" if n > 0 else "✗ 零训练样本！"
            print(f"  [{idx}] {label:<16} {n:>5} 框   {flag}")
            if n == 0:
                problems.append(f"类别 '{label}'(id {idx}) 零训练样本，模型不可能输出它")
        extra = sorted(set(counts) - {str(i) for i in range(len(authoritative))}, key=int)
        if extra:
            print(f"  ⚠ 标注里还有超出类别表范围的 id: {extra}")
            problems.append(f"标注含越界 class id {extra}")

        # 目录名 vs 该目录标注实际用的 id
        print(f"\n--- 3. 各目录标注用的 class id 是否等于目录名在 {source} 里的 id ---")
        for sub in sorted(p.name for p in (dataset / "labels").iterdir() if p.is_dir()):
            ids = collections.Counter()
            for f in (dataset / "labels" / sub).rglob("*.txt"):
                for line in f.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) >= 5:
                        ids[parts[0]] += 1
            want = expected_id.get(sub)
            got = sorted(ids, key=lambda x: int(x))
            if want is None:
                print(f"  {sub:<16} 目录名不在类别表里（id 用了 {','.join(got)}）")
                problems.append(f"目录 '{sub}' 不在类别表里")
            elif got == [str(want)]:
                print(f"  {sub:<16} id {','.join(got)} ✓")
            else:
                actual_names = ", ".join(
                    authoritative[int(g)] for g in got
                    if g.isdigit() and int(g) < len(authoritative))
                print(f"  {sub:<16} id {','.join(got)} ✗ 应该是 {want}"
                      f"（这些框会被当成 '{actual_names}' 训练）")
                problems.append(
                    f"目录 '{sub}' 的标注用了 id {','.join(got)}，应为 {want} —— "
                    f"跑 tools/relabel_dataset.py --apply 修复后重训")

    # ---- 4. 真实图片推理，看输出的名字对不对 ----
    print(f"\n--- 4. 各目录抽 {args.samples} 张图推理，看输出的类别名 ---")
    images_root = dataset / "images"
    if not images_root.is_dir():
        print(f"  找不到 {images_root}，跳过")
    else:
        for sub in sorted(p.name for p in images_root.iterdir() if p.is_dir()):
            imgs = sorted((images_root / sub).glob("*.png"))[: args.samples]
            if not imgs:
                imgs = sorted((images_root / sub).glob("*.jpg"))[: args.samples]
            if not imgs:
                print(f"  {sub:<16} 没有图片")
                continue
            got = []
            for img in imgs:
                frame = imread_unicode(img)
                if frame is None:
                    continue
                r = model.predict(frame, imgsz=640, conf=args.conf, verbose=False)[0]
                if r.boxes is None or len(r.boxes) == 0:
                    got.append("(无检出)")
                else:
                    got.append(",".join(f"{names[int(b.cls[0])]}:{float(b.conf[0]):.2f}"
                                        for b in r.boxes))
            ok = all(sub in g for g in got)
            print(f"  {sub:<16} 期望 {sub:<14} 实际 {' | '.join(got)}   {'✓' if ok else '✗'}")
            if not ok:
                problems.append(f"目录 '{sub}' 的图片推理出的类别名不是 '{sub}'")

    # ---- 5. 目标类别可用性 ----
    if args.target:
        print(f"\n--- 5. 目标类别 '{args.target}' 可用性 ---")
        idx = [i for i, n in names.items() if n == args.target]
        if not idx:
            print(f"  ✗ 模型类别表里没有 '{args.target}'，工作流按这个名字永远匹配不到")
            print(f"    模型现有类别: {list(names.values())}")
            problems.append(f"target '{args.target}' 不在模型类别表里")
        else:
            n = counts.get(str(idx[0]), 0) if counts else None
            print(f"  ✓ 存在，id = {idx[0]}"
                  + (f"，训练框 {n} 个" if n is not None else ""))
            if n == 0:
                problems.append(f"target '{args.target}' 零训练样本")

    print("\n" + "=" * 74)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("全部检查通过：模型类别名与标注、目录、data.yaml 一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
