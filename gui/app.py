# gui/app.py
import sys, asyncio, logging, threading, queue
from pathlib import Path
from typing import Dict, Any, Optional

from PyQt6.QtWidgets import (QApplication, QMainWindow, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QHBoxLayout, QWidget, QHeaderView, QLabel,
                             QPushButton, QDialog, QFormLayout, QLineEdit, QComboBox,
                             QMessageBox, QAbstractItemView, QFileDialog, QCheckBox,
                             QDoubleSpinBox)
from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor

from core.tag_registry import TagRegistry
from drivers.modbus_tcp import ModbusTcpDriver
from drivers.internal import InternalDriver
from core.scripting import LuaEngine
from core.project_manager import ProjectManager
from shared.models import Tag
from gui.script_editor import ScriptEditorDialog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-7s] %(message)s")
logger = logging.getLogger("GUI")

class TagTreeWidget(QTreeWidget):
    """Расширенное дерево с сигналом перемещения элементов"""
    orderChanged = pyqtSignal()
    
    def dropEvent(self, event):
        super().dropEvent(event)
        if event.isAccepted():
            self.orderChanged.emit()

class TagEditDialog(QDialog):
    def __init__(self, parent=None, tag_data: Optional[Dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Редактор тега" if tag_data else "Новый тег")
        self.setModal(True); self.resize(450, 420)
        self.tag_data = tag_data or {}
        layout = QFormLayout()

        self.name_edit = QLineEdit(self.tag_data.get("name", ""))
        self.path_edit = QLineEdit(self.tag_data.get("path", ""))
        self.path_edit.setPlaceholderText("Цех/Участок/Оборудование")
        self.source_combo = QComboBox()
        self.source_combo.addItems(["internal", "modbus_tcp"])
        self.source_combo.setCurrentText(self.tag_data.get("source", "internal"))
        self.address_edit = QLineEdit(self.tag_data.get("address", ""))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["float32", "bool", "int16", "uint16", "int32", "uint32", "string"])
        self.type_combo.setCurrentText(self.tag_data.get("type", "float32"))

        self.enabled_check = QCheckBox()
        self.enabled_check.setChecked(self.tag_data.get("enabled", True))

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.1, 3600)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSingleStep(0.1)
        iv = self.tag_data.get("poll_interval")
        self.interval_spin.setValue(iv if iv else 1.0)
        self.interval_spin.setSpecialValueText("По умолчанию (драйвер)")

        self.disconnect_edit = QLineEdit()
        d_val = self.tag_data.get("disconnect_value")
        self.disconnect_edit.setText(str(d_val) if d_val is not None else "")
        self.disconnect_edit.setPlaceholderText("Оставьте пустым для сохранения последнего значения")

        layout.addRow("Имя:", self.name_edit)
        layout.addRow("Путь:", self.path_edit)
        layout.addRow("Источник:", self.source_combo)
        layout.addRow("Адрес:", self.address_edit)
        layout.addRow("Тип:", self.type_combo)
        layout.addRow("✅ Отслеживание:", self.enabled_check)
        layout.addRow("⏱ Период опроса (с):", self.interval_spin)
        layout.addRow("⚡ Значение при дисконекте:", self.disconnect_edit)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("💾 Сохранить"); cancel_btn = QPushButton("❌ Отмена")
        save_btn.clicked.connect(self.accept); cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(save_btn); btn_layout.addWidget(cancel_btn)
        layout.addRow(btn_layout)
        self.setLayout(layout)

    def get_data(self) -> Dict[str, Any]:
        disc_str = self.disconnect_edit.text().strip()
        t = self.type_combo.currentText()
        disc_val = None
        if disc_str:
            try:
                if t == "bool": disc_val = disc_str.lower() in ("1", "true", "yes", "вкл", "on")
                elif t in ("int16", "uint16", "int32", "uint32"): disc_val = int(disc_str)
                elif t == "float32": disc_val = float(disc_str)
                else: disc_val = disc_str
            except ValueError:
                QMessageBox.warning(self, "Ошибка формата", "Неверный формат для значения при дисконекте")
                return {}
        
        interval = self.interval_spin.value()
        return {
            "name": self.name_edit.text().strip(),
            "path": self.path_edit.text().strip(),
            "source": self.source_combo.currentText(),
            "address": self.address_edit.text().strip(),
            "type": self.type_combo.currentText(),
            "enabled": self.enabled_check.isChecked(),
            "poll_interval": interval if interval > 0.1 else None,  # None = использовать драйвер
            "disconnect_value": disc_val
        }

