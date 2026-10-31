"""便携包里的 MCP 启动器。

嵌入式 Python（python310._pth）是隔离模式，不会把当前目录加入 sys.path，
所以 `python -m mcp_server` 会报 No module named 'mcp_server'。
这个包装脚本显式把项目根目录加进去，再转交给 mcp_server 的 CLI。

开发环境（正常安装的 Python）仍然可以直接 `python -m mcp_server`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())