"""Startup login for Baqylau Student (Bento look, same palette).

Server mode is default: server URL + student login/password -> validated
against Baqylau Web before the main window opens. "Local mode" skips the
server (dev/offline, camera works immediately, no gating).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget, QFrame)

from .lang import app_lang, set_app_lang
from .lang import tr as _tr_str

logger = logging.getLogger(__name__)

ORG, APP = "Baqylau", "Student"

STYLESHEET = """
QDialog#LoginDialog { background: #F4F6F0; }
QLabel#LoginTitle { font-size: 26px; font-weight: 900; color: #1A1A1A; }
QLabel#LoginSub { font-size: 13px; color: #6B6B6B; }
QFrame#LoginHero { background: #1E1E1E; border-radius: 18px; }
QLabel#HeroEmoji { font-size: 34px; }
QLabel#HeroTitle { color: #FFFFFF; font-size: 19px; font-weight: 900; }
QLabel#HeroSub { color: #BBBBBB; font-size: 12px; }
QLabel#FieldLabel { font-size: 12px; font-weight: 700; color: #1A1A1A; }
QLineEdit { background: #FFFFFF; border: 2px solid #E4E4E4; border-radius: 14px;
    padding: 11px 14px; font-size: 14px; color: #1A1A1A; selection-background-color: #D8D4FF; }
QLineEdit:focus { border: 2px solid #1A1A1A; }
QLabel#LoginError { color: #C62828; font-weight: 700; font-size: 12px; }
QPushButton#LoginGo { background: #1A1A1A; color: #FFFFFF; border: none;
    border-radius: 16px; padding: 13px; font-size: 15px; font-weight: 800; }
QPushButton#LoginGo:hover { background: #333333; }
QPushButton#LoginGo:pressed { background: #000000; }
QPushButton#LoginLocal { background: transparent; color: #6B6B6B; border: none;
    padding: 8px; font-size: 13px; font-weight: 700; text-decoration: underline; }
QPushButton#LoginLocal:hover { color: #1A1A1A; }
QLabel#Chip { background: #D8D4FF; border-radius: 11px; padding: 5px 12px;
    font-size: 12px; font-weight: 700; color: #1A1A1A; }
