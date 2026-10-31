"""构建「解压即用」的 Windows 便携发布包。

产出两个目录和对应的 zip：

    dist/RocoAutomation-lite-<日期>/   CPU 版（任何 64 位 Windows 都能跑）
    dist/RocoAutomation-full-<日期>/   CUDA 版（需要 NVIDIA 显卡 + 较新驱动）

两个包结构完全一样，只有 torch 的来源不同：

    RocoAutomation-<变体>/
      app/                        整个项目（保持 main.py + submodules 原结构）
      runtime/python310/          便携 Python + 已装好的依赖
      start-hunt.bat              自动抓精灵
      start-enter-game.bat        自动进入游戏
      start-mcp.bat               MCP 连接器（http://127.0.0.1:8766/mcp）
      install-driver.bat          安装 Interception 驱动（管理员 + 重启）
      check-env.bat               自检：依赖、显卡、模型、工作流
      README.txt                  使用说明

为什么不用 PyInstaller：项目里所有路径都是 `Path(__file__).parent / "submodules"`，
冻结成单文件后这套结构会失效。便携 Python + 原目录结构零改动、还能让用户自己改配置。

用法（在项目根目录）：

    python tools\\build_release.py                 # 两个包都出
    python tools\\build_release.py --variant lite  # 只出 CPU 版
    python tools\\build_release.py --variant full --no-zip

首次构建会下载 Python 嵌入式包、pip、以及 torch（CUDA 版约 2.5GB），
缓存在 build/release/_cache，二次构建会快很多。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_ROOT = PROJECT_ROOT / "build" / "release"
STAGE_ROOT = BUILD_ROOT / "stage"
CACHE_DIR = BUILD_ROOT / "_cache"
DIST_DIR = PROJECT_ROOT / "dist"

PY_VERSION = "3.10.11"
PY_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
)
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# 与开发机实测通过的版本保持一致；升级前先在本机跑一遍 tests。
COMMON_PACKAGES: Sequence[str] = (
    "pyyaml==6.0.3",
    "numpy==2.2.6",
    "opencv-python==5.0.0.93",
    "pywin32==312",
    "pillow==12.2.0",
    "mcp==1.29.0",
    "ultralytics==8.4.100",
)
TORCH_PACKAGES: Sequence[str] = ("torch==2.11.0", "torchvision==0.26.0")
TORCH_INDEX = {
    "lite": "https://download.pytorch.org/whl/cpu",
    "full": "https://download.pytorch.org/whl/cu128",
}

# 运行 torch / opencv 需要 MSVC 运行库；Python 嵌入式包只带了 vcruntime140.dll
MSVC_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")

EXCLUDE_DIR_NAMES = {
    ".git", ".qwen", "__pycache__", ".idea", ".vscode",
    "build", "dist", "logs", "datasets", "debug_frames",
    "tests", "venv", ".venv", "env",
}
EXCLUDE_FILE_NAMES = {
    "_test_diagnose.py", "build_release.py", ".gitmodules", ".gitignore",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

REQUIRED_FILES = (
    "main.py",
    "config/workflows/catch_pet.yaml",
    "config/workflows/enter_game.yaml",
    "config/global.yaml",
    "config/global_hunt.yaml",
    "submodules/RocoKingdom-Clicker/InterceptionCore.py",
    "submodules/RocoKingdom-Spotting/pipeline.py",
    "submodules/RocoKingdom-Spotting/models/best.pt",
    "submodules/RocoKingdom-Spotting/labels/enter_world.png",
    # 抓包功能可选，但既然打进了包就检查一下，缺了要明确报出来
    "submodules/Roco-Kingdom-Protocol-Parser/rkpp_live_tools.py",
    "mcp_server/__main__.py",
    "tools/calibrate_aim.py",
)


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------

def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(url: str, dest: Path) -> Path:
    """下载到缓存；已存在则直接复用（不校验，Python 官方包不带 checksum 清单）。"""
    if dest.exists() and dest.stat().st_size > 0:
        log(f"复用缓存: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    ensure_dir(dest.parent)
    log(f"下载 {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_report = 0.0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            now = time.time()
            if total and now - last_report > 2:
                log(f"  {done / 1e6:.1f}/{total / 1e6:.1f} MB")
                last_report = now
    tmp.replace(dest)
    log(f"下载完成: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def run(cmd: Sequence[str], cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None, check: bool = True) -> int:
    """执行子进程并把输出实时转发到控制台。"""
    log("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {proc.returncode}）: {cmd[0]}")
    return proc.returncode


def dir_size_mb(path: Path) -> float:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total / 1e6


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------
# 便携 Python
# ----------------------------------------------------------------------

def extract_python(py_zip: Path, runtime_dir: Path) -> Path:
    ensure_dir(runtime_dir)
    with zipfile.ZipFile(py_zip) as zf:
        zf.extractall(runtime_dir)

    py_exe = runtime_dir / "python.exe"
    if not py_exe.exists():
        raise SystemExit(f"嵌入式 Python 解压后找不到 python.exe: {runtime_dir}")

    # 嵌入式发行版默认禁用 site-packages；必须打开才能 import 安装的包
    pth = runtime_dir / f"python{PY_VERSION.split('.')[0]}{PY_VERSION.split('.')[1]}._pth"
    if not pth.exists():
        candidates = list(runtime_dir.glob("python*._pth"))
        if not candidates:
            raise SystemExit(f"找不到 ._pth 文件: {runtime_dir}")
        pth = candidates[0]

    lines = pth.read_text(encoding="utf-8").splitlines()
    cleaned: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped in ("#import site", "import site", "Lib\\site-packages", "Lib/site-packages"):
            continue
        cleaned.append(line)
    cleaned.extend(["Lib\\site-packages", "import site"])
    pth.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    log(f"已启用 site-packages: {pth.name}")

    # torch / opencv 依赖 MSVC 运行库，嵌入式包不带 msvcp140
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    copied = []
    for name in MSVC_DLLS:
        src = sys32 / name
        if src.exists():
            shutil.copy2(src, runtime_dir / name)
            copied.append(name)
    if copied:
        log(f"已内置 MSVC 运行库: {', '.join(copied)}")
    else:
        log("警告: 没找到 MSVC 运行库，目标机器可能需要安装 VC++ 2015-2022 运行库")

    return py_exe


def install_pip(py_exe: Path) -> None:
    get_pip = download(GET_PIP_URL, CACHE_DIR / "get-pip.py")
    env = _clean_env()
    run([py_exe, str(get_pip), "--no-warn-script-location"], env=env)
    run([py_exe, "-m", "pip", "--version"], env=env)


def install_packages(py_exe: Path, variant: str) -> None:
    env = _clean_env()
    pip_cache = ensure_dir(CACHE_DIR / "pip")
    common = [
        py_exe, "-m", "pip", "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--cache-dir", str(pip_cache),
    ]

    # torch 体积最大且必须来自专用 index，先单独装，后面的包就不会再动它
    run([*common, "--index-url", TORCH_INDEX[variant], *TORCH_PACKAGES], env=env)
    run([*common, "--index-url", "https://pypi.org/simple", *COMMON_PACKAGES], env=env)


def _clean_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def verify_runtime(py_exe: Path, variant: str) -> None:
    """装完立刻自检，避免把坏包发出去。"""
    code = (
        "import sys, torch, ultralytics, cv2, numpy, yaml, mcp, win32gui, PIL;"
        "print('python', sys.version.split()[0]);"
        "print('torch', torch.__version__, 'cuda_available', torch.cuda.is_available());"
        "print('ultralytics', ultralytics.__version__);"
        "print('cv2', cv2.__version__, 'numpy', numpy.__version__)"
    )
    env = _clean_env()
    result = subprocess.run(
        [str(py_exe), "-c", code],
        env=env, text=True, encoding="utf-8", errors="replace",
        capture_output=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
        raise SystemExit("运行时自检失败：依赖导入不完整")
    if variant == "full" and "cuda_available True" not in result.stdout:
        log("警告: CUDA 版自检时 torch.cuda.is_available() 为 False —— "
            "本机没显卡驱动是正常的，但发布前最好在有 N 卡的机器上验证一次")


# ----------------------------------------------------------------------
# 复制项目
# ----------------------------------------------------------------------

def _ignore(directory: str, names: List[str]) -> Iterable[str]:
    ignored = []
    for name in names:
        path = Path(directory) / name
        if path.is_dir() and name in EXCLUDE_DIR_NAMES:
            ignored.append(name)
        elif path.is_file() and (name in EXCLUDE_FILE_NAMES or path.suffix in EXCLUDE_SUFFIXES):
            ignored.append(name)
        elif name == ".git":          # 子模块里的 .git 是文件
            ignored.append(name)
    return ignored


def copy_project(app_dir: Path) -> None:
    log(f"复制项目到 {app_dir}")
    shutil.copytree(PROJECT_ROOT, app_dir, ignore=_ignore)
    ensure_dir(app_dir / "logs")


def check_required_files(app_dir: Path) -> None:
    missing = [name for name in REQUIRED_FILES if not (app_dir / name).exists()]
    if missing:
        raise SystemExit(
            "发布包缺少必需文件（模型被 gitignore，需从工作区拷贝）:\n  "
            + "\n  ".join(missing)
        )


# ----------------------------------------------------------------------
# 启动脚本与说明
# ----------------------------------------------------------------------

def _write_bat(path: Path, body: str) -> None:
    """批处理一律用 CRLF + ASCII 内容，避免中文代码页问题。"""
    path.write_text(body.replace("\n", "\r\n"), encoding="ascii")
    log(f"写入 {path.name}")


def write_launchers(stage: Path) -> None:
    _write_bat(stage / "start-hunt.bat", """@echo off
