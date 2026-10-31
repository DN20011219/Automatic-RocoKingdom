"""安全层 — 操作准入检查与动作日志审计。

所有操作类工具执行前必须经过 :class:`SafetyGuard` 检查；
每次操作（无论成败）写入环形日志缓冲，供 ``get_action_log`` 审计。
详见设计文档 §6。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional

from mcp_server.config import SafetyConfig

# 动作日志环形缓冲容量
_LOG_CAPACITY = 200

# 参数快照中字符串字段的最大长度
_ARG_STR_LIMIT = 200


@dataclass
class ActionRecord:
    """单次操作记录。"""

    # 发生时间戳（time.time()）
    ts: float

    # 工具名（如 "click" / "execute_actions"）
    tool: str

    # 参数快照（长字段已截断）
    args: Dict[str, Any]

    # 是否成功
    ok: bool

    # 失败原因（成功时为 None）
    error: Optional[str]

    # 执行耗时（毫秒）
    duration_ms: float


class SafetyGuard:
    """操作安全守卫。

    职责：
    1. 操作总开关检查（``allow_actions``）；
    2. 动作序列长度上限检查（``max_sequence_len``）；
    3. 动作日志环形缓冲记录与查询。

    线程安全：内部使用锁保护日志缓冲。
    """

    def __init__(self, config: SafetyConfig) -> None:
        """初始化安全守卫。

        Args:
            config: 安全层配置。
        """
        self._config = config
        self._log: Deque[ActionRecord] = deque(maxlen=_LOG_CAPACITY)
        self._lock = threading.Lock()

    @property
    def allow_actions(self) -> bool:
        """操作类工具是否被允许。"""
        return self._config.allow_actions

    def check_action(self, tool: str) -> None:
        """检查单个操作是否被允许。

        Args:
            tool: 工具名（用于日志与错误信息）。

        Raises:
            BridgeError: 操作被安全策略拒绝（error_code=blocked_by_safety）。
        """
        from mcp_server.bridge import BridgeError

        if not self._config.allow_actions:
            raise BridgeError(
                "blocked_by_safety",
                f"操作 {tool} 被安全策略拒绝：mcp.safety.allow_actions = false（服务处于只读模式）",
            )

    def check_sequence(self, count: int) -> None:
        """检查动作序列长度是否超限。

        Args:
            count: 序列中动作数量。

        Raises:
            BridgeError: 超过 ``max_sequence_len``（error_code=blocked_by_safety）。
        """
        from mcp_server.bridge import BridgeError

        limit = self._config.max_sequence_len
        if count > limit:
            raise BridgeError(
                "blocked_by_safety",
                f"动作序列长度 {count} 超过上限 {limit}（mcp.safety.max_sequence_len）",
            )

    def record(
        self,
        tool: str,
        args: Dict[str, Any],
        ok: bool,
        error: Optional[str] = None,
        duration_ms: float = 0.0,
    ) -> None:
        """记录一次操作到环形日志。

        Args:
            tool: 工具名。
            args: 参数快照（内部会截断过长字段）。
            ok: 是否成功。
            error: 失败原因。
            duration_ms: 执行耗时（毫秒）。
        """
        record = ActionRecord(
            ts=time.time(),
            tool=tool,
            args=self._truncate_args(args),
            ok=ok,
            error=error,
            duration_ms=round(duration_ms, 1),
        )
        with self._lock:
            self._log.append(record)

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """返回最近的操作记录（最新在最后）。

        Args:
            limit: 最多返回条数。

        Returns:
            ActionRecord 的 dict 列表。
        """
        with self._lock:
            items = [asdict(r) for r in self._log]
        return items[-max(0, limit):]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_args(args: Dict[str, Any]) -> Dict[str, Any]:
        """截断参数快照中的过长字段，避免日志膨胀。"""
        result: Dict[str, Any] = {}
        for key, value in args.items():
            text = repr(value)
            if len(text) > _ARG_STR_LIMIT:
                result[key] = text[:_ARG_STR_LIMIT] + "..."
            else:
                result[key] = value
        return result
