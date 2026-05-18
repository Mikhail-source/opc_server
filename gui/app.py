# gui/app.py
import sys, asyncio, logging, threading, queue, time
from pathlib import Path
from typing import Dict, Any, Optional

from PyQt6.QtWidgets import (QApplication, QMainWindow, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QHBoxLayout, QWidget, QHeaderView, QLabel,
                             QPushButton, QDialog, QFormLayout, QLineEdit, QComboBox,
                             QMessageBox, QAbstractItemView, QFileDialog, QMenu, QMenuBar,
                             QCheckBox, QDoubleSpinBox)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor

from core.tag_registry import TagRegistry, ConfigWatcher
from drivers.modbus_tcp import ModbusTcpDriver
from drivers.internal import InternalDriver
from core.scripting import LuaEngine
from core.project_manager import ProjectManager
from shared.models import Tag
from gui.script_editor import ScriptEditorDialog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-7s] %(message)s")
logger = logging.getLogger("GUI")

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
        self.update_queue, self.cmd_queue, self.log_queue = update_q, cmd_q, log_q
        self.project = project_data
        self.registry = TagRegistry()
        self.lua_engine = LuaEngine(self.registry, log_queue=self.log_queue)
        self.running = False; self._stop_event = threading.Event()

    async def _run_async(self):
        # 1. Загрузка тегов из проекта
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

        # 3. Драйверы
        drivers_cfg = self.project.get("drivers", [])
        tasks = []
        for d in drivers_cfg:
            if d["type"] == "modbus_tcp":
                drv = ModbusTcpDriver(d); await drv.connect()
                tasks.append(asyncio.create_task(drv.poll_loop(self.registry, d.get("poll_interval", 1.0))))
            elif d["type"] == "internal":
                drv = InternalDriver(d); await drv.connect()
                tasks.append(asyncio.create_task(drv.poll_loop(self.registry, d.get("poll_interval", 10.0))))

        self.running = True
        while not self._stop_event.is_set():
            await self._process_commands()
            snapshot = {n: {"value": t.value, "quality": t.quality, "timestamp": t.timestamp} for n, t in self.registry.tags.items()}
            try: self.update_queue.put_nowait(snapshot)
            except queue.Full: pass
            await asyncio.sleep(1.0)

        await self.lua_engine.stop()

    async def _process_commands(self):
        while not self.cmd_queue.empty():
            try:
                cmd = self.cmd_queue.get_nowait(); action = cmd["action"]; data = cmd.get("data", {})
                valid_keys = {"name", "path", "source", "address", "type", "disconnect_value", "enabled", "poll_interval"}
                
                if action == "add_tag":
                    tag_kwargs = {k: data.get(k) for k in valid_keys}
                    self.registry.tags[data["name"]] = Tag(**tag_kwargs)
                    self.project.setdefault("tags", []).append(tag_kwargs)
                    
                elif action == "update_tag":
                    old = data["old_name"]; new_data = data["new"]
                    new_kwargs = {k: new_data.get(k) for k in valid_keys}
                    self.registry.tags.pop(old, None)
                    self.registry.tags[new_data["name"]] = Tag(**new_kwargs)
                    for i, t in enumerate(self.project["tags"]):
                        if t.get("name") == old: self.project["tags"][i] = new_kwargs; break
                        
                elif action == "delete_tag":
                    self.registry.tags.pop(data["name"], None)
                    self.project["tags"] = [t for t in self.project["tags"] if t.get("name") != data["name"]]
                    
                elif action == "reload_script":
                    self.lua_engine.reload(data.get("content", ""))
                    
                self.cmd_queue.task_done()
            except queue.Empty: break

    def start(self):
        threading.Thread(target=self._run_threaded, daemon=True).start()
    def _run_threaded(self):
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        try: loop.run_until_complete(self._run_async())
        except Exception as e: logger.error(f"Backend error: {e}")
        finally: loop.close()
    def stop(self): self._stop_event.set(); self.running = False
    def send_command(self, cmd: Dict): self.cmd_queue.put_nowait(cmd)

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
        for txt, slot in [("➕ Добавить", self._add_tag), ("✏️ Изменить", self._edit_tag),
                          ("🗑 Удалить", self._delete_tag), ("📝 Скрипты", lambda: self._open_script_editor()),
                          ("🔄 Перезагрузить скрипт", lambda: self.backend.send_command({"action":"reload_script","data":{"content": self.script_editor.editor.toPlainText()}}))]:
            b = QPushButton(txt); b.clicked.connect(slot); toolbar.addWidget(b)
        toolbar.addStretch()

        self.project_name_lbl = QLabel("📁 Проект: Не открыт")
        self.project_name_lbl.setStyleSheet("font-weight: bold; color: #0055aa; padding: 0 15px; font-size: 13px;")
        toolbar.addWidget(self.project_name_lbl)

        self.status_lbl = QLabel("⏳ Ожидание проекта..."); toolbar.addWidget(self.status_lbl)
        layout.addLayout(toolbar)

        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["Тег", "Значение", "Качество", "Источник"])
        self.tree.setAlternatingRowColors(True); self.tree.setAnimated(True); self.tree.setIndentation(20)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.tree)

        self.update_q = queue.Queue(maxsize=2)
        self.cmd_q = queue.Queue()
        self.log_q = queue.Queue(maxsize=200)
        self.backend = None
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
        if self.backend: self.backend.stop()
        self.backend = ServerBackend(self.update_q, self.cmd_q, self.log_q, proj_data)
        self.backend.start()
        self.script_editor = ScriptEditorDialog(proj_data, lambda c: self.backend.send_command({"action":"reload_script","data":{"content":c}}), self)
        self._tree_initialized = False

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

    def closeEvent(self, event):
        if self.backend: self.backend.stop()
        logger.info("🛑 Сервер остановлен."); event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion")
    window = TagMonitor(); window.show()
    sys.exit(app.exec())