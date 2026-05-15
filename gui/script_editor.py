import re
from pathlib import Path
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
                             QPushButton, QLabel, QFileDialog, QMessageBox)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont

class LuaHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rules = []

        # Ключевые слова
        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor("#cc7832"))
        kw_fmt.setFontWeight(QFont.Weight.Bold)
        for kw in ["and", "break", "do", "else", "elseif", "end", "false", "for",
                   "function", "goto", "if", "in", "local", "nil", "not", "or",
                   "repeat", "return", "then", "true", "until", "while"]:
            self.rules.append((re.compile(rf'\b{kw}\b'), kw_fmt))

        # Строки
        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor("#6a8759"))
        self.rules.append((re.compile(r'"[^"\\]*(\\.[^"\\]*)*"'), str_fmt))
        self.rules.append((re.compile(r"'[^'\\]*(\\.[^'\\]*)*'"), str_fmt))

        # Комментарии
        com_fmt = QTextCharFormat()
        com_fmt.setForeground(QColor("#808080"))
        self.rules.append((re.compile(r'--[^\n]*'), com_fmt))

        # Числа
        num_fmt = QTextCharFormat()
        num_fmt.setForeground(QColor("#6897bb"))
        self.rules.append((re.compile(r'\b[+-]?[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?\b'), num_fmt))

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


class ScriptEditorDialog(QDialog):
    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle("📝 Редактор Lua-скриптов")
        self.resize(900, 700)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.script_path = Path("scripts/main.lua")

        # --- Layout ---
        layout = QVBoxLayout(self)
        
        # Toolbar
        tb = QHBoxLayout()
        self.btn_load = QPushButton("📂 Загрузить")
        self.btn_save = QPushButton("💾 Сохранить")
        self.btn_apply = QPushButton("🔄 Применить")
        self.btn_clear = QPushButton("🗑 Очистить логи")
        self.status_lbl = QLabel("⏳ Готов")
        tb.addWidget(self.btn_load)
        tb.addWidget(self.btn_save)
        tb.addWidget(self.btn_apply)
        tb.addStretch()
        tb.addWidget(self.btn_clear)
        tb.addWidget(self.status_lbl)
        layout.addLayout(tb)

        # Editor
        self.editor = QPlainTextEdit()
        self.editor.setTabStopDistance(40)
        self.editor.setPlaceholderText("local temp = tag_get('Temp_Reactor')\nif temp > 80 then\n  tag_set('Alarm', true)\nend")
        LuaHighlighter(self.editor.document())
        layout.addWidget(self.editor)

        # Logs
        self.log_panel = QPlainTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setMaximumHeight(200)
        self.log_panel.setPlaceholderText("📊 Логи выполнения скрипта...")
        layout.addWidget(self.log_panel)

        # Connect
        self.btn_load.clicked.connect(self.load_script)
        self.btn_save.clicked.connect(self.save_script)
        self.btn_apply.clicked.connect(self.apply_script)
        self.btn_clear.clicked.connect(lambda: self.log_panel.clear())

        # Auto-load on open
        self.load_script()

        # Log polling
        self.timer = QTimer()
        self.timer.timeout.connect(self.poll_logs)
        self.timer.start(300)

    def load_script(self):
        path, _ = QFileDialog.getOpenFileName(self, "Загрузить скрипт", str(self.script_path), "Lua Files (*.lua);;All Files (*)")
        if path:
            self.script_path = Path(path)
            self.editor.setPlainText(self.script_path.read_text(encoding="utf-8"))
            self.status_lbl.setText(f"📂 {self.script_path.name}")

    def save_script(self):
        try:
            self.script_path.parent.mkdir(parents=True, exist_ok=True)
            self.script_path.write_text(self.editor.toPlainText(), encoding="utf-8")
            self.status_lbl.setText("💾 Сохранено")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка сохранения", str(e))

    def apply_script(self):
        self.save_script()
        self.backend.send_command({"action": "reload_script", "data": {"path": str(self.script_path)}})
        self.status_lbl.setText("🔄 Перезагружается...")

    def poll_logs(self):
        while not self.backend.log_queue.empty():
            try:
                msg = self.backend.log_queue.get_nowait()
                self.log_panel.appendPlainText(f"[{self.backend.log_queue.queue.qsize()}] {msg}")
                self.log_panel.verticalScrollBar().setValue(self.log_panel.verticalScrollBar().maximum())
            except:
                break