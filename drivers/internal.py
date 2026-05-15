import asyncio
import logging
import json
from pathlib import Path
from typing import Dict, List, Any
from dataclasses import asdict
from drivers.base import BaseDriver, Tag

logger = logging.getLogger(__name__)

class InternalDriver(BaseDriver):
    """
    Драйвер для тегов, хранящихся в памяти сервера.
    Поддерживает:
    - Чтение/запись в реальном времени
    - Автосохранение значений на диск (persistence)
    - Инициализацию из конфига или из сохранённого файла
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = True  # Всегда "подключён"
        self._memory: Dict[str, Tag] = {}
        self._persistence_path = Path(config.get("persistence_file", "data/internal_tags.json"))
        self._save_interval = float(config.get("save_interval", 10.0))  # сек
        self._save_task: asyncio.Task | None = None

    async def connect(self) -> bool:
        # Загружаем сохранённые значения при старте
        await self._load_persistence()
        self.connected = True
        logger.info("[InternalDriver] Инициализирован")
        return True

    async def disconnect(self):
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        await self._save_persistence()  # Финальное сохранение
        self.connected = False
        logger.info("[InternalDriver] Остановлен")

    async def read_batch(self, tags: List[Tag]) -> List[Tag]:
        """Чтение значений из памяти"""
        result = []
        for tag in tags:
            if tag.name in self._memory:
                # Возвращаем актуальное значение из памяти
                cached = self._memory[tag.name]
                tag.value = cached.value
                tag.quality = cached.quality
                tag.timestamp = cached.timestamp
            else:
                # Если тег новый — инициализируем дефолтом из конфига
                tag.quality = "Good"
                tag.timestamp = asyncio.get_event_loop().time()
                self._memory[tag.name] = tag
            result.append(tag)
        return result

    async def write_batch(self, tags: List[Tag]) -> List[Tag]:
        """Запись значений в память + триггер автосохранения"""
        now = asyncio.get_event_loop().time()
        for tag in tags:
            if tag.name in self._memory:
                # Обновляем существующий тег
                self._memory[tag.name].value = tag.value
                self._memory[tag.name].quality = "Good"
                self._memory[tag.name].timestamp = now
            else:
                # Создаём новый
                tag.quality = "Good"
                tag.timestamp = now
                self._memory[tag.name] = tag
            tag.quality = "Good"  # Подтверждение успешной записи
        return tags

    async def _load_persistence(self):
        """Загрузка сохранённых значений при старте"""
        if not self._persistence_path.exists():
            return
        try:
            with open(self._persistence_path, "r") as f:
                data = json.load(f)
            for name, val in data.items():
                if name in self._memory:
                    self._memory[name].value = val
                    logger.debug(f"[Internal] Loaded '{name}' = {val}")
        except Exception as e:
            logger.warning(f"[Internal] Failed to load persistence: {e}")

    async def _save_persistence(self):
        """Сохранение текущих значений на диск"""
        try:
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            data = {name: t.value for name, t in self._memory.items() if t.value is not None}
            with open(self._persistence_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug(f"[Internal] Saved {len(data)} values to {self._persistence_path}")
        except Exception as e:
            logger.error(f"[Internal] Failed to save persistence: {e}")

    async def poll_loop(self, registry, interval: float = 1.0):
        """
        Внутренние теги не опрашиваются — они обновляются по запросу.
        Но мы запускаем фоновую задачу автосохранения.
        """
        async def _autosave_loop():
            while True:
                await asyncio.sleep(self._save_interval)
                if self.connected:
                    await self._save_persistence()
        
        self._save_task = asyncio.create_task(_autosave_loop())
        
        # Бесконечный цикл, чтобы poll_loop не завершался
        while True:
            await asyncio.sleep(3600)  # Sleep long, wake only for shutdown

    def register_tag(self, tag: Tag):
        """Программная регистрация тега (для скриптов)"""
        self._memory[tag.name] = tag
        logger.info(f"[Internal] Registered tag: {tag.name}")

    def get_value(self, name: str) -> Any:
        """Получить значение тега (для скриптов)"""
        return self._memory.get(name, Tag(name=name, value=None)).value

    def set_value(self, name: str, value: Any):
        """Установить значение тега (для скриптов)"""
        if name in self._memory:
            self._memory[name].value = value
            self._memory[name].timestamp = asyncio.get_event_loop().time()