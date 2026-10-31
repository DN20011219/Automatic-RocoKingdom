"""键名 → Windows 虚拟键码（vk_code）映射。

供 `press_key` / `press_combo` 工具使用：大模型传入可读键名，
由 :func:`resolve_key` 统一转换为 Interception 驱动所需的 vk_code。
详见设计文档 §4.4。
"""

from __future__ import annotations

from typing import Union

# 常用键名 → vk_code 映射（键名统一小写匹配）
KEY_MAP: dict[str, int] = {
    # 控制键
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "capslock": 0x14,
    # 导航键
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pgup": 0x21,
    "pageup": 0x21,
    "pgdn": 0x22,
    "pagedown": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    # 系统键
    "printscreen": 0x2C,
    "scrolllock": 0x91,
    "pause": 0x13,
    "win": 0x5B,
    # 标点符号（美式键盘布局）
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}

# 功能键 F1-F12
for _i in range(1, 13):
    KEY_MAP[f"f{_i}"] = 0x6F + _i


def resolve_key(key: Union[str, int]) -> int:
    """将键名或数字转换为 vk_code。

    支持：
    - 数字：原样视为 vk_code 返回；
    - 单字符字母 ``a``-``z`` / 数字 ``0``-``9``；
    - :data:`KEY_MAP` 中的常用键名（不区分大小写）。

    Args:
        key: 键名字符串或 vk_code 整数。

    Returns:
        Windows 虚拟键码。

    Raises:
        KeyError: 键名无法识别。
    """
    # 数字直接视为 vk_code
    if isinstance(key, int):
        return key
    if isinstance(key, float):
        return int(key)

    name = str(key).strip().lower()
    if not name:
        raise KeyError(f"空键名: {key!r}")

    # 单字符字母
    if len(name) == 1:
        if "a" <= name <= "z":
            return 0x41 + (ord(name) - ord("a"))
        if "0" <= name <= "9":
            return 0x30 + (ord(name) - ord("0"))

    # 常用键名（含 F 键与标点）
    if name in KEY_MAP:
        return KEY_MAP[name]

    raise KeyError(f"无法识别的键名: {key!r}")
