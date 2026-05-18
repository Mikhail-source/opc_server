# core/tag_registry.py
import yaml
import asyncio
import hashlib
import dataclasses
import threading
import time
from pathlib import Path
from typing import Dict, Any
from watchdog.events import FileSystemEventHandler
from shared.models import Tag

class TagRegistry:
    def __init__(self):
        self.tags: Dict[str, Tag] = {}
        self._async_lock = asyncio.Lock()      # для asyncio-задач
        self._sync_lock = threading.Lock()     # для Lua/потоков

    # 🔹 Потокобезопасные методы для Lua
    def update_tag_sync(self, name: str, value: Any, quality: str = "Good", timestamp: float = 0.0):
        with self._sync_lock:
            if name in self.tags:
                t = self.tags[name]
                t.value = value
                t.quality = quality
                t.timestamp = timestamp or time.time()

    def get_tag_value_sync(self, name: str) -> Any:
        with self._sync_lock:
            t = self.tags.get(name)
            return t.value if t else None

    # 🔹 Асинхронные методы для GUI/бэкенда
    async def update_tag_async(self, name: str, value: Any, quality: str = "Good", timestamp: float = 0.0):
        async with self._async_lock:
            if name in self.tags:
                self.tags[name].value = value
                self.tags[name].quality = quality
                self.tags[name].timestamp = timestamp or asyncio.get_event_loop().time()

    def load_config(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        new_tags = {}
        valid_fields = {f.name for f in dataclasses.fields(Tag)}
        for t in cfg.get("tags", []):
            tag_kwargs = {k: v for k, v in t.items() if k in valid_fields}
            if t.get("source") == "internal" and "value" not in tag_kwargs:
                defaults = {"bool": False, "int16": 0, "uint16": 0, "int32": 0, "uint32": 0, "float32": 0.0, "string": ""}
                tag_kwargs["value"] = defaults.get(t.get("type", "float32"), None)
            new_tags[t["name"]] = Tag(**tag_kwargs)
        self.tags.update(new_tags)
        print(f"[CONFIG] Loaded {len(new_tags)} tags.")

    async def add_tag(self, tag: Tag):
        async with self._async_lock:
            self.tags[tag.name] = tag

    async def remove_tag(self, name: str):
        async with self._async_lock:
            self.tags.pop(name, None)

    async def update_tag(self, old_name: str, new_tag: Tag):  # ← для CRUD из GUI
        async with self._async_lock:
            if old_name in self.tags:
                del self.tags[old_name]
            self.tags[new_tag.name] = new_tag

class ConfigWatcher(FileSystemEventHandler):
    def __init__(self, registry: TagRegistry, path: Path):
        self.registry = registry
        self.path = path
        self._hash = hashlib.md5(path.read_bytes()).hexdigest()
    def on_modified(self, event):
        if event.src_path == str(self.path):
            new_hash = hashlib.md5(self.path.read_bytes()).hexdigest()
            if new_hash != self._hash:
                self._hash = new_hash
                try:
                    self.registry.load_config(self.path)
                    print("[CONFIG] Successfully reloaded tags.")
                except Exception as e:
                    print(f"[CONFIG] Reload failed: {e}")