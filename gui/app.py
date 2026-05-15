import sys
import asyncio
import logging
import threading
import queue
import yaml
from pathlib import Path
from typing import Dict

from PyQt6.QtWidgets import (QApplication, QMainWindow, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QLabel)
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor

from core.tag_registry import TagRegistry, ConfigWatcher
from drivers.modbus_tcp import ModbusTcpDriver
from watchdog.observers import Observer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-7s] %(message)s")
logger = logging.getLogger("GUI")

class ServerBackend:
    """Асинхронный сервер, работающий в отдельном потоке"""
    def __init__(self, update_queue: queue.Queue):
        self.update_queue = update_queue
        self.registry = TagRegistry()
        self.running = False
        self._stop_event = threading.Event()

    async def _run_async(self):
        cfg_path = Path("config/tags.yaml").absolute()
        if not cfg_path.exists():
            logger.error(f"❌ Конфиг не найден: {cfg_path}")
            self._stop_event.set()
            return

        self.registry.load_config(cfg_path)
        logger.info(f"✅ Загружено тегов: {len(self.registry.tags)}")

        # Hot-reload
        event_handler = ConfigWatcher(self.registry, cfg_path)
        observer = Observer()
        observer.schedule(event_handler, path=str(cfg_path.parent), recursive=False)
        observer.start()

        # Драйверы
        drivers_cfg = yaml.safe_load(cfg_path.read_text()).get("drivers", [])
        tasks = []
        for d_cfg in drivers_cfg:
            if d_cfg["type"] == "modbus_tcp":
                driver = ModbusTcpDriver(d_cfg)
                await driver.connect()
                tasks.append(asyncio.create_task(driver.poll_loop(self.registry, d_cfg.get("poll_interval", 1.0))))

        self.running = True
        while not self._stop_event.is_set():
            # Отправляем snapshot в GUI раз в секунду
            snapshot = {
                name: {"value": t.value, "quality": t.quality, "timestamp": t.timestamp}
                for name, t in self.registry.tags.items()
            }
            try:
                self.update_queue.put_nowait(snapshot)
            except queue.Full:
                pass  # Пропускаем, если GUI не успевает
            await asyncio.sleep(1.0)

        observer.stop()
        observer.join()

    def start(self):
        self._thread = threading.Thread(target=self._run_threaded, daemon=True)
        self._thread.start()

    def _run_threaded(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_async())
        except Exception as e:
            logger.error(f"Backend error: {e}")
        finally:
            loop.close()

    def stop(self):
        self._stop_event.set()
        self.running = False


class TagMonitor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OPC Server Monitor")
        self.resize(1200, 750)

        # Layout
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        
        self.status_lbl = QLabel("⏳ Подключение к серверу...")
        self.status_lbl.setStyleSheet("color: gray; font-weight: bold; padding: 5px;")
        layout.addWidget(self.status_lbl)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Tag Name", "Value", "Quality", "Timestamp"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # Backend & Queue
        self.tag_queue = queue.Queue(maxsize=2)
        self.backend = ServerBackend(self.tag_queue)
        self.backend.start()

        # Таймер обновления GUI
        self.timer = QTimer()
        self.timer.timeout.connect(self._poll_queue)
        self.timer.start(500)

    def _poll_queue(self):
        while not self.tag_queue.empty():
            try:
                snapshot = self.tag_queue.get_nowait()
                self._update_table(snapshot)
            except queue.Empty:
                break

    def _update_table(self, snapshot: Dict):
        self.status_lbl.setText(f"🟢 Тегов в памяти: {len(snapshot)} | Обновлено: {asyncio.get_event_loop().time() % 86400:.1f}s")
        
        self.table.setRowCount(len(snapshot))
        for i, (name, data) in enumerate(snapshot.items()):
            self.table.setItem(i, 0, QTableWidgetItem(name))
            
            val_str = f"{data['value']:.4f}" if isinstance(data['value'], float) else str(data['value'] or "---")
            self.table.setItem(i, 1, QTableWidgetItem(val_str))

            qual_item = QTableWidgetItem(data['quality'])
            if data['quality'] == "Good":
                qual_item.setForeground(QColor("#00aa00"))
                qual_item.setBackground(QColor("#e6ffe6"))
            elif data['quality'] == "Bad":
                qual_item.setForeground(QColor("#cc0000"))
                qual_item.setBackground(QColor("#ffe6e6"))
            self.table.setItem(i, 2, qual_item)

            ts_str = f"{data['timestamp']:.2f}" if data['timestamp'] else "N/A"
            self.table.setItem(i, 3, QTableWidgetItem(ts_str))

    def closeEvent(self, event):
        self.timer.stop()
        self.backend.stop()
        logger.info("🛑 Сервер остановлен. Закрытие окна.")
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Современный кроссплатформенный стиль
    window = TagMonitor()
    window.show()
    sys.exit(app.exec())