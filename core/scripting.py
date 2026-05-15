import asyncio
import logging
import time
from pathlib import Path
from lupa import LuaRuntime

logger = logging.getLogger(__name__)

class LuaEngine:
    def __init__(self, registry):
        self.registry = registry
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        
        # 1️⃣ Регистрируем функции в глобальной области Lua ОДИН РАЗ
        gl = self.lua.globals()
        gl['tag_get'] = self._tag_get
        gl['tag_set'] = self._tag_set
        gl['log']     = self._log
        gl['time']    = time.time
        
        self._func = None
        self._running = False
        self._task = None
        self._path = None

    def load_script(self, path: Path):
        self._path = path
        if not path.exists():
            logger.warning(f"📜 Script not found: {path}")
            self._func = None
            return
        try:
            code = path.read_text()
            # Оборачиваем в функцию, чтобы локальные переменные (local x) 
            # очищались после каждого выполнения, но глобальные сохранялись
            self._func = self.lua.execute(f"return function() {code} end")
            logger.info(f"✅ Lua script loaded: {path.name}")
        except Exception as e:
            logger.error(f"❌ Lua script load error: {e}")
            self._func = None

    async def start(self, interval: float = 1.0):
        if not self._func:
            logger.warning("⚠️ Cannot start Lua engine: no script loaded")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(interval))

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self, interval: float):
        while self._running:
            try:
                self._execute()
            except Exception as e:
                logger.error(f"❌ Script execution error: {e}")
            await asyncio.sleep(interval)

    def _execute(self):
        if self._func:
            # Функция вызывается без аргументов, т.к. globals уже настроены
            self._func()

    def _tag_get(self, name: str):
        tag = self.registry.tags.get(name)
        return tag.value if tag else None

    def _tag_set(self, name: str, value):
        if name in self.registry.tags:
            tag = self.registry.tags[name]
            tag.value = value
            tag.quality = "Good"
            tag.timestamp = time.time()
        else:
            from shared.models import Tag
            self.registry.tags[name] = Tag(
                name=name, value=value, type="float32", source="script", quality="Good"
            )

    def _log(self, msg: str):
        logger.info(f"[LUA] {msg}")

    def reload(self):
        """Перезагрузка скрипта без остановки сервера"""
        if self._path:
            self.load_script(self._path)
            if self._func and self._running:
                logger.info("🔄 Lua script reloaded on-the-fly")