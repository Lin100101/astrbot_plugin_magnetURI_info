import asyncio
import time
from datetime import datetime
from astrbot.api import logger

class PerformanceMonitor:
    def __init__(self):
        self.metrics = {
            'api_calls': 0,
            'api_errors': 0,
            'screenshot_downloads': 0,
            'total_response_time': 0.0,
            'cache_hits': 0,
            'cache_misses': 0,
            'start_time': datetime.now()
        }
        self._lock = asyncio.Lock()
    
    async def record_api_call(self, duration: float, success: bool):
        async with self._lock:
            self.metrics['api_calls'] += 1
            self.metrics['total_response_time'] += duration
            if not success:
                self.metrics['api_errors'] += 1
    
    async def record_cache_operation(self, hit: bool):
        async with self._lock:
            if hit:
                self.metrics['cache_hits'] += 1
            else:
                self.metrics['cache_misses'] += 1
    
    async def record_screenshot_download(self):
        async with self._lock:
            self.metrics['screenshot_downloads'] += 1

    def get_stats(self) -> dict:
        total_requests = self.metrics['api_calls']
        avg_response_time = (self.metrics['total_response_time'] / max(1, total_requests))
        total_cache_ops = self.metrics['cache_hits'] + self.metrics['cache_misses']
        cache_hit_rate = self.metrics['cache_hits'] / max(1, total_cache_ops)
        
        return {
            'total_api_calls': total_requests,
            'api_error_rate': self.metrics['api_errors'] / max(1, total_requests),
            'avg_response_time_ms': avg_response_time * 1000,
            'cache_hit_rate': cache_hit_rate,
            'screenshot_downloads': self.metrics['screenshot_downloads'],
            'uptime_hours': (datetime.now() - self.metrics['start_time']).total_seconds() / 3600
        }
