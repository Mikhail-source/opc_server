# drivers/base.py
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

    async def poll_loop(self, registry, default_interval: float = 1.0):
        import asyncio, logging
        log = logging.getLogger(self.__class__.__name__)
        self._last_poll: dict[str, float] = {}  # {tag_name: timestamp}
        driver_name = self.config.get("name", "")

        while True:
            if not self.connected:
                await asyncio.sleep(2)
                await self.connect()
                continue

            now = asyncio.get_event_loop().time()
            tags_to_poll = []
            next_due = now + 10.0  # fallback

            for tag in registry.tags.values():
                if tag.source != driver_name:
                    continue
                if not getattr(tag, 'enabled', True):
                    continue

                interval = getattr(tag, 'poll_interval', None) or default_interval
                last = self._last_poll.get(tag.name, 0)
                due_time = last + interval

                if now >= due_time:
                    tags_to_poll.append(tag)
                    self._last_poll[tag.name] = now
                    due_time = now + interval

                next_due = min(next_due, due_time)

            if tags_to_poll:
                updated = await self.read_batch(tags_to_poll)
                for t in updated:
                    if t.name in registry.tags:
                        registry.tags[t.name].value = t.value
                        registry.tags[t.name].quality = t.quality
                        registry.tags[t.name].timestamp = t.timestamp
                log.info(f"Опрошено {len(tags_to_poll)} тегов")

            # Точный сон до ближайшего тега (минимум 0.1с для реакции на изменения)
            sleep_time = max(0.1, next_due - asyncio.get_event_loop().time())
            await asyncio.sleep(sleep_time)