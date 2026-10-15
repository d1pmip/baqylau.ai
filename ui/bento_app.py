"""Soft Bento Grid desktop HUD (PySide6).

Layout:
  Header: avatar + student name + status badge + session timer + EEG/cam dots
  Grid:  Video (dark) | EEG attention (purple) / Fatigue (green) | Alerts (yellow)
  Controls: Snapshot / Pause / Quit pastel buttons.

Video + EEG run in separate QThreads so the UI never freezes.
Console logs are redirected into the alert feed / status widgets.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time

from PySide6.QtCore import (QEasingCurve, QObject, QParallelAnimationGroup,
                             QPropertyAnimation, QSequentialAnimationGroup,
                             QThread, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QProgressBar,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .lang import app_lang as _app_lang, set_app_lang as _set_app_lang
from .lang import tr as _tr_str

logger = logging.getLogger(__name__)

LEVEL_COLORS = {
    "LOW": "#2E7D32",
    "MEDIUM": "#EF6C00",
    "HIGH": "#C62828",
    "CRITICAL": "#7B0000",
}

# Человекочитаемые статусы для ленты «Последние события».
# Никаких «Риск 100» / RISK100 в UI быть не должно — только понятный текст.
EVENT_TITLES = {
    "phone": "Обнаружен телефон",
    "phone_raised": "Обнаружен телефон, поднят к экрану (вероятно съёмка)",
    "reading_device": "Взгляд вниз + телефон (вероятно чтение с устройства)",
    "head_turn": "Обнаружен поворот головы (вероятно списывание)",
    "head_sustained": "Голова повёрнута долго (вероятно списывание)",
    "head_stress": "Поворот головы + стресс (вероятно списывание)",
    "gaze_off": "Взгляд мимо экрана",
    "gaze_sustained": "Взгляд мимо долго (вероятно шпаргалка)",
    "gaze_focus": "Взгляд в сторону + фокус (вероятно шпаргалка)",
    "gaze_relaxed": "Взгляд в сторону (рассеянность)",
    "face_missing": "Лицо не в кадре",
    "multi_person": "Обнаружено второе лицо",
    "shortcut": "Запрещённая клавиша",
    "focus_lost": "Уход из окна экзамена",
    "jaw": "Возможно разговор",
    "drowsy": "Усталость / сонливость",
    "snapshot": "Попытка создания скриншота",
    "snapshot_ok": "Скриншот сохранён",
    "snapshot_fail": "Не удалось создать скриншот",
    "evidence": "Сохранено доказательство (фото/видео)",
    "calibration": "Калибровка",
    "session": "Сессия",
    "exam": "Экзамен",
    "warn": "Предупреждение",
}

_RISK100_RE = re.compile(r"(?i)\bRISK\s*100\b|RISK100|риск\s*100\b")
# Пути к файлам скриншотов/доказательств в деталях — ученику не показываем.
_SHOT_PATH_RE = re.compile(
    r"\s*\[[^\]]*(?:snapshots|SCREEN_|CRITICAL_|EVIDENCE_|\.png|\.jpg|\.mp4)[^\]]*\]",
    re.IGNORECASE)


def human_event_title(kind: str) -> str:
    return EVENT_TITLES.get(str(kind or "").strip(), str(kind or "Событие"))


def clean_event_text(text: str, fallback_kind: str = "") -> str:
    """Убрать любые упоминания «Риск 100»/RISK100 и пути скриншотов."""
    t = str(text or "")
    t = _RISK100_RE.sub("", t)
    t = _SHOT_PATH_RE.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -:;,. ")
    if not t:
        return human_event_title(fallback_kind)
    return t


def format_feed_text(level: str, kind: str, detail: str) -> str:
    title = human_event_title(kind)
    body = clean_event_text(detail, fallback_kind=kind)
    # Не дублируем заголовок, если detail уже начинается с него.
    if body.lower().startswith(title.lower()):
        return f"[{level}] {body}"
    return f"[{level}] {title}: {body}" if body else f"[{level}] {title}"


class FatigueRing(QWidget):
    """Circular fatigue indicator (0-10 scale). Repaints on set_value()."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self.setMinimumSize(130, 130)
        self.setMaximumSize(160, 160)

    def set_value(self, v: float) -> None:
        self._value = max(0.0, min(10.0, float(v)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            side = min(self.width(), self.height()) - 16
            x, y = (self.width() - side) // 2, (self.height() - side) // 2
            p.setPen(QPen(QColor("#FFFFFF"), 12, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(x, y, side, side, 0, 360 * 16)
            frac = self._value / 10.0
            color = "#2E7D32" if frac < 0.4 else ("#EF6C00" if frac < 0.65 else "#C62828")
            p.setPen(QPen(QColor(color), 12, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(x, y, side, side, 90 * 16, -int(360 * 16 * frac))
            p.setPen(Qt.black)
            p.drawText(x, y, side, side, Qt.AlignCenter, f"{self._value:.2f}")
            p.end()
        except Exception:
            pass


class VideoWorker(QObject):
    """Camera + VisionEngine loop. Emits annotated QImage + VisionState + BGR frame."""

    frame_ready = Signal(object, object, object)  # (QImage, VisionState, BGR np.ndarray)
    finished = Signal()

    def __init__(self, vision, camera: int = 0, mirror: bool = False) -> None:
        super().__init__()
        self._vision = vision
        self._camera = camera
        self._mirror = mirror
        self._running = True

    def stop(self) -> None:
        self._running = False

    def arm(self) -> None:
        self._running = True

    def run(self) -> None:
        import cv2

        cap = cv2.VideoCapture(self._camera)
        if not cap.isOpened():
            logger.error("Cannot open camera %s", self._camera)
            self.finished.emit()
            return
        while self._running:
            ok, frame = cap.read()
            if not ok or frame is None:
                QThread.msleep(50)
                continue
            if self._mirror:
                frame = cv2.flip(frame, 1)
            try:
                state = self._vision.process(frame)
                annotated = self._vision.annotate(frame.copy(), state)
                rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
                self.frame_ready.emit(img, state, frame.copy())
            except Exception as exc:  # never kill the thread
                logger.debug("video tick failed: %s", exc)
            QThread.msleep(33)  # ~30 fps
        cap.release()
        self.finished.emit()


class EEGWorker(QObject):
    """EEGEngine loop at ~4 Hz. Emits EEGState."""

    eeg_ready = Signal(object)
    finished = Signal()

    def __init__(self, eeg) -> None:
        super().__init__()
        self._eeg = eeg
        self._running = True
        self._step = 0.25

    def stop(self) -> None:
        self._running = False

    def arm(self) -> None:
        self._running = True

    def run(self) -> None:
        if self._eeg is not None:
            try:
                self._eeg.start()
            except Exception as exc:
                logger.warning("EEG start failed: %s", exc)
        while self._running:
            try:
                if self._eeg is not None:
                    self.eeg_ready.emit(self._eeg.tick())
            except Exception as exc:
                logger.debug("eeg tick failed: %s", exc)
            # QThread.msleep takes int ms; accumulate precisely enough
            QThread.msleep(int(self._step * 1000))
        try:
            if self._eeg is not None:
                self._eeg.stop()
        except Exception:
            pass
        self.finished.emit()


class BentoWindow(QMainWindow):
    """Main Soft-Bento window. Owns fusion (RiskEngine + Synchronizer)."""

    def __init__(self, cfg, args, vision, eeg, risk, sync, alerts,
                 student: str = "Student", security=None,
                 client=None, standalone: bool = True) -> None:
        super().__init__()
        self.cfg, self.args = cfg, args
        self.vision, self.eeg = vision, eeg
        self.risk, self.sync, self.alerts = risk, sync, alerts
        self.security = security
        self.client = client
        self.standalone = standalone
        self._paused = False
        self._t0 = time.time()
        self._last_frame = None
        self._last_bgr = None
        self._risk_state = None
        self._calib_was = False
        self._last_video_fuse = 0.0
        self._video_fuse_min_dt = 0.5  # vision-only fuse не чаще 2 Гц
        self._last_sec_t = 0.0
        # session gating (server mode): camera OFF until teacher starts exam
        self._session_id = None
        self._session_active = standalone
        self._vthread = None
        self._vworker = None
        self._ethread = None
        self._eworker = None
        # evidence ring buffer: ~9s @10fps, 320px (клипы при максимальном риске)
        from collections import deque as _dq
        self._ring = _dq(maxlen=90)
        self._ring_tick = 0
        self._last_evidence_t = 0.0
        self._last_thumb_t = 0.0
        self._thumb_interval = float(getattr(args, "thumb_interval", 4.0) or 4.0)
        # exam widget state (режим одного вопроса: QStackedWidget + точки)
        self._exam_deadline = 0.0
        self._exam_groups = []
        self._exam_current = 0
        self._exam_dots: list = []
        self._exam_pages: list = []
        self._exam_pixmaps: dict = {}
        self._exam_img_labels: dict = {}
        self._exam_badges: dict = {}
        # анимации: переход вопросов + пульс точек + hover-тень + нажатия
        self._exam_anim = None
        self._exam_anim_groups: list = []
        self._exam_nav_seq = 0
        self._dot_anims: list = []
        self._hover_anims: dict = {}
        self._hover_base_pos: dict = {}
        self._press_anims: list = []
        # язык интерфейса + язык варианта экзамена (RU/KK)
        self._app_lang = _app_lang()
        self._exam_lang = self._app_lang
        self._build_ui(student)
        self._build_workers()
        if standalone:
            self._start_capture()
        else:
            self._sess_timer = QTimer(self)
            self._sess_timer.timeout.connect(self._poll_session)
            self._sess_timer.start(5000)
            self.badge.setText(self._t("badge_wait"))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_clock)
        self._timer.start(1000)
        self._hb_timer = QTimer(self)
        self._hb_timer.timeout.connect(self._heartbeat)
        self._hb_timer.start(int(self._thumb_interval * 1000))

    # -- UI ------------------------------------------------------------
    def _card(self, name: str) -> QFrame:
        f = QFrame()
        f.setObjectName(name)
        f.setProperty("card", True)
        f.setLayout(QVBoxLayout())
        # Веб-панель: .kpi padding 18px 20px, .exam 22px 24px, gap 14-16.
        f.layout().setContentsMargins(18, 18, 20, 18)
        f.layout().setSpacing(12)
        return f

    def _build_ui(self, student: str) -> None:
        self.setWindowTitle("Baqylau — Student")
        self.resize(1240, 780)
        self.setMinimumSize(1024, 640)
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        # Веб .wrap: max-width 1240px, padding 20px 20px 28px, gap 14.
        lay.setSpacing(14)
        lay.setContentsMargins(20, 20, 20, 28)

        # Header (веб .top: padding 12px 16px 12px 14px, gap 12-28, sticky-card)
        header = QFrame()
        header.setObjectName("HeaderCard")
        header.setProperty("card", True)
        hl = QHBoxLayout(header)
        hl.setSpacing(12)
        hl.setContentsMargins(14, 10, 16, 10)
        avatar = QLabel(student[:1].upper() or "S")
        avatar.setObjectName("Avatar")
        avatar.setAlignment(Qt.AlignCenter)
        title_box = QVBoxLayout()
        title_box.addWidget(self._mk("AppTitle", student))
        self.app_sub_lbl = self._mk("AppSub", self._t("app_sub"))
        title_box.addWidget(self.app_sub_lbl)
        hl.addWidget(avatar)
        hl.addLayout(title_box)
        hl.addStretch(1)
        self.badge = QLabel(self._t("badge_active"))
        self.badge.setObjectName("Badge")
        hl.addWidget(self.badge)
        self.clock = QLabel("00:00")
        self.clock.setObjectName("CardTitle")
        hl.addWidget(self.clock)
        self.eeg_dot = QLabel("● EEG")
        self.eeg_dot.setObjectName("Dot")
        self.cam_dot = QLabel("● CAM")
        self.cam_dot.setObjectName("Dot")
        self.sec_dot = QLabel("● SEC")
        self.sec_dot.setObjectName("Dot")
        self.sec_dot.setStyleSheet("color:#999999;")
        hl.addWidget(self.eeg_dot)
        hl.addWidget(self.cam_dot)
        hl.addWidget(self.sec_dot)
        # Тумблер языка интерфейса + варианта экзамена: RU / KZ.
        from PySide6.QtWidgets import QButtonGroup as _QBG
        self.btn_lang_ru = QPushButton("RU")
        self.btn_lang_kk = QPushButton("KZ")
        self._lang_group = _QBG(self)
        for _lb in (self.btn_lang_ru, self.btn_lang_kk):
            _lb.setCheckable(True)
            _lb.setFixedSize(46, 28)
            _lb.setCursor(Qt.PointingHandCursor)
            self._lang_group.addButton(_lb)
            hl.addWidget(_lb)
        self.btn_lang_ru.clicked.connect(lambda: self._switch_app_lang("ru"))
        self.btn_lang_kk.clicked.connect(lambda: self._switch_app_lang("kk"))
        try:
            self._sync_lang_buttons()
        except Exception:
            pass
        lay.addWidget(header)

        # Grid
        grid = QGridLayout()
        grid.setSpacing(14)

        # Video (dark, spans 2 rows)
        self.video_card = self._card("VideoCard")
        self.video_card.layout().addWidget(self._mk_dark_title("Камера  •  YOLOv8 + gaze"))
        self.calib_label = QLabel("")
        self.calib_label.setStyleSheet(
            "background:#FFF4A3; color:#1A1A1A; font-weight:800; font-size:15px;"
            "border-radius:14px; padding:10px;")
        self.calib_label.setAlignment(Qt.AlignCenter)
        self.calib_label.setVisible(False)
        self.video_card.layout().addWidget(self.calib_label)
        self.video = QLabel("Ожидание камеры…")
        self.video.setObjectName("VideoLabel")
        self.video.setAlignment(Qt.AlignCenter)
        # Веб .cam aspect-ratio 16/10, radius 14: компактное превью без giant-пустоты.
        self.video.setMinimumSize(520, 325)
        self.video_card.layout().addWidget(self.video, 1)
        self.vision_line = QLabel("pose: —   gaze: —   persons: —")
        self.vision_line.setStyleSheet("color:#BBBBBB;")
        self.video_card.layout().addWidget(self.vision_line)
        grid.addWidget(self.video_card, 0, 0, 2, 1)

        # EEG (purple)
        self.eeg_card = self._card("EegCard")
        self.eeg_card.layout().addWidget(self._mk("CardTitle", "EEG  •  Внимание (Muse 1.3)"))
        self.focus_wrap, self.focus_bar = self._bar("FocusBar", "Фокус")
        self.stress_wrap, self.stress_bar = self._bar("StressBar", "Стресс")
        self.alpha_lbl = QLabel("α/β/θ: —")
        self.eeg_card.layout().addWidget(self.focus_wrap)
        self.eeg_card.layout().addWidget(self.stress_wrap)
        self.eeg_card.layout().addWidget(self.alpha_lbl)
        self.artifact_lbl = QLabel("артефакты: —")
        self.eeg_card.layout().addWidget(self.artifact_lbl)
        grid.addWidget(self.eeg_card, 0, 1)

        # Fatigue (green)
        self.fat_card = self._card("FatigueCard")
        self.fat_card.layout().addWidget(self._mk("CardTitle", "Усталость / Fatigue"))
        row = QHBoxLayout()
        self.ring = FatigueRing()
        row.addWidget(self.ring)
        right = QVBoxLayout()
        self.fatigue_wrap, self.fatigue_bar = self._bar("FatigueBar", "Fatigue")
        self.risk_big = QLabel("RISK 0")
        self.risk_big.setObjectName("RiskBig")
        self.risk_level = QLabel("OK")
        self.risk_level.setObjectName("CardTitle")
        right.addWidget(self.fatigue_wrap)
        right.addWidget(self.risk_big)
        right.addWidget(self.risk_level)
        # Сегмент-бар риска как .track на сайте (пороги 20/45/65/100).
        _segs = QHBoxLayout()
        _segs.setContentsMargins(0, 0, 0, 0)
        _segs.setSpacing(4)
        self._risk_segs = []
        for _ in range(4):
            _s = QFrame()
            _s.setObjectName("RiskSeg")
            _s.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            _segs.addWidget(_s)
            self._risk_segs.append(_s)
        right.addLayout(_segs)
        row.addLayout(right, 1)
        self.fat_card.layout().addLayout(row)
        grid.addWidget(self.fat_card, 0, 2)

        # Alerts — как веб-блок «Последние события» (aside.card.ev, padding 18px).
        self.alerts_card = self._card("AlertsCard")
        self.alerts_card.layout().addWidget(self._mk("CardTitle", "Последние события"))
        self.feed = QListWidget()
        self.feed.setMinimumHeight(180)
        self.feed.setMaximumHeight(260)
        self.alerts_card.layout().addWidget(self.feed, 1)
        grid.addWidget(self.alerts_card, 1, 1, 1, 2)

        # Exam card: режим одного вопроса на весь экран (без общего скролла).
        self.exam_card = self._card("ExamCard")
        self.exam_card_title = self._mk("CardTitle", self._t("exam_card"))
        self.exam_card.layout().addWidget(self.exam_card_title)
        self.exam_title = QLabel("—")
        self.exam_card.layout().addWidget(self.exam_title)
        self.exam_timer = QLabel("")
        self.exam_timer.setObjectName("CardTitle")
        self.exam_card.layout().addWidget(self.exam_timer)
        from PySide6.QtWidgets import QStackedWidget
        self.exam_progress = QLabel("")
        self.exam_progress.setObjectName("CardTitle")
        self.exam_progress.setAlignment(Qt.AlignCenter)
        self.exam_card.layout().addWidget(self.exam_progress)
        self.exam_stack = QStackedWidget()
        self.exam_stack.setMinimumHeight(420)
        self.exam_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        try:
            self.exam_stack.installEventFilter(self)
        except Exception:
            pass
        self.exam_card.layout().addWidget(self.exam_stack, 1)
        self.exam_card.setVisible(False)
        grid.addWidget(self.exam_card, 2, 0, 1, 3)

        # Отдельный нижний блок навигации по заданиям: Назад/Вперед,
        # точки-индикаторы, отправка и статус скриншотов. Виден всегда
        # вместе с карточкой экзамена (в т.ч. в exam-only режиме ученика).
        self.exam_nav_card = self._card("ExamNavCard")
        # Навигация: компактные кнопки (не на всю ширину), стиль из палитры.
        self.btn_prev = QPushButton(self._t("nav_back"))
        self.btn_prev.setObjectName("BtnPause")
        self.btn_next = QPushButton(self._t("nav_fwd"))
        self.btn_next.setObjectName("BtnSnap")
        for _nb in (self.btn_prev, self.btn_next):
            _nb.setFixedHeight(40)
            _nb.setFixedWidth(220)
            _nb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            try:
                _nb.installEventFilter(self)
            except Exception:
                pass
        self.btn_prev.clicked.connect(self._exam_prev)
        self.btn_next.clicked.connect(self._exam_next)
        _nav_row = QHBoxLayout()
        _nav_row.setContentsMargins(0, 0, 0, 0)
        _nav_row.setSpacing(8)
        _nav_row.addStretch(1)
        _nav_row.addWidget(self.btn_prev)
        _nav_row.addWidget(self.btn_next)
        _nav_row.addStretch(1)
        self.exam_nav_card.layout().addLayout(_nav_row)
        # Точки-индикаторы: зелёная — текущий, серые — остальные, кликабельны.
        _dots_wrap = QWidget()
        _dots_wrap.setLayout(QHBoxLayout())
        _dots_wrap.layout().setContentsMargins(0, 2, 0, 0)
        _dots_wrap.layout().setSpacing(6)
        _dots_wrap.layout().setAlignment(Qt.AlignCenter)
        self._exam_dots_wrap = _dots_wrap
        self.exam_nav_card.layout().addWidget(_dots_wrap)
        self.btn_submit = QPushButton(self._t("nav_submit"))
        self.btn_submit.setObjectName("BtnSnap")
        self.btn_submit.clicked.connect(self._submit_exam)
        # Веб .btn.big центрируется; в десктопе — компактная кнопка по центру.
        # Все три кнопки навигации одного размера: 220x40.
        self.btn_submit.setFixedHeight(40)
        self.btn_submit.setFixedWidth(220)
        self.btn_submit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        try:
            self.btn_submit.installEventFilter(self)
        except Exception:
            pass
        _submit_row = QHBoxLayout()
        _submit_row.setContentsMargins(0, 2, 0, 0)
        _submit_row.setSpacing(8)
        _submit_row.addStretch(1)
        _submit_row.addWidget(self.btn_submit)
        _submit_row.addStretch(1)
        self.exam_nav_card.layout().addLayout(_submit_row)
        self.exam_nav_card.setVisible(False)
        grid.addWidget(self.exam_nav_card, 3, 0, 1, 3)

        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)
        lay.addLayout(grid, 1)

        # Controls — компактные кнопки как веб .btn (padding 10px 18px,
        # radius 12px), без растягивания на всю ширину.
        ctrl = QFrame()
        ctrl.setObjectName("ControlCard")
        ctrl.setProperty("card", True)
        cl = QHBoxLayout(ctrl)
        cl.setSpacing(8)
        cl.setContentsMargins(14, 10, 14, 10)
        self.btn_snap = QPushButton(self._t("btn_snap"))
        self.btn_snap.setObjectName("BtnSnap")
        self.btn_pause = QPushButton(self._t("btn_pause"))
        self.btn_pause.setObjectName("BtnPause")
        self.btn_quit = QPushButton(self._t("btn_quit"))
        self.btn_quit.setObjectName("BtnQuit")
        self.btn_snap.clicked.connect(self._snapshot)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_quit.clicked.connect(self.close)
        for _b in (self.btn_snap, self.btn_pause, self.btn_quit):
            _b.setFixedHeight(36)
            _b.setMinimumWidth(120)
            _b.setMaximumWidth(180)
            _b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            try:
                _b.installEventFilter(self)
            except Exception:
                pass
        cl.addWidget(self.btn_snap)
        cl.addWidget(self.btn_pause)
        cl.addWidget(self.btn_quit)
        cl.addStretch(1)
        lay.addWidget(ctrl)
        self._apply_role_view()

    def _apply_role_view(self) -> None:
        """Exam-only view for students (server mode): test UI only.

        Camera, EEG, fatigue, risk and violation feed are hidden — full
        telemetry goes to the teacher/admin web panel instead.
        """
        if self.standalone:
            return
        for w in (self.video_card, self.eeg_card, self.fat_card, self.alerts_card,
                  self.btn_snap, self.btn_pause):
            try:
                w.setVisible(False)
            except Exception:
                pass

    def _t(self, key: str):
        return _tr_str(self._app_lang, key)

    def _switch_app_lang(self, code: str) -> None:
        """Тумблер RU/KZ: интерфейс + перезагрузка варианта экзамена с ответами."""
        try:
            if code not in ("ru", "kk") or code == self._app_lang:
                try:
                    self._sync_lang_buttons()
                except Exception:
                    pass
                return
            saved = {}
            try:
                for i, grp in enumerate(getattr(self, "_exam_groups", []) or []):
                    saved[i] = grp.checkedId()
            except Exception:
                saved = {}
            self._app_lang = code
            _set_app_lang(code)
            self._apply_app_lang()
            try:
                if (self.client is not None and not self.standalone
                        and self.exam_card.isVisible() and self._session_active):
                    self._load_exam(code)
                    for i, cid in saved.items():
                        try:
                            if 0 <= i < len(self._exam_groups) and cid is not None and cid >= 0:
                                btn = self._exam_groups[i].button(cid)
                                if btn is not None:
                                    btn.setChecked(True)
                        except Exception:
                            continue
                    try:
                        self._paint_opts(self._exam_groups[self._exam_current])
                    except Exception:
                        pass
                    for i in range(len(self._exam_groups)):
                        self._refresh_q_badge(i)
            except Exception:
                pass
            try:
                self._sync_lang_buttons()
            except Exception:
                pass
        except Exception:
            pass

    def _sync_lang_buttons(self) -> None:
        try:
            self.btn_lang_ru.setChecked(self._app_lang == "ru")
            self.btn_lang_kk.setChecked(self._app_lang == "kk")
        except Exception:
            pass

    def _apply_app_lang(self) -> None:
        """Перевести весь хром интерфейса без перезапуска."""
        try:
            self.app_sub_lbl.setText(self._t("app_sub"))
        except Exception:
            pass
        try:
            self.exam_card_title.setText(self._t("exam_card"))
        except Exception:
            pass
        try:
            self.btn_prev.setText(self._t("nav_back"))
            self.btn_next.setText(self._t("nav_end") if self._at_last_exam() else self._t("nav_fwd"))
            self.btn_submit.setText(self._t("nav_submit"))
            self.btn_snap.setText(self._t("btn_snap"))
            self.btn_pause.setText(self._t("btn_pause"))
            self.btn_quit.setText(self._t("btn_quit"))
        except Exception:
            pass
        try:
            if self._session_active:
                self.badge.setText(self._t("badge_going"))
            else:
                self.badge.setText(self._t("badge_wait"))
        except Exception:
            pass
        try:
            self._tick_clock()
        except Exception:
            pass
        try:
            n = len(getattr(self, "_exam_pages", []) or [])
            if n:
                self.exam_progress.setText(
                    f"{self._t('q_of')} {int(self._exam_current) + 1} / {n}")
        except Exception:
            pass

    def _at_last_exam(self) -> bool:
        try:
            n = len(getattr(self, "_exam_pages", []) or [])
            return n > 0 and int(self._exam_current) >= n - 1
        except Exception:
            return False

    def _mk(self, obj: str, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(obj)
        return lbl

    def _mk_dark_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#FFFFFF; font-weight:700; font-size:13px;")
        return lbl

    def _bar(self, obj: str, title: str):
        """Create labeled progress bar. Returns (wrap, bar); keep both refs alive."""
        wrap = QWidget()
        box = QVBoxLayout(wrap)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel(title))
        bar = QProgressBar(wrap)
        bar.setObjectName(obj)
        bar.setRange(0, 100)
        box.addWidget(bar)
        return wrap, bar

    # -- threads --------------------------------------------------------
    def _build_workers(self) -> None:
        """Create worker objects (threads start in _start_capture)."""
        self._vworker = VideoWorker(self.vision, self.args.camera, self.args.mirror)
        self._eworker = EEGWorker(self.eeg) if self.eeg is not None else None

    def _start_capture(self) -> None:
        """Open camera + start CV/EEG threads. No-op if already running."""
        if self._vthread is not None:
            return
        self._vworker.arm()
        self._vthread = QThread(self)
        self._vworker.moveToThread(self._vthread)
        self._vthread.started.connect(self._vworker.run)
        self._vworker.frame_ready.connect(self._on_frame)
        self._vthread.start()
        if self._eworker is not None and self._ethread is None:
            self._eworker.arm()
            self._ethread = QThread(self)
            self._eworker.moveToThread(self._ethread)
            self._ethread.started.connect(self._eworker.run)
            self._eworker.eeg_ready.connect(self._on_eeg)
            self._ethread.start()
        logger.info("Capture started (camera ON)")

    def _stop_capture(self) -> None:
        """Stop threads + release camera (exam finished / idle)."""
        try:
            if self._vworker is not None:
                self._vworker.stop()
            if self._vthread is not None:
                self._vthread.quit()
                self._vthread.wait(2000)
        except Exception:
            pass
        self._vthread = None
        try:
            if self._eworker is not None:
                self._eworker.stop()
            if self._ethread is not None:
                self._ethread.quit()
                self._ethread.wait(2000)
        except Exception:
            pass
        self._ethread = None
        self.video.setText("Камера выключена — ожидайте начала экзамена")
        self.video.setPixmap(QPixmap())
        try:
            self.cam_dot.setStyleSheet("color:#999999;")
        except Exception:
            pass
        logger.info("Capture stopped (camera OFF)")

    # -- slots ----------------------------------------------------------
    def _on_frame(self, img: QImage, vstate, bgr=None) -> None:
        if self._paused:
            return
        self._last_frame = (img, vstate)
        self._last_bgr = bgr.copy() if bgr is not None else None
        # evidence ring: every 3rd frame @320px (~10fps x 9s)
        try:
            self._ring_tick += 1
            if self._ring_tick % 3 == 0 and bgr is not None:
                import cv2

                h, w = bgr.shape[:2]
                sc = 320.0 / max(1, w)
                self._ring.append(cv2.resize(bgr, (320, max(1, int(h * sc)))))
        except Exception:
            pass
        self.sync.push_vision(vstate)
        pix = QPixmap.fromImage(img).scaled(
            self.video.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.video.setPixmap(pix)
        self.cam_dot.setStyleSheet("color:#2E7D32;")
        # подсказка калибровки (читается из estimator, benign race)
        try:
            prompt = self.vision.pose_gaze.calib_prompt
        except Exception:
            prompt = ""
        self.calib_label.setVisible(bool(prompt))
        if prompt:
            self.calib_label.setText(prompt)
            self._calib_was = True
        elif getattr(self, "_calib_was", False):
            self._calib_was = False
            pg = self.vision.pose_gaze
            self._push_feed("LOW", "calibration",
                            f"Готово: yaw[{pg.yaw_left:.0f},{pg.yaw_right:.0f}] "
                            f"pitch[{pg.pitch_down:.0f},{pg.pitch_up:.0f}] "
                            f"gaze>{pg.gaze_thresh:.0f}")
            self.badge.setText(self._t("badge_active"))
        det = vstate.detector
        self.vision_line.setText(
            f"pose: {vstate.pose.direction} yaw {vstate.pose.yaw:+.0f}° pitch {vstate.pose.pitch:+.0f}°  "
            f"gaze: {'OFF' if vstate.gaze.off_screen else 'on'} "
            f"({vstate.gaze.yaw_deg:+.0f},{vstate.gaze.pitch_deg:+.0f})  "
            f"persons: {det.person_count}  phone: {'YES' if det.phone_detected else 'no'}"
        )
        now = time.time()
        if now - self._last_video_fuse >= self._video_fuse_min_dt:
            self._last_video_fuse = now
            self._fuse(vstate, None)

    def _on_eeg(self, estate) -> None:
        if self._paused:
            return
        self.sync.push_eeg(estate)
        m = getattr(estate, "metrics", None)
        ok = bool(getattr(estate, "ok", False) and m is not None)
        self.eeg_dot.setStyleSheet("color:#2E7D32;" if ok else "color:#C62828;")
        if ok:
            self.focus_bar.setValue(int(min(m.focus / 3, 1) * 100))
            self.stress_bar.setValue(int(min(m.stress / 3, 1) * 100))
            self.fatigue_bar.setValue(int(min(m.fatigue / 6, 1) * 100))
            self.ring.set_value(m.fatigue)
            self.alpha_lbl.setText(f"α {m.alpha:.1f}  β {m.beta:.1f}  θ {m.theta:.1f}")
            flags = []
            if m.blink:
                flags.append(f"blink×{m.blink_count}")
            if m.jaw_clench:
                flags.append("JAW")
            if m.focus_drop:
                flags.append("FOCUS-DROP")
            if m.signal_loss:
                flags.append("SIG-LOSS")
            self.artifact_lbl.setText("артефакты: " + (" ".join(flags) if flags else "нет"))
        fused = self.sync.fused()
        if fused is not None:
            self._fuse(fused.vision, fused.eeg)

    def _fuse(self, vision, eeg_state) -> None:
        try:
            sec = self.security.drain_events() if self.security is not None else []
            if sec:
                self._last_sec_t = time.time()
            rs = self.risk.update(vision, eeg_state if eeg_state is not None else self._empty_eeg(), sec)
        except Exception:
            return
        self._risk_state = rs
        # SEC-индикатор: серый выкл / зелёный ок / красный недавнее нарушение
        try:
            if self.security is None or not self.security.active:
                self.sec_dot.setStyleSheet("color:#999999;")
            elif time.time() - self._last_sec_t < 10.0:
                self.sec_dot.setStyleSheet("color:#C62828;")
            else:
                self.sec_dot.setStyleSheet("color:#2E7D32;")
        except Exception:
            pass
        # На калибровке риск заморожен: показываем CAL, в лог/ленту ничего.
        if bool(getattr(vision, "calibrating", False)):
            self.risk_big.setText("RISK —")
            self.risk_level.setText("Калибровка…")
            self.risk_level.setStyleSheet("color:#6B6B6B; font-size:16px; font-weight:800;")
            self.badge.setText("Калибровка…")
            self._paint_risk_segs(0.0, "OK")
            return
        self.risk_big.setText(f"RISK {rs.score:.0f}")
        self.risk_level.setText(rs.level)
        color = LEVEL_COLORS.get(rs.level, "#1A1A1A")
        self.risk_level.setStyleSheet(f"color:{color}; font-size:16px; font-weight:800;")
        self._paint_risk_segs(float(rs.score), rs.level)
        if rs.events:
            # Use stored BGR frame directly (no QImage->numpy conversion).
            frame = getattr(self, "_last_bgr", None)
            try:
                self.alerts.log(rs, frame)
            except Exception:
                pass
            for ev in rs.events:
                self._add_feed_item(ev.level, ev.kind, ev.detail)
        self._maybe_evidence(rs)

    def _add_feed_item(self, level: str, kind: str, detail: str) -> None:
        text = format_feed_text(level, kind, detail)
        item = QListWidgetItem(text)
        item.setForeground(Qt.white if level == "CRITICAL" else Qt.black)
        item.setBackground(Qt.transparent)
        bg = {"LOW": "#E8F5E9", "MEDIUM": "#FFF3E0", "HIGH": "#FFCDD2",
              "CRITICAL": "#7B0000"}.get(level, "#FFFFFF")
        try:
            from PySide6.QtGui import QColor

            item.setBackground(QColor(bg))
        except Exception:
            pass
        try:
            item.setToolTip(f"{human_event_title(kind)} · {level}")
        except Exception:
            pass
        self.feed.addItem(item)
        self.feed.scrollToBottom()
        while self.feed.count() > 200:
            self.feed.takeItem(0)

    def _paint_risk_segs(self, score: float, level: str) -> None:
        """4 сегмента как .track на сайте: заливка по порогам 20/45/65/100."""
        try:
            segs = getattr(self, "_risk_segs", []) or []
            if not segs:
                return
            color = {"LOW": "#2E7D32", "MEDIUM": "#EF6C00", "HIGH": "#C62828",
                     "CRITICAL": "#7B0000"}.get(str(level), "#2E7D32")
            fill = sum(1 for th in (20.0, 45.0, 65.0, 100.0) if float(score) >= th - 1e-9)
            for i, s in enumerate(segs):
                s.setStyleSheet(
                    f"background:{color}; border-radius:4px; min-height:8px; max-height:8px;"
                    if i < fill else
                    "background:#E4E4E4; border-radius:4px; min-height:8px; max-height:8px;")
        except Exception:
            pass

    def _empty_eeg(self):
        class _E:
            ok = False
            metrics = None
        return _E()

    def _snapshot(self) -> None:
        # Ручной снимок: файл тихо сохраняется для преподавателя
        # (alerts.snapshot + лог), ученику ничего про скриншоты не пишем.
        try:
            frame = getattr(self, "_last_bgr", None)
            if self._risk_state is not None and frame is not None:
                self.alerts.snapshot(frame, self._risk_state)
        except Exception as exc:
            logger.warning("snapshot failed: %s", exc)

    def _play_notif(self) -> None:
        """Звук уведомлений (fuckensound.wav, Windows winsound, async)."""
        try:
            import sys as _sys
            base = getattr(_sys, "_MEIPASS", None)
            if not base:
                base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
            p = os.path.join(base, "fuckensound.wav")
            if not os.path.exists(p):
                return
            try:
                import winsound as _w
            except Exception:
                return
            _w.PlaySound(os.path.abspath(p),
                         _w.SND_FILENAME | _w.SND_ASYNC | _w.SND_NODEFAULT)
        except Exception:
            pass

    def _push_feed(self, level: str, kind: str, detail: str) -> None:
        self._add_feed_item(level, kind, detail)

    # -- exam session (server mode: camera OFF until teacher starts) ------
    def _poll_session(self) -> None:
        if self.standalone or self.client is None:
            return
        try:
            s = self.client.session()
        except Exception:
            return
        try:
            status, sid = s.get("status", "idle"), s.get("session_id")
            for wtxt in s.get("warnings", []) or []:
                try:
                    from PySide6.QtWidgets import QMessageBox

                    QMessageBox.warning(self, "Baqylau — предупреждение",
                                        str(wtxt) + "\nВернись к экзамену.")
                    try:
                        self._play_notif()
                    except Exception:
                        pass
                except Exception:
                    pass
                self._push_feed("HIGH", "warn", str(wtxt))
            if status == "active" and sid != self._session_id:
                self._session_id = sid
                self._session_active = True
                self._start_capture()
                self.badge.setText(self._t("badge_going"))
                self._push_feed("LOW", "session", f"Экзамен запущен: {s.get('title', '')}")
                self._load_exam()
            elif self._session_active and self._session_id is not None and status != "active":
                self._session_active = False
                self._stop_capture()
                self.exam_card.setVisible(False)
                try:
                    self.exam_nav_card.setVisible(False)
                except Exception:
                    pass
                self._exam_deadline = 0.0
                self.badge.setText(self._t("badge_done"))
                self._push_feed("LOW", "session", "Экзамен завершён преподавателем")
        except Exception as exc:
            logger.debug("session poll failed: %s", exc)

    def _load_exam(self, lang: str | None = None) -> None:
        try:
            lang = lang or getattr(self, "_exam_lang", "ru")
            self._exam_lang = lang
            ex = self.client.exam(lang)
            qs = ex.get("questions", [])
            self.exam_title.setText(ex.get("title", "Экзамен"))
            # clear old pages/dots
            try:
                while self.exam_stack.count():
                    _w = self.exam_stack.widget(0)
                    self.exam_stack.removeWidget(_w)
                    _w.deleteLater()
            except Exception:
                pass
            try:
                _dl = self._exam_dots_wrap.layout()
                while _dl.count():
                    _it = _dl.takeAt(0)
                    if _it.widget():
                        _it.widget().deleteLater()
            except Exception:
                pass
            from PySide6.QtWidgets import QButtonGroup, QLabel, QRadioButton, QVBoxLayout

            self._exam_groups = []
            self._exam_pages = []
            self._exam_dots = []
            self._exam_pixmaps = {}
            self._exam_img_labels = {}
            self._exam_badges = {}
            self._exam_current = 0
            for qi, q in enumerate(qs):
                # Вопрос с картинкой-заданием: split на две половины —
                # слева текст + варианты, справа картинка во всю высоту.
                # Без картинки — прежняя вертикальная раскладка.
                has_img = False
                pm = None
                if q.get("image"):
                    try:
                        from PySide6.QtGui import QPixmap

                        raw = self.client.image(q["image"]) if self.client else b""
                        if raw:
                            _pm = QPixmap()
                            if _pm.loadFromData(raw) and not _pm.isNull():
                                pm = _pm
                                has_img = True
                    except Exception as exc:
                        logger.debug("question image failed: %s", exc)
                        has_img = False
                from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QRadioButton, QVBoxLayout

                exam_name = str(ex.get("title", "Экзамен"))
                qtext = str(q.get("q", "")).strip() or "(вопрос без текста)"
                qlbl = QLabel(f"{qi + 1}. {qtext}")
                qlbl.setObjectName("ExamQ")
                qlbl.setWordWrap(True)
                meta = QLabel(f"{exam_name} · вопрос {qi + 1}")
                meta.setObjectName("ExamMeta")
                meta.setWordWrap(True)
                grp = QButtonGroup(self)
                _opts: list = []
                for oi, opt in enumerate(q.get("options", [])):
                    rb = QRadioButton(str(opt))
                    rb.setObjectName("ExamOpt")
                    grp.addButton(rb, oi)
                    rb.toggled.connect(lambda _c, g=grp, i=qi: (self._paint_opts(g),
                                                               self._refresh_q_badge(i)))
                    try:
                        rb.installEventFilter(self)
                    except Exception:
                        pass
                    _opts.append(rb)
                self._exam_groups.append(grp)
                if has_img and pm is not None:
                    self._exam_pixmaps[qi] = pm
                    page = QWidget()
                    page.setLayout(QHBoxLayout())
                    page.layout().setContentsMargins(8, 8, 8, 8)
                    page.layout().setSpacing(12)
                    left = QWidget()
                    left.setLayout(QVBoxLayout())
                    left.layout().setContentsMargins(0, 0, 0, 0)
                    left.layout().setSpacing(10)
                    left.layout().setAlignment(Qt.AlignTop)
                    left.layout().addWidget(meta)
                    left.layout().addWidget(qlbl)
                    for rb in _opts:
                        left.layout().addWidget(rb)
                    left.layout().addStretch(1)
                    page.layout().addWidget(left, 1)
                    # Чёрный медиа-бокс как .cam на сайте + бейдж статуса.
                    # Прижат к верху (вровень с вопросом), высоту диктует фото.
                    mbox = QFrame()
                    mbox.setObjectName("MediaBox")
                    mbox.setLayout(QVBoxLayout())
                    mbox.layout().setContentsMargins(6, 6, 6, 6)
                    mbox.layout().setSpacing(6)
                    mbox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
                    _brow = QHBoxLayout()
                    _brow.setContentsMargins(0, 0, 0, 0)
                    _brow.addStretch(1)
                    badge = QLabel(f"{self._t('q_badge')} {qi + 1}")
                    badge.setObjectName("MediaBadge")
                    badge.setProperty("done", False)
                    _brow.addWidget(badge)
                    mbox.layout().addLayout(_brow)
                    img = QLabel()
                    img.setObjectName("ExamImg")
                    img.setAlignment(Qt.AlignCenter)
                    img.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
                    img.setMinimumSize(240, 180)
                    self._exam_img_labels[qi] = img
                    mbox.layout().addWidget(img)
                    self._exam_badges[qi] = badge
                    page.layout().addWidget(mbox, 1, Qt.AlignTop)
                else:
                    # Без картинки — вопрос крупнее на всю ширину карточки.
                    qlbl.setObjectName("ExamQBig")
                    page = QWidget()
                    page.setLayout(QVBoxLayout())
                    page.layout().setContentsMargins(12, 12, 12, 12)
                    page.layout().setSpacing(14)
                    page.layout().setAlignment(Qt.AlignTop)
                    page.layout().addWidget(meta)
                    page.layout().addWidget(qlbl)
                    for rb in _opts:
                        page.layout().addWidget(rb)
                    page.layout().addStretch(1)
                self._exam_pages.append(page)
                self.exam_stack.addWidget(page)
                # кликабельная точка-индикатор (серая 10px; активная станет
                # зелёным овалом 26px через QSS + width-анимацию 150мс)
                dot = QPushButton("")
                dot.setObjectName("ExamDot")
                dot.setMinimumSize(10, 10)
                dot.setMaximumSize(10, 10)
                dot.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                dot.setProperty("active", False)
                dot.setCursor(Qt.PointingHandCursor)
                dot.setToolTip(f"{self._t('q_badge')} {qi + 1}")
                dot.clicked.connect(lambda _c, _i=qi: self._show_exam_index(_i))
                try:
                    dot.installEventFilter(self)
                except Exception:
                    pass
                self._exam_dots_wrap.layout().addWidget(dot)
                self._exam_dots.append(dot)
            self._exam_deadline = (time.time() + int(ex.get("duration_min", 30)) * 60) if qs else 0.0
            self.btn_submit.setEnabled(True)
            self.exam_card.setVisible(True)
            try:
                self.exam_nav_card.setVisible(True)
            except Exception:
                pass
            self._show_exam_index(0, animate=False)
        except Exception as exc:
            logger.warning("exam load failed: %s", exc)

    def _show_exam_index(self, idx: int, animate: bool = True) -> None:
        try:
            n = len(self._exam_pages)
            if n <= 0:
                return
            idx = max(0, min(int(idx), n - 1))
            prev = int(self._exam_current)
            if idx == prev and self.exam_stack.currentIndex() == idx:
                self.exam_progress.setText(f"{self._t('q_of')} {idx + 1} / {n}")
                self._refresh_exam_dots(idx, animate=False)
                try:
                    self.btn_prev.setEnabled(idx > 0)
                    self.btn_next.setText(self._t("nav_fwd") if idx < n - 1 else self._t("nav_end"))
                except Exception:
                    pass
                self._fit_current_image()
                self._fit_current_image_later()
                return
            direction = 1 if idx > prev else -1
            self._exam_current = idx
            if animate:
                self._animate_question_switch(prev, idx, direction)
            else:
                try:
                    self._exam_nav_seq = int(getattr(self, "_exam_nav_seq", 0) or 0) + 1
                except Exception:
                    pass
                self._stop_exam_anims()
                self.exam_stack.setCurrentIndex(idx)
            self.exam_progress.setText(f"{self._t('q_of')} {idx + 1} / {n}")
            self._refresh_exam_dots(idx, animate=animate, prev=prev)
            self._animate_dots_from_bottom()
            try:
                self.btn_prev.setEnabled(idx > 0)
                self.btn_next.setText(self._t("nav_fwd") if idx < n - 1 else self._t("nav_end"))
            except Exception:
                pass
            self._fit_current_image()
            self._fit_current_image_later()
        except Exception as exc:
            logger.debug("show exam index failed: %s", exc)

    def _refresh_exam_dots(self, idx: int, animate: bool = True, prev: int | None = None) -> None:
        try:
            if prev is None:
                prev = idx
            for di, dot in enumerate(self._exam_dots):
                is_active = (di == idx)
                try:
                    dot.setProperty("active", is_active)
                    dot.style().unpolish(dot)
                    dot.style().polish(dot)
                    dot.setToolTip(f"{self._t('q_badge')} {di + 1}" + (self._t("tip_cur") if is_active else ""))
                except Exception:
                    pass
            if animate and prev != idx:
                self._animate_dot_width(prev, False)
                self._animate_dot_width(idx, True)
            else:
                for di, dot in enumerate(self._exam_dots):
                    w = 26 if di == idx else 10
                    try:
                        dot.setMinimumWidth(w)
                        dot.setMaximumWidth(w)
                    except Exception:
                        pass
        except Exception:
            pass

    # -- exam animations (только движение/прозрачность; цвета из палитры) --
    def _stop_exam_anims(self) -> None:
        """Остановить все анимации перехода и сбросить эффекты страниц."""
        try:
            for _g in list(getattr(self, "_exam_anim_groups", []) or []):
                try:
                    _g.stop()
                except Exception:
                    pass
        except Exception:
            pass
        self._exam_anim_groups = []
        self._exam_anim = None
        try:
            for _p in getattr(self, "_exam_pages", []) or []:
                try:
                    _p.setGraphicsEffect(None)
                except Exception:
                    pass
        except Exception:
            pass

    def _animate_question_switch(self, old_idx: int, new_idx: int, direction: int = 1) -> None:
        """Fade out старого (1.0->0.0) + fade/slide in нового за ~200-250мс.

        Каждая навигация получает поколение (sid): колбэки устаревших
        переходов молча выходят, поэтому быстрые клики не оставляют страницу
        с opacity 0 / сдвинутой (налезание вопросов).
        """
        try:
            self._exam_nav_seq = int(getattr(self, "_exam_nav_seq", 0) or 0) + 1
            sid = self._exam_nav_seq
            self._stop_exam_anims()
            pages = getattr(self, "_exam_pages", [])
            if not pages or new_idx < 0 or new_idx >= len(pages):
                self.exam_stack.setCurrentIndex(new_idx)
                return
            try:
                self.exam_stack.layout().activate()
            except Exception:
                pass
            old_page = pages[old_idx] if 0 <= old_idx < len(pages) else None
            new_page = pages[new_idx]
            seq = QSequentialAnimationGroup(self)
            if old_page is not None and old_idx != new_idx:
                try:
                    eff_out = QGraphicsOpacityEffect(old_page)
                    old_page.setGraphicsEffect(eff_out)
                    fade_out = QPropertyAnimation(eff_out, b"opacity", self)
                    fade_out.setDuration(120)
                    fade_out.setStartValue(1.0)
                    fade_out.setEndValue(0.0)
                    fade_out.setEasingCurve(QEasingCurve.OutCubic)
                    seq.addAnimation(fade_out)
                except Exception:
                    pass

            def _swap() -> None:
                if sid != getattr(self, "_exam_nav_seq", 0):
                    return
                try:
                    if old_page is not None:
                        old_page.setGraphicsEffect(None)
                except Exception:
                    pass
                try:
                    self.exam_stack.setCurrentIndex(new_idx)
                except Exception:
                    pass

            # fade+slide нового: параллельная группа 220мс
            par = QParallelAnimationGroup(self)
            try:
                eff_in = QGraphicsOpacityEffect(new_page)
                new_page.setGraphicsEffect(eff_in)
                eff_in.setOpacity(0.0)
                fade_in = QPropertyAnimation(eff_in, b"opacity", self)
                fade_in.setDuration(220)
                fade_in.setStartValue(0.0)
                fade_in.setEndValue(1.0)
                fade_in.setEasingCurve(QEasingCurve.OutCubic)
                par.addAnimation(fade_in)
            except Exception:
                pass
            try:
                from PySide6.QtCore import QPoint as _QP
                base_pos = new_page.pos()
                slide = QPropertyAnimation(new_page, b"pos", self)
                slide.setDuration(220)
                slide.setStartValue(base_pos + _QP(40 * int(direction or 1), 0))
                slide.setEndValue(base_pos)
                slide.setEasingCurve(QEasingCurve.OutCubic)
                par.addAnimation(slide)
            except Exception:
                pass

            def _finish() -> None:
                if sid != getattr(self, "_exam_nav_seq", 0):
                    return
                try:
                    _swap()
                except Exception:
                    pass
                try:
                    par.start()
                except Exception:
                    pass

            def _cleanup() -> None:
                if sid != getattr(self, "_exam_nav_seq", 0):
                    return
                try:
                    new_page.setGraphicsEffect(None)
                except Exception:
                    pass
                try:
                    # вернуть страницу ровно в layout-позицию после slide
                    self.exam_stack.layout().activate()
                except Exception:
                    pass
                # layout устаканился после swap — переподогнать картинку
                try:
                    self._fit_current_image()
                    self._fit_current_image_later()
                except Exception:
                    pass

            try:
                par.finished.connect(_cleanup)
            except Exception:
                pass
            if seq.animationCount() > 0:
                try:
                    seq.finished.connect(_finish)
                except Exception:
                    pass
                self._exam_anim_groups = [seq, par]
                self._exam_anim = seq
                seq.start()
            else:
                _finish()
                self._exam_anim_groups = [par]
                self._exam_anim = par
        except Exception:
            try:
                self.exam_stack.setCurrentIndex(new_idx)
            except Exception:
                pass

    def _animate_dot_width(self, dot_idx: int, active: bool) -> None:
        """Пульс точки 10px <-> 26px за 150мс (зелёный овал — активная)."""
        try:
            if dot_idx < 0 or dot_idx >= len(self._exam_dots):
                return
            dot = self._exam_dots[dot_idx]
            target = 26 if active else 10
            grp = QParallelAnimationGroup(self)
            for prop in (b"minimumWidth", b"maximumWidth"):
                try:
                    a = QPropertyAnimation(dot, prop, self)
                    a.setDuration(150)
                    try:
                        start = dot.minimumWidth() if prop == b"minimumWidth" else dot.maximumWidth()
                    except Exception:
                        start = 10
                    a.setStartValue(int(start))
                    a.setEndValue(int(target))
                    a.setEasingCurve(QEasingCurve.OutCubic)
                    grp.addAnimation(a)
                except Exception:
                    continue
            # держать ссылку, иначе GC остановит анимацию
            try:
                from PySide6.QtCore import QAbstractAnimation as _QAA
                _run = _QAA.State.Running
                self._dot_anims = [g for g in getattr(self, "_dot_anims", [])
                                   if g.state() == _run]
                self._dot_anims = self._dot_anims[-8:]
            except Exception:
                self._dot_anims = getattr(self, "_dot_anims", [])[-8:]
            self._dot_anims.append(grp)
            grp.start()
        except Exception:
            pass

    def _animate_dots_from_bottom(self) -> None:
        """Точки-индикаторы выезжают снизу с fade при смене вопроса (~240мс).

        Анимируются pos (+14px -> место) и opacity; layout не трогаем —
        конечная позиция совпадает с позицией layout, прыжка нет.
        """
        try:
            from PySide6.QtCore import QPoint as _QP
            wrap = getattr(self, "_exam_dots_wrap", None)
            lay = wrap.layout() if wrap is not None else None
            if lay is None:
                return
            kids = []
            for i in range(lay.count()):
                try:
                    _w = lay.itemAt(i).widget()
                except Exception:
                    _w = None
                if _w is not None:
                    kids.append(_w)
            if not kids:
                return
            grp = QParallelAnimationGroup(self)
            for w in kids:
                try:
                    base = w.pos()
                    a = QPropertyAnimation(w, b"pos", self)
                    a.setDuration(240)
                    a.setStartValue(base + _QP(0, 14))
                    a.setEndValue(base)
                    a.setEasingCurve(QEasingCurve.OutCubic)
                    grp.addAnimation(a)
                    eff = QGraphicsOpacityEffect(w)
                    eff.setOpacity(0.0)
                    w.setGraphicsEffect(eff)
                    o = QPropertyAnimation(eff, b"opacity", self)
                    o.setDuration(240)
                    o.setStartValue(0.0)
                    o.setEndValue(1.0)
                    o.setEasingCurve(QEasingCurve.OutCubic)
                    grp.addAnimation(o)
                except Exception:
                    continue

            def _done() -> None:
                try:
                    for w in kids:
                        try:
                            if w.graphicsEffect() is not None:
                                w.setGraphicsEffect(None)
                        except Exception:
                            pass
                except Exception:
                    pass

            try:
                grp.finished.connect(_done)
            except Exception:
                pass
            try:
                self._dot_anims = (getattr(self, "_dot_anims", []) or [])[-8:]
            except Exception:
                self._dot_anims = []
            self._dot_anims.append(grp)
            grp.start()
        except Exception:
            pass

    def _set_hover_shadow(self, widget, on: bool, radius: float = 14.0) -> None:
        """Micro-interaction: тень плавно проявляется/гаснет (мягкий ховер).

        Анимация blurRadius 0<->radius за ~480мс InOutCubic — как длинные
        плавные ховеры на сайте (transition .5s cubic-bezier(.2,.8,.2,1)).
        Тень не трогает layout (сдвиг по pos рвал геометрию и накладывал
        варианты ответов друг на друга).
        """
        try:
            if widget is None:
                return
            from PySide6.QtWidgets import QGraphicsDropShadowEffect
            from PySide6.QtGui import QColor as _QC
            key = id(widget)
            try:
                old = self._hover_anims.get(key)
                if old is not None:
                    old.stop()
            except Exception:
                pass
            eff = None
            try:
                eff = widget.graphicsEffect()
            except Exception:
                pass
            if on:
                if not isinstance(eff, QGraphicsDropShadowEffect):
                    eff = QGraphicsDropShadowEffect(widget)
                    eff.setOffset(0, 2)
                    try:
                        eff.setColor(_QC(26, 26, 26, 90))
                    except Exception:
                        pass
                    try:
                        eff.setBlurRadius(0.0)
                    except Exception:
                        pass
                    widget.setGraphicsEffect(eff)
                anim = QPropertyAnimation(eff, b"blurRadius", self)
                anim.setDuration(480)
                try:
                    anim.setStartValue(float(eff.blurRadius()))
                except Exception:
                    anim.setStartValue(0.0)
                anim.setEndValue(float(radius))
                anim.setEasingCurve(QEasingCurve.InOutCubic)
            else:
                if not isinstance(eff, QGraphicsDropShadowEffect):
                    return
                anim = QPropertyAnimation(eff, b"blurRadius", self)
                anim.setDuration(480)
                try:
                    anim.setStartValue(float(eff.blurRadius()))
                except Exception:
                    anim.setStartValue(float(radius))
                anim.setEndValue(0.0)
                anim.setEasingCurve(QEasingCurve.InOutCubic)

                def _rm() -> None:
                    try:
                        cur = widget.graphicsEffect()
                        if (isinstance(cur, QGraphicsDropShadowEffect)
                                and float(cur.blurRadius()) <= 0.5
                                and not widget.underMouse()):
                            widget.setGraphicsEffect(None)
                    except Exception:
                        pass

                try:
                    anim.finished.connect(_rm)
                except Exception:
                    pass
            self._hover_anims[key] = anim
            anim.start()
        except Exception:
            pass

    def _animate_press(self, widget) -> None:
        """Анимация нажатия: мягкое погружение opacity 1->0.82->1 (~320мс).

        Безопасна для layout (прозрачность, не геометрия). Тень ховера на
        время нажатия снимается и возвращается, если курсор остался на кнопке.
        """
        try:
            if widget is None:
                return
            from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsOpacityEffect
            try:
                had_shadow = isinstance(widget.graphicsEffect(), QGraphicsDropShadowEffect)
            except Exception:
                had_shadow = False
            eff = QGraphicsOpacityEffect(widget)
            try:
                eff.setOpacity(1.0)
            except Exception:
                pass
            widget.setGraphicsEffect(eff)
            grp = QSequentialAnimationGroup(self)
            down = QPropertyAnimation(eff, b"opacity", self)
            down.setDuration(100)
            down.setStartValue(1.0)
            down.setEndValue(0.82)
            down.setEasingCurve(QEasingCurve.InCubic)
            up = QPropertyAnimation(eff, b"opacity", self)
            up.setDuration(220)
            up.setStartValue(0.82)
            up.setEndValue(1.0)
            up.setEasingCurve(QEasingCurve.InOutCubic)
            grp.addAnimation(down)
            grp.addAnimation(up)

            def _done() -> None:
                try:
                    if had_shadow and widget.underMouse():
                        self._set_hover_shadow(widget, True)
                    else:
                        try:
                            if widget.graphicsEffect() is eff:
                                widget.setGraphicsEffect(None)
                        except Exception:
                            pass
                except Exception:
                    pass

            try:
                grp.finished.connect(_done)
            except Exception:
                pass
            try:
                self._press_anims = (getattr(self, "_press_anims", []) or [])[-11:]
                self._press_anims.append(grp)
            except Exception:
                pass
            grp.start()
        except Exception:
            pass

    def eventFilter(self, obj, event) -> bool:
        # Тень-лифт только для кнопок: варианты ответов и точки живут в
        # layout — их двигать/свечить нельзя, иначе налезают друг на друга.
        _lift_btns = (getattr(self, "btn_prev", None), getattr(self, "btn_next", None),
                      getattr(self, "btn_submit", None), getattr(self, "btn_snap", None),
                      getattr(self, "btn_pause", None),
                      getattr(self, "btn_quit", None))
        try:
            from PySide6.QtCore import QEvent as _QE
            et = event.type()
            if obj is getattr(self, "exam_stack", None) and et == _QE.Resize:
                try:
                    self._fit_current_image()
                except Exception:
                    pass
                try:
                    return super().eventFilter(obj, event)
                except Exception:
                    return False
            if et == _QE.MouseButtonPress:
                try:
                    is_btn = isinstance(obj, QPushButton)
                except Exception:
                    is_btn = False
                try:
                    is_opt = getattr(obj, "objectName", lambda: "")() == "ExamOpt"
                except Exception:
                    is_opt = False
                if is_btn or is_opt:
                    self._animate_press(obj)
            elif et in (_QE.HoverEnter, _QE.Enter):
                if obj in _lift_btns:
                    self._set_hover_shadow(obj, True)
            elif et in (_QE.HoverLeave, _QE.Leave):
                if obj in _lift_btns:
                    self._set_hover_shadow(obj, False)
        except Exception:
            pass
        try:
            return super().eventFilter(obj, event)
        except Exception:
            return False

    def _exam_prev(self) -> None:
        self._show_exam_index(self._exam_current - 1)

    def _exam_next(self) -> None:
        n = len(self._exam_pages)
        if n <= 0:
            return
        if self._exam_current >= n - 1:
            # на последнем вопросе — фокус на кнопке отправки
            try:
                self.btn_submit.setFocus()
            except Exception:
                pass
            return
        self._show_exam_index(self._exam_current + 1)

    def _fit_current_image(self, _retry: int = 0) -> None:
        """Масштабировать картинку текущего вопроса под размер окна.

        Повтор через singleShot, пока layout не устаканится: fit по
        промежуточному мелкому стеку давал навсегда мелкую картинку.
        """
        try:
            from PySide6.QtCore import Qt as _Qt, QTimer as _QT
            qi = self._exam_current
            pm = self._exam_pixmaps.get(qi)
            lbl = self._exam_img_labels.get(qi)
            if pm is None or lbl is None:
                return
            sw, sh = self.exam_stack.width(), self.exam_stack.height()
            if not self.exam_card.isVisible() or sw < 240 or sh < 260:
                # layout ещё не устаканился (или окно скрыто): повторить позже.
                # Пока pixmap не выставлен — ждать дольше (до ~5с).
                limit = 40 if lbl.pixmap() is None else 8
                if _retry < limit:
                    _QT.singleShot(120, lambda: self._fit_current_image(_retry + 1))
                return
            # split-раскладка: ширина — по РЕАЛЬНОМУ боксу (иначе pixmap шире
            # label и QLabel обрезает картинку по краям), высота — по стеку.
            _real_w = 0
            try:
                _real_w = int(lbl.width() or 0)
            except Exception:
                _real_w = 0
            if _real_w < 240:
                try:
                    _box = lbl.parent()
                    _real_w = int(_box.width() or 0) - 12 if _box is not None else 0
                except Exception:
                    _real_w = 0
            if _real_w >= 240:
                avail_w = max(220, _real_w - 4)
            else:
                avail_w = max(220, sw // 2 - 60)
            avail_h = max(180, sh - 90)
            cur = lbl.pixmap()
            scaled = pm.scaled(avail_w, avail_h, _Qt.KeepAspectRatio, _Qt.SmoothTransformation)
            if cur is None or cur.width() != scaled.width() or cur.height() != scaled.height():
                lbl.setPixmap(scaled)
        except Exception:
            pass

    def _fit_current_image_later(self) -> None:
        try:
            from PySide6.QtCore import QTimer as _QT
            _QT.singleShot(60, lambda: self._fit_current_image(0))
        except Exception:
            pass

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        try:
            super().resizeEvent(event)
        except Exception:
            pass
        try:
            self._fit_current_image()
        except Exception:
            pass

    @staticmethod
    def _paint_opts(grp) -> None:
        """Highlight the selected answer: filled pill + bold, rest plain."""
        try:
            checked = grp.checkedButton()
            for btn in grp.buttons():
                btn.setProperty("picked", btn is checked)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
        except Exception:
            pass

    def _refresh_q_badge(self, qi: int) -> None:
        """Бейдж медиа-бокса: «Отвечен» (зелёный) или номер вопроса (серый)."""
        try:
            badge = self._exam_badges.get(qi)
            if badge is None:
                return
            answered = False
            try:
                if 0 <= qi < len(self._exam_groups):
                    answered = self._exam_groups[qi].checkedId() != -1
            except Exception:
                pass
            badge.setText(self._t("answered") if answered else f"{self._t('q_badge')} {qi + 1}")
            badge.setProperty("done", bool(answered))
            badge.style().unpolish(badge)
            badge.style().polish(badge)
        except Exception:
            pass

    def _submit_exam(self) -> None:
        try:
            if not self._exam_groups:
                return
            ans = []
            for grp in self._exam_groups:
                ans.append(grp.checkedId())
            r = self.client.submit(ans)
            self._exam_deadline = 0.0
            self.exam_timer.setText(f"{self._t('result')}: {r.get('score', '?')}/{r.get('total', '?')}")
            self.btn_submit.setEnabled(False)
            self._push_feed("LOW", "exam", f"Ответы отправлены: {r.get('score')}/{r.get('total')}")
        except Exception as exc:
            logger.warning("submit failed: %s", exc)

    def _heartbeat(self) -> None:
        """Periodic thumbnail + risk summary to teacher panel (low load)."""
        try:
            if self.standalone or self.client is None or not self._session_active:
                return
            if self._last_bgr is None:
                return
            import cv2

            h, w = self._last_bgr.shape[:2]
            scale = 320.0 / max(1, w)
            small = cv2.resize(self._last_bgr, (320, max(1, int(h * scale))))
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
            if not ok:
                return
            rs = self._risk_state
            viol = [{"kind": e.kind, "detail": e.detail}
                    for e in (rs.events if rs else [])][:5]
            self.client.heartbeat(float(rs.score) if rs else 0.0,
                                  str(rs.level) if rs else "OK",
                                  viol, bytes(buf.tobytes()))
        except Exception as exc:
            logger.debug("heartbeat failed: %s", exc)

    def _maybe_evidence(self, rs) -> None:
        """Максимальный риск -> фото + короткий видеоклип учителю/админу + локально."""
        try:
            if self.standalone or self.client is None or not self._session_active:
                return
            if float(getattr(rs, "score", 0)) < 100.0:
                return
            now = time.time()
            if now - self._last_evidence_t < 60.0:
                return
            self._last_evidence_t = now
            frames = [f.copy() for f in self._ring]
            bgr = self._last_bgr.copy() if self._last_bgr is not None else None
            import threading

            threading.Thread(target=self._evidence_job,
                             args=(float(rs.score), frames, bgr,
                                   int(self._session_id or 0)),
                             daemon=True).start()
        except Exception:
            pass

    def _evidence_job(self, score: float, frames: list, bgr, session_id: int) -> None:
        if not frames and bgr is None:
            logger.warning("evidence skipped: ring buffer empty (no frames recorded)")
            return
        try:
            import cv2
            import os

            try:
                snapdir = self.alerts.dated_snapshot_dir()
            except Exception:
                snapdir = getattr(self.alerts, "snapshot_dir", "data/logs/snapshots")
            os.makedirs(snapdir, exist_ok=True)
            tag = time.strftime("%Y%m%d_%H%M%S")
            jobs = []
            if bgr is not None:
                p = os.path.join(snapdir, f"EVIDENCE_{tag}_{int(score)}.jpg")
                cv2.imwrite(p, bgr)
                jobs.append((p, "photo"))
            if frames:
                h, w = frames[0].shape[:2]
                vp = os.path.join(snapdir, f"EVIDENCE_{tag}_{int(score)}.mp4")
                # avc1 = H.264 in MP4: plays in Chrome/Edge/Firefox.
                # (mp4v = MPEG-4 Part 2: black player in browsers.)
                wr = None
                for fourcc in ("avc1", "mp4v"):
                    try:
                        cand = cv2.VideoWriter(
                            vp, cv2.VideoWriter_fourcc(*fourcc), 10, (w, h))
                        if cand.isOpened():
                            wr = cand
                            break
                        cand.release()
                    except Exception:
                        pass
                if wr is not None:
                    for f in frames:
                        try:
                            wr.write(f)
                        except Exception:
                            pass
                    wr.release()
                    if _playable(vp):
                        jobs.append((vp, "video"))
                    else:
                        try:
                            os.remove(vp)
                        except Exception:
                            pass
                        logger.warning("evidence clip unreadable, dropped")
                else:
                    logger.warning("no video encoder available")
            for path, kind in jobs:
                try:
                    with open(path, "rb") as fh:
                        self.client.evidence(fh.read(), os.path.basename(path),
                                             kind, score, session_id)
                except Exception as exc:
                    logger.debug("evidence upload %s failed: %s", kind, exc)
            logger.info("Evidence saved+sent: %d files (R=%.0f)", len(jobs), score)
        except Exception as exc:
            logger.warning("evidence job failed: %s", exc)

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.badge.setText("⏸ Пауза" if self._paused else "● Экзамен активен")
        self.badge.setProperty("class", "paused" if self._paused else "")
        self.btn_pause.setText("Продолжить" if self._paused else "Пауза")

    def _tick_clock(self) -> None:
        s = int(time.time() - self._t0)
        self.clock.setText(f"{s // 60:02d}:{s % 60:02d}")
        # exam countdown + autosubmit
        try:
            if self._exam_deadline and self._session_active:
                left = int(self._exam_deadline - time.time())
                if left <= 0:
                    self.exam_timer.setText(self._t("time_up"))
                    self._submit_exam()
                else:
                    self.exam_timer.setText(f"{self._t('left')}: {left // 60:02d}:{left % 60:02d}")
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        try:
            if hasattr(self, "_sess_timer"):
                self._sess_timer.stop()
            if hasattr(self, "_hb_timer"):
                self._hb_timer.stop()
        except Exception:
            pass
        try:
            self._stop_capture()
        except Exception:
            pass
        try:
            if self.security is not None:
                self.security.stop()
        except Exception:
            pass
        try:
            self.vision.close()
        except Exception:
            pass
        super().closeEvent(event)


def run_bento(cfg, args, vision, eeg, risk, sync, alerts, student: str = "Дмитрий Горбунов",
              security=None, client=None, standalone: bool = True) -> int:
    """Create QApplication, apply QSS, show BentoWindow. Returns exit code."""
    import os

    app = QApplication.instance() or QApplication([])
    try:
        from core.res import resource

        qss_path = resource("ui", "bento_style.qss")
    except Exception:
        qss_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bento_style.qss")
    try:
        with open(qss_path, encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except Exception as exc:
        logger.warning("QSS load failed: %s", exc)
    try:
        from .branding import app_icon

        _icon = app_icon()
        if _icon is not None and not _icon.isNull():
            app.setWindowIcon(_icon)
    except Exception:
        pass
    win = BentoWindow(cfg, args, vision, eeg, risk, sync, alerts, student, security,
                    client, standalone)
    try:
        from .branding import app_icon

        _wi = app_icon()
        if _wi is not None and not _wi.isNull():
            win.setWindowIcon(_wi)
    except Exception:
        pass
    _run_bento_show(win, args)
    # F11 — переключить fullscreen (удобно и на Windows, и на Linux).
    try:
        from PySide6.QtGui import QAction, QKeySequence

        def _toggle_fs() -> None:
            try:
                if win.isFullScreen():
                    win.showNormal()
                else:
                    win.showFullScreen()
            except Exception:
                pass

        _act = QAction(win)
        _act.setShortcut(QKeySequence("F11"))
        _act.triggered.connect(_toggle_fs)
        win.addAction(_act)
    except Exception:
        pass
    return app.exec()


def apply_windows_kiosk(win) -> bool:
    """Только Windows: убрать кнопку «свернуть» (экзамен без сворачивания).

    Вызывать ДО первого show(). На Linux/macOS не применяется.
    Возвращает True, если киоск-флаги выставлены.
    """
    try:
        from PySide6.QtCore import Qt as _Q
        flags = win.windowFlags()
        flags &= ~_Q.WindowMinimizeButtonHint
        win.setWindowFlags(flags)
        return True
    except Exception:
        return False


def _run_bento_show(win, args) -> None:
    """Кроссплатформенный показ окна.

    На Windows (и с --fullscreen): fullscreen + киоск без кнопки свернуть.
    На Linux/macOS при тестах: обычное окно. Флаг --windowed отменяет всё.
    """
    try:
        want_full = bool(getattr(args, "fullscreen", False))
        want_windowed = bool(getattr(args, "windowed", False))
        is_win = sys.platform.startswith("win") or os.name == "nt"
        if want_windowed:
            win.show()
            return
        if want_full or is_win:
            if is_win:
                apply_windows_kiosk(win)
            try:
                win.showFullScreen()
            except Exception:
                try:
                    win.setWindowState(win.windowState() | Qt.WindowFullScreen)
                    win.show()
                except Exception:
                    win.show()
            return
        win.show()
    except Exception:
        try:
            win.show()
        except Exception:
            pass


def _playable(path: str) -> bool:
    """True when the clip re-opens with >=1 decodable frame. Never raises."""
    try:
        import cv2
        import os

        if not os.path.exists(path) or os.path.getsize(path) < 20_000:
            return False
        cap = cv2.VideoCapture(path)
        try:
            ok, _ = cap.read()
            n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            return bool(ok) and n > 0
        finally:
            cap.release()
    except Exception:
        return False
