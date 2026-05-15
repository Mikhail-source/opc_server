from dataclasses import dataclass
from typing import Any

@dataclass
class Tag:
    name: str
    source: str = ""
    address: str = ""
    type: str = "float32"
    source: str = "" 
    value: Any = None
    quality: str = "Unknown"
    timestamp: float = 0.0