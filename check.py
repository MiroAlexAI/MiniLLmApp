"""Проверки проекта без LLM: синтаксис JS (node --check), ссылки на id,
дубликаты глобальных имён между частями, длина частей, парность тегов.

Использование:
    python builder/check.py            # проверить последний проект
    python builder/check.py --selftest # оффлайн-проверка самого чекера (без GPU)
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project import SIG_RE, USED_ID_RE, ID_RE  # noqa: E402

NODE = "node"
DECL_RE = re.compile(
    r"^(?:async\s+)?function\s+(\w+)|^const\s+(\w+)|^let\s+(\w+)|^class\s+(\w+)")
ANY_DECL_RE = re.compile(
    r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)")
CALL_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "typeof",
            "new", "do", "else", "in", "of", "await", "yield", "delete", "void"}
BUILTINS = set("""
document window console Math JSON String Number Array Object Boolean Date RegExp
Set Map WeakMap Promise Error parseInt parseFloat isNaN isFinite setTimeout
clearTimeout setInterval clearInterval requestAnimationFrame cancelAnimationFrame
alert confirm prompt fetch localStorage sessionStorage location navigator history
performance getComputedStyle matchMedia structuredClone URL URLSearchParams
AudioContext OfflineAudioContext OscillatorNode GainNode AudioBufferSourceNode
CustomEvent Event Audio Image FileReader Blob FormData WebSocket Worker
requestIdleCallback queueMicrotask Reflect Proxy Symbol BigInt Intl WebAudio
getUserMedia MediaRecorder MediaStreamTrack speechSynthesis SpeechSynthesisUtterance
navigator_mediaDevices enumerateDevices TextDecoder TextEncoder
ClipboardItem ClipboardEvent DataTransfer DataTransferItem MutationObserver
ResizeObserver IntersectionObserver OffscreenCanvas FontFace btoa atob crypto
sharedArrayBuffer atexit DOMParser XMLSerializer
""".split())


def strip_code(text: str) -> str:
    """Убрать строки и комментарии (заменяются пробелами, переводы строк остаются)."""
    out, i, n, mode = [], 0, len(text), None
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if mode is None:
            if c in "'\"`":
                mode = c
                out.append(" ")
                i += 1
            elif c == "/" and nxt == "/":
                mode = "//"
                out.append("  ")
                i += 2
            elif c == "/" and nxt == "*":
                mode = "/*"
                out.append("  ")
                i += 2
            else:
                out.append(c)
                i += 1
        else:
            if mode == "//" and c == "\n":
                mode = None
                out.append(c)
            elif mode == "/*" and c == "*" and nxt == "/":
                mode = None
                out.append("  ")
                i += 2
            elif mode in "'\"`" and c == "\\":
                out.append("  ")
                i += 2
            elif mode in "'\"`" and c == mode:
                mode = None
                out.append(" ")
                i += 1
            else:
                out.append(c if c == "\n" else " ")
                i += 1
    return "".join(out)


def _count_args(argtext: str) -> int:
    argtext = argtext.strip()
    if not argtext:
        return 0
    depth, count = 0, 1
    for ch in argtext:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def check_calls(parts_texts):
    """parts_texts: [(имя, код)]. Проверка вызовов между частями:
    1) вызов функции, не объявленной нигде (кроме встроенных) — ошибка;
    2) вызов верхнеуровневой функции с БОЛЬШИМ числом аргументов, чем объявлено."""
    errs = []
    known = set(BUILTINS)
    top_params = {}  # имя -> число параметров верхнеуровневого объявления
    for _name, code in parts_texts:
        s = strip_code(code)
        for m in ANY_DECL_RE.finditer(s):
            known.add(m.group(1) or m.group(2))
        for line in s.splitlines():
            m = DECL_RE.match(line)  # верхний уровень (колонка 0)
            if not m:
                continue
            ident = next(g for g in m.groups() if g)
            fm = re.match(r"(?:async\s+)?function\s+\w+\s*\(([^)]*)\)", line.strip())
            if fm:
                top_params[ident] = _count_args(fm.group(1))
    for name, code in parts_texts:
        s = strip_code(code)
        for m in CALL_RE.finditer(s):
            fname = m.group(1)
            if fname in KEYWORDS or fname in known:
                continue
            errs.append(f"{name}: вызов неописанной функции {fname}()")
        # несоответствие числа аргументов (только вызовы без вложенных скобок)
        for m in re.finditer(r"(?<![\w$.])(\w+)\s*\(([^(){}]*)\)", s):
            fname, args = m.group(1), m.group(2)
            if fname in KEYWORDS or fname not in top_params:
                continue
            try:
                declared = top_params[fname]
            except KeyError:
                continue
            given = _count_args(args)
            if given > declared:
                errs.append(f"{name}: {fname}() вызвана с {given} арг., "
                            f"объявлена с {declared} — проверь сигнатуру")
    return errs


def check_js_syntax(part_name: str, code: str) -> list:
    """Синтаксис JS через node --check. Возвращает список строк-ошибок."""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "part.js"
        f.write_text(code, encoding="utf-8")
        try:
            r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
        except FileNotFoundError:
            return []  # node не установлен — проверку пропускаем
        if r.returncode == 0:
            return []
        msg = (r.stderr or r.stdout or "синтаксическая ошибка").strip()
        msg = msg.replace(str(f), part_name)
        out = []
        for l in msg.splitlines():
            l = l.strip()
            if not l:
                continue
            if not l.startswith(part_name):
                l = f"{part_name}: {l}"
            out.append(l)
        return out[:4]


def check_html_balance(part_name: str, code: str) -> list:
    """Грубая проверка парности часто встречающихся тегов."""
    errs = []
    for tag in ("div", "section", "canvas", "script", "style", "table", "ul", "button"):
        opens = len(re.findall(rf"<{tag}\b", code))
        closes = len(re.findall(rf"</{tag}>", code))
        if opens != closes:
            errs.append(f"{part_name}: <{tag}> открывается {opens} раз, закрывается {closes}")
    return errs


def check_part(proj, name: str) -> list:
    """Проверки одной части (после каждой записи)."""
    p = proj.parts_dir / name
    if not p.exists():
        return [f"{name}: файл не найден"]
    text = p.read_text(encoding="utf-8", errors="replace")
    errs = []
    n = len(text.splitlines())
    if not text.strip():
        errs.append(f"{name}: файл пуст")
    if n > 300:
        errs.append(f"{name}: {n} строк — слишком длинная часть "
                    f"(лимит 300; .js цель 120–250, остальные 15–80)")
    if name.endswith(".js"):
        errs += check_js_syntax(name, text)
    elif name.endswith(".html") and not name.endswith(".head.html"):
        errs += check_html_balance(name, text)
        low = text.lower()
        for bad, why in (("<!doctype", "части .html — фрагменты тела страницы, "
                                        "обёртку добавляет сборка"),
                         ("<html", "убери <html>: часть вставляется внутрь готовой страницы"),
                         ("<head", "убери <head>: вынеси содержимое в часть .head.html"),
                         ("<body", "убери <body>: часть вставляется внутрь готовой страницы"),
                         ("<script", "убери <script>: код — только в частях .js")):
            if bad in low:
                errs.append(f"{name}: {why} (найден {bad})")
    return errs


def check_project(proj) -> list:
    """Полная проверка: части по отдельности + связи между ними."""
    errs = []
    parts = proj.parts()
    if not parts:
        return ["в parts/ нет ни одной части"]

    globals_map = {}   # имя -> (часть, строка объявления)
    html_ids = set()
    used_ids = {}      # id -> часть

    for f in parts:
        name = f.name
        errs += check_part(proj, name)
        text = f.read_text(encoding="utf-8", errors="replace")
        if name.endswith(".js"):
            for line in text.splitlines():
                m = DECL_RE.match(line)  # только верхний уровень (колонка 0):
                if m:                    # локальные переменные внутри функций — не дубликаты
                    ident = next(g for g in m.groups() if g)
                    if ident in globals_map and globals_map[ident][0] != name:
                        first, first_line = globals_map[ident]
                        errs.append(f"{name}: '{ident}' уже объявлен в {first} "
                                    f"({first_line.strip()[:60]}); убери объявление "
                                    f"из этой части и просто используй готовое: "
                                    f"{line.strip()[:60]}")
                    globals_map[ident] = (name, line)
            for a, b in USED_ID_RE.findall(text):
                used_ids[a or b] = name
        elif name.endswith(".html"):
            html_ids.update(ID_RE.findall(text))

    for ident, part in sorted(used_ids.items()):
        if ident not in html_ids:
            errs.append(f"{part}: элемент id='{ident}' не найден ни в одной HTML-части")
    errs += check_calls([(f.name, f.read_text(encoding="utf-8", errors="replace"))
                         for f in parts if f.name.endswith(".js")])
    return list(dict.fromkeys(errs))


def _block_end(lines, start):
    """Конец (эксклюзивно) объявления, начинающегося на строке start:
    до точки с запятой для простых const/let, до парной закрывающей скобки для
    function/class/объектных литералов. Плюс одна пустая строка после."""
    j, depth, seen_brace = start, 0, False
    while j < len(lines):
        l = lines[j]
        depth += l.count("{") - l.count("}")
        if "{" in l:
            seen_brace = True
        j += 1
        if seen_brace:
            if depth <= 0:
                break
        elif l.rstrip().endswith(";") or depth < 0:
            break
    if j < len(lines) and not lines[j].strip():
        j += 1
    return j


def autostrip_html_wrapper(proj) -> int:
    """HTML-часть оказалась целым документом — оставить только содержимое <body>.
    Плюс в любых .html-фрагментах (не .head.html) вырезаются <script> блоки:
    код живёт только в .js частях, иначе сборка ломается дважды. Число правок."""
    fixed = 0
    for f in proj.parts():
        if not f.name.endswith(".html") or f.name.endswith(".head.html"):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        new = text
        if "<body" in new.lower():
            m = re.search(r"<body[^>]*>(.*)</body>", new, re.S | re.I)
            if m:
                new = m.group(1).strip()
        # вырезаем все <script>...</script> — JS собирается только из .js частей
        new2 = re.sub(r"[ \t]*<script\b[^>]*>[\s\S]*?</script>[ \t]*\n?", "", new,
                      flags=re.I)
        new2 = re.sub(r"[ \t]*<script\b[^>]*/?>[ \t]*\n?", "", new2, flags=re.I)
        new = new2.strip()
        if new and new != text.rstrip():
            f.write_text(new + "\n", encoding="utf-8")
            fixed += 1
    return fixed


def autodedup_globals(proj) -> int:
    """Убрать из поздних частей верхнеуровневые объявления, дословно повторяющие
    ранние (целиком, блоками). Возвращает число удалённых объявлений."""
    removed = 0
    seen = {}  # имя -> строка (нормализованная)
    for f in proj.parts():
        if not f.name.endswith(".js"):
            continue
        backup = f.read_text(encoding="utf-8", errors="replace")
        lines = backup.splitlines()
        i, n_removed = 0, 0
        while i < len(lines):
            m = DECL_RE.match(lines[i])  # только верхний уровень (колонка 0)
            if m:
                ident = next(g for g in m.groups() if g)
                norm = " ".join(lines[i].split())
                if ident in seen and seen[ident] == norm:
                    end = _block_end(lines, i)
                    lines = lines[:i] + lines[end:]  # вырезаем блок целиком
                    n_removed += 1
                    continue
                seen[ident] = norm
            i += 1
        if n_removed:
            new = "\n".join(lines).rstrip() + "\n"
            if check_js_syntax(f.name, new):  # не получилось — откат всего файла
                f.write_text(backup, encoding="utf-8")
            else:
                f.write_text(new, encoding="utf-8")
                removed += n_removed
    return removed


def remove_decl_from_part(proj, part: str, ident: str) -> bool:
    """Удалить верхнеуровневое объявление ident из JS-части целиком (блок по
    балансу скобок). Синтаксис проверяется; при ошибке откат. Успех — True."""
    f = proj.parts_dir / part
    if not f.exists():
        return False
    backup = f.read_text(encoding="utf-8", errors="replace")
    lines = backup.splitlines()
    start = None
    for i, l in enumerate(lines):
        m = DECL_RE.match(l)
        if m and next(g for g in m.groups() if g) == ident:
            start = i
            break
    if start is None:
        return False
    new = lines[:start] + lines[_block_end(lines, start):]
    f.write_text("\n".join(new).rstrip() + "\n", encoding="utf-8")
    if check_js_syntax(part, "\n".join(new)):
        f.write_text(backup, encoding="utf-8")  # откат: удалили лишнее
        return False
    return True


def _selftest() -> int:
    """Проверка чекера на заведомо хорошем и плохом коде. GPU не нужен."""
    good_js = "const A = 1;\nfunction foo(x) { return x + A; }\n"
    bad_js = "function foo( { return; }\n"
    assert check_js_syntax("30-good.js", good_js) == [], "хороший JS должен проходить"
    errs = check_js_syntax("30-bad.js", bad_js)
    assert errs, "плохой JS должен давать ошибку"
    print("node --check:", errs[0])

    e = check_html_balance("10-b.html", "<div><div></div>")
    assert any("div" in x for x in e), "должна быть непарность div"

    m = DECL_RE.match("function init() {")
    assert m and m.group(1) == "init"
    m = DECL_RE.match("const SPEED = 5;")
    assert m and m.group(2) == "SPEED"
    ids = ID_RE.findall('<div id="app"><canvas id="cv"></canvas></div>')
    assert ids == ["app", "cv"]
    used = [a or b for a, b in USED_ID_RE.findall(
        "getElementById('app'); document.querySelector('#cv'); querySelectorAll('.x')")]
    assert used == ["app", "cv"], used

    # проверка вызовов между частями
    errs = check_calls([
        ("30-init.js", "function updateDisplay() { render(); }\nfunction render() {}\n"),
        ("40-timer.js", "startTimer();\nupdateDisplay('work-timer', timeRemaining);\n"
                        "function startTimer() { stopTimer(); }\n"),
    ])
    msgs = "\n".join(errs)
    assert "неописанной функции stopTimer" in msgs, errs
    assert "updateDisplay() вызвана с 2 арг." in msgs, errs
    assert "неописанной функции startTimer" not in msgs, errs
    assert "неописанной функции render" not in msgs, errs
    assert "неописанной функции getElementById" not in msgs, errs
    print("selftest check.py: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.selftest:
        return _selftest()
    from project import Project
    proj = Project.last()
    print(f"Проект: {proj.root}")
    errs = check_project(proj)
    if errs:
        print(f"Ошибок: {len(errs)}")
        for e in errs:
            print(" -", e)
        return 1
    print("Проверки пройдены: синтаксис JS, id, дубликаты имён, парность тегов.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