setlocal
cd /d "%~dp0app"
title RocoAutomation - auto hunt
echo Starting auto hunt. Press Ctrl+C to stop safely.
echo.
"%~dp0runtime\\python310\\python.exe" main.py -w config\\workflows\\catch_pet.yaml -c config\\global_hunt.yaml %*
echo.
echo Stopped. Press any key to close this window.
pause >nul
""")

    _write_bat(stage / "start-enter-game.bat", """@echo off
setlocal
cd /d "%~dp0app"
title RocoAutomation - enter game
"%~dp0runtime\\python310\\python.exe" main.py -w config\\workflows\\enter_game.yaml %*
echo.
pause >nul
""")

    _write_bat(stage / "start-mcp.bat", """@echo off
setlocal
cd /d "%~dp0app"
title RocoAutomation - MCP server
echo MCP endpoint: http://127.0.0.1:8766/mcp
"%~dp0runtime\\python310\\python.exe" tools\\launch_mcp.py --transport http --port 8766 %*
echo.
pause >nul
""")

    _write_bat(stage / "install-driver.bat", """@echo off
setlocal
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
echo Installing Interception keyboard/mouse driver...
"%~dp0app\\submodules\\RocoKingdom-Clicker\\third\\Interception\\command line installer\\install-interception.exe" /install
echo.
echo Done. REBOOT the computer before running start-hunt.bat.
pause
""")

    _write_bat(stage / "check-env.bat", """@echo off
