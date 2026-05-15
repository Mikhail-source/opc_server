from abc import ABC, abstractmethod
from typing import Dict, Any, List
import asyncio
from shared.models import Tag

class BaseDriver(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.connected = False

    @abstractmethod
    async def connect(self) -> bool: ...
    @abstractmethod
    async def read_batch(self, tags: List[Tag]) -> List[Tag]: ...
    @abstractmethod
    async def write_batch(self, tags: List[Tag]) -> List[Tag]: ...
    @abstractmethod
    async def disconnect(self): ...

    async def poll_loop(self, registry, interval: float = 1.0):
        import logging
        log = logging.getLogger(self.__class__.__name__)
        
        while True:
            if not self.connected:
                log.warning("Нет подключения. Повтор через 2с...")
                await asyncio.sleep(2)
                await self.connect()
                continue
                
            batch = [t for t in registry.tags.values() if t.address.startswith(self.config.get("prefix", ""))]
            if batch:
                updated = await self.read_batch(batch)
                for t in updated:
                    registry.update_tag(t.name, t.value, t.quality, t.timestamp)
                log.info(f"Опрошено {len(batch)} тегов, обновлено: {sum(1 for t in updated if t.quality=='Good')}")
            await asyncio.sleep(interval)