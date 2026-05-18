import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Dict, List, Tuple, Any
from collections import defaultdict

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException
from pymodbus.pdu import ExceptionResponse

from drivers.base import BaseDriver, Tag

logger = logging.getLogger(__name__)

# Префиксы адресов -> (Function Code, Read/Write, Max Chunk Size)
ADDR_CONFIG = {
    '0x': (1, True, 2000),   # Coils (R/W)
    '1x': (2, False, 2000),  # Discrete Inputs (R)
    '3x': (4, False, 125),   # Input Registers (R)
    '4x': (3, True, 125)     # Holding Registers (R/W)
}

class ModbusTcpDriver(BaseDriver):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.host = config.get("host", "127.0.0.1")
        self.port = config.get("port", 502)
        self.unit = config.get("unit", 0)
        self.retry_delay = float(config.get("retry_delay", 5.0))
        self.client: AsyncModbusTcpClient | None = None
        self._reconnect_task: asyncio.Task | None = None

    async def connect(self) -> bool:
        if self.connected:
            return True
        try:
            self.client = AsyncModbusTcpClient(self.host, port=self.port)
            await self.client.connect()
            if self.client.is_socket_open():
                self.connected = True
                logger.info(f"[ModbusTCP] Connected to {self.host}:{self.port} (Unit={self.unit})")
                return True
            raise ConnectionException("Socket not open")
        except Exception as e:
            logger.error(f"[ModbusTCP] Connection failed: {e}")
            self.connected = False
            self._schedule_reconnect()
            return False

    def _schedule_reconnect(self):
        if self._reconnect_task and not self._reconnect_task.done():
            return
        async def _retry():
            await asyncio.sleep(self.retry_delay)
            await self.connect()
        self._reconnect_task = asyncio.create_task(_retry())

    async def disconnect(self):
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self.client:
            self.client.close()
        self.connected = False
        logger.info("[ModbusTCP] Disconnected")

    def _parse_address(self, addr: str) -> Tuple[int, int, str]:
        for prefix, (fc, _, _) in ADDR_CONFIG.items():
            if addr.startswith(prefix):
                return fc, int(addr[2:]), prefix
        raise ValueError(f"Invalid Modbus address: {addr}")

    def _encode_value(self, value: Any, tag_type: str) -> list[int]:
        """Преобразует Python-значение в список 16-битных регистров"""
        if tag_type == 'float32':
            return list(struct.unpack('>HH', struct.pack('>f', float(value))))
        if tag_type == 'int32':
            return list(struct.unpack('>HH', struct.pack('>i', int(value))))
        if tag_type == 'uint32':
            return list(struct.unpack('>HH', struct.pack('>I', int(value))))
        if tag_type == 'int16':
            return [int(value) & 0xFFFF]
        if tag_type == 'uint16':
            return [int(value) & 0xFFFF]
        if tag_type == 'bool':
            return [1 if value else 0]
        return [int(value)]

    def _decode_value(self, raw: Any, tag_type: str, fc: int) -> Any:
        """Преобразует ответ Modbus в Python-значение"""
        if fc in (1, 2):  # Coils / Discrete
            return bool(raw[0])
        if tag_type in ('float32', 'int32', 'uint32'):
            packed = struct.pack('>HH', raw[0], raw[1])
            if tag_type == 'float32': return struct.unpack('>f', packed)[0]
            if tag_type == 'int32': return struct.unpack('>i', packed)[0]
            return struct.unpack('>I', packed)[0]
        return raw[0]

    async def read_batch(self, tags: List[Tag]) -> List[Tag]:
        if not self.connected:
            await self.connect()
            if not self.connected:
                return [Tag(t.name, t.address, t.type, None, "Bad") for t in tags]

        # Группируем теги по FC и префиксу для пакетного чтения
        groups = defaultdict(list)
        for tag in tags:
            try:
                fc, addr, prefix = self._parse_address(tag.address)
                groups[(fc, prefix)].append(tag)
            except Exception as e:
                tag.quality = "Bad"
                logger.error(f"Parse error for {tag.name}: {e}")

        updated = []
        max_loop = asyncio.get_event_loop().time()

        for (fc, prefix), group in groups.items():
            # Сортируем по адресу и разбиваем на непрерывные блоки
            group.sort(key=lambda t: int(t.address[2:]))
            max_chunk = ADDR_CONFIG[prefix][2]
            chunks = []
            current_chunk = [group[0]]
            
            for tag in group[1:]:
                addr = int(tag.address[2:])
                prev_addr = int(current_chunk[-1].address[2:])
                # Учитываем 32-битные типы (занимают 2 регистра)
                step = 2 if tag.type in ('float32','int32','uint32') else 1
                prev_step = 2 if current_chunk[-1].type in ('float32','int32','uint32') else 1
                
                if addr == prev_addr + prev_step and len(current_chunk) * (1 + (1 if current_chunk[-1].type in ('float32','int32','uint32') else 0)) < max_chunk:
                    current_chunk.append(tag)
                else:
                    chunks.append(current_chunk)
                    current_chunk = [tag]
            chunks.append(current_chunk)

            # Чтение чанками
            for chunk in chunks:
                start_addr = int(chunk[0].address[2:])
                count = sum(2 if t.type in ('float32','int32','uint32') else 1 for t in chunk)
                try:
                    if fc == 1: res = await self.client.read_coils(start_addr, count, unit=self.unit)
                    elif fc == 2: res = await self.client.read_discrete_inputs(start_addr, count, unit=self.unit)
                    elif fc == 3: res = await self.client.read_holding_registers(start_addr, count, unit=self.unit)
                    elif fc == 4: res = await self.client.read_input_registers(start_addr, count, unit=self.unit)
                    else: continue

                    if res.isError() or isinstance(res, ExceptionResponse):
                        for t in chunk:
                            t.quality, t.value = "Bad", None
                        continue

                    raw_data = res.bits if fc in (1,2) else res.registers
                    idx = 0
                    for t in chunk:
                        step = 2 if t.type in ('float32','int32','uint32') else 1
                        try:
                            t.value = self._decode_value(raw_data[idx:idx+step], t.type, fc)
                            t.quality = "Good"
                        except Exception as e:
                            logger.error(f"Decode error {t.name}: {e}")
                            t.value = getattr(t, 'disconnect_value', None) or t.value
                            t.quality = "Bad"
                        t.timestamp = max_loop
                        idx += step
                        updated.append(t)
                except Exception as e:
                    logger.error(f"Read chunk failed: {e}")
                    for t in chunk:
                        t.quality = "Bad"
                        t.timestamp = asyncio.get_event_loop().time()
                        # 🔹 Логика значения при дисконекте
                        if getattr(t, 'disconnect_value', None) is not None:
                            t.value = t.disconnect_value
                        # Иначе: t.value НЕ меняется → остаётся последнее известное
                        updated.append(t)

        return updated

    async def write_batch(self, tags: List[Tag]) -> List[Tag]:
        if not self.connected:
            await self.connect()
        if not self.connected:
            return [Tag(t.name, t.address, t.type, None, "Bad") for t in tags]

        updated = []
        for tag in tags:
            try:
                fc, addr, prefix = self._parse_address(tag.address)
                if not ADDR_CONFIG[prefix][1]:
                    tag.quality = "ReadOnly"
                    updated.append(tag)
                    continue

                regs = self._encode_value(tag.value, tag.type)
                if fc == 1:
                    res = await self.client.write_coil(addr, bool(tag.value), unit=self.unit)
                else:
                    res = await self.client.write_registers(addr, regs, unit=self.unit) if len(regs) > 1 else \
                          await self.client.write_register(addr, regs[0], unit=self.unit)

                tag.quality = "Good" if not res.isError() else "Bad"
                tag.timestamp = asyncio.get_event_loop().time()
                updated.append(tag)
            except Exception as e:
                logger.error(f"Write error {tag.name}: {e}")
                tag.quality = "Bad"
                updated.append(tag)
        return updated