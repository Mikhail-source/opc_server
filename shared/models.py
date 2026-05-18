# shared/models.py
from dataclasses import dataclass
from typing import Any

@dataclass
class Tag:
    name: str
    address: str = ""
    type: str = "float32"
    source: str = ""
    path: str = ""
    value: Any = None
    quality: str = "Unknown"
    timestamp: float = 0.0
    disconnect_value: Any = None
    enabled: bool = True          # 🔹 Отключить отслеживание
    poll_interval: float = None   # 🔹 Индивидуальный период (переопределяет драйвер)