setlocal
cd /d "%~dp0app"
title RocoAutomation - environment check
"%~dp0runtime\\python310\\python.exe" tools\\check_env.py %*
echo.
pause >nul
""")

    _write_bat(stage / "calibrate-aim.bat", """@echo off
setlocal
cd /d "%~dp0app"
title RocoAutomation - aim calibration
echo Put the game window in the foreground with a pet visible, then follow the prompts.
"%~dp0runtime\\python310\\python.exe" tools\\calibrate_aim.py --workflow config\\workflows\\catch_pet.yaml %*
echo.
pause >nul
""")

    _write_bat(stage / "verify-model.bat", """@echo off
setlocal
cd /d "%~dp0app"
title RocoAutomation - model check
"%~dp0runtime\\python310\\python.exe" tools\\verify_model.py --target pipaniao %*
echo.
pause >nul
""")


def write_readme(stage: Path, variant: str) -> None:
    variant_note = (
        "CPU 版：不依赖显卡，任何 64 位 Windows 都能跑，实测约 13fps。"
        if variant == "lite" else
        "CUDA 版（cu128）：需要 NVIDIA 显卡和较新驱动（建议 570 及以上），实测约 18fps。"
    )
    text = f"""Automatic-RocoKingdom 便携版（{variant}）

{variant_note}

