import asyncio
import ipaddress
import aiohttp
import urllib.parse
from astrbot.api import logger

class _SafeResolver(aiohttp.DefaultResolver):
    def __init__(self, host_allowlist: set[str] | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.host_allowlist = host_allowlist

    async def resolve(self, host: str, port: int, family: int = 0) -> list[dict]:
        host_lower = host.lower()
        if host_lower in ("localhost",) or host_lower.endswith(".local"):
            raise OSError(f"Refused to resolve private host: {host}")
        
        if self.host_allowlist is not None:
            if host_lower not in self.host_allowlist and not any(host_lower.endswith("." + h) for h in self.host_allowlist):
                raise OSError(f"Host not in allowlist: {host}")

        ips = await super().resolve(host, port, family)
        
        for info in ips:
            try:
                ip_str = info.get("host")
                if not ip_str:
                    continue
                ip_obj = ipaddress.ip_address(ip_str)
                if (
                    ip_obj.is_private
                    or ip_obj.is_loopback
                    or ip_obj.is_link_local
                    or ip_obj.is_multicast
                    or ip_obj.is_reserved
                    or ip_obj.is_unspecified
                ):
                    raise OSError(f"Resolved to private IP: {ip_str}")
            except ValueError:
                pass
        return ips

async def get_safe_session(host_allowlist: set[str] | None = None) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        limit=100,              
        limit_per_host=30,      
        ttl_dns_cache=300,      
        keepalive_timeout=30,   
        use_dns_cache=True,     
        enable_cleanup_closed=True,
        resolver=_SafeResolver(host_allowlist=host_allowlist)
    )
    timeout = aiohttp.ClientTimeout(
        total=30,               
        connect=10,             
        sock_read=20            
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,        
        headers={
            'User-Agent': 'AstrBot-MagnetParser/1.0',
            'Accept': 'application/json, text/plain;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
    )