QLabel#Chip:nth-child(2) { background: #D4F5A6; }
"""


def try_login(server: str, username: str, password: str, timeout: float = 6.0):
    """Validate credentials, return BaqylauClient or None. Never raises."""
    try:
        from core.net.client import BaqylauClient

        c = BaqylauClient(server, timeout=timeout)
        return c if c.login(username, password) else None
    except Exception as exc:
        logger.debug("login failed: %s", exc)
        return None


class LoginDialog(QDialog):
    """Student auth page shown before the main window."""

    def __init__(self, parent: QWidget | None = None, server: str = "",
                 username: str = "") -> None:
        super().__init__(parent)
        self._lang = app_lang()
        self.setObjectName("LoginDialog")
        self.setWindowTitle("Baqylau — вход ученика")
        self.setMinimumWidth(400)
        self.setStyleSheet(STYLESHEET)
        _apply_brand_icon(self)
        self.client = None
        self.local_mode = False
        last = QSettings(ORG, APP).value("server", "http://127.0.0.1:5050")

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(26, 24, 26, 24)

        langrow = QHBoxLayout()
        langrow.addStretch(1)
        self.btn_ru = QPushButton("RU")
        self.btn_kz = QPushButton("KZ")
        for _b in (self.btn_ru, self.btn_kz):
            _b.setCheckable(True)
            _b.setFixedSize(46, 28)
            _b.setCursor(Qt.PointingHandCursor)
            langrow.addWidget(_b)
        lay.addLayout(langrow)
        self.btn_ru.clicked.connect(lambda: self._set_lang("ru"))
        self.btn_kz.clicked.connect(lambda: self._set_lang("kk"))

        hero = QFrame()
        hero.setObjectName("LoginHero")
        hl = QHBoxLayout(hero)
        hl.setContentsMargins(18, 16, 18, 16)
        logo = QLabel()
        logo.setObjectName("HeroEmoji")
        try:
            from .branding import logo_pixmap

            pm = logo_pixmap(96)
            if pm is not None and not pm.isNull():
                logo.setPixmap(pm.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                logo.setFixedSize(44, 44)
            else:
                logo.setText("🍈")
        except Exception:
            logo.setText("🍈")
        hl.addWidget(logo)
        hbox = QVBoxLayout()
        ht = QLabel("Baqylau")
        ht.setObjectName("HeroTitle")
        self.hero_sub = QLabel("")
        self.hero_sub.setObjectName("HeroSub")
        hbox.addWidget(ht)
        hbox.addWidget(self.hero_sub)
        hl.addLayout(hbox, 1)
        lay.addWidget(hero)

        self.login_title = QLabel("")
        self.login_title.setObjectName("LoginTitle")
        lay.addWidget(self.login_title)
        self.login_sub = QLabel("")
        self.login_sub.setObjectName("LoginSub")
        self.login_sub.setWordWrap(True)
        lay.addWidget(self.login_sub)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.chip_lbls = []
        for _ in range(3):
            ch = QLabel("")
            ch.setObjectName("Chip")
            chips.addWidget(ch)
            self.chip_lbls.append(ch)
        chips.addStretch(1)
        lay.addLayout(chips)

        self.fl_server = self._flabel("")
        lay.addWidget(self.fl_server)
        self.ed_server = QLineEdit(server or str(last))
        self.ed_server.setPlaceholderText("http://192.168.1.10:5050")
        lay.addWidget(self.ed_server)
        self.fl_login = self._flabel("")
        lay.addWidget(self.fl_login)
        self.ed_login = QLineEdit(username)
        self.ed_login.setPlaceholderText("напр. ivan_petrov")
        lay.addWidget(self.ed_login)
        self.fl_pass = self._flabel("")
        lay.addWidget(self.fl_pass)
        self.ed_pass = QLineEdit()
        self.ed_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_pass.setPlaceholderText("••••••••")
        self.ed_pass.returnPressed.connect(self._on_login)
        lay.addWidget(self.ed_pass)

        self.error = QLabel("")
        self.error.setObjectName("LoginError")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)

        self.btn_login = QPushButton("")
        self.btn_login.setObjectName("LoginGo")
        self.btn_login.setCursor(Qt.PointingHandCursor)
        self.btn_login.clicked.connect(self._on_login)
        lay.addWidget(self.btn_login)

        self.btn_local = QPushButton("")
        self.btn_local.setObjectName("LoginLocal")
        self.btn_local.setCursor(Qt.PointingHandCursor)
        self.btn_local.clicked.connect(self._on_local)
        lay.addWidget(self.btn_local, alignment=Qt.AlignCenter)
        self._apply_lang()

    @staticmethod
    def _flabel(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("FieldLabel")
        return lbl

    def _t(self, key: str):
        return _tr_str(self._lang, key)

    def _set_lang(self, code: str) -> None:
        if code not in ("ru", "kk"):
            return
        self._lang = code
        set_app_lang(code)
        self._apply_lang()

    def _apply_lang(self) -> None:
        try:
            t = self._t
            self.hero_sub.setText(t("hero_sub"))
            self.login_title.setText(t("login_title"))
            self.login_sub.setText(t("login_sub"))
            for lbl, txt in zip(self.chip_lbls, t("login_chips")):
                lbl.setText(txt)
            self.fl_server.setText(t("f_server"))
            self.fl_login.setText(t("f_login"))
            self.fl_pass.setText(t("f_pass"))
            self.ed_login.setPlaceholderText(t("login_ph"))
            self.btn_login.setText(t("btn_go"))
            self.btn_local.setText(t("btn_local"))
            self.btn_ru.setChecked(self._lang == "ru")
            self.btn_kz.setChecked(self._lang == "kk")
        except Exception:
            pass

    def _on_login(self) -> None:
        server = self.ed_server.text().strip()
        username = self.ed_login.text().strip()
        password = self.ed_pass.text()
        if not server or not username:
            self.error.setText(self._t("err_need"))
            return
        self.error.setText(self._t("err_conn"))
        self.error.repaint()
        c = try_login(server, username, password)
        if c is None:
            self.error.setText(self._t("err_fail"))
            return
        QSettings(ORG, APP).setValue("server", server)
        self.client = c
        self.accept()

    def _on_local(self) -> None:
        self.local_mode = True
        self.reject()


def _apply_brand_icon(widget) -> None:
    """Set Baqylau mark as window icon. Never raises."""
    try:
        from .branding import app_icon

        icon = app_icon()
        if icon is not None and not icon.isNull():
            widget.setWindowIcon(icon)
    except Exception:
        pass
