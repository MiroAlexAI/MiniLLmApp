"""Клиент llama-server (OpenAI-совместимый API) для агента-сборщика.

Особенности:
- обязательная пауза между запросами (cooldown) — щадящий режим питания GPU
  (по данным README: Gemma-E2B на RX 6800M с паузами — ни одного отключения);
- статистика токенов и скорости;
- при ошибке 400 из-за chat_template_kwargs повторяет запрос без него
  (на случай, если сервер запущен без --jinja или с --reasoning off).
"""
import json
import time
import urllib.error
import urllib.request


class ContextOverflow(Exception):
    """Запрос не влезает в контекст сервера."""


class LLM:
    def __init__(self, port=8080, cooldown=4.0, max_tokens=3072, timeout=900, verbose=True,
                 temperature=None):
        self.base = f"http://127.0.0.1:{port}"
        self.cooldown = cooldown
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.verbose = verbose
        self.temperature = temperature  # None = настройки сервера; число — своё, на каждый запрос
        self.model = "local-model"
        self.stats = {"requests": 0, "prompt_tokens": 0, "generated_tokens": 0,
                      "llm_seconds": 0.0}
        self._detect_model()

    def _detect_model(self):
        try:
            with urllib.request.urlopen(self.base + "/v1/models", timeout=5) as r:
                data = json.load(r)
            ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
            if ids:
                self.model = ids[0]
        except Exception:
            pass

    def _post(self, body):
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=self.timeout))

    def chat(self, messages, tools=None, think=False, max_tokens=None):
        """Один запрос. Возвращает (message, finish_reason). Пауза — после ответа."""
        body = {"model": self.model, "messages": messages,
                "max_tokens": max_tokens or self.max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if tools:
            body["tools"] = tools
        variants = [dict(body, chat_template_kwargs={"enable_thinking": bool(think)}), body]

        r = None
        last = "нет ответа"
        for b in variants:
            for _ in range(2):  # по одной повторной попытке на вариант
                try:
                    r = self._post(b)
                    break
                except urllib.error.HTTPError as e:
                    try:
                        detail = e.read().decode("utf-8", "replace")[:500]
                    except Exception:
                        detail = ""
                    last = f"HTTP {e.code}: {detail}"
                    low = detail.lower()
                    if e.code == 400 and any(w in low for w in
                                             ("chat_template_kwargs", "enable_thinking", "unknown field")):
                        break  # попробовать тело без chat_template_kwargs
                    if any(w in low for w in ("context", "exceed", "too large", "too many tokens")):
                        raise ContextOverflow(last)
                    time.sleep(3)
                except urllib.error.URLError as e:
                    last = f"нет соединения: {e}"
                    time.sleep(3)
            if r is not None:
                break
        if r is None:
            raise RuntimeError(f"сервер не ответил: {last}")

        choice = r["choices"][0]
        msg = choice["message"]
        msg.pop("reasoning_content", None)  # размышления в историю не сохраняем
        t = r.get("timings", {})
        p_n, g_n = t.get("prompt_n", 0), t.get("predicted_n", 0)
        p_s = t.get("prompt_per_second", 0) or 0
        g_s = t.get("predicted_per_second", 0) or 0
        self.stats["requests"] += 1
        self.stats["prompt_tokens"] += p_n
        self.stats["generated_tokens"] += g_n
        self.stats["llm_seconds"] += ((t.get("prompt_ms", 0) or 0) + (t.get("predicted_ms", 0) or 0)) / 1000
        if self.verbose:
            print(f"    LLM: промпт {p_n} ток. ({p_s:.0f} т/с), ответ {g_n} ток. "
                  f"({g_s:.1f} т/с), finish={choice.get('finish_reason')}")
        time.sleep(self.cooldown)  # щадящая пауза между запросами
        return msg, choice.get("finish_reason")

    def print_stats(self):
        s = self.stats
        if s["requests"] == 0:
            return
        print(f"Запросов к модели: {s['requests']}, промпт {s['prompt_tokens']} ток., "
              f"сгенерировано {s['generated_tokens']} ток., чистое время LLM {s['llm_seconds']:.0f} с "
              f"(паузы между запросами не считаются)")