一、解压后先做两件事

1) 安装键盘鼠标驱动（只做一次，需要管理员权限）

   右键 install-driver.bat → 以管理员身份运行
   装完必须重启电脑

   不装也能运行：脚本会自动退回 Windows 自带的 SendInput，
   但转视角的精度和一致性会差一些，建议还是装上。

2) 把游戏窗口调成“窗口化”或“无边框窗口”，进入游戏世界后保持前台

   脚本只认前台窗口（标题包含“洛克王国：世界”）。
   Alt+Tab 切走的瞬间它会自动停手，不会乱点别的窗口。

二、开始抓精灵

   双击 start-hunt.bat

   它会循环执行：识别目标 → 转视角瞄准 → 长按丢球 → 补回视角轴线 → 换方向找下一只。
   按 Ctrl+C 停止。

   想换目标或改次数，不必改配置文件，直接在命令行里加参数：

       start-hunt.bat --var target_pet=sansan --var max_catches=10

   想进游戏（登录界面/主界面 → 进入世界）：

       双击 start-enter-game.bat

三、目录说明

   app\\                       项目本体（改 config\\ 里的 YAML 即可调参）
   runtime\\python310\\         自带的 Python，不要单独删改
   start-hunt.bat              自动抓精灵
   start-enter-game.bat        自动进入游戏
   start-mcp.bat               MCP 连接器（给大模型接入口，默认只监听本机）
   install-driver.bat          安装 Interception 驱动
   check-env.bat               自检：依赖、显卡、模型、工作流
   calibrate-aim.bat           重新标定“鼠标位移 → 画面位移”比率
   verify-model.bat            检查模型类别名（抓不到目标时先跑这个）

   注意：整个文件夹可以整体移动，但不要只挪 app\\ 或 runtime\\ 里的东西，
   代码是按相对路径找 submodules 和配置的。

四、常见问题

1) 一只都抓不到
   - 先跑 verify-model.bat，确认输出里有 pipaniao 这个类别
   - 确认游戏窗口标题包含 config\\global.yaml 里的 spotting.window_keyword
   - 看画面上的绿色检测框有没有套住精灵：有框但打不中 = 瞄准参数问题；
     没框 = 模型或窗口问题
   - 空丢球多：在 start-hunt.bat 后面加 --var confirm_frames=2 --var pet_min_score=0.6

2) 球总是丢偏
   - 换过分辨率或改过游戏内鼠标灵敏度后，必须重新标定：
     双击 calibrate-aim.bat，按提示测出 aim_gain_x / aim_gain_y，
     填回 app\\config\\workflows\\catch_pet.yaml
   - 落点整体偏左/偏上：调 aim_offset_x / aim_offset_y（正值向右/向下）

3) 帧率太低 / 画面卡
   - 确认用的是 global_hunt.yaml（start-hunt.bat 已经带了 -c 参数）
   - CPU 版约 13fps，CUDA 版约 18fps；开了别的录屏/叠加层会更慢

4) 想抓别的精灵
   - start-hunt.bat --var target_pet=<类别名>
   - 类别名以模型里实际有的为准：verify-model.bat 会列出来

五、可选功能

MCP 连接器（给 Claude/Qwen 等客户端读取画面、下发操作）：
   双击 start-mcp.bat，端点 http://127.0.0.1:8766/mcp
   默认只监听 127.0.0.1，不对外开放；详细配置见 app\\config\\global.yaml 的 mcp 段。

战斗协议解析（RKPP，进阶，需要额外环境）：
   需要自己安装 Npcap，并用管理员权限的 PowerShell 在包根目录运行：
   runtime\\python310\\python.exe app\\submodules\\Roco-Kingdom-Protocol-Parser\\rkpp_live_tools.py opencode-server --iface "以太网" --port 8195 --relay-port 8765
   不启动时协议源会自动静默重试，不影响抓精灵。
