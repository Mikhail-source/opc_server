# core/scripting.py
import asyncio
import logging
import time
import threading
import math
from lupa import LuaRuntime
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
HEAVY_EXECUTOR = ThreadPoolExecutor(thread_name_prefix="LuaHeavy", max_workers=2)

class LuaEngine:
    def __init__(self, registry, log_queue=None):
        self.registry = registry
        self.log_queue = log_queue
        self._lua_lock = threading.Lock()
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        
        gl = self.lua.globals()
        
        # 🔹 Функции для работы с тегами
        gl['tag_get'] = self._tag_get
        gl['tag_set'] = self._tag_set
        gl['log'] = self._log
        gl['time'] = time.time
        
        # 🔹 Математика: комбинируем Python math + Lua-совместимые функции
        gl['math'] = math  # sin, cos, sqrt, pi, exp, log и т.д.
        gl['max'] = max    # ← ДОБАВИТЬ: математический максимум (из builtins)
        gl['min'] = min    # ← ДОБАВИТЬ: математический минимум (из builtins)
        gl['abs'] = abs    # ← Опционально: модуль числа
        
        # 🔹 Тяжёлые вычисления в фоне
        gl['heavy_compute'] = self._heavy_compute
        
        self._func = None
        self._running = False
        self._task = None

    def load_script(self, script_content: str):
        try:
            self._func = self.lua.execute(f"return function() {script_content} end")
            logger.info("✅ Lua script loaded from project")
        except Exception as e:
            logger.error(f"❌ Lua script load error: {e}")
            self._func = None

    async def start(self, interval: float = 1.0):
        if not self._func:
            logger.warning("⚠️ Cannot start Lua engine: empty script")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(interval))

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass

    async def _run_loop(self, interval: float):
        while self._running:
            try:
                await asyncio.to_thread(self._execute)
            except Exception as e:
                logger.error(f"❌ Script execution error: {e}")
            await asyncio.sleep(interval)

    def _execute(self):
        if self._func:
            with self._lua_lock:
                self._func()

    def _tag_get(self, name: str):
        return self.registry.get_tag_value_sync(name)

    def _tag_set(self, name: str, value):
        self.registry.update_tag_sync(name, value, "Good")

    def _log(self, msg: str):
        logger.info(f"[LUA] {msg}")
        if self.log_queue:
            try: self.log_queue.put_nowait(f"{time.strftime('%H:%M:%S')} {msg}")
            except: pass

    def _heavy_compute(self, python_callable, *args, **kwargs):
        future = HEAVY_EXECUTOR.submit(python_callable, *args, **kwargs)
        return future.result(timeout=5.0)

    def reload(self, new_content: str):
        self.load_script(new_content)
        if self._func and self._running:
            logger.info("🔄 Lua script reloaded on-the-fly")