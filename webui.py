"""Локальный веб-интерфейс для builder-агента.

Запуск:  python builder/webui.py  (или START_AGENT_UI.bat)
Откроется на http://127.0.0.1:8090

Только стандартная библиотека. Задачи агента (new/step/fix/part) запускает
подпроцессами — по одной за раз (GPU всё равно один), лог пишется в logs/uijob_*.log
и отдаётся в интерфейс. Чтение проектов и проверки — прямо в этом процессе.
"""
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build import build  # noqa: E402
from check import check_project  # noqa: E402
from project import Project, WORKSPACE  # noqa: E402

PORT = 8090
HTML_FILE = pathlib.Path(__file__).resolve().parent / "agent_ui.html"
LOG_DIR = ROOT / "logs"

_job_lock = threading.Lock()
_job = None  # {"proc", "log_path", "title", "started", "file"}


def start_job(args, title):
    global _job
    with _job_lock:
        if _job and _job["proc"].poll() is None:
            return None, "задача уже выполняется, дождитесь окончания"
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"uijob_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
        f = open(log_path, "w", encoding="utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(
            [sys.executable, *args], cwd=str(ROOT),
            stdout=f, stderr=subprocess.STDOUT, env=env)
        _job = {"proc": proc, "log_path": str(log_path), "file": f,
                "title": title, "started": time.time()}
    return _job, None


def job_status():
    if not _job:
        return {"running": False}
    code = _job["proc"].poll()
    running = code is None
    tail = ""
    try:
        _job["file"].flush()
        with open(_job["log_path"], "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-7000:]
    except OSError:
        pass
    return {"running": running, "exit": code, "title": _job["title"],
            "started": _job["started"], "log": tail,
            "log_path": _job["log_path"]}


def projects_list():
    out = []
    if WORKSPACE.exists():
        for pj in sorted(WORKSPACE.glob("*/project.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                st = json.loads(pj.read_text(encoding="utf-8"))
                parts = [f.name for f in pj.parent.glob("parts/*") if f.is_file()]
                out.append({"path": str(pj.parent), "name": st.get("name", pj.parent.name),
                            "task": st.get("task", ""), "parts": len(parts),
                            "mtime": pj.stat().st_mtime})
            except (OSError, ValueError):
                continue
    return out


def project_details(path):
    proj = Project(pathlib.Path(path))
    st = proj.state
    errs = check_project(proj)
    dist = proj.dist_dir / "index.html"
    # уроки для перезапуска: из состояния или из последней заметки о перезапуске
    lessons = st.get("lessons", "")
    if not lessons:
        for n in reversed(st.get("notes", [])):
            if "ПЕРЕЗАПУСК" in n and "Уроки:" in n:
                lessons = n.split("Уроки:", 1)[1].strip()
                break
    name = st.get("name") or proj.root.name
    restart_name = f"{name}-v{(proj.root.name + '-').split('-')[-1] or ''}".rstrip("-")
    m = re.match(r"^(.*?)-v(\d+)$", name)
    restart_name = f"{m.group(1)}-v{int(m.group(2)) + 1}" if m else f"{name}-v2"
    return {
        "path": str(proj.root), "name": name, "title": st.get("title"),
        "task": st.get("task"),
        "lessons": lessons,
        "restart_name": restart_name,
        "plan": [{"name": p["name"], "desc": p.get("desc", ""),
                  "lines": proj.part_lines(p["name"])} for p in st.get("plan", [])],
        "extra_parts": [{"name": f.name, "lines": proj.part_lines(f.name)}
                        for f in proj.parts()
                        if f.name not in {p["name"] for p in st.get("plan", [])}],
        "notes": st.get("notes", [])[-8:],
        "errors": errs,
        "dist": str(dist) if dist.exists() else None,
        "dist_lines": (len(dist.read_text(encoding="utf-8").splitlines())
                       if dist.exists() else 0),
    }


def llm_health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def settings_args(s, base):
    """Общие опции из настроек интерфейса -> аргументы CLI агента."""
    args = list(base)
    if s.get("temperature") is not None:
        args += ["--temperature", str(s["temperature"])]
    if s.get("cooldown"):
        args += ["--cooldown", str(s["cooldown"])]
    if s.get("maxTokens"):
        args += ["--max-tokens", str(int(s["maxTokens"]))]
    return args


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, data, ctype="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/" or url.path == "/index.html":
                self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/projects":
                self._send(200, {"projects": projects_list(), "llm": llm_health()})
            elif url.path == "/api/project":
                self._send(200, project_details(parse_qs(url.query)["path"][0]))
            elif url.path == "/api/job":
                self._send(200, job_status())
            elif url.path == "/api/llm_health":
                self._send(200, {"ok": llm_health()})
            else:
                self._send(404, {"error": "нет такого пути"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            b = self._body()
            s = b.get("settings", {})
            if url.path == "/api/new":
                task = str(b.get("task", "")).strip()
                if len(task) < 5:
                    return self._send(400, {"error": "опишите задачу подробнее"})
                name = str(b.get("name", "")).strip() or \
                    f"app-{dt.datetime.now():%m%d_%H%M}"
                args = settings_args(s, ["builder/agent.py", "new", task, "--name", name])
                if str(b.get("lessons", "")).strip():
                    args += ["--lessons", str(b["lessons"]).strip()]
                job, err = start_job(args, f"новый проект: {name}")
                return self._send(409 if err else 200,
                                  {"error": err} if err else {"ok": True, "job": job["title"]})
            if url.path == "/api/step":
                instr = str(b.get("instruction", "")).strip()
                if len(instr) < 3:
                    return self._send(400, {"error": "пустая инструкция"})
                path = b.get("path")
                base = ["builder/agent.py", "step", instr]
                if path:
                    base += ["--project", path]
                job, err = start_job(settings_args(s, base), f"правка: {instr[:40]}")
                return self._send(409 if err else 200,
                                  {"error": err} if err else {"ok": True})
            if url.path == "/api/fix":
                base = ["builder/agent.py", "fix"]
                if b.get("path"):
                    base += ["--project", b["path"]]
                job, err = start_job(settings_args(s, base), "проверка и исправление")
                return self._send(409 if err else 200,
                                  {"error": err} if err else {"ok": True})
            if url.path == "/api/part":
                pname = str(b.get("part", "")).strip()
                desc = str(b.get("desc", "")).strip()
                if not pname:
                    return self._send(400, {"error": "укажите имя части"})
                base = ["builder/agent.py", "part", pname] + ([desc] if desc else [])
                if b.get("path"):
                    base += ["--project", b["path"]]
                job, err = start_job(settings_args(s, base), f"часть {pname}")
                return self._send(409 if err else 200,
                                  {"error": err} if err else {"ok": True})
            if url.path == "/api/build":
                proj = Project(pathlib.Path(b["path"])) if b.get("path") else Project.last()
                dst = build(proj)
                proj.render_project_md()
                return self._send(200, {"ok": True, "dist": str(dst)})
            if url.path == "/api/stop":
                if _job and _job["proc"].poll() is None:
                    _job["proc"].kill()
                    return self._send(200, {"ok": True, "stopped": True})
                return self._send(200, {"ok": True, "stopped": False})
            self._send(404, {"error": "нет такого пути"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, *a):  # тише в консоли
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Интерфейс агента: http://127.0.0.1:{PORT}  (Ctrl+C — остановить)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
