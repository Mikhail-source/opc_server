import sys
import asyncio
import logging
import threading
import queue
from pathlib import Path
from typing import Dict, Any, Optional

from ruamel.yaml import YAML
from PyQt6.QtWidgets import (QApplication, QMainWindow, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QHBoxLayout, QWidget, QHeaderView, QLabel,
                             QPushButton, QDialog, QFormLayout, QLineEdit, QComboBox,
                             QDoubleSpinBox, QMessageBox, QAbstractItemView, QSpinBox)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor

from core.tag_registry import TagRegistry, ConfigWatcher
from drivers.modbus_tcp import ModbusTcpDriver
from drivers.internal import InternalDriver
from shared.models import Tag
from watchdog.observers import Observer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-7s] %(message)s")
logger = logging.getLogger("GUI")

yaml = YAML()
yaml.preserve_quotes = True
yaml.explicit_start = True

class TagEditDialog(QDialog):
    def __init__(self, parent=None, tag_data: Optional[Dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Редактор тега" if tag_data else "Новый тег")
        self.setModal(True)
        self.resize(400, 350)
        self.tag_data = tag_data or {}

        layout = QFormLayout()
        self.name_edit = QLineEdit(self.tag_data.get("name", ""))
        self.source_combo = QComboBox()
        self.source_combo.addItems(["internal", "modbus_tcp"])
        if self.tag_data.get("source"):
            self.source_combo.setCurrentText(self.tag_data["source"])

        self.address_edit = QLineEdit(self.tag_data.get("address", ""))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["float32", "bool", "int16", "uint16", "int32", "uint32", "string"])
        self.type_combo.setCurrentText(self.tag_data.get("type", "float32"))

        self.value_edit = QLineEdit(str(self.tag_data.get("value", "") if self.tag_data.get("value") is not None else ""))

        layout.addRow("Имя:", self.name_edit)
        layout.addRow("Источник:", self.source_combo)
        layout.addRow("Адрес:", self.address_edit)
        layout.addRow("Тип:", self.type_combo)
        layout.addRow("Нач. значение:", self.value_edit)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("💾 Сохранить")
        cancel_btn = QPushButton("❌ Отмена")
        save_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addRow(btn_layout)

        self.setLayout(layout)

    def get_data(self) -> Dict[str, Any]:
        val_str = self.value_edit.text().strip()
        t = self.type_combo.currentText()
        try:
            if t == "bool":
                val = val_str.lower() in ("1", "true", "yes", "вкл")
            elif t in ("int16", "uint16", "int32", "uint32"):
                val = int(val_str) if val_str else 0
            elif t == "float32":
                val = float(val_str) if val_str else 0.0
            else:
                val = val_str
        except ValueError:
            val = None

        return {
            "name": self.name_edit.text().strip(),
            "source": self.source_combo.currentText(),
            "address": self.address_edit.text().strip(),
            "type": self.type_combo.currentText(),
            "value": val
        }

class ServerBackend:
    def __init__(self, update_queue: queue.Queue, cmd_queue: queue.Queue, cfg_path: Path):
        self.update_queue = update_queue
        self.cmd_queue = cmd_queue
        self.cfg_path = cfg_path
        self.registry = TagRegistry()
        self.running = False
        self._stop_event = threading.Event()

    async def _run_async(self):
        if not self.cfg_path.exists():
            logger.error(f"❌ Конфиг не найден: {self.cfg_path}")
            self._stop_event.set()
            return

        self.registry.load_config(self.cfg_path)
        logger.info(f"✅ Загружено тегов: {len(self.registry.tags)}")

        # Hot-reload для внешних правок
        event_handler = ConfigWatcher(self.registry, self.cfg_path)
        observer = Observer()
        observer.schedule(event_handler, path=str(self.cfg_path.parent), recursive=False)
        observer.start()

        # Драйверы
        drivers_cfg = yaml.load(self.cfg_path.read_text()).get("drivers", [])
        tasks = []
        for d_cfg in drivers_cfg:
            if d_cfg["type"] == "modbus_tcp":
                driver = ModbusTcpDriver(d_cfg)
                await driver.connect()
                tasks.append(asyncio.create_task(driver.poll_loop(self.registry, d_cfg.get("poll_interval", 1.0))))
            elif d_cfg["type"] == "internal":
                driver = InternalDriver(d_cfg)
                await driver.connect()
                tasks.append(asyncio.create_task(driver.poll_loop(self.registry, d_cfg.get("poll_interval", 10.0))))

        self.running = True
        while not self._stop_event.is_set():
            # Обработка команд от GUI
            await self._process_commands()
            
            # Отправка snapshot в GUI
            snapshot = {
                name: {"value": t.value, "quality": t.quality, "timestamp": t.timestamp}
                for name, t in self.registry.tags.items()
            }
            try:
                self.update_queue.put_nowait(snapshot)
            except queue.Full:
                pass
            await asyncio.sleep(1.0)

        observer.stop()
        observer.join()

    async def _process_commands(self):
        while not self.cmd_queue.empty():
            try:
                cmd = self.cmd_queue.get_nowait()
                action = cmd["action"]
                data = cmd.get("data", {})
                
                if action == "add_tag":
                    await self.registry.add_tag(Tag(**data))
                    await self._save_config()
                elif action == "update_tag":
                    await self.registry.update_tag(data["old_name"], Tag(**data["new"]))
                    await self._save_config()
                elif action == "delete_tag":
                    await self.registry.remove_tag(data["name"])
                    await self._save_config()
                self.cmd_queue.task_done()
            except queue.Empty:
                break

    async def _save_config(self):
        try:
            cfg = yaml.load(self.cfg_path.read_text())
            # Обновляем список тегов в конфиге
            cfg["tags"] = [
                {
                    "name": t.name,
                    "source": t.source,
                    "address": t.address,
                    "type": t.type,
                    "value": t.value
                }
                for t in self.registry.tags.values()
            ]
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f)
            logger.info("💾 Конфиг сохранён")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения конфига: {e}")

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

    def send_command(self, cmd: Dict):
        self.cmd_queue.put_nowait(cmd)


