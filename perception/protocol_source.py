"""协议信息源 — RKPP (Roco-Kingdom-Protocol-Parser) relay 客户端。

连接 RKPP 子模块（opencode-server 模式）的本地 HTTP relay，
后台线程订阅 /events NDJSON 实时流，将解析后的战斗协议事件
写入 RuntimeContext 并通过 EventBus 发出，作为视觉之外的第二信息源。

RKPP 服务需独立启动（需要 Npcap 与会话 key）：

    python submodules/Roco-Kingdom-Protocol-Parser/rkpp_live_tools.py \
        opencode-server --iface "以太网" --port 8195 --relay-port 8765

本模块只读消费协议事件，不发送任何游戏数据。
协议源不可用时静默重试，工作流不应硬依赖协议数据（可回退视觉判断）。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from core.context import RuntimeContext
from core.events import (
    EventBus,
    EVENT_PROTOCOL_CONNECTED,
    EVENT_PROTOCOL_DISCONNECTED,
    EVENT_PROTOCOL_EVENT,
)

logger = logging.getLogger(__name__)


class ProtocolSource:
    """RKPP relay 协议信息源。

    用法：
        source = ProtocolSource(config, context, event_bus)
        source.setup()     # 启动后台订阅线程（enabled 时）
        ...
        source.cleanup()   # 停止线程并断开连接
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]],
        context: RuntimeContext,
        event_bus: EventBus,
    ) -> None:
        self._config = config or {}
        self._context = context
        self._event_bus = event_bus

        self._enabled: bool = bool(self._config.get("enabled", True))
        self._relay_url: str = str(
            self._config.get("relay_url", "http://127.0.0.1:8765")
        ).rstrip("/")
        self._history_size: int = int(self._config.get("history_size", 200))
        self._reconnect_sec: float = float(self._config.get("reconnect_sec", 3.0))
        self._read_timeout_sec: float = float(
            self._config.get("read_timeout_sec", 30.0)
        )
        self._log_events: bool = bool(self._config.get("log_events", False))

        self._recent: Deque[Dict[str, Any]] = deque(maxlen=self._history_size)
        self._seen_row_indexes: Deque[int] = deque(maxlen=1000)
        self._lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._response = None  # 当前 /events 流（cleanup 时强制关闭）
        self._connected = False
        self._event_count = 0
        self._warned_unreachable = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """启动后台订阅线程。"""
        if not self._enabled:
            logger.info("协议信息源已禁用 (protocol.enabled = false)")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="rkpp-protocol-source"
        )
        self._thread.start()
        logger.info("协议信息源已启动: %s", self._relay_url)

    def cleanup(self) -> None:
        """停止后台线程并释放连接。"""
        if self._thread is None:
            return

        self._stop_event.set()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

        self._thread.join(timeout=2.0)
        self._thread = None
        self._connected = False
        logger.info("协议信息源已停止 (累计接收 %d 条事件)", self._event_count)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def stats(self) -> Dict[str, Any]:
        """返回信息源运行状态摘要。"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "connected": self._connected,
                "relay_url": self._relay_url,
                "events": self._event_count,
                "history": len(self._recent),
            }

    def recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """返回最近收到的协议事件（最新在最后）。"""
        with self._lock:
            items = list(self._recent)
        return items[-max(0, limit):]

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """订阅主循环：连接 → 读事件 → 断线自动重连。"""
        while not self._stop_event.is_set():
            try:
                self._consume_stream()
            except Exception as e:
                if not self._stop_event.is_set():
                    self._handle_disconnect(f"流读取异常: {e}")
            else:
                self._handle_disconnect("服务端关闭了连接")

            if self._stop_event.is_set():
                break

            # 重连前等待（可被 stop 打断）
            self._stop_event.wait(self._reconnect_sec)

    def _consume_stream(self) -> None:
        """连接 /events 并持续消费 NDJSON 事件。"""
        events_url = f"{self._relay_url}/events"
        request = urllib.request.Request(events_url)
        response = urllib.request.urlopen(request, timeout=self._read_timeout_sec)
        self._response = response

        self._set_connected(True)
        self._warned_unreachable = False
        logger.info("已连接协议信息源: %s", events_url)
        self._event_bus.emit(EVENT_PROTOCOL_CONNECTED, url=events_url)

        try:
            while not self._stop_event.is_set():
                try:
                    line = response.readline()
                except (socket.timeout, TimeoutError):
                    # 读超时仅说明一段时间无事件；用 /health 确认服务端存活
                    if not self._server_alive():
                        return
                    continue

                if not line:
                    return  # 服务端关闭连接

                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("协议事件 JSON 解析失败: %s", text[:120])
                    continue

                self._handle_event(event)
        finally:
            self._response = None
            try:
                response.close()
            except Exception:
                pass

    def _server_alive(self) -> bool:
        """通过 /health 检查 relay 服务是否存活。"""
        try:
            with urllib.request.urlopen(
                f"{self._relay_url}/health", timeout=3.0
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _handle_disconnect(self, reason: str) -> None:
        """统一处理断线：更新状态、发事件、记日志。"""
        was_connected = self._connected
        self._set_connected(False)

        if was_connected:
            logger.warning("协议信息源断开 (%s)，%0.1fs 后重试", reason, self._reconnect_sec)
            self._event_bus.emit(EVENT_PROTOCOL_DISCONNECTED, reason=reason)
        elif not self._warned_unreachable:
            self._warned_unreachable = True
            logger.warning(
                "协议信息源不可达: %s（请确认 RKPP opencode-server 已启动），将持续重试",
                self._relay_url,
            )
        else:
            logger.debug("协议信息源仍不可达: %s", self._relay_url)

    def _set_connected(self, value: bool) -> None:
        self._connected = value

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _handle_event(self, event: Dict[str, Any]) -> None:
        """接收单条协议事件：去重 → 写入 context → 发事件。"""
        row_index = event.get("row_index")
        with self._lock:
            # /events 重连时会重放最近历史，按 row_index 去重
            if isinstance(row_index, int):
                if row_index in self._seen_row_indexes:
                    return
                self._seen_row_indexes.append(row_index)

            event["_received_at"] = time.time()
            self._recent.append(event)
            self._event_count += 1

        # 写入 RuntimeContext（按多个维度索引，供条件查询）
        events_map = self._context.last_protocol_events
        for key in (
            event.get("summary_kind"),
            event.get("opcode_name"),
            event.get("opencode"),
        ):
            key = str(key or "").strip()
            if key:
                events_map[key] = event
        self._context.last_protocol_event = event

        if self._log_events:
            logger.info(
                "协议事件: %s | %s",
                event.get("opencode") or event.get("opcode_name"),
                event.get("summary_text") or event.get("meaning") or "",
            )
        else:
            logger.debug(
                "协议事件: %s | %s",
                event.get("opencode") or event.get("opcode_name"),
                event.get("summary_text") or event.get("meaning") or "",
            )

        self._event_bus.emit(
            EVENT_PROTOCOL_EVENT,
            event=event,
            context=self._context,
        )
