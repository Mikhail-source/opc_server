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
    value: Any = None               # 🔹 Текущее значение (runtime, не сохраняется в конфиг)
    quality: str = "Unknown"
    timestamp: float = 0.0
    disconnect_value: Any = None    # 🔹 Значение при потере связи (опционально)