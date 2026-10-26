"""Агент-сборщик больших HTML+JS приложений из маленьких частей на локальной LLM.

Почему так: у маленькой локальной модели не хватает контекста на файл 500+ строк,
а длинный диалог «гниёт» к третьей правке. Здесь каждое обращение к модели —
короткое и одинакового размера: приложение состоит из частей по 30–120 строк,
состояние лежит на диске (parts/, project.json), история диалога не накапливается.
Правки можно делать сколько угодно — промпт не растёт.

Команды:
    python builder/agent.py new "задача" --name app   # новый проект от плана до сборки
    python builder/agent.py step "добавь ..."         # доработка по инструкции
    python builder/agent.py fix                       # автопроверка + исправление
    python builder/agent.py part 30-logic.js          # переписать одну часть
    python builder/agent.py status                    # состояние и проверки
Опции: --port 8080 --cooldown 4 --max-tokens 3072 --project <путь>
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import build  # noqa: E402
from check import (autodedup_globals, autostrip_html_wrapper, check_part,  # noqa: E402
                   check_project, remove_decl_from_part)
from llm import ContextOverflow, LLM  # noqa: E402
from project import Project, guess_ext, sanitize_name  # noqa: E402

SYSTEM = (
    "Ты — генератор кода одностраничных HTML+JS приложений. "
    "Проект: разметка (HTML-фрагмент), стили (.css) и ВЕСЬ JavaScript одним файлом. "
    "Код передавай ТОЛЬКО инструментом write_part — целиком содержимое одной части. "
    "JS — обычный браузерный: без import/export, глобальные const/function. "
    "Комментарии и тексты интерфейса — по-русски. Отвечай кратко, без лишних слов."
)

# Универсальные правила против типовых ошибок (игровые поля, объекты, DOM).
# Проверено на малых моделях: каждое правило — из реального сбоя.
RULES = (
    "ПРАВИЛА (нарушение = ошибка):\n"
    "1. Один источник истины: состояние — в глобальных переменных/объекте state; "
    "DOM перерисовывается функцией render() ИЗ состояния.\n"
    "2. Поле/сетка строится циклом по константам (ROWS, COLS, TILE), не руками.\n"
    "3. ЗАПРЕЩЕНЫ выдуманные пространства имён (gameLogic.x, App.logic.x, "
    "aiController.x) — только глобальные const/let/function. Вызывай только то, "
    "что объявлено в этом же файле или стандартный DOM API.\n"
    "4. ЕДИНОЕ представление пустой клетки/ячейки во всём коде (например, ''), "
    "проверяй одинаково везде: if (!board[r][c]).\n"
    "5. Все id — из HTML-части; обработчики вешаются ОДИН раз при старте; "
    "для динамических ячеек — делегирование клика на контейнер (e.target.dataset).\n"
    "6. Только vanilla API, которые точно существуют: DOM, setInterval/Timeout, "
    "localStorage, Math, JSON, WebAudio (осциллятор). Без библиотек и фреймворков.\n"
    "7. Компактно: плотный код, без мёртвого кода, console.log и дубликатов."
)

# Бюджет промпта в токенах (примерно; RU ~3 символа на токен). Проверено: модель
# эффективно правит контекст до ~6000 токенов, дальше качество падает.
PROMPT_BUDGET = 5500


def est_tokens(text):
    return len(text) // 3

TOOL_WRITE = {"type": "function", "function": {
    "name": "write_part",
    "description": "Записать одну часть проекта целиком (30–120 строк кода).",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "имя части, например 30-logic.js"},
        "content": {"type": "string", "description": "полное содержимое части"}},
        "required": ["name", "content"]}}}

TOOL_READ = {"type": "function", "function": {
    "name": "read_part",
    "description": "Прочитать одну часть проекта.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"}}, "required": ["name"]}}}

TOOL_PLAN = {"type": "function", "function": {
    "name": "save_plan",
    "description": "Сохранить план частей проекта.",
    "parameters": {"type": "object", "properties": {
        "parts": {"type": "array", "description": "список частей по порядку сборки",
                  "items": {"type": "object", "properties": {
                      "name": {"type": "string", "description": "NN-имя.ext"},
                      "desc": {"type": "string", "description": "детально: объявляет, вызывает, id"}},
                      "required": ["name", "desc"]}}},
        "required": ["parts"]}}}

TOOL_SPEC = {"type": "function", "function": {
    "name": "save_spec",
    "description": "Сохранить инженерное ТЗ (реферат) по теме разработки.",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string", "description": "название темы"},
        "content": {"type": "string", "description": "текст ТЗ в markdown, 50–90 строк"}},
        "required": ["title", "content"]}}}

FENCE_RE = re.compile(r"```[A-Za-z0-9]*[ \t]*\n(.*?)```", re.S)
PLAN_LINE_RE = re.compile(
    r"^\s*[-*\d.)]+\s*(\S+\.(?:head\.html|html|css|js))\s*[—:-]\s*(.+)$", re.M | re.I)


def tool_calls_of(msg):
    """[(id, имя, аргументы)] из ответа модели."""
    out = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as e:
            args = {"_error": f"аргументы не JSON: {e}"}
        out.append((c.get("id", ""), fn.get("name", ""), args))
    return out


# ---------------------------------------------------------------- фазы агента

def phase_spec(llm, task, lessons=""):
    """Фаза 1: инженерное ТЗ (реферат). Свежий контекст; модель лучше работает,
    когда сначала структурирует тему, а код пишет по готовому ТЗ в новой сессии.
    lessons — ошибки прошлой попытки (для перезапуска проекта)."""
    user = (
        f"ТЕМА: {task}\n\n"
        "Напиши компактное инженерное ТЗ для разработки этого одностраничного "
        "HTML+JS приложения. КОД БУДЕТ ПЛОТНЫЙ И ПРОСТОЙ:\n"
        "- весь JavaScript — один файл 120–250 строк, без классов-обёрток ради "
        "архитектуры: глобальные const/let/function и один объект состояния;\n"
        "- только vanilla HTML/CSS/JS, БЕЗ внешних библиотек, фреймворков и CDN;\n"
        "- только API, которые точно знаешь: DOM, setInterval/Timeout, localStorage, "
        "Math, JSON, WebAudio (осциллятор);\n"
        "- лёгкий стиль: простые функции 5–20 строк, минимум вложенности.\n\n"
        "Структура ТЗ строго такая:\n"
        "# ТЗ: <название>\n"
        "## 1. Цель и сценарий (2-3 предложения)\n"
        "## 2. Функциональные требования (нумерованный список, 6-10 пунктов)\n"
        "## 3. Данные и состояние (переменные и их типы; как представлена пустая "
        "клетка/ячейка — единообразно)\n"
        "## 4. Функции (список: имя(аргументы) -> что делает; всё в одном файле)\n"
        "## 5. Ключевые алгоритмы (правила, подсчёт, ИИ — пошагово, просто)\n"
        "## 6. Интерфейс (элементы с id, что где отображается)\n"
        "## 7. Сценарии проверки (5-6 конкретных: действие -> ожидаемый результат)\n"
        + (f"\n## 8. Ошибки прошлой версии — НЕ ПОВТОРЯТЬ:\n{lessons}\n" if lessons else "")
        + "\n50–80 строк, по делу, без воды. Вызови save_spec(title, content).")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]
    for _ in range(3):
        msg, _fin = llm.chat(msgs, tools=[TOOL_SPEC], think=True)
        for _id, name, args in tool_calls_of(msg):
            if name == "save_spec" and (args.get("content") or "").strip():
                return {"title": str(args.get("title", "")).strip() or "ТЗ",
                        "content": str(args["content"]).strip()}
        text = (msg.get("content") or "").strip()
        if len(text) > 400 and "##" in text:
            return {"title": "ТЗ", "content": text}
        msgs.append(msg)
        msgs.append({"role": "user", "content": "Вызови save_spec(title, content)."})
    raise SystemExit("Модель не написала ТЗ. Переформулируйте задачу и повторите.")


MAX_PARTS = 3  # проверено на малых моделях: разметка + стили + ВЕСЬ JS одним файлом


def phase_plan(llm, spec, task):
    """Фаза 2: план из ТЗ. Ровно 3 части: HTML-фрагмент, стили, весь JS одним
    файлом. Больше частей — больше межчастевых контрактов, на которых малая
    модель ломается (проверено на практике)."""
    user = (
        f"ТЕМА: {task}\n\n"
        f"ТЗ (инженерный реферат):\n{spec['content'][:6000]}\n\n"
        "Разбей приложение РОВНО на 3 части для сборки одного index.html:\n"
        "10-body.html — ФРАГМЕНТ body: только разметка (без <!DOCTYPE>, <html>, "
        "<head>, <body> и БЕЗ <script>), у всех элементов, которыми управляет JS, "
        "есть id, кнопки без disabled, 15–50 строк;\n"
        "20-style.css — стили, лёгкие, 30–80 строк;\n"
        "30-app.js — ВЕСЬ JavaScript ОДНИМ файлом 120–250 строк: состояние, "
        "логика, ИИ, отрисовка render(), обработчики, запуск в конце файла.\n"
        "Никаких 4-х частей и отдельных файлов логики/ИИ/UI.\n"
        "Для каждой части опиши: ОБЪЯВЛЯЕТ (имена) / ИСПОЛЬЗУЕТ id из HTML.\n"
        "Вызови save_plan списком из трёх частей.")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]
    for _ in range(3):
        msg, _fin = llm.chat(msgs, tools=[TOOL_PLAN], think=True)
        for _id, name, args in tool_calls_of(msg):
            if name == "save_plan" and args.get("parts"):
                plan, seen, js = [], set(), 0
                for i, p in enumerate(args["parts"], start=1):
                    raw = str(p.get("name", ""))
                    desc = str(p.get("desc", "")).strip()[:400]
                    nm = sanitize_name(raw, i * 10)
                    if not re.search(r"\.(head\.html|html|css|js)$", raw.strip().strip("`\"'")):
                        nm = re.sub(r"\.(head\.html|html|css|js)$", "." + guess_ext(desc), nm)
                    if nm.endswith(".js"):
                        js += 1
                        if js > 1:  # JS-часть одна — вторую не принимаем
                            continue
                    while nm in seen:
                        num = str(int(nm[:2]) + 1).zfill(2)
                        nm = num + nm[2:]
                    seen.add(nm)
                    plan.append({"name": nm, "desc": desc})
                    if len(plan) >= MAX_PARTS:
                        break
                if plan:
                    return plan
        lines = plan_lines_from_text(msg.get("content") or "")
        if lines:
            return [{"name": sanitize_name(p["name"], i * 10), "desc": p["desc"]}
                    for i, p in enumerate(lines, start=1)][:7]
        msgs.append(msg)
        msgs.append({"role": "user", "content": "Вызови save_plan(parts=[...])."})
    raise SystemExit("Модель не составила план. Попробуйте переформулировать задачу.")


def _part_prompt(proj, part, desc, compact, spec_excerpt=""):
    task = proj.state["task"]
    plan = "\n".join(f"- {p['name']} — {p['desc']}" for p in proj.state["plan"])
    iface = proj.interfaces() or "(пока нет)"
    if compact:  # аварийное сжатие: бюджет ~6000 токенов на весь промпт
        plan = "\n".join(f"- {p['name']}" for p in proj.state["plan"])
        iface = "\n".join(iface.splitlines()[:15])
        spec_excerpt = spec_excerpt[:1200]
    written = ", ".join(p.name for p in proj.parts()) or "(нет)"
    size = "120–250 строк" if part.endswith(".js") else "15–80 строк"
    head = (
        f"ЗАДАЧА: {task}\n\n"
        + (f"ИЗ ТЗ (ключевое):\n{spec_excerpt}\n\n" if spec_excerpt else "")
        + f"ПЛАН:\n{plan}\n\n"
        f"УЖЕ ЗАПИСАНО: {written}\n\nИНТЕРФЕЙСЫ:\n{iface}\n\n"
        + RULES + "\n\n"
        f"НАПИШИ ЧАСТЬ: {part} — {desc}\n"
        f'Вызови write_part(name="{part}", content=...): полное содержимое '
        f"только этой части, {size}.")
    if est_tokens(head) > PROMPT_BUDGET and not compact:
        print(f"    промпт {est_tokens(head)} ток. — сжимаю до бюджета {PROMPT_BUDGET}")
        return _part_prompt(proj, part, desc, compact=True, spec_excerpt=spec_excerpt)
    return head


def phase_part(llm, proj, part, desc, compact=False, errors=None):
    """Сгенерировать одну часть. Свежий контекст, до 4 попыток с исправлениями.
    errors: список ошибок прошлых попыток — модель пишет часть сразу с их учётом."""
    prompt = _part_prompt(proj, part, desc, compact,
                          spec_excerpt=proj.state.get("spec_excerpt", ""))
    if errors:
        prompt += ("\n\nИЗВЕСТНЫЕ ОШИБКИ ПРОШЛОЙ ПОПЫТКИ — напиши сразу без них:\n- "
                   + "\n- ".join(errors[:8]))
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt}]
    for _round in range(4):
        try:
            msg, fin = llm.chat(msgs, tools=[TOOL_WRITE], think=False)
        except ContextOverflow:
            if not compact:
                print("    контекст мал — повтор с укороченным промптом")
                return phase_part(llm, proj, part, desc, compact=True, errors=errors)
            raise
        calls = tool_calls_of(msg)
        # фолбэк: код в тексте без инструмента
        if not calls:
            fence = FENCE_RE.search(msg.get("content") or "")
            if fence and len(fence.group(1)) > 60:
                calls = [("", "write_part", {"name": part, "content": fence.group(1)})]
            else:
                msgs.append(msg)
                msgs.append({"role": "user", "content":
                             'Вызови инструмент write_part(name="' + part + '", content=...).'})
                continue
        done = False
        msgs.append(msg)
        for cid, name, args in calls:
            if args.get("_error"):
                result = args["_error"]
            elif name != "write_part":
                result = f"неизвестный инструмент {name}; нужен write_part"
            elif args.get("name", "").strip() != part:
                result = (f"ожидалась часть {part}, пришло {args.get('name')!r}; "
                          "вызови write_part для " + part)
            else:
                try:
                    proj.write_part(part, str(args.get("content", "")))
                except ValueError as e:
                    result = f"ошибка: {e}"
                else:
                    errs = check_part(proj, part)
                    if errs:
                        result = "записано, но проверки нашли ошибки:\n- " + "\n- ".join(errs)
                    else:
                        result = f"OK: {part} записана, проверки пройдены"
                        done = True
            msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
        if done:
            proj.note(f"написана часть {part}")
            return True
        if fin == "length":
            msgs.append({"role": "user", "content":
                         "Ответ обрезан по лимиту. Сократи часть до 110 строк и вызови "
                         "write_part снова (полное содержимое)."})
    print(f"  ! часть {part}: не удалось получить чистый вариант за 4 попытки")
    return False


def phase_fix_part(llm, proj, part, errors):
    """Исправить одну часть по списку ошибок проверок."""
    content = ""
    try:
        content = proj.read_part(part)
    except Exception:
        pass
    user = (
        f"ЗАДАЧА: {proj.state['task']}\n\n"
        f"ИНТЕРФЕЙСЫ (уже объявлено в ДРУГИХ частях — не объявляй это снова):\n"
        f"{proj.interfaces() or '(нет)'}\n\n"
        f"В части {part} проверки нашли ошибки:\n- " + "\n- ".join(errors) +
        f"\n\nТЕКУЩЕЕ СОДЕРЖИМОЕ {part}:\n```\n{content[:6000]}\n```\n\n"
        f"Исправь и вызови write_part(name=\"{part}\", content=...) с полным "
        "новым содержимым части. Минимум изменений, остальное не трогай. "
        "Если имя уже объявлено в другой части — убери повторное объявление "
        "здесь и пользуйся готовым.")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]
    for _round in range(3):
        msg, _fin = llm.chat(msgs, tools=[TOOL_WRITE], think=False)
        calls = tool_calls_of(msg)
        if not calls:
            fence = FENCE_RE.search(msg.get("content") or "")
            if fence:
                calls = [("", "write_part", {"name": part, "content": fence.group(1)})]
            else:
                msgs.append(msg)
                msgs.append({"role": "user", "content":
                             f'Вызови write_part(name="{part}", content=...).'})
                continue
        msgs.append(msg)
        for cid, name, args in calls:
            if name == "write_part" and args.get("name", "").strip() == part:
                try:
                    proj.write_part(part, str(args.get("content", "")))
                except ValueError as e:
                    msgs.append({"role": "tool", "tool_call_id": cid,
                                 "content": f"ошибка: {e}"})
                    continue
                errs = check_part(proj, part)
                result = ("OK: исправлено, проверок для части нет ошибок" if not errs
                          else "остались ошибки:\n- " + "\n- ".join(errs))
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                if not errs:
                    proj.note(f"исправлена часть {part}")
                    return True
            else:
                msgs.append({"role": "tool", "tool_call_id": cid,
                             "content": f"нужен write_part для {part}"})
    return False


def group_errors_by_part(errors):
    """['30-x.js: ...'] -> {'30-x.js': ['30-x.js: ...']} (ключ — имя части)."""
    groups = {}
    for e in errors:
        m = re.match(r"^(\d{2}-[\w\-]+\.(?:head\.html|html|css|js)):", e)
        key = m.group(1) if m else None
        groups.setdefault(key, []).append(e)
    return groups


DUP_ERR_RE = re.compile(r"^(\d{2}-[\w\-]+\.js): '(\w+)' уже объявлен")


def autoremove_dups(proj, errors):
    """Повторные объявления верхнего уровня убираем сами (блоком), без модели.
    Возвращает ошибки, которые остались."""
    left = []
    for e in errors:
        m = DUP_ERR_RE.match(e)
        if m and remove_decl_from_part(proj, m.group(1), m.group(2)):
            print(f"Автоудаление: убрано повторное объявление {m.group(2)} из {m.group(1)}")
        else:
            left.append(e)
    return left


def fix_all(llm, proj, max_rounds=2):
    """Проверка и исправление. Проверено на практике: если за 2 раунда точечных
    правок ошибки не ушли — дальше правки только плодят новые. Поэтому после
    2 раундов проблемные части ПЕРЕГЕНЕРИРУЮТСЯ с нуля (свежий контекст, ошибки
    известны заранее), а не чинятся по кругу."""
    def autoclean():
        n = autostrip_html_wrapper(proj)
        if n:
            print(f"Автоочистка HTML: обёртка документа убрана в {n} части(ях)")
        n = autodedup_globals(proj)
        if n:
            print(f"Автоочистка: удалено повторных объявлений {n}")

    for _r in range(max_rounds):
        autoclean()
        errs = check_project(proj)
        if not errs:
            return []
        errs = autoremove_dups(proj, errs)
        if not errs:
            return []
        groups = group_errors_by_part(errs)
        print(f"Правки (раунд {_r + 1}/{max_rounds}): ошибок {len(errs)} в частях {list(groups)}")
        fixed_any = False
        for part, elist in groups.items():
            if part is None:
                print("  (без привязки к части):", *elist, sep="\n  ")
                continue
            fixed_any |= phase_fix_part(llm, proj, part, elist)
        autodedup_globals(proj)
        if not fixed_any:
            break

    # перегенерация проблемных частей с нуля
    autoclean()
    errs = check_project(proj)
    if not errs:
        return []
    groups = {p: el for p, el in group_errors_by_part(errs).items()
              if p is not None and p.endswith(".js")}
    if groups:
        print(f"Правки не помогли — перегенерирую с нуля: {list(groups)[:3]}")
        for part, elist in list(groups.items())[:3]:
            desc = next((p["desc"] for p in proj.state["plan"] if p["name"] == part),
                        "см. план и ИНТЕРФЕЙСЫ")
            phase_part(llm, proj, part, desc, errors=elist)
            autostrip_html_wrapper(proj)
            autodedup_globals(proj)
        # модель могла снова объявить чужие имена — снимаем детерминированно
        errs = autoremove_dups(proj, check_project(proj))
        if not errs:
            return []
    left = check_project(proj)
    if left:
        # лимит правок исчерпан: дальше только плодятся новые — полный перезапуск
        lessons = "; ".join(e.split(": ", 1)[-1][:80] for e in left[:5])
        proj.state["lessons"] = lessons
        proj.note(f"ПЕРЕЗАПУСК: правки не помогли ({len(left)} ошибок). "
                  f"Уроки: {lessons}")
        proj.save()
        print("\n== ПРАВИЛО ОСТАНОВА: 2 раунда правок + перегенерация не помогли. "
              "Дальше править бессмысленно — перезапустите проект, ошибки уйдут "
              "в ТЗ новой версии: ==")
        print(f'  python builder/agent.py new "{proj.state["task"][:80]}" '
              f'--name {proj.state["name"]}-v2 --lessons "{lessons}"')
    return left


def phase_step(llm, proj, instruction):
    """Доработка по инструкции: модель читает и пишет части, мы проверяем каждую."""
    errs = check_project(proj)
    parts_list = "\n".join(f"- {f.name} ({proj.part_lines(f.name)} стр.)" for f in proj.parts())
    user = (
        f"ЗАДАЧА: {proj.state['task']}\n\nЧАСТИ:\n{parts_list or '(нет)'}\n\n"
        f"ИНТЕРФЕЙСЫ:\n{proj.interfaces() or '(нет)'}\n"
        + (f"\nОШИБКИ ПРОВЕРОК:\n- " + "\n- ".join(errs[:10]) + "\n" if errs else "") +
        f"\nИНСТРУКЦИЯ: {instruction}\n\n"
        "Можно read_part(name) — прочесть часть, write_part(name, content) — заменить "
        "её целиком. Меняй минимум частей; если часть стала длиннее 150 строк — раздели "
        "на две с новым номером. Новые части тоже через write_part.")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]
    wrote = []
    for _round in range(8):
        msg, _fin = llm.chat(msgs, tools=[TOOL_WRITE, TOOL_READ], think=False)
        calls = tool_calls_of(msg)
        if not calls:
            break
        msgs.append(msg)
        for cid, name, args in calls:
            if args.get("_error"):
                result = args["_error"]
            elif name == "read_part":
                try:
                    result = proj.read_part(str(args.get("name", "")))[:6000]
                except Exception as e:
                    result = f"ошибка: {e}"
            elif name == "write_part":
                pname = str(args.get("name", "")).strip()
                try:
                    proj.write_part(pname, str(args.get("content", "")))
                except ValueError as e:
                    result = f"ошибка: {e}"
                else:
                    errs = check_part(proj, pname)
                    wrote.append(pname)
                    result = (f"OK: {pname} записана" if not errs
                              else "записано, но есть ошибки:\n- " + "\n- ".join(errs))
            else:
                result = f"неизвестный инструмент {name}"
            msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
    if wrote:
        proj.note(f"правка ({instruction[:60]}): {', '.join(dict.fromkeys(wrote))}")
    return wrote


# ---------------------------------------------------------------- команды CLI

def load_project(a):
    return Project(Path(a.project)) if a.project else Project.last()


def cmd_new(a, llm):
    print(f"Новый проект: {a.name}")
    print(f"Задача: {a.task}")
    proj = Project.create(a.name, a.task, title=a.title or a.name)
    t0 = time.time()
    task = a.task + (f" ({a.hint})" if a.hint else "")

    print("\n[1/4] ТЗ-реферат (свежий контекст)...")
    spec = phase_spec(llm, task, lessons=a.lessons or "")
    spec_md = proj.root / "SPEC.md"
    spec_md.write_text(f"# {spec['title']}\n\n{spec['content']}\n", encoding="utf-8")
    # ключевые строки ТЗ для промптов частей (разделы данные/архитектура/алгоритмы)
    keep = []
    for line in spec["content"].splitlines():
        if line.startswith("##") or (keep and line.strip()):
            keep.append(line)
    proj.state["spec_excerpt"] = "\n".join(keep)[:1800]
    proj.note("написано ТЗ (SPEC.md)")
    proj.save()
    print(f"  {spec['title']}: {len(spec['content'].splitlines())} строк -> SPEC.md")

    print("\n[2/4] План частей из ТЗ (новый контекст)...")
    plan = phase_plan(llm, spec, task)
    proj.state["plan"] = plan
    proj.save()
    proj.render_project_md()
    for p in plan:
        print(f"  {p['name']} — {p['desc'][:100]}")

    print("\n[3/4] Генерация частей (по одной, свежий контекст)...")
    ok = 0
    for i, p in enumerate(plan, start=1):
        print(f"  ({i}/{len(plan)}) {p['name']}")
        ok += phase_part(llm, proj, p["name"], p["desc"])
    print(f"  написано чисто: {ok}/{len(plan)}")

    print("\n[4/4] Проверки и исправления (макс. 2 раунда, потом перегенерация)...")
    left = fix_all(llm, proj)
    proj.render_project_md()
    dst = build(proj)
    llm.print_stats()
    print(f"\nГотово за {time.time() - t0:.0f} с. Проект: {proj.root}")
    print(f"Открыть приложение: {dst}  (или BUILD_OPEN.bat)")
    if left:
        print(f"Остались ошибки ({len(left)}): запустите  python builder/agent.py fix")
        for e in left[:8]:
            print("  -", e)
    return 1 if left else 0


def cmd_step(a, llm):
    proj = load_project(a)
    print(f"Проект: {proj.root}\nИнструкция: {a.instruction}")
    wrote = phase_step(llm, proj, a.instruction)
    if not wrote:
        print("Модель не записала ни одной части — уточните инструкцию.")
        return 1
    left = fix_all(llm, proj, max_rounds=1)
    proj.render_project_md()
    dst = build(proj)
    llm.print_stats()
    print(f"Изменены части: {', '.join(dict.fromkeys(wrote))}")
    print(f"Открыть: {dst}")
    return 1 if left else 0


def cmd_fix(a, llm):
    proj = load_project(a)
    errs = check_project(proj)
    if not errs:
        print("Проверки чистые — исправлять нечего.")
        return 0
    left = fix_all(llm, proj)
    proj.render_project_md()
    build(proj)
    llm.print_stats()
    if left:
        print(f"Остались ошибки ({len(left)}):")
        for e in left[:12]:
            print("  -", e)
        return 1
    print("Все исправлено, сборка обновлена.")
    return 0


def cmd_part(a, llm):
    proj = load_project(a)
    desc = " ".join(a.desc) if a.desc else ""
    if not desc:
        desc = next((p["desc"] for p in proj.state["plan"] if p["name"] == a.name), "")
    print(f"Переписываю часть {a.name}: {desc}")
    ok = phase_part(llm, proj, a.name, desc)
    left = fix_all(llm, proj, max_rounds=1) if ok else check_project(proj)
    proj.render_project_md()
    build(proj)
    llm.print_stats()
    return 0 if ok else 1


def cmd_status(a, _llm):
    proj = load_project(a)
    md = proj.root / "PROJECT.md"
    if md.exists():
        print(md.read_text(encoding="utf-8"))
    else:
        print(f"Проект: {proj.root} (PROJECT.md ещё нет)")
    errs = check_project(proj)
    print("Проверки:", "чисто" if not errs else f"{len(errs)} ошибок")
    for e in errs[:12]:
        print("  -", e)
    dist = proj.dist_dir / "index.html"
    print("Сборка:", dist if dist.exists() else "ещё не собрано (BUILD_OPEN.bat)")
    return 0


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=8080)
    common.add_argument("--cooldown", type=float, default=4.0,
                        help="пауза после каждого запроса, с (щадящий режим GPU)")
    common.add_argument("--max-tokens", type=int, default=3072)
    common.add_argument("--project", default="", help="путь к папке проекта (по умолчанию последний)")
    common.add_argument("--temperature", type=float, default=None,
                        help="температура генерации (по умолчанию — настройки сервера)")

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="новый проект: план -> части -> проверки -> сборка",
                       parents=[common])
    p.add_argument("task")
    p.add_argument("--name", default="app")
    p.add_argument("--title", default="")
    p.add_argument("--hint", default="", help="подсказка к плану (какие части нужны)")
    p.add_argument("--lessons", default="",
                   help="ошибки прошлой версии (попадут в ТЗ разделом «не повторять»)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("step", help="доработка по инструкции", parents=[common])
    p.add_argument("instruction")
    p.set_defaults(fn=cmd_step)

    p = sub.add_parser("fix", help="проверить и исправить ошибки", parents=[common])
    p.set_defaults(fn=cmd_fix)

    p = sub.add_parser("part", help="переписать одну часть", parents=[common])
    p.add_argument("name")
    p.add_argument("desc", nargs="*", help="описание (по умолчанию из плана)")
    p.set_defaults(fn=cmd_part)

    p = sub.add_parser("status", help="состояние проекта", parents=[common])
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    llm = LLM(port=a.port, cooldown=a.cooldown, max_tokens=a.max_tokens, temperature=a.temperature)
    try:
        raise SystemExit(a.fn(a, llm))
    except KeyboardInterrupt:
        print("\nОстановлено. Прогресс сохранён на диске — продолжайте любой командой.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
