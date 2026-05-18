# gui/script_editor.py
import re
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
                             QPushButton, QLabel, QMessageBox)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont

class LuaHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rules = []
        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor("#cc7832"))
        kw_fmt.setFontWeight(QFont.Weight.Bold)
        for kw in ["and", "break", "do", "else", "elseif", "end", "false", "for",
                   "function", "goto", "if", "in", "local", "nil", "not", "or",
                   "repeat", "return", "then", "true", "until", "while"]:
            self.rules.append((re.compile(rf'\b{kw}\b'), kw_fmt))
        str_fmt = QTextCharFormat(); str_fmt.setForeground(QColor("#6a8759"))
        self.rules.append((re.compile(r'"[^"\\]*(\\.[^"\\]*)*"'), str_fmt))
        self.rules.append((re.compile(r"'[^'\\]*(\\.[^'\\]*)*'"), str_fmt))
        com_fmt = QTextCharFormat(); com_fmt.setForeground(QColor("#808080"))
        self.rules.append((re.compile(r'--[^\n]*'), com_fmt))
        num_fmt = QTextCharFormat(); num_fmt.setForeground(QColor("#6897bb"))
        self.rules.append((re.compile(r'\b[+-]?[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?\b'), num_fmt))

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)

class ScriptEditorDialog(QDialog):
    def __init__(self, project_data, on_apply, parent=None):
        super().__init__(parent)
        self.project_data = project_data
        self.on_apply = on_apply
        self.setWindowTitle("📝 Редактор Lua-скриптов")
        self.resize(900, 700)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)

        layout = QVBoxLayout(self)
        tb = QHBoxLayout()
        self.btn_save = QPushButton("💾 Сохранить в проект")
        self.btn_apply = QPushButton("🔄 Применить")
        self.btn_clear = QPushButton("🗑 Очистить логи")
        self.status_lbl = QLabel("⏳ Готов")
        for b in (self.btn_save, self.btn_apply, self.btn_clear): tb.addWidget(b)
        tb.addStretch(); tb.addWidget(self.status_lbl)
        layout.addLayout(tb)

        self.editor = QPlainTextEdit()
        self.editor.setTabStopDistance(40)
        self.editor.setPlaceholderText("local temp = tag_get('Temp_Reactor')\nif temp > 80 then\n  tag_set('Alarm', true)\nend")
        LuaHighlighter(self.editor.document())
        layout.addWidget(self.editor)

        self.log_panel = QPlainTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setMaximumHeight(200)
        self.log_panel.setPlaceholderText("📊 Логи выполнения...")
        layout.addWidget(self.log_panel)

        self.btn_save.clicked.connect(self._save_to_project)
        self.btn_apply.clicked.connect(self._apply_script)
        self.btn_clear.clicked.connect(self.log_panel.clear)

        # Загрузка из проекта
        self._load_from_project()

        self.timer = QTimer()
        self.timer.timeout.connect(self._poll_logs)
        self.timer.start(300)

    def _load_from_project(self):
        script = self.project_data.get("script", {})
        content = script.get("content", "") if isinstance(script, dict) else ""
        self.editor.setPlainText(content)
        self.status_lbl.setText("📄 Загружено из проекта")

    def _save_to_project(self):
        self.project_data.setdefault("script", {})["content"] = self.editor.toPlainText()
        self.status_lbl.setText("💾 Сохранено в проект")

    def _apply_script(self):
        self._save_to_project()
        if self.on_apply:
            self.on_apply(self.editor.toPlainText())
        self.status_lbl.setText("🔄 Перезагружается...")

    def _poll_logs(self):
        if hasattr(self, "log_queue") and self.log_queue:
            while not self.log_queue.empty():
                try:
                    msg = self.log_queue.get_nowait()
                    self.log_panel.appendPlainText(f"[{self.log_queue.qsize()}] {msg}")
                    self.log_panel.verticalScrollBar().setValue(self.log_panel.verticalScrollBar().maximum())
                except: break