"""
    (stage / "README.txt").write_text(text, encoding="utf-8-sig")
    log("写入 README.txt")


def write_build_info(stage: Path, variant: str, version: str) -> None:
    py_exe = stage / "runtime" / "python310" / "python.exe"
    lines = [
        f"variant: {variant}",
        f"version: {version}",
        f"built_at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"python: {PY_VERSION}",
        f"torch_index: {TORCH_INDEX[variant]}",
        f"torch_packages: {', '.join(TORCH_PACKAGES)}",
        f"packages: {', '.join(COMMON_PACKAGES)}",
        f"runtime_python: {py_exe}",
    ]
    (stage / "BUILD_INFO.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("写入 BUILD_INFO.txt")


# ----------------------------------------------------------------------
# 打包
# ----------------------------------------------------------------------

def clean_stage(stage: Path) -> None:
    """删掉运行时产生的缓存，避免把绝对路径和临时文件打进包。"""
    removed = 0
    for path in list(stage.rglob("__pycache__")):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    for path in list(stage.rglob("*.pyc")):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    for name in ("mcp_out.txt", "mcp_err.txt"):
        (stage / "app" / name).unlink(missing_ok=True)
    if removed:
        log(f"清理缓存: 删除 {removed} 个 __pycache__ / .pyc")


def zip_stage(stage: Path, variant: str, version: str) -> Path:
    ensure_dir(DIST_DIR)
    archive = DIST_DIR / f"{stage.name}.zip"
    if archive.exists():
        archive.unlink()
    clean_stage(stage)
    log(f"压缩 {stage.name}（{dir_size_mb(stage):.0f} MB，需要几分钟）")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage.parent))
    size_mb = archive.stat().st_size / 1e6
    digest = sha256_of(archive)
    (DIST_DIR / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    log(f"完成: {archive.name} ({size_mb:.0f} MB)")
    log(f"sha256: {digest}")
    return archive


def build(variant: str, version: str, py_zip_arg: Optional[Path], do_zip: bool) -> None:
    started = time.time()
    name = f"RocoAutomation-{variant}-{version}"
    stage = STAGE_ROOT / name
    if stage.exists():
        log(f"清理旧目录 {stage}")
        shutil.rmtree(stage)
    ensure_dir(stage)
    ensure_dir(CACHE_DIR)

    py_zip = py_zip_arg or CACHE_DIR / f"python-{PY_VERSION}-embed-amd64.zip"
    if not py_zip.exists():
        download(PY_EMBED_URL, py_zip)

    log(f"== 准备便携 Python ({variant}) ==")
    runtime_dir = stage / "runtime" / "python310"
    py_exe = extract_python(py_zip, runtime_dir)
    install_pip(py_exe)

    log(f"== 安装依赖 ({variant}) ==")
    install_packages(py_exe, variant)
    verify_runtime(py_exe, variant)

    log("== 复制项目 ==")
    app_dir = stage / "app"
    copy_project(app_dir)
    check_required_files(app_dir)

    log("== 生成启动脚本 ==")
    write_launchers(stage)
    write_readme(stage, variant)
    write_build_info(stage, variant, version)

    clean_stage(stage)
    log(f"包目录: {stage}  总大小 {dir_size_mb(stage):.0f} MB")
    if do_zip:
        zip_stage(stage, variant, version)
    log(f"完成 {variant}，耗时 {(time.time() - started) / 60:.1f} 分钟")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Automatic-RocoKingdom 便携发布包")
    parser.add_argument("--variant", choices=["lite", "full", "both"], default="both",
                        help="lite=CPU 版，full=CUDA 版，both=两个都出（默认）")
    parser.add_argument("--version", default=date.today().strftime("%Y%m%d"),
                        help="包名里的版本号（默认今天日期 YYYYMMDD）")
    parser.add_argument("--python-zip", type=Path, default=None,
                        help="本地已有的 python-3.10.11-embed-amd64.zip（离线构建用）")
    parser.add_argument("--no-zip", action="store_true",
                        help="只生成目录，不压缩成 zip（调试用）")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("发布包只能在 Windows 上构建（需要 Windows 版 Python 与 DLL）")

    variants = ["lite", "full"] if args.variant == "both" else [args.variant]
    for variant in variants:
        build(variant, args.version, args.python_zip, do_zip=not args.no_zip)
    return 0


if __name__ == "__main__":
    sys.exit(main())