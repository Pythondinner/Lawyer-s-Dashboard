"""通用重试装饰器,给所有外部API调用(Tavily、DeepSeek)加一层网络抖动/限流兜底。
规则型:重试次数、退避时间都是写死的,不涉及任何判断,纯粹是稳定性兜底。
"""

import functools
import time


def with_retry(max_retries: int = 2, base_delay: float = 1.5, exceptions: tuple = (Exception,)):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_error = e
                    if attempt < max_retries:
                        time.sleep(base_delay * (attempt + 1))
                        continue
            raise last_error

        return wrapper

    return decorator
