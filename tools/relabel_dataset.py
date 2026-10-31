"""修复 YOLO 数据集的类别编号错位。

问题背景：
    数据集原本是 4 类（cloversketch=0, gjy=1, qjk=2, sansan=3）。后来在 data.yaml
    里插入了 beise（新的 0）并追加了 yunxing（5），但**旧目录的标注文件没有重新
    编号**，于是训练出来的模型类别整体错位一格：

        图片目录        标注 id    data.yaml 里该 id 的名字    应该是
        beise             0        beise                       0  ✓
        cloversketch      0        beise      ← 和 beise 撞了   1  ✗
        gjy               1        cloversketch                2  ✗
        qjk               2        gjy                         3  ✗
        sansan            3        qjk                         4  ✗
        yunxing           5        yunxing                     5  ✓

    后果：class 4（sansan）一个训练样本都没有，模型永远不会输出 "sansan"；
    真正的 sansan 全部以 "qjk" 的名义输出。按名字匹配的工作流一只都打不到。

修复方式：
    本项目按目录组织标注（labels/<类别名>/xxx.txt），且实测**每个标注文件只含
    单一类别、无空文件**，所以可以按"文件所在目录名"无歧义地重写类别 id。

用法：
    python tools/relabel_dataset.py                  # dry-run，只打印将要改什么
    python tools/relabel_dataset.py --apply          # 真正写盘（自动生成 .bak 备份）
    python tools/relabel_dataset.py --dataset PATH   # 指定其他数据集

写盘后必须**重新训练**，旧模型不会自己变对。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

DEFAULT_DATASET = Path(r"C:\Users\17676\Desktop\开发中\RocoKingdom-Spotting\datasets\yolo_dataset")


def read_names(data_yaml: Path) -> list:
    """从 data.yaml 读类别名列表（不依赖 ultralytics，避免加载模型）。"""
    names = None
    for line in data_yaml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("names:"):
            body = stripped[len("names:"):].strip()
            # 支持 ['a', 'b'] 和 [a, b] 两种写法
            items = [p.strip().strip("'\"") for p in body.strip("[]").split(",")]
            names = [i for i in items if i]
    if not names:
        raise ValueError(f"没能从 {data_yaml} 解析出 names")
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="修复 YOLO 数据集类别编号错位")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="数据集根目录")
    parser.add_argument("--apply", action="store_true",
                        help="真正写盘（不加就是 dry-run，只打印不改）")
    parser.add_argument("--no-backup", action="store_true",
                        help="写盘时不生成 .bak 备份（默认会备份）")
    args = parser.parse_args()

    dataset = Path(args.dataset)
    labels_dir = dataset / "labels"
    data_yaml = dataset / "data.yaml"

    if not labels_dir.is_dir() or not data_yaml.exists():
        print(f"数据集结构不对：找不到 {labels_dir} 或 {data_yaml}")
        return 1

    names = read_names(data_yaml)
    print(f"data.yaml 类别表: {names}\n")
    name_to_id = {name: idx for idx, name in enumerate(names)}

    # 按目录统计当前 id → 目标 id
    plan = {}          # 目录名 -> (当前 id 集合, 目标 id)
    files_by_dir = {}
    for label_file in sorted(labels_dir.rglob("*.txt")):
        folder = label_file.parent.name
        files_by_dir.setdefault(folder, []).append(label_file)
        ids = set()
        for line in label_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 5:
                ids.add(parts[0])
        current = plan.get(folder, (set(), None))[0]
        plan[folder] = (current | ids, name_to_id.get(folder))

    print(f"{'目录':<16}{'当前 id':<12}{'目标 id':<10}{'文件数':<8}状态")
    print("-" * 62)
    to_fix = []
    for folder in sorted(plan):
        current_ids, target_id = plan[folder]
        count = len(files_by_dir[folder])
        if target_id is None:
            print(f"{folder:<16}{','.join(sorted(current_ids)) or '-':<12}"
                  f"{'?':<10}{count:<8}✗ 目录名不在 data.yaml 的 names 里，跳过")
            continue
        current_str = ",".join(sorted(current_ids)) or "-"
        if current_ids == {str(target_id)}:
            print(f"{folder:<16}{current_str:<12}{target_id:<10}{count:<8}✓ 已正确")
        elif len(current_ids) > 1:
            print(f"{folder:<16}{current_str:<12}{target_id:<10}{count:<8}"
                  f"✗ 该目录混了多个 id，不敢自动改，请人工确认")
        else:
            print(f"{folder:<16}{current_str:<12}{target_id:<10}{count:<8}→ 需要重编号")
            to_fix.append((folder, target_id))

    if not to_fix:
        print("\n没有需要修改的目录。")
        return 0

    total_files = sum(len(files_by_dir[f]) for f, _ in to_fix)
    print(f"\n待修改：{len(to_fix)} 个目录，共 {total_files} 个标注文件")

    if not args.apply:
        print("\n[dry-run] 没有写任何文件。确认无误后加 --apply 执行。")
        return 0

    changed_boxes = 0
    for folder, target_id in to_fix:
        for label_file in files_by_dir[folder]:
            if not args.no_backup:
                backup = label_file.with_suffix(label_file.suffix + ".bak")
                if not backup.exists():
                    shutil.copy2(label_file, backup)

            lines = label_file.read_text(encoding="utf-8").splitlines()
            new_lines = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 5:
                    if parts[0] != str(target_id):
                        changed_boxes += 1
                    parts[0] = str(target_id)
                    new_lines.append(" ".join(parts))
                elif line.strip():
                    new_lines.append(line)
            label_file.write_text("\n".join(new_lines) + ("\n" if new_lines else ""),
                                  encoding="utf-8")

    print(f"已重写 {total_files} 个文件、{changed_boxes} 个框的类别 id"
          + ("" if args.no_backup else "（原文件备份为 *.txt.bak）"))
    print("\n下一步：重新训练模型。训练完把新的 best.pt 同步到")
    print("  submodules/RocoKingdom-Spotting/models/best.pt")
    print("然后验证模型对 sansan 目录的图片输出的名字确实是 'sansan'。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
