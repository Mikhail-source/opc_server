import yaml
import asyncio
import hashlib
import dataclasses
from pathlib import Path
from typing import Dict
from watchdog.events import FileSystemEventHandler
from drivers.base import Tag  # или from shared.models import Tag

class TagRegistry:
    def __init__(self):
        self.tags: Dict[str, Tag] = {}
        self._lock = asyncio.Lock()

    def update_tag(self, name: str, value, quality="Good", timestamp=0.0):
        async def _update():
            async with self._lock:
                if name in self.tags:
                    self.tags[name].value = value
                    self.tags[name].quality = quality
                    self.tags[name].timestamp = timestamp or asyncio.get_event_loop().time()
        asyncio.create_task(_update())

    def load_config(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        
        new_tags = {}
        # Получаем список допустимых полей dataclass Tag
        valid_fields = {f.name for f in dataclasses.fields(Tag)}

        for t in cfg.get("tags", []):
            tag_kwargs = {k: v for k, v in t.items() if k in valid_fields}
            
            # Если источник — internal, регистрируем в InternalDriver
            if t.get("source") == "internal":
                # Убедимся, что значение инициализировано
                if "value" not in tag_kwargs:
                    # Дефолтные значения по типу
                    defaults = {"bool": False, "int16": 0, "uint16": 0, "int32": 0, "uint32": 0, "float32": 0.0, "string": ""}
                    tag_kwargs["value"] = defaults.get(t.get("type", "float32"), None)
            
            new_tags[t["name"]] = Tag(**tag_kwargs)
            
        self.tags.update(new_tags)
        print(f"[CONFIG] Loaded {len(new_tags)} tags.")

    async def add_tag(self, tag: Tag):
        async with self._lock:
            self.tags[tag.name] = tag

    async def remove_tag(self, name: str):
        async with self._lock:
            self.tags.pop(name, None)

    async def update_tag(self, old_name: str, new_tag: Tag):
        async with self._lock:
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