class ServerBackend:
    def __init__(self, update_q, cmd_q, log_q, project_data: Dict):
        self.update_queue = update_q
        self.cmd_queue = cmd_q
        self.log_queue = log_q
        self.project = project_data
        self.registry = TagRegistry()
        self.lua_engine = LuaEngine(self.registry, log_queue=self.log_queue)
        self.running = False
        self._stop_event = threading.Event()
        self._thread = None
        # ❌ Убрали self._drivers = {} → драйверы теперь локальны для цикла запуска

    async def _run_async(self):
        # 1. Загрузка тегов
        tags_data = self.project.get("tags", [])
        valid_keys = {"name", "path", "source", "address", "type", "disconnect_value", "enabled", "poll_interval"}
        self.registry.tags = {
            t["name"]: Tag(**{k: v for k, v in t.items() if k in valid_keys})
            for t in tags_data if t.get("name")
        }
        logger.info(f"✅ Загружено тегов из проекта: {len(self.registry.tags)}")

        # 2. Lua
        script_content = self.project.get("script", {}).get("content", "")
        self.lua_engine.load_script(script_content)
        await self.lua_engine.start(interval=self.project.get("settings", {}).get("script_interval", 1.0))

        # 3. Драйверы (🔹 НОВЫЕ экземпляры при КАЖДОМ запуске!)
        drivers = []
        tasks = []
        drivers_cfg = self.project.get("drivers", [])
        for d in drivers_cfg:
            if d["type"] == "modbus_tcp":
                drv = ModbusTcpDriver(d)
                drivers.append(drv)
                await drv.connect()  # 🔹 Явное подключение до запуска опроса
                tasks.append(asyncio.create_task(drv.poll_loop(self.registry, d.get("poll_interval", 1.0))))
            elif d["type"] == "internal":
                drv = InternalDriver(d)
                drivers.append(drv)
                await drv.connect()
                tasks.append(asyncio.create_task(drv.poll_loop(self.registry, d.get("poll_interval", 10.0))))

        self.running = True
        try:
            # Основной цикл сервера
            while not self._stop_event.is_set():
                await self._process_commands()
                snapshot = {n: {"value": t.value, "quality": t.quality, "timestamp": t.timestamp} 
                            for n, t in self.registry.tags.items()}
                try: self.update_queue.put_nowait(snapshot)
                except queue.Full: pass
                await asyncio.sleep(1.0)
        finally:
            # 🔹 Гарантированная очистка при любом выходе из цикла
            await self.lua_engine.stop()
            for drv in drivers:
                try: await drv.disconnect()
                except Exception as e: logger.debug(f"Driver cleanup: {e}")
            self.running = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return  # Игнорируем повторные клики, пока поток жив
        self._stop_event.clear()
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
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.running = False

    async def _process_commands(self):
        while not self.cmd_queue.empty():
            try:
                cmd = self.cmd_queue.get_nowait()
                action = cmd["action"]
                data = cmd.get("data", {})
                valid_keys = {"name", "path", "source", "address", "type", "disconnect_value", "enabled", "poll_interval"}
                
                if action == "add_tag":
                    tag_kwargs = {k: data.get(k) for k in valid_keys}
                    self.registry.tags[data["name"]] = Tag(**tag_kwargs)
                    self.project.setdefault("tags", []).append(tag_kwargs)
                elif action == "update_tag":
                    old = data["old_name"]; new_data = data["new"]
                    new_kwargs = {k: new_data.get(k) for k in valid_keys}
                    self.registry.tags.pop(old, None); self.registry.tags[new_data["name"]] = Tag(**new_kwargs)
                    for i, t in enumerate(self.project["tags"]):
                        if t.get("name") == old: self.project["tags"][i] = new_kwargs; break
                elif action == "delete_tag":
                    self.registry.tags.pop(data["name"], None)
                    self.project["tags"] = [t for t in self.project["tags"] if t.get("name") != data["name"]]
                elif action == "reload_script":
                    self.lua_engine.reload(data.get("content", ""))
                self.cmd_queue.task_done()
            except queue.Empty:
                break

    def send_command(self, cmd: Dict): 
        self.cmd_queue.put_nowait(cmd)

