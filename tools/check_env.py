"""发布包自检：依赖能不能导入、显卡可不可用、模型和工作流在不在。

不做任何键鼠操作，也不启动引擎，可以放心双击运行。

    python tools/check_env.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_MODULES = (
    "yaml",
    "numpy",
    "cv2",
    "win32gui",
    "PIL.Image",
    "torch",
    "torchvision",
    "ultralytics",
    "mcp",
)

REQUIRED_FILES = (
    "config/global.yaml",
    "config/global_hunt.yaml",
    "config/workflows/catch_pet.yaml",
    "config/workflows/enter_game.yaml",
    "submodules/RocoKingdom-Clicker/InterceptionCore.py",
    "submodules/RocoKingdom-Spotting/pipeline.py",
    "submodules/RocoKingdom-Spotting/config.json",
    "submodules/RocoKingdom-Spotting/models/best.pt",
    "submodules/RocoKingdom-Spotting/labels/enter_world.png",
)


def main() -> int:
    failed = False

    print(f"Python      : {sys.version.split()[0]} ({sys.executable})")

    for name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            print(f"[缺失] {name}: {exc}")
            failed = True
            continue
        version = getattr(module, "__version__", "")
        print(f"[ OK ] {name}{(' ' + str(version)) if version else ''}")

    try:
        import torch
        cuda = torch.cuda.is_available()
        print(f"显卡        : cuda_available={cuda}"
              + (f" ({torch.cuda.get_device_name(0)})" if cuda else " (CPU 推理)"))
    except Exception as exc:  # noqa: BLE001
        print(f"[缺失] torch 无法检查显卡: {exc}")
        failed = True

    for rel in REQUIRED_FILES:
        path = ROOT / rel
        if path.exists():
            size = path.stat().st_size
            print(f"[ OK ] {rel} ({size / 1e6:.2f} MB)" if size > 1e6 else f"[ OK ] {rel}")
        else:
            print(f"[缺失] {rel}")
            failed = True

    print()
    print("自检失败，请按上面的 [缺失] 项排查。" if failed else "自检通过，可以运行 start-hunt.bat。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())