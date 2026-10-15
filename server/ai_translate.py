"""Groq AI translation (OpenAI-compatible API, stdlib only, no new deps).

Model: openai/gpt-oss-20b (see screenshot config: base URL
https://api.groq.com/openai/v1, protocol openai-completions).

Key is NEVER stored in the repo: read from GROQ_API_KEY env var.
Windows:  set GROQ_API_KEY=gsk_...
Linux:    export GROQ_API_KEY=gsk_...
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MODEL = "openai/gpt-oss-20b"
BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
TIMEOUT = float(os.environ.get("GROQ_TIMEOUT", "60"))


def key_configured() -> bool:
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def _chat(messages: list, max_tokens: int = 2000) -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY не задан")
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "temperature": 0.1, "messages": messages}).encode()
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        raise RuntimeError(f"Groq HTTP {e.code}: {detail}")
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError("Пустой ответ Groq")


def translate_exam_to_kk(questions: list) -> list:
    """Translate [{q, options}] RU->KK. Returns [{q, options}] in same order.

    Raises RuntimeError with human text on any failure (caller -> 502).
    """
    if not isinstance(questions, list) or not questions:
        raise RuntimeError("Нет вопросов для перевода")
    if len(questions) > 100:
        raise RuntimeError("Слишком много вопросов (макс. 100)")
    payload = [{"q": str(q.get("q", "")), "options": [str(o) for o in q.get("options", [])]}
               for q in questions]
    prompt = ("Переведи тест с русского на казахский (кириллица). "
              "Работай в два шага: 1) переведи, 2) ПЕРЕЧИТАЙ свой перевод "
              "как редактор — исправь согласования, термины и падежи. "
              "Ответь СТРОГО JSON-массивом той же длины: "
              '[{"q": "...", "options": ["...", ...]}]. '
              "Без пояснений, без markdown. Входной JSON:\n"
              + json.dumps(payload, ensure_ascii=False))
    raw = _chat([{"role": "user", "content": prompt}],
                max_tokens=min(4000, 400 + 40 * sum(len(q.get("options", [])) + 1 for q in questions)))
    # Groq иногда оборачивает в ```json — чистим.
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        out = json.loads(text)
    except Exception:
        raise RuntimeError("AI вернул не JSON")
    if not isinstance(out, list) or len(out) != len(payload):
        raise RuntimeError("AI вернул не то число вопросов")
    res = []
    for src, tr in zip(payload, out):
        if not isinstance(tr, dict):
            raise RuntimeError("AI вернул битый вопрос")
        opts = tr.get("options", [])
        if not isinstance(opts, list) or len(opts) != len(src["options"]):
            raise RuntimeError("AI вернул не то число вариантов")
        res.append({"q": str(tr.get("q", "")), "options": [str(o) for o in opts]})
    # Проверка перевода: если длинные тексты вернулись 1-в-1 без единой
    # казахской буквы — модель эхом вернула исходник, просим повторить.
    _kk = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
    src_all = " ".join(q["q"] for q in payload).strip()
    tr_all = " ".join(r["q"] for r in res).strip()
    if (len(src_all) > 20 and tr_all == src_all
            and not any(ch in _kk for ch in tr_all)):
        raise RuntimeError("AI вернул текст без перевода — нажмите кнопку ещё раз")
    return res
