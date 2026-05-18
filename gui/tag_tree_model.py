# gui/tag_tree_model.py
from PyQt6.QtCore import Qt, QAbstractItemModel, QModelIndex, QVariant
from PyQt6.QtGui import QStandardItemModel, QStandardItem
from typing import Dict, List, Any

class TagTreeModel:
    """
    Создаёт иерархическую структуру из плоского списка тегов.
    Формат: Цех → Участок → Оборудование → [Теги]
    """
    def __init__(self):
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Тег", "Значение", "Качество", "Источник"])
        self._items_cache: Dict[str, QStandardItem] = {}

    def clear(self):
        self.model.clear()
        self.model.setHorizontalHeaderLabels(["Тег", "Значение", "Качество", "Источник"])
        self._items_cache.clear()

    def _get_or_create_item(self, path_parts: List[str]) -> QStandardItem:
        """Создаёт или возвращает существующий элемент по пути"""
        if not path_parts:
            return self.model.invisibleRootItem()

        parent = self.model.invisibleRootItem()
        current_path = ""

        for i, part in enumerate(path_parts):
            current_path = f"{current_path}/{part}" if current_path else part
            
            # Ищем существующий элемент
            found = None
            for row in range(parent.rowCount()):
                child = parent.child(row, 0)
                if child.text() == part:
                    found = child
                    break

            if not found:
                # Создаём новый
                found = QStandardItem(part)
                found.setEditable(False)
                if i == len(path_parts) - 1:
                    # Листовой элемент (оборудование или тег)
                    found.setData("leaf", Qt.ItemDataRole.UserRole)
                else:
                    # Папка
                    found.setData("folder", Qt.ItemDataRole.UserRole)
                    # Делаем шрифт жирным для папок
                    font = found.font()
                    font.setBold(True)
                    found.setFont(font)
                parent.appendRow(found)

            parent = found

        return parent

    def add_tag(self, name: str, path: str, value: Any, quality: str, source: str):
        """Добавляет тег в дерево с защитой от некорректных типов"""
        # Гарантируем, что path — строка
        path = str(path).strip() if path else ""
        
        # Разбиваем путь на части, фильтруем пустые
        path_parts = [p.strip() for p in path.split("/") if p.strip()] if path else []
    
        # Последняя часть пути — это "оборудование" или группа, тег будет внутри
        parent = self._get_or_create_item(path_parts)
        
        # Создаём элементы для тега
        name_item = QStandardItem(name)
        name_item.setEditable(False)
        name_item.setData("tag", Qt.ItemDataRole.UserRole)
        name_item.setData(name, Qt.ItemDataRole.UserRole + 1)  # Имя тега для поиска

        value_item = QStandardItem(str(value) if value is not None else "---")
        value_item.setEditable(False)
        
        qual_item = QStandardItem(quality)
        qual_item.setEditable(False)
        if quality == "Good":
            qual_item.setForeground(Qt.GlobalColor.darkGreen)
        elif quality == "Bad":
            qual_item.setForeground(Qt.GlobalColor.red)

        source_item = QStandardItem(source)
        source_item.setEditable(False)

        parent.appendRow([name_item, value_item, qual_item, source_item])

    def update_tag(self, name: str, value: Any, quality: str):
        """Обновляет значение тега в дереве"""
        # Ищем тег по всему дереву
        root = self.model.invisibleRootItem()
        self._update_tag_recursive(root, name, value, quality)

    def _update_tag_recursive(self, parent: QStandardItem, name: str, value: Any, quality: str):
        for row in range(parent.rowCount()):
            item = parent.child(row, 0)
            if item.data(Qt.ItemDataRole.UserRole) == "tag" and item.data(Qt.ItemDataRole.UserRole + 1) == name:
                # Обновляем значение
                value_item = parent.child(row, 1)
                qual_item = parent.child(row, 2)
                if value_item:
                    value_item.setText(str(value) if value is not None else "---")
                if qual_item:
                    qual_item.setText(quality)
                    if quality == "Good":
                        qual_item.setForeground(Qt.GlobalColor.darkGreen)
                    elif quality == "Bad":
                        qual_item.setForeground(Qt.GlobalColor.red)
                return
            # Рекурсивный поиск в подпапках
            if item.rowCount() > 0:
                self._update_tag_recursive(item, name, value, quality)

    def expand_all(self, view):
        """Раскрывает все узлы дерева"""
        view.expandAll()
        # Опционально: свернуть верхний уровень
        # for i in range(self.model.rowCount()):
        #     view.collapse(self.model.index(i, 0))