class TagMonitor(QMainWindow):
    def __init__(self, cfg_path: Path = Path("config/tags.yaml")):
        super().__init__()
        self.setWindowTitle("OPC Server Manager")
        self.resize(1300, 800)
        self.cfg_path = cfg_path

        # Layout
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Toolbar
        toolbar = QHBoxLayout()
        self.btn_add = QPushButton("➕ Добавить тег")
        self.btn_edit = QPushButton("✏️ Изменить")
        self.btn_del = QPushButton("🗑 Удалить")
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_edit)
        toolbar.addWidget(self.btn_del)
        toolbar.addStretch()
        self.status_lbl = QLabel("⏳ Подключение...")
        toolbar.addWidget(self.status_lbl)
        layout.addLayout(toolbar)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Name", "Source", "Value", "Quality", "Timestamp"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # Backend & Queues
        self.update_queue = queue.Queue(maxsize=2)
        self.cmd_queue = queue.Queue()
        self.backend = ServerBackend(self.update_queue, self.cmd_queue, cfg_path)
        self.backend.start()

        # Connect buttons
        self.btn_add.clicked.connect(self._add_tag)
        self.btn_edit.clicked.connect(self._edit_tag)
        self.btn_del.clicked.connect(self._delete_tag)
        self.table.doubleClicked.connect(self._edit_tag)

        # Timer
        self.timer = QTimer()
        self.timer.timeout.connect(self._poll_queue)
        self.timer.start(500)

    def _poll_queue(self):
        while not self.update_queue.empty():
            try:
                snapshot = self.update_queue.get_nowait()
                self._update_table(snapshot)
            except queue.Empty:
                break

    def _update_table(self, snapshot: Dict):
        self.status_lbl.setText(f"🟢 Тегов: {len(snapshot)}")
        self.table.setRowCount(len(snapshot))
        for i, (name, data) in enumerate(snapshot.items()):
            tag_obj = self.backend.registry.tags.get(name)
            self.table.setItem(i, 0, QTableWidgetItem(name))
            self.table.setItem(i, 1, QTableWidgetItem(tag_obj.source if tag_obj else ""))
            
            val_str = f"{data['value']:.4f}" if isinstance(data['value'], float) else str(data['value'] or "---")
            self.table.setItem(i, 2, QTableWidgetItem(val_str))

            qual_item = QTableWidgetItem(data['quality'])
            if data['quality'] == "Good":
                qual_item.setForeground(QColor("#008000"))
            elif data['quality'] == "Bad":
                qual_item.setForeground(QColor("#cc0000"))
            self.table.setItem(i, 3, qual_item)
            self.table.setItem(i, 4, QTableWidgetItem(f"{data['timestamp']:.2f}" if data['timestamp'] else "N/A"))

    def _get_selected_tag(self) -> Optional[str]:
        rows = self.table.selectedItems()
        return rows[0].text() if rows else None

    def _add_tag(self):
        dlg = TagEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            if not data["name"]:
                QMessageBox.warning(self, "Ошибка", "Имя тега не может быть пустым")
                return
            if data["name"] in self.backend.registry.tags:
                QMessageBox.warning(self, "Ошибка", f"Тег '{data['name']}' уже существует")
                return
            self.backend.send_command({"action": "add_tag", "data": data})

    def _edit_tag(self):
        name = self._get_selected_tag()
        if not name:
            QMessageBox.information(self, "Подсказка", "Выберите тег для редактирования")
            return
        tag = self.backend.registry.tags.get(name)
        if not tag:
            return
        dlg = TagEditDialog(self, {
            "name": tag.name, "source": tag.source, "address": tag.address,
            "type": tag.type, "value": tag.value
        })
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_data = dlg.get_data()
            self.backend.send_command({
                "action": "update_tag",
                "data": {"old_name": name, "new": new_data}
            })

    def _delete_tag(self):
        name = self._get_selected_tag()
        if not name:
            QMessageBox.information(self, "Подсказка", "Выберите тег для удаления")
            return
        if QMessageBox.question(self, "Подтверждение", f"Удалить тег '{name}'?") == QMessageBox.StandardButton.Yes:
            self.backend.send_command({"action": "delete_tag", "data": {"name": name}})

    def closeEvent(self, event):
        self.timer.stop()
        self.backend.stop()
        logger.info("🛑 Сервер остановлен. Закрытие окна.")
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = TagMonitor()
    window.show()
    sys.exit(app.exec())