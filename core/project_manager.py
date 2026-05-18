# core/project_manager.py
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

class ProjectManager:
    # Папка конфигурации приложения (кэш, логи, история)
    APP_DIR = Path.home() / ".opc_server"
    
    # 📁 Отдельная папка для хранения проектов (создаётся рядом с кодом)
    PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"
    
    LAST_PROJECT_FILE = APP_DIR / "last_project.txt"
    RECENT_PROJECTS_FILE = APP_DIR / "recent_projects.txt"
    MAX_RECENT = 5

    def __init__(self):
        self.APP_DIR.mkdir(parents=True, exist_ok=True)
        self.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)  # Авто-создание
        self.current_project: Optional[Dict[str, Any]] = None
        self.project_path: Optional[Path] = None

    def load_project(self, path: Path) -> Dict[str, Any]:
        path = Path(path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Проект не найден: {path}")
        with open(path, "r", encoding="utf-8") as f:
            proj = yaml.safe_load(f) or {}
        self.current_project = proj
        self.project_path = path
        self._update_last_project(path)
        return proj

    def save_project(self, path: Path, data: Dict[str, Any]) -> bool:
        try:
            path = Path(path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            meta = data.setdefault("project", {})
            meta["last_modified"] = datetime.now().isoformat()
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            self.current_project = data
            self.project_path = path
            self._update_last_project(path)
            return True
        except Exception as e:
            print(f"❌ Ошибка сохранения проекта: {e}")
            return False

    def save_current(self) -> bool:
        if self.project_path and self.current_project:
            return self.save_project(self.project_path, self.current_project)
        return False

    def _update_last_project(self, path: Path):
        with open(self.LAST_PROJECT_FILE, "w", encoding="utf-8") as f:
            f.write(str(path))
        self._update_recent(path)

    def _update_recent(self, path: Path):
        recents = self.get_recent_projects()
        path_str = str(path)
        if path_str in recents: recents.remove(path_str)
        recents.insert(0, path_str)
        with open(self.RECENT_PROJECTS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(recents[:self.MAX_RECENT]))

    def get_recent_projects(self) -> List[str]:
        if not self.RECENT_PROJECTS_FILE.exists(): return []
        return [p.strip() for p in self.RECENT_PROJECTS_FILE.read_text().splitlines() if p.strip()]

    def get_last_project_path(self) -> Optional[Path]:
        if self.LAST_PROJECT_FILE.exists():
            p = Path(self.LAST_PROJECT_FILE.read_text().strip())
            return p if p.exists() else None
        return None