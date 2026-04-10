import asyncio
import random
from datetime import datetime, timedelta
from astrbot.api import logger

class RetryManager:
    def __init__(self):
        self.failure_counts = {}
        self.circuit_breakers = {}
    
    async def execute_with_retry(
        self, 
        func, 
        *args,
        max_retries: int = 2,
        base_delay: float = 0.2,
        max_delay: float = 2.0,
        key: str = "default",
        **kwargs
    ):
        if self._is_circuit_open(key):
            raise Exception(f"Circuit breaker open for {key}")
        
        for attempt in range(max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                
                if key in self.failure_counts:
                    del self.failure_counts[key]
                
                return result
                
            except Exception as e:
                self.failure_counts[key] = self.failure_counts.get(key, 0) + 1
                
                if self.failure_counts[key] >= 5:
                    self._open_circuit(key)
                
                if attempt == max_retries:
                    raise e
                
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, delay * 0.1)
                
                logger.warning(f"请求失败，{delay:.2f}秒后重试 (attempt {attempt + 1})")
                await asyncio.sleep(delay)
    
    def _is_circuit_open(self, key: str) -> bool:
        if key not in self.circuit_breakers:
            return False
        
        open_time, timeout = self.circuit_breakers[key]
        if datetime.now() - open_time > timedelta(seconds=timeout):
            del self.circuit_breakers[key]
            return False
        
        return True
    
    def _open_circuit(self, key: str, timeout: int = 60):
        self.circuit_breakers[key] = (datetime.now(), timeout)
        logger.error(f"熔断器开启 for {key}, timeout: {timeout}s")
