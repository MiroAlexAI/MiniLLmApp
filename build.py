"""Сборка одностраничного приложения: parts/ -> dist/index.html.

Части склеиваются в порядке имён (NN-): .head.html в <head>, .html в <body>,
.css в один <style>, .js в один <script> перед </body>.

Использование:
    python builder/build.py            # собрать последний проект
    python builder/build.py --open     # собрать и открыть в браузере
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project import Project  # noqa: E402

TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{head}</head>
<body>
{body}<script>
{js}</script>
</body>
</html>
"""


def build(proj: Project) -> Path:
    head, body, css, js = [], [], [], []
    for f in proj.parts():
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        if f.name.endswith(".head.html"):
            head.append(f"<!-- ===== part: {f.name} ===== -->\n{text}")
        elif f.name.endswith(".html"):
            body.append(f"<!-- ===== part: {f.name} ===== -->\n{text}")
        elif f.name.endswith(".css"):
            css.append(f"/* ===== part: {f.name} ===== */\n{text}")
        elif f.name.endswith(".js"):
            js.append(f"// ===== part: {f.name} =====\n{text}")
    if css:
        head.append("<style>\n" + "\n\n".join(css) + "\n</style>")
    out = TEMPLATE.format(
        title=proj.state.get("title") or proj.state["name"],
        head=("\n".join(head) + "\n") if head else "",
        body=("\n".join(body) + "\n") if body else "<p>(нет HTML-частей)</p>\n",
        js="\n\n".join(js) if js else "// (нет JS-частей)")
    proj.dist_dir.mkdir(exist_ok=True)
    dst = proj.dist_dir / "index.html"
    dst.write_text(out, encoding="utf-8")
    total = len(out.splitlines())
    print(f"Собрано: {dst}  ({total} строк, частей: {len(proj.parts())})")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="открыть результат в браузере")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    proj = Project.last()
    dst = build(proj)
    if a.open:
        os.startfile(dst)  # noqa — Windows
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
