"""Проект приложения: части в parts/, состояние в project.json, отчёт PROJECT.md, сборка в dist/.

Имя части: NN-имя.ext, где NN — порядок (10, 20, 30...), ext — html | head.html | css | js.
Части .head.html попадают в <head>, .html — в <body>, .css — в <style>, .js — в <script>.

Состояние проекта хранится на диске, а не в истории диалога: каждый запрос к модели
собирается заново и остаётся маленьким, сколько бы правок ни сделали.
"""
import datetime as dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace"
BUILDER = Path(__file__).resolve().parent
LAST = BUILDER / "last_project.txt"

NAME_RE = re.compile(r"^(\d{2})-[A-Za-z0-9_\-]+\.(?:head\.html|html|css|js)$")
SIG_RE = re.compile(
    r"^(?:async\s+)?function\s+\w+|^const\s+\w+\s*=|^let\s+\w+\s*=|^class\s+\w+", re.M)
ID_RE = re.compile(r"id=[\"']([\w\-]+)[\"']")
USED_ID_RE = re.compile(
    r"getElementById\(\s*[\"']([\w\-]+)[\"']\s*\)|querySelector(?:All)?\(\s*[\"']#([\w\-]+)")


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def sanitize_name(raw: str, order_hint: int) -> str:
    """Привести имя части к виду NN-имя.ext (модели иногда пишут просто «logic.js»
    или копируют шаблон «имя.ext» буквально)."""
    raw = (raw or "").strip().strip("`\"'").replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[^\w.\-]", "-", raw)
    # мусорные суффиксы вроде «.ext», скопированные из шаблона
    raw = re.sub(r"\.(ext|extension|example|файл|имя)$", "", raw, flags=re.I)
    m_ext = re.search(r"(head\.html|html|css|js)$", raw)
    ext = m_ext.group(0) if m_ext else ""
    body = (raw[:m_ext.start()] if m_ext else raw).strip(".-")
    m = re.match(r"^(\d{2})-(.+)$", body)
    if m:
        body = m.group(2)
        num = m.group(1)
    else:
        num = f"{order_hint:02d}"
    body = body or "part"
    return f"{num}-{body}.{ext or 'js'}"


def guess_ext(desc: str) -> str:
    """Расширение по смыслу описания, когда модель его не указала."""
    d = (desc or "").lower()
    if "размет" in d or "верст" in d or "html" in d:
        return "html"
    if "стил" in d or "css" in d or "оформлен" in d:
        return "css"
    return "js"


class Project:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.parts_dir = self.root / "parts"
        self.dist_dir = self.root / "dist"
        self.state_file = self.root / "project.json"
        self.state = {"name": self.root.name, "title": self.root.name, "task": "",
                      "plan": [], "done": [], "notes": []}
        if self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text(encoding="utf-8")))

    # --- создание и поиск ---

    @classmethod
    def create(cls, name: str, task: str, title: str = "") -> "Project":
        root = WORKSPACE / name
        i = 2
        while root.exists():
            root = WORKSPACE / f"{name}-{i}"
            i += 1
        root.mkdir(parents=True)
        p = cls(root)
        p.state = {"name": name, "title": title or name, "task": task,
                   "plan": [], "done": [], "notes": []}
        p.parts_dir.mkdir(exist_ok=True)
        p.dist_dir.mkdir(exist_ok=True)
        p.save()
        p.note("проект создан")
        return p

    @classmethod
    def last(cls) -> "Project":
        if LAST.exists():
            root = Path(LAST.read_text(encoding="utf-8").strip())
            if (root / "project.json").exists():
                return cls(root)
        # fallback: самый свежий проект в workspace
        cands = sorted(WORKSPACE.glob("*/project.json"), key=lambda p: p.stat().st_mtime)
        if not cands:
            raise SystemExit("Проект не найден. Создайте: python builder/agent.py new \"задача\" --name app")
        return cls(cands[-1].parent)

    def remember(self):
        LAST.write_text(str(self.root), encoding="utf-8")

    # --- части ---

    def part_path(self, name: str) -> Path:
        if not valid_name(name):
            raise ValueError(f"недопустимое имя части: {name} (нужно NN-имя.ext)")
        p = (self.parts_dir / name).resolve()
        if self.parts_dir.resolve() not in p.parents:
            raise ValueError(f"путь вне папки частей: {name}")
        return p

    def parts(self):
        if not self.parts_dir.exists():
            return []
        files = [f for f in self.parts_dir.iterdir() if f.is_file() and valid_name(f.name)]
        return sorted(files, key=lambda f: f.name)

    def part_lines(self, name: str) -> int:
        p = self.parts_dir / name
        if not p.exists():
            return 0
        return len(p.read_text(encoding="utf-8", errors="replace").splitlines())

    def write_part(self, name: str, content: str):
        p = self.part_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.rstrip() + "\n", encoding="utf-8")
        if name not in self.state["done"]:
            self.state["done"].append(name)
        self.save()

    def read_part(self, name: str) -> str:
        return self.part_path(name).read_text(encoding="utf-8", errors="replace")

    def interfaces(self, max_lines: int = 70) -> str:
        """Краткая сводка уже написанного: сигнатуры JS и id из HTML."""
        out = []
        ids = []
        for f in self.parts():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f.name.endswith(".js"):
                sigs = SIG_RE.findall(text)
                if sigs:
                    out.append(f"{f.name}: " + "; ".join(s.strip()[:70] for s in sigs[:12]))
            elif f.name.endswith(".html"):
                ids += ID_RE.findall(text)
        if ids:
            out.append("id в HTML: " + ", ".join(dict.fromkeys(ids)))
        return "\n".join(out[:max_lines])

    # --- состояние ---

    def save(self):
        self.state_file.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        self.remember()

    def note(self, text: str):
        self.state["notes"] = (self.state["notes"] or [])[-49:] + [
            f"{dt.datetime.now():%Y-%m-%d %H:%M} {text}"]
        self.render_project_md()

    def render_project_md(self):
        lines = [f"# {self.state['title']}", "",
                 f"Задача: {self.state['task']}", "",
                 "Сборка: BUILD_OPEN.bat или `python builder/build.py`", "", "## Части", ""]
        planned = {p["name"] for p in self.state["plan"]}
        for p in self.state["plan"]:
            n = self.part_lines(p["name"])
            mark = "x" if n else " "
            size = f" ({n} стр.)" if n else ""
            lines.append(f"- [{mark}] {p['name']} — {p.get('desc', '')}{size}")
            if n:
                self.state["done"] = sorted(set(self.state["done"]) | {p["name"]})
        for f in self.parts():
            if f.name not in planned:
                lines.append(f"- [x] {f.name} — (вне плана, {self.part_lines(f.name)} стр.)")
        lines += ["", "## Заметки", ""]
        lines += [f"- {n}" for n in self.state["notes"][-30:]]
        (self.root / "PROJECT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
