import hashlib
import re
from typing import Optional, Dict
from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class CacheEntry:
    data: dict | bytes
    timestamp: datetime
    hit_count: int = 0

class SmartCache:
    def __init__(self, default_ttl: int = 300, max_size: int = 1000):
        self._cache: Dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
    
    def _generate_key(self, url_or_magnet: str) -> str:
        match = re.search(r'urn:btih:([A-Za-z0-9]{32,40})', url_or_magnet, re.IGNORECASE)
        if match:
            return match.group(1).lower()
        return hashlib.md5(url_or_magnet.encode()).hexdigest()
    
    def get(self, key_str: str) -> Optional[dict | bytes]:
        key = self._generate_key(key_str)
        entry = self._cache.get(key)
        
        if entry and datetime.now() - entry.timestamp < timedelta(seconds=self._default_ttl):
            entry.hit_count += 1
            self._hits += 1
            return entry.data
        
        if entry:
            del self._cache[key]
            
        self._misses += 1
        return None
    
    def set(self, key_str: str, data: dict | bytes):
        key = self._generate_key(key_str)
        
        if len(self._cache) >= self._max_size and key not in self._cache:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].timestamp)
            del self._cache[oldest_key]
        
        self._cache[key] = CacheEntry(data=data, timestamp=datetime.now())