class TagMonitor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OPC Server Manager")
        self.resize(1300, 800)
        self.proj_mgr = ProjectManager()
        self._setup_menu()
        self._init_ui()
        self._load_last_project()

    def _setup_menu(self):
        mb = self.menuBar()
        file_menu = mb.addMenu("📁 Файл")
        file_menu.addAction("🆕 Новый проект", self.new_project)
        file_menu.addAction("📂 Открыть...", self.open_project)
        file_menu.addAction("💾 Сохранить", self.save_project)
        file_menu.addAction("💾 Сохранить как...", self.save_as_project)
        file_menu.addSeparator()
        self.recent_menu = file_menu.addMenu("📜 Последние")
        self._update_recent_menu()
        file_menu.addSeparator()
        file_menu.addAction("🚪 Выход", self.close)

    def _update_recent_menu(self):
        self.recent_menu.clear()
        for p in self.proj_mgr.get_recent_projects():
            # Показываем только имя файла, но храним полный путь
            act = self.recent_menu.addAction(Path(p).name)
            act.triggered.connect(lambda _, path=p: self._load_project(Path(path)))

    def _init_ui(self):
        central = QWidget(); self.setCentralWidget(central); layout = QVBoxLayout(central)
        toolbar = QHBoxLayout()
        
        # 🔹 ОДНА КНОПКА-ПЕРЕКЛЮЧАТЕЛЬ
        self.btn_toggle = QPushButton("▶️ Запустить сервер")
        self.btn_toggle.setStyleSheet("background-color: #2ecc71; color: white; font-weight: bold; padding: 5px 10px; min-width: 150px;")
        self.btn_toggle.clicked.connect(self._toggle_server)
        toolbar.addWidget(self.btn_toggle)
        
        # Остальные кнопки
        for txt, slot in [("➕ Добавить", self._add_tag), ("✏️ Изменить", self._edit_tag),
                          ("🗑 Удалить", self._delete_tag), ("📝 Скрипты", self._open_script_editor),
                          ("🔄 Скрипт", self._reload_script_from_editor)]:
            b = QPushButton(txt); b.clicked.connect(slot); toolbar.addWidget(b)
            
        toolbar.addStretch()
        self.project_name_lbl = QLabel("📁 Проект: Не открыт")
        self.project_name_lbl.setStyleSheet("font-weight: bold; color: #0055aa; padding: 0 15px; font-size: 13px;")
        self.status_lbl = QLabel("⏳ Ожидание проекта...")
        toolbar.addWidget(self.project_name_lbl)
        toolbar.addWidget(self.status_lbl)
        layout.addLayout(toolbar)

        # 🔹 Кастомное дерево с поддержкой Drag & Drop
        self.tree = TagTreeWidget()
        self.tree.setHeaderLabels(["Тег", "Значение", "Качество", "Источник"])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(20)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSortingEnabled(False)  # ❌ Отключаем автосортировку
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        
        # Подключаем сигнал перемещения
        self.tree.orderChanged.connect(self._on_tree_order_changed)
        
        layout.addWidget(self.tree)

        self.update_q = queue.Queue(maxsize=2)
        self.cmd_q = queue.Queue()
        self.log_q = queue.Queue(maxsize=200)
        self.backend = None
        self._toggle_busy = False  # 🔹 Блокировка повторных кликов
        self.script_editor = None

        self.timer = QTimer(); self.timer.timeout.connect(self._poll_queue); self.timer.start(500)

    def _load_last_project(self):
        last = self.proj_mgr.get_last_project_path()
        if last and last.exists():
            self._load_project(last)
        else:
            self._new_empty_project()

    def _load_project(self, path: Path):
        try:
            proj = self.proj_mgr.load_project(path)
            self.status_lbl.setText(f"📂 Загружен: {path.name}")
            self._start_backend(proj)
            self._update_project_display()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить проект:\n{e}")
            self._new_empty_project()

    def _new_empty_project(self):
        self.status_lbl.setText("📄 Новый проект")
        self._start_backend({"project":{"name":"Новый проект"}, "drivers":[], "tags":[], "script":{"content":""}})
        self._update_project_display()

    def _start_backend(self, proj_data):
        if self.backend:
            self.backend.stop()
            import time; time.sleep(0.2)
        self.backend = ServerBackend(self.update_q, self.cmd_q, self.log_q, proj_data)
        
        self.script_editor = ScriptEditorDialog(proj_data, lambda c: self.backend.send_command({"action":"reload_script","data":{"content":c}}), self)
        
        # 🔹 Заполняем дерево сразу из proj_data (не ждем запуска бэкенда!)
        self._populate_tree_from_project_data(proj_data)
        
        self.btn_toggle.setEnabled(True)
        self.status_lbl.setText("📄 Проект загружен. Нажмите '▶️ Запустить сервер'")

    def _update_project_display(self):
        """Обновляет отображение имени и пути проекта в GUI"""
        if self.backend and self.backend.project:
            proj_meta = self.backend.project.get("project", {})
            name = proj_meta.get("name", "Без имени")
            path = self.proj_mgr.project_path
            if path:
                self.project_name_lbl.setText(f"📁 Проект: {name}  │  📂 {path.name}")
            else:
                self.project_name_lbl.setText(f"📁 Проект: {name}  │  ⚠️ Не сохранён")
        else:
            self.project_name_lbl.setText("📁 Проект: Не открыт")

    def _toggle_server(self):
        if self._toggle_busy or not self.backend:
            return
        self._toggle_busy = True
        self.btn_toggle.setEnabled(False)

        if self.backend.running:
            self.backend.stop()
            self.btn_toggle.setText("▶️ Остановка...")
            self.status_lbl.setText("⏹️ Остановка сервера...")
        else:
            self.backend.start()
            self.btn_toggle.setText("⏹️ Запуск...")
            self.status_lbl.setText("🟢 Запуск сервера...")

        # Разблокировка кнопки через 1.5 сек (время на join/старт потока)
        QTimer.singleShot(1500, self._on_toggle_finish)

    def _on_toggle_finish(self):
        self._toggle_busy = False
        if not self.backend: return
        self.btn_toggle.setEnabled(True)
        if self.backend.running:
            self.btn_toggle.setText("⏹️ Остановить сервер")
            self.btn_toggle.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold; padding: 5px 10px;")
            self.status_lbl.setText("🟢 Сервер запущен")
            self._tree_initialized = False
        else:
            self.btn_toggle.setText("▶️ Запустить сервер")
            self.btn_toggle.setStyleSheet("background-color: #2ecc71; color: white; font-weight: bold; padding: 5px 10px;")
            self.status_lbl.setText("⏹️ Сервер остановлен")

    def _reload_script_from_editor(self):
        if self.backend and self.script_editor:
            self.backend.send_command({"action":"reload_script","data":{"content": self.script_editor.editor.toPlainText()}})
            self.status_lbl.setText("🔄 Скрипт перезагружается...")        

    def new_project(self):
        self._new_empty_project()
        # Сразу предлагаем сохранить в папку проектов
        self.save_as_project()

    def open_project(self):
        start_dir = str(self.proj_mgr.PROJECTS_DIR)
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", start_dir, 
                                              "YAML Projects (*.yaml *.yml);;All Files (*)")
        if path: self._load_project(Path(path))

    def save_project(self): self._save_proj(self.proj_mgr.project_path)

    def save_as_project(self):
        start_dir = str(self.proj_mgr.PROJECTS_DIR)
        default_name = "новый_проект.yaml"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект как", 
                                              str(self.proj_mgr.PROJECTS_DIR / default_name), 
                                              "YAML Projects (*.yaml)")
        if path: self._save_proj(Path(path))

    def _save_proj(self, path: Optional[Path] = None):
        if not self.backend or not self.backend.project: return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", ".", "YAML Projects (*.yaml)")
            if not path: return; path = Path(path)
        if self.proj_mgr.save_project(path, self.backend.project):
            self.status_lbl.setText(f"💾 Сохранено: {path.name}")
            self._update_recent_menu()
            self._update_project_display()

    def _open_script_editor(self):
        if self.script_editor: self.script_editor.show()

    def _poll_queue(self):
        if not self.backend: return
        while not self.update_q.empty():
            try:
                snap = self.update_q.get_nowait(); self._update_tree(snap)
            except queue.Empty: break
        # Обновляем логи редактора
        if self.script_editor: self.script_editor.log_queue = self.log_q

    def _update_tree(self, snap: Dict):
        if not self._tree_initialized:
            self._save_gui_state()      # 🔹 Сохраняем ДО перестройки
            self.tree.clear()
            for n, d in snap.items():
                t = self.backend.registry.tags.get(n)
                path = t.path if t else "Без группы"
                parts = [p.strip() for p in path.split("/") if p.strip()]
                parent = self.tree.invisibleRootItem()
                for p in parts:
                    found = None
                    for i in range(parent.childCount()):
                        if parent.child(i).text(0) == p: found = parent.child(i); break
                    if not found:
                        found = QTreeWidgetItem([p, "", "", ""]); found.setExpanded(True)
                        for c in range(1,4): found.setForeground(c, QColor("#666666"))
                        parent.addChild(found)
                    parent = found
                item = QTreeWidgetItem([n, str(d['value']) if d['value'] is not None else "---", d['quality'], t.source if t else ""])
                if d['quality'] == "Good": item.setForeground(2, QColor("#008000"))
                elif d['quality'] == "Bad": item.setForeground(2, QColor("#cc0000"))
                parent.addChild(item)
            self._tree_initialized = True
            self._restore_gui_state()   # 🔹 Восстанавливаем ПОСЛЕ
        else:
            self._update_tree_recursive(self.tree.invisibleRootItem(), snap)
        self.status_lbl.setText(f"🟢 Тегов: {len(snap)}")

    def _update_tree_recursive(self, parent, snap):
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child.childCount() == 0:
                name = child.text(0)
                if name in snap:
                    child.setText(1, str(snap[name]['value']) if snap[name]['value'] is not None else "---")
                    child.setText(2, snap[name]['quality'])
                    if snap[name]['quality'] == "Good": child.setForeground(2, QColor("#008000"))
                    elif snap[name]['quality'] == "Bad": child.setForeground(2, QColor("#cc0000"))
            else: self._update_tree_recursive(child, snap)

    def _populate_tree_from_project_data(self, proj_data: Dict):
        self._save_gui_state()
        self.tree.clear()
        root = self.tree.invisibleRootItem()
        
        # 🔹 Сохраняем порядок появления путей (как в YAML), без сортировки
        seen_paths = []
        tags_by_path = {}
        for tag_data in proj_data.get("tags", []):
            name = tag_data.get("name")
            if not name: continue
            path = tag_data.get("path", '') or "Без группы"
            if path not in tags_by_path:
                tags_by_path[path] = []
                seen_paths.append(path)
            tags_by_path[path].append(tag_data)

        for path in seen_paths:  # ← Итерируем в порядке YAML
            tag_list = tags_by_path[path]
            parts = [p.strip() for p in path.split("/") if p.strip()]
            parent = root
            for p in parts:
                found = None
                for i in range(parent.childCount()):
                    if parent.child(i).text(0) == p: found = parent.child(i); break
                if not found:
                    found = QTreeWidgetItem([p, "", "", ""])
                    for c in range(1,4): found.setForeground(c, QColor("#666666"))
                    parent.addChild(found)
                parent = found

            for tag_data in tag_list:
                name = tag_data.get("name")
                val = tag_data.get("value")
                val_str = str(val) if val is not None else "---"
                quality = tag_data.get("quality", "Unknown")
                source = tag_data.get("source", "")
                item = QTreeWidgetItem([name, val_str, quality, source])
                if quality == "Good": item.setForeground(2, QColor("#008000"))
                elif quality == "Bad": item.setForeground(2, QColor("#cc0000"))
                parent.addChild(item)
                
        self._tree_initialized = True
        self._restore_gui_state()

    def _get_selected_tag(self):
        items = self.tree.selectedItems()
        return items[0].text(0) if items and items[0].childCount() == 0 else None

    def _add_tag(self):
        if not self.backend: return
        dlg = TagEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            if not data: return
            if not data["name"]: QMessageBox.warning(self, "Ошибка", "Имя тега не может быть пустым"); return
            self.backend.send_command({"action":"add_tag","data":data})
            self._tree_initialized = False

    def _edit_tag(self):
        if not self.backend: return
        name = self._get_selected_tag()
        if not name: QMessageBox.information(self, "Подсказка", "Выберите тег"); return
        t = self.backend.registry.tags.get(name)
        if not t: return
        dlg = TagEditDialog(self, {"name":t.name,"path":t.path,"source":t.source,"address":t.address,"type":t.type,"value":t.value})
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.backend.send_command({"action":"update_tag","data":{"old_name":name,"new":dlg.get_data()}})
            self._tree_initialized = False

    def _delete_tag(self):
        if not self.backend: return
        name = self._get_selected_tag()
        if not name: QMessageBox.information(self, "Подсказка", "Выберите тег"); return
        if QMessageBox.question(self, "Подтверждение", f"Удалить тег '{name}'?") == QMessageBox.StandardButton.Yes:
            self.backend.send_command({"action":"delete_tag","data":{"name":name}})
            self._tree_initialized = False

    def _on_tree_order_changed(self):
        """Вызывается после перетаскивания элемента в дереве"""
        reply = QMessageBox.question(
            self, "Перемещение тега",
            "Вы действительно хотите изменить порядок/иерархию тегов?\n"
            "Это автоматически обновит конфигурационный файл проекта.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._reorder_project_tags_from_tree()
            if self.proj_mgr.project_path:
                self.proj_mgr.save_current()
            self.status_lbl.setText("💾 Порядок тегов обновлён")
        else:
            # Отменяем визуальное перемещение, перезагружая дерево из конфига
            self._populate_tree_from_project_data(self.backend.project)
            self.status_lbl.setText("↩️ Перемещение отменено")

    def _reorder_project_tags_from_tree(self):
        """Синхронизирует порядок и пути тегов в проекте с текущим видом дерева"""
        if not self.backend or "tags" not in self.backend.project:
            return

        tags_lookup = {t["name"]: t for t in self.backend.project["tags"]}
        new_tags_list = []

        def traverse(parent, current_path_parts):
            for i in range(parent.childCount()):
                child = parent.child(i)
                name = child.text(0)
                if child.childCount() == 0:  # Это тег (лист)
                    if name in tags_lookup:
                        tag_data = tags_lookup[name].copy()
                        tag_data["path"] = "/".join(current_path_parts) if current_path_parts else ""
                        new_tags_list.append(tag_data)
                else:  # Это папка
                    traverse(child, current_path_parts + [name])

        traverse(self.tree.invisibleRootItem(), [])
        self.backend.project["tags"] = new_tags_list
        self._tree_initialized = False  # Принудительное обновление при следующем снимке

    def closeEvent(self, event):
        self._save_gui_state()          # 🔹 Сохраняем состояние перед выходом
        if self.backend and self.backend.running:
            self.backend.stop()
        logger.info("🛑 Приложение закрыто."); event.accept()

    def _get_expanded_paths(self):
        """Собирает пути всех развёрнутых узлов дерева"""
        paths = []
        def traverse(parent, current_path):
            for i in range(parent.childCount()):
                child = parent.child(i)
                name = child.text(0)
                if not name: continue
                path = current_path + [name]
                if child.isExpanded():
                    paths.append(path)
                traverse(child, path)
        traverse(self.tree.invisibleRootItem(), [])
        return paths

    def _save_gui_state(self):
        """Сохраняет состояние дерева в проект (и тихо на диск)"""
        if not self.backend or not self.backend.project: return
        self.backend.project.setdefault("gui_state", {})["tree_expanded"] = self._get_expanded_paths()
        # Тихое сохранение, если проект уже существует на диске
        if self.proj_mgr.project_path and self.proj_mgr.project_path.exists():
            self.proj_mgr.save_current()

    def _restore_gui_state(self):
        """Разворачивает узлы дерева по сохранённым путям"""
        if not self.backend: return
        expanded_paths = self.backend.project.get("gui_state", {}).get("tree_expanded", [])
        for path in expanded_paths:
            parent = self.tree.invisibleRootItem()
            found = True
            for part in path:
                found = False
                for i in range(parent.childCount()):
                    if parent.child(i).text(0) == part:
                        parent = parent.child(i)
                        found = True
                        break
                if not found: break
            if found and parent is not self.tree.invisibleRootItem():
                parent.setExpanded(True)

if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion")
    window = TagMonitor(); window.show()
    sys.exit(app.exec())