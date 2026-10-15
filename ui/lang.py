"""Student desktop app strings: Russian (default) + Kazakh.

Persisted in QSettings (Baqylau/Student, key "lang"). No PySide6 import at
module level so parity checks can run without Qt.
"""
from __future__ import annotations

LANGS = ("ru", "kk")

APP_STR = {
    "ru": {
        "login_title": "Вход ученика",
        "login_sub": "Камера включится только после старта экзамена преподавателем.",
        "login_chips": ["Взгляд", "Смартфон", "Защита"],
        "f_server": "Сервер панели:", "f_login": "Логин ученика:",
        "f_pass": "Пароль:", "login_ph": "напр. ivan_petrov",
        "err_need": "Укажите сервер и логин", "err_conn": "Подключение…",
        "err_fail": "Не удалось войти: проверьте сервер, логин и пароль",
        "btn_go": "Войти  →",
        "btn_local": "продолжить в локальном режиме (без сервера)",
        "hero_sub": "Локальный AI-прокторинг  •  Qostanai",
        "app_sub": "Baqylau  •  YOLO + MediaPipe + защита окружения",
        "badge_active": "● Экзамен активен", "badge_going": "● Экзамен идёт",
        "badge_done": "■ Экзамен завершён", "badge_wait": "Ожидание экзамена…",
        "exam_card": "Экзамен", "q_of": "Вопрос",
        "q_badge": "Вопрос", "answered": "Отвечен", "tip_cur": " (текущий)",
        "nav_back": "← Назад", "nav_fwd": "Вперед →", "nav_end": "К концу →",
        "nav_submit": "✔  Отправить ответы",
        "btn_snap": "Сделать снимок", "btn_pause": "Пауза",
        "btn_quit": "■  Завершить",
        "result": "Результат", "time_up": "Время вышло — отправляю…",
        "left": "Осталось",
    },
    "kk": {
        "login_title": "Оқушы кіруі",
        "login_sub": "Камера мұғалім емтиханды бастағаннан кейін ғана қосылады.",
        "login_chips": ["Көзқарас", "Смартфон", "Қорғау"],
        "f_server": "Панель сервері:", "f_login": "Оқушы логині:",
        "f_pass": "Құпия сөз:", "login_ph": "мыс. ivan_petrov",
        "err_need": "Сервер мен логинді көрсетіңіз", "err_conn": "Қосылуда…",
        "err_fail": "Кіру мүмкін болмады: сервер, логин және құпия сөзді тексеріңіз",
        "btn_go": "Кіру  →",
        "btn_local": "жергілікті режимде жалғастыру (серверсіз)",
        "hero_sub": "Жергілікті AI-прокторинг  •  Qostanai",
        "app_sub": "Baqylau  •  YOLO + MediaPipe + ортаны қорғау",
        "badge_active": "● Емтихан белсенді", "badge_going": "● Емтихан жүріп жатыр",
        "badge_done": "■ Емтихан аяқталды", "badge_wait": "Емтихан күтілуде…",
        "exam_card": "Емтихан", "q_of": "Сұрақ",
        "q_badge": "Сұрақ", "answered": "Жауап берілді", "tip_cur": " (ағымдағы)",
        "nav_back": "← Артқа", "nav_fwd": "Алға →", "nav_end": "Соңына →",
        "nav_submit": "✔  Жауаптарды жіберу",
        "btn_snap": "Суретке түсіру", "btn_pause": "Кідірту",
        "btn_quit": "■  Аяқтау",
        "result": "Нәтиже", "time_up": "Уақыт бітті — жіберілуде…",
        "left": "Қалды",
    },
}


def app_lang() -> str:
    try:
        from PySide6.QtCore import QSettings
        lang = str(QSettings("Baqylau", "Student").value("lang", "ru"))
    except Exception:
        lang = "ru"
    return lang if lang in LANGS else "ru"


def set_app_lang(code: str) -> None:
    if code not in LANGS:
        return
    try:
        from PySide6.QtCore import QSettings
        QSettings("Baqylau", "Student").setValue("lang", code)
    except Exception:
        pass


def tr(lang: str, key: str):
    return APP_STR.get(lang, APP_STR["ru"]).get(key, APP_STR["ru"].get(key, key))
