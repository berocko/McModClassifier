"""速率控制器 + 线程安全打印."""

import time
import threading

MAX_WORKERS = 20
MODRINTH_RATE = 5       # Modrinth 每秒请求数
MCMOD_RATE = 2          # mcmod.cn 每秒请求数

_print_lock = threading.Lock()


def tprint(*args, **kwargs):
    """线程安全的 print."""
    with _print_lock:
        print(*args, **kwargs)


class RateLimiter:
    """令牌桶速率控制器 — 线程安全."""

    def __init__(self, rate_per_second):
        self.rate = rate_per_second
        self.tokens = rate_per_second
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        """获取一个令牌，必要时阻塞."""
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                time.sleep(wait)
                self.tokens = 0
                self.last_refill = time.monotonic()
            else:
                self.tokens -= 1


_modrinth_limiter = RateLimiter(MODRINTH_RATE)
_mcmod_limiter = RateLimiter(MCMOD_RATE)
