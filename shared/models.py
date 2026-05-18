from dataclasses import dataclass
from typing import Any

@dataclass
class Tag:
    name: str
    address: str = ""
    type: str = "float32"
    source: str = ""
    path: str = ""          # ← Новое поле: "Цех 1/Участок/Оборудование"
    value: Any = None
    quality: str = "Unknown"
    timestamp: float = 0.0