"""重试/容错工具。"""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry(
    max_attempts: int = 3,
    delay_sec: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,),
) -> Callable:
    """重试装饰器。

    Args:
        max_attempts: 最大尝试次数
        delay_sec: 初始延迟秒数
        backoff_factor: 延迟倍增因子
        exceptions: 需要捕获的异常类型

    用法：
        @retry(max_attempts=3, delay_sec=0.5)
        def risky_operation():
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            current_delay = delay_sec
            last_exception: Optional[Exception] = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts:
                        logger.warning(
                            "%s 第 %d 次尝试失败: %s，%.1fs 后重试",
                            func.__name__, attempt, e, current_delay,
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff_factor
                    else:
                        logger.error(
                            "%s 已达最大尝试次数 %d，放弃",
                            func.__name__, max_attempts,
                        )

            raise last_exception  # type: ignore

        return wrapper
    return decorator


def retry_call(
    func: Callable[..., T],
    args: tuple = (),
    kwargs: Optional[dict] = None,
    max_attempts: int = 3,
    delay_sec: float = 1.0,
) -> Optional[T]:
    """函数式重试调用。"""
    kwargs = kwargs or {}
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < max_attempts:
                logger.warning("调用失败 (attempt %d/%d): %s", attempt, max_attempts, e)
                time.sleep(delay_sec)
            else:
                logger.error("调用最终失败: %s", e)
    return None
