# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""多平台管理对话框。

集中管理各设备平台的登录态与账号：米家（扫码登录，可切换账号）与
开机卡、美的等账号密码平台（可记住账号）。登录状态变化后通知主窗口
重新合并设备列表。

卡片按 PlatformRegistry.platforms 动态生成，不写死平台：支持账号
密码登录的平台（supports_password_login）渲染同一套表单；来自插件
的平台（美的）额外显示插件/协议层版本与「检查更新」入口。
"""

import logging

import qtawesome as qta
import shiboken6
from PySide6.QtCore import QSize, Qt, Signal, QByteArray
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core import platform_store
from app.core.models import DeviceInfo
from app.core.platforms import PlatformRegistry
from app.core.platforms.base import CaptchaRequired
from app.plugins import apply_update, check_update, discover
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import (
    SiColors,
    PRIMARY_TEXT,
    primary_button_qss,
    secondary_button_qss,
)
from app.ui.toast import Toast

logger = logging.getLogger(__name__)

_PANEL_W = 620


def _alive(widget) -> bool:
    """异步回调到达时窗口/卡片可能已被关闭（快速开关对话框时）。
    与 main_window 的场景回调同一防护模式。"""
    return widget is not None and shiboken6.isValid(widget)


class PlatformManagerDialog(OverlayDialog):
    """多平台登录与管理面板。"""

    # 平台登录态变化：主窗口据此重新拉取合并设备列表
    platforms_changed = Signal()
    # 米家扫码登录/切换账号（由主窗口打开扫码对话框）
    mijia_relogin_requested = Signal()

    def __init__(self, registry: PlatformRegistry, jobs, devices=None,
                 parent=None):
        super().__init__(parent)
        self._registry = registry
        self._jobs = jobs
        self._devices: list[DeviceInfo] = devices or []
        self._busy = False
        self._cards: dict[str, _PlatformCard] = {}
        # 插件信息（platform_id -> PluginInfo）；无插件则为空
        self._plugins = {info.id: info for info in discover()}

        self.setWindowTitle("多平台管理")

        outer = QVBoxLayout(self._panel)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        # ---- 标题 ----
        head = QHBoxLayout()
        head.setSpacing(10)
        icon = QLabel()
        icon.setFixedSize(28, 28)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(qta.icon("mdi.lan", color=SiColors.THEME).pixmap(24, 24))
        head.addWidget(icon)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("多平台管理")
        title.setFont(QFont("Microsoft YaHei UI", 14, QFont.Weight.DemiBold))
        title_col.addWidget(title)
        subtitle = QLabel("登录各设备平台后，其设备将合并显示在主界面、托盘与桌面挂件")
        title_col.addWidget(subtitle)
        head.addLayout(title_col, stretch=1)
        head.addWidget(self._make_close_button(),
                       alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        # ---- 平台卡片列表 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        host_lay = QVBoxLayout(host)
        host_lay.setContentsMargins(0, 0, 0, 0)
        host_lay.setSpacing(12)
        for platform in self._registry.platforms:
            card = _PlatformCard(self._registry, platform, self._devices,
                                 plugin_info=self._plugins.get(
                                     platform.platform_id))
            if getattr(platform, "supports_password_login", False):
                card.login_requested.connect(self._on_password_login)
                card.logout_requested.connect(self._on_password_logout)
                card.refresh_captcha_requested.connect(
                    lambda pid: self._fetch_captcha(pid))
            if platform.platform_id == "mijia":
                card.relogin_requested.connect(self.mijia_relogin_requested)
            card.update_requested.connect(self._on_plugin_update)
            self._cards[platform.platform_id] = card
            host_lay.addWidget(card)
        host_lay.addStretch(1)
        scroll.setWidget(host)
        outer.addWidget(scroll, stretch=1)

        # ---- 底部扩展说明 ----
        self._hint = QLabel(
            "需要接入新的设备平台？在 app/plugins 下放一个插件目录"
            "（plugin.json + 适配器），即可自动获得主界面、托盘、挂件与"
            "定时任务的支持。")
        self._hint.setWordWrap(True)
        self._hint.setFont(QFont("Microsoft YaHei UI", 9))
        outer.addWidget(self._hint)

        self._apply_styles()
        self._probe_status()

    # ---------- 状态探测 ----------

    def _probe_status(self) -> None:
        """后台读取各平台的本地登录态，用于面板展示。

        注意：这里必须用 ``session_active()`` 而非 ``is_logged_in()``。
        ``is_logged_in()`` 会发起 ``list_home`` 联网校验，美的云端在
        token 失效时会**自动自愈重登**（= 用户手机上弹登录提醒），于是
        仅仅是打开这个管理面板就会触发美的重新登录、手机连环弹通知——
        这正是“点开多平台选项卡就收到登录通知、且每次都重复请求”的根因。
        ``session_active()`` 只看内存里是否已登录（美的为 ``_cloud is
        not None``，开机卡为 ``_user_name is not None``），零网络、零打扰，
        一次登录在内存里可多次复用。真正的重登只发生在用户显式登录或
        实际操作设备而会话已丢失时（由 _require_cloud / auto_login 负责）。
        """
        self._set_busy(True)
        self._jobs.submit(
            lambda: {p.platform_id: (p.session_active(), p.status_text())
                     for p in self._registry.platforms},
            on_success=self._on_status_probed,
            on_error=lambda exc: (
                logger.warning("平台状态探测失败: %s", exc),
                self._set_busy(False)),
        )

    def _on_status_probed(self, statuses: dict) -> None:
        if not _alive(self):
            return
        self._set_busy(False)
        for platform_id, (logged_in, text) in statuses.items():
            card = self._cards.get(platform_id)
            if card is not None:
                card.set_logged_in(bool(logged_in))
                card.set_status(text)

    # ---------- 账号密码平台（开机卡 / 美的，通用） ----------

    def _password_platform(self, platform_id: str):
        for platform in self._registry.platforms:
            if platform.platform_id == platform_id:
                return platform
        return None

    def _on_password_login(self, platform_id: str, username: str,
                           password: str, remember: bool,
                           vcode: str = "") -> None:
        card = self._cards.get(platform_id)
        platform = self._password_platform(platform_id)
        if card is None or platform is None:
            return
        self._set_busy(True)
        card.set_status(f"正在登录{platform.display_name}…")
        kwargs = dict(username=username, password=password, remember=remember)
        if vcode:
            kwargs["vcode"] = vcode
            kwargs["vcode_id"] = card._vcode_id
        self._jobs.submit(
            lambda: platform.login(**kwargs),
            on_success=lambda _: self._on_login_done(platform_id, card),
            on_error=lambda exc: self._on_login_failed(platform_id, card, exc),
        )

    def _on_login_done(self, platform_id: str, card: "_PlatformCard") -> None:
        if not _alive(self) or not _alive(card):
            return
        self._set_busy(False)
        platform = self._password_platform(platform_id)
        card.set_status(platform.status_text() if platform else "已登录")
        card.set_logged_in(True)
        # 登录后顺手拉一次设备数，徽标立即反映真实数量
        self._jobs.submit(
            lambda: len(platform.list_devices()),
            on_success=lambda count: (
                card.set_device_count(count) if _alive(card) else None),
            on_error=lambda exc: logger.info("登录后拉取设备数失败: %s", exc),
        )
        Toast.info(self, f"{platform.display_name}登录成功，设备列表已更新", 2500)
        self.platforms_changed.emit()

    def _on_login_failed(self, platform_id: str, card: "_PlatformCard",
                         error: Exception) -> None:
        if not _alive(self) or not _alive(card):
            return
        self._set_busy(False)
        if isinstance(error, CaptchaRequired):
            # 服务端要求验证码：取图并展示
            self._fetch_captcha(platform_id)
            return
        message = str(error) or "登录失败"
        logger.warning("%s 登录失败: %s", platform_id, message)
        card.set_status(message, error=True)
        if getattr(card, "_vcode_id", ""):
            # 已处于验证码模式（如 -2102 验证码错误）：刷新图片重试
            self._fetch_captcha(platform_id)

    def _fetch_captcha(self, platform_id: str) -> None:
        """后台取验证码图片（CMD=101），加载成功由卡片展示。"""
        card = self._cards.get(platform_id)
        platform = self._password_platform(platform_id)
        if card is None or platform is None:
            return
        self._set_busy(True)
        card.set_status("正在获取验证码…")
        self._jobs.submit(
            platform.get_captcha,
            on_success=lambda data: self._on_captcha_loaded(card, data),
            on_error=lambda exc: self._on_captcha_failed(card, exc),
        )

    def _on_captcha_loaded(self, card: "_PlatformCard", data: dict) -> None:
        if not _alive(self) or not _alive(card):
            return
        self._set_busy(False)
        try:
            card.show_captcha(data.get("vcode_id", ""), data.get("vcode_img", ""))
        except Exception as exc:
            card.set_status(f"验证码显示失败：{exc}", error=True)
            return
        card.set_status("请输入图中验证码后重试", error=True)

    def _on_captcha_failed(self, card: "_PlatformCard",
                           error: Exception) -> None:
        if not _alive(self) or not _alive(card):
            return
        self._set_busy(False)
        card.set_status(
            f"获取验证码失败：{str(error) or type(error).__name__}",
            error=True)

    def _on_password_logout(self, platform_id: str) -> None:
        card = self._cards.get(platform_id)
        platform = self._password_platform(platform_id)
        if card is None or platform is None:
            return
        self._set_busy(True)
        card.set_status("正在退出登录…")
        self._jobs.submit(
            platform.logout,
            on_success=lambda _: self._on_logout_done(platform_id, card),
            on_error=lambda exc: self._on_logout_failed(platform_id, card, exc),
        )

    def _on_logout_done(self, platform_id: str, card: "_PlatformCard") -> None:
        if not _alive(self) or not _alive(card):
            return
        self._set_busy(False)
        card.set_status("未登录")
        card.set_logged_in(False)
        card.set_device_count(0)
        Toast.info(self, "已退出登录", 2500)
        self.platforms_changed.emit()

    def _on_logout_failed(self, platform_id: str, card: "_PlatformCard",
                          error: Exception) -> None:
        if not _alive(self) or not _alive(card):
            return
        self._set_busy(False)
        message = str(error) or "退出登录失败"
        logger.warning("%s 退出登录失败: %s", platform_id, message)
        card.set_status(message, error=True)

    # ---------- 插件更新（GitHub） ----------

    def _on_plugin_update(self, platform_id: str) -> None:
        card = self._cards.get(platform_id)
        info = self._plugins.get(platform_id)
        if card is None or info is None:
            return
        card.set_update_status("正在检查更新…")
        self._jobs.submit(
            lambda: check_update(info),
            on_success=lambda result: self._on_update_checked(
                platform_id, card, info, result),
            on_error=lambda exc: self._on_update_failed(card, exc),
        )

    def _on_update_checked(self, platform_id: str, card: "_PlatformCard",
                           info, result) -> None:
        if not _alive(self) or not _alive(card):
            return
        if not result:
            card.set_update_status("已是最新")
            Toast.info(self, f"{info.name}已是最新版本", 2500)
            return
        current = result.get("current") or info.upstream_version or "未知"
        latest = result.get("latest") or ""
        card.set_update_status(f"发现新版本 {latest}")
        box = QMessageBox(self)
        box.setWindowTitle("插件更新")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"{info.name} 协议层有新版本")
        box.setInformativeText(
            f"当前 {current} → 最新 {latest}\n\n"
            "更新会下载并替换协议层文件，重启程序后生效。")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() != QMessageBox.StandardButton.Yes:
            card.set_update_status("")
            return
        card.set_update_status("正在下载更新…")
        self._jobs.submit(
            lambda: apply_update(info, result.get("zip_url") or ""),
            on_success=lambda version: self._on_update_applied(
                card, info, version),
            on_error=lambda exc: self._on_update_failed(card, exc),
        )

    def _on_update_applied(self, card: "_PlatformCard", info, version: str) -> None:
        if not _alive(self) or not _alive(card):
            return
        card.set_update_status(f"已更新到 {version}")
        card.set_version_text(
            f"插件 v{info.version} · 协议层 {version}")
        Toast.info(self, f"{info.name}协议层已更新，重启程序后生效", 4000)

    def _on_update_failed(self, card: "_PlatformCard",
                          error: Exception) -> None:
        if not _alive(self) or not _alive(card):
            return
        message = str(error) or "更新失败"
        logger.warning("插件更新失败: %s", message)
        card.set_update_status(f"更新失败：{message}", error=True)

    # ---------- 公共 ----------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for card in self._cards.values():
            card.set_busy(busy)

    def update_devices(self, devices: list[DeviceInfo]) -> None:
        """主窗口设备列表刷新后同步各平台设备数。"""
        self._devices = devices or []
        for card in self._cards.values():
            card.set_devices(self._devices)

    # ---------- 样式与布局 ----------

    def _apply_styles(self) -> None:
        for card in self._cards.values():
            card.retheme()
        self._hint.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;")

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().resizeEvent(event)
        self._place_overlay()
        pw = min(_PANEL_W, max(360, self.width() - 60))
        ph = min(560, self.height() - 60)
        x = (self.width() - pw) // 2
        y = (self.height() - ph) // 2
        self._panel.setGeometry(x, y, pw, ph)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        self._center_on_screen(_PANEL_W, 560)
        self.raise_()
        self._fade_in()


class _PlatformCard(QFrame):
    """单个平台的管理卡片：状态、设备数、登录控件、插件更新。"""

    login_requested = Signal(str, str, str, bool, str)  # 平台/账号/密码/记住/验证码
    refresh_captcha_requested = Signal(str)             # 平台 id：请求刷新验证码
    logout_requested = Signal(str)                      # 平台 id
    relogin_requested = Signal()                   # 米家：扫码登录/切换账号
    update_requested = Signal(str)                 # 插件：检查更新

    def __init__(self, registry: PlatformRegistry, platform, devices=None,
                 plugin_info=None, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._platform = platform
        self._devices: list[DeviceInfo] = devices or []
        self._plugin_info = plugin_info
        self._busy = False
        self._vcode_id = ""          # 当前验证码挑战的 VCodeId（空=无）
        self.setObjectName("platformCard")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        # ---- 头部：图标 + 名称 + 状态徽标 + 设备数 ----
        head = QHBoxLayout()
        head.setSpacing(10)
        icon = QLabel()
        icon.setFixedSize(30, 30)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(qta.icon(platform.icon_name,
                                color=SiColors.THEME).pixmap(24, 24))
        head.addWidget(icon)

        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        self._name_label = QLabel(platform.display_name)
        self._name_label.setFont(
            QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
        name_col.addWidget(self._name_label)
        self._desc_label = QLabel(platform.description)
        self._desc_label.setWordWrap(True)
        name_col.addWidget(self._desc_label)
        head.addLayout(name_col, stretch=1)

        self._badge = QLabel()
        self._badge.setFixedHeight(22)
        head.addWidget(self._badge, alignment=Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        lay.addWidget(self._status_label)

        # ---- 插件版本行（仅插件平台） ----
        if plugin_info is not None:
            version_row = QHBoxLayout()
            version_row.setSpacing(8)
            self._version_label = QLabel()
            self._version_label.setWordWrap(True)
            self.set_version_text(
                f"插件 v{plugin_info.version} · 协议层 "
                f"{plugin_info.upstream_version or '未知'}")
            version_row.addWidget(self._version_label, stretch=1)
            self._update_btn = QPushButton("检查更新")
            self._update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._update_btn.setFixedHeight(28)
            self._update_btn.setToolTip("从 GitHub 检查协议层新版本")
            self._update_btn.clicked.connect(
                lambda: self.update_requested.emit(platform.platform_id))
            version_row.addWidget(self._update_btn)
            lay.addLayout(version_row)

        # ---- 平台专属控件 ----
        if getattr(platform, "supports_password_login", False):
            lay.addLayout(self._build_password_form(platform.platform_id))
        elif platform.platform_id == "mijia":
            lay.addLayout(self._build_mijia_row())

        self._refresh_counts()
        self._logged_in = platform.session_active()
        self.set_status(platform.status_text())
        # 登录控件显隐以当前登录态为准（未登录不显示退出，已登录不显示登录）
        self._apply_login_controls()
        self.retheme()

    # ---------- 米家卡片 ----------

    def _build_mijia_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._mijia_btn = QPushButton("扫码登录")
        self._mijia_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mijia_btn.setFixedHeight(32)
        self._mijia_btn.setToolTip("用米家 APP 扫码登录；已登录时可切换账号")
        self._mijia_btn.clicked.connect(self.relogin_requested)
        row.addStretch(1)
        row.addWidget(self._mijia_btn)
        return row

    # ---------- 账号密码平台（开机卡 / 美的，同一套表单） ----------

    def _build_password_form(self, platform_id: str) -> QVBoxLayout:
        form = QVBoxLayout()
        form.setSpacing(8)

        self._user_edit = QLineEdit()
        self._user_edit.setPlaceholderText("账号 / 手机号")
        self._user_edit.setFixedHeight(32)
        self._pass_edit = QLineEdit()
        self._pass_edit.setPlaceholderText("密码")
        self._pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_edit.setFixedHeight(32)
        self._remember = QCheckBox("记住账号密码")
        # 回填已保存的账号（取消勾选即清除本地保存）
        account = platform_store.load_account(platform_id)
        if account.get("username"):
            self._user_edit.setText(str(account["username"]))
            self._pass_edit.setText(str(account.get("password") or ""))
            self._remember.setChecked(True)
        form.addWidget(self._user_edit)
        form.addWidget(self._pass_edit)
        form.addWidget(self._remember)

        # 图形验证码区：仅开机卡多次失败被要求时显示（默认隐藏）
        self._vcode_widget = QWidget()
        vcode_lay = QVBoxLayout(self._vcode_widget)
        vcode_lay.setContentsMargins(0, 0, 0, 0)
        vcode_lay.setSpacing(6)
        img_row = QHBoxLayout()
        img_row.setSpacing(8)
        self._vcode_img_label = QLabel()
        self._vcode_img_label.setFixedSize(110, 42)
        self._vcode_img_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vcode_img_label.setToolTip("点击图片刷新验证码")
        self._vcode_img_label.setScaledContents(True)
        self._vcode_img_label.mousePressEvent = (
            lambda _e: self.refresh_captcha_requested.emit(
                self._platform.platform_id))
        img_row.addWidget(self._vcode_img_label)
        self._vcode_refresh_btn = QPushButton("刷新")
        self._vcode_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vcode_refresh_btn.setFixedHeight(32)
        self._vcode_refresh_btn.clicked.connect(
            lambda: self.refresh_captcha_requested.emit(
                self._platform.platform_id))
        img_row.addWidget(self._vcode_refresh_btn)
        img_row.addStretch(1)
        vcode_lay.addLayout(img_row)
        vcode_lay.addWidget(QLabel("验证码（图中字符，区分大小写）"))
        self._vcode_edit = QLineEdit()
        self._vcode_edit.setPlaceholderText("输入图中字符")
        self._vcode_edit.setFixedHeight(32)
        vcode_lay.addWidget(self._vcode_edit)
        self._vcode_widget.setVisible(False)
        form.addWidget(self._vcode_widget)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._logout_btn = QPushButton("退出登录")
        self._logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._logout_btn.setFixedHeight(32)
        self._logout_btn.clicked.connect(
            lambda: self.logout_requested.emit(platform_id))
        buttons.addWidget(self._logout_btn)
        self._login_btn = QPushButton("登录")
        self._login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._login_btn.setFixedHeight(32)
        self._login_btn.clicked.connect(self._emit_login)
        buttons.addWidget(self._login_btn)
        form.addLayout(buttons)
        return form

    def _emit_login(self) -> None:
        vcode = ""
        if getattr(self, "_vcode_widget", None) and self._vcode_widget.isVisible():
            vcode = self._vcode_edit.text().strip()
        self.login_requested.emit(
            self._platform.platform_id,
            self._user_edit.text().strip(),
            self._pass_edit.text(),
            self._remember.isChecked(),
            vcode,
        )

    def show_captcha(self, vcode_id: str, data_uri: str) -> None:
        """展示验证码图片并切入验证码输入模式。"""
        self._vcode_id = vcode_id or ""
        try:
            _, b64 = data_uri.split(",", 1)
            pix = QPixmap()
            pix.loadFromData(QByteArray.fromBase64(b64.encode("ascii")))
            self._vcode_img_label.setPixmap(
                pix.scaledToHeight(42, Qt.TransformationMode.SmoothTransformation))
        except Exception as exc:
            logger.warning("验证码图片解码失败: %s", exc)
            self._vcode_img_label.setText("图片加载失败")
        self._vcode_edit.clear()
        self._vcode_widget.setVisible(True)
        self._vcode_edit.setFocus()

    def _reset_captcha(self) -> None:
        self._vcode_id = ""
        if getattr(self, "_vcode_widget", None) is not None:
            self._vcode_widget.setVisible(False)
        if getattr(self, "_vcode_edit", None) is not None:
            self._vcode_edit.clear()

    def _apply_login_controls(self) -> None:
        """按登录态切换登录/退出控件。

        未登录：显示账号密码表单与「登录」，隐藏「退出登录」；
        已登录：隐藏整个登录表单（含登录按钮），只留「退出登录」。
        """
        logged_in = bool(getattr(self, "_logged_in", False))
        for widget in (getattr(self, "_user_edit", None),
                       getattr(self, "_pass_edit", None),
                       getattr(self, "_remember", None),
                       getattr(self, "_login_btn", None)):
            if widget is not None:
                widget.setVisible(not logged_in)
        if getattr(self, "_logout_btn", None) is not None:
            self._logout_btn.setVisible(logged_in)
        if logged_in and getattr(self, "_vcode_widget", None) is not None:
            # 登录成功后同步隐藏验证码区，避免残留
            self._vcode_widget.setVisible(False)
        if getattr(self, "_mijia_btn", None) is not None:
            # 米家单个按钮兼顾登录与切换账号：按状态改文案
            self._mijia_btn.setText("切换账号" if logged_in else "扫码登录")

    # ---------- 状态刷新 ----------

    def set_status(self, text: str, error: bool = False) -> None:
        self._status_label.setText(text)
        color = SiColors.ERROR_TEXT if error else SiColors.TEXT_SECONDARY
        self._status_label.setStyleSheet(
            f"color: {color}; background: transparent; font-size: 9pt;")

    def set_version_text(self, text: str) -> None:
        if getattr(self, "_version_label", None) is not None:
            self._version_label.setText(text)

    def set_update_status(self, text: str, error: bool = False) -> None:
        """更新过程文案（显示在版本行）。"""
        if getattr(self, "_version_label", None) is None:
            return
        base = self._version_label.property("base_text") or ""
        if not base:
            base = self._version_label.text()
            self._version_label.setProperty("base_text", base)
        self._version_label.setText(text or base)
        color = SiColors.ERROR_TEXT if error else SiColors.TEXT_MUTED
        self._version_label.setStyleSheet(
            f"color: {color}; background: transparent; font-size: 9pt;")

    def set_devices(self, devices: list[DeviceInfo]) -> None:
        self._devices = devices or []
        self._count_override = None
        self._refresh_counts()

    def set_device_count(self, count: int) -> None:
        """后台拉到真实设备数后直接覆盖徽标（不等主窗口刷新）。"""
        self._count_override = int(count)
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        override = getattr(self, "_count_override", None)
        if override is not None:
            count = override
        else:
            count = sum(1 for d in self._devices
                        if d.platform == self._platform.platform_id)
        self._badge.setText(f"{count} 台设备")
        logged_in = bool(getattr(self, "_logged_in", False))
        self._badge.setStyleSheet(
            f"background: {SiColors.SURFACE}; border: 1px solid {SiColors.LINE};"
            f" border-radius: 11px; padding: 0 10px; font-size: 9pt;"
            f" color: {SiColors.TEXT_SECONDARY if not logged_in else SiColors.TEXT_PRIMARY};")

    def set_logged_in(self, logged_in: bool) -> None:
        self._logged_in = logged_in
        if logged_in:
            self._reset_captcha()
        self._refresh_counts()
        self._apply_login_controls()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (getattr(self, "_login_btn", None),
                       getattr(self, "_logout_btn", None),
                       getattr(self, "_mijia_btn", None),
                       getattr(self, "_update_btn", None)):
            if widget is not None:
                widget.setEnabled(not busy)

    # ---------- 样式 ----------

    def retheme(self) -> None:
        self.setStyleSheet(
            f"QFrame#platformCard {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 14px; }}")
        self._name_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._desc_label.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;"
            " font-size: 9pt;")
        line_edit_qss = (
            f"QLineEdit {{ background: {SiColors.SURFACE};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 8px;"
            f" padding: 4px 10px; color: {SiColors.TEXT_PRIMARY};"
            " font-size: 10pt; }"
            f"QLineEdit:focus {{ border-color: {SiColors.THEME}; }}")
        for edit in (getattr(self, "_user_edit", None),
                     getattr(self, "_pass_edit", None)):
            if edit is not None:
                edit.setStyleSheet(line_edit_qss)
        if getattr(self, "_remember", None) is not None:
            self._remember.setStyleSheet(
                f"QCheckBox {{ color: {SiColors.TEXT_SECONDARY};"
                " background: transparent; font-size: 9pt; }")
        if getattr(self, "_login_btn", None) is not None:
            self._login_btn.setStyleSheet(primary_button_qss("4px 18px"))
            self._login_btn.setIcon(qta.icon("mdi.login", color=PRIMARY_TEXT))
            self._login_btn.setIconSize(QSize(16, 16))
        if getattr(self, "_vcode_edit", None) is not None:
            self._vcode_edit.setStyleSheet(line_edit_qss)
        if getattr(self, "_vcode_refresh_btn", None) is not None:
            self._vcode_refresh_btn.setStyleSheet(secondary_button_qss("4px 18px"))
        if getattr(self, "_vcode_img_label", None) is not None:
            self._vcode_img_label.setStyleSheet(
                f"border: 1px solid {SiColors.LINE}; border-radius: 6px;"
                f" background: {SiColors.SURFACE};")
        if getattr(self, "_logout_btn", None) is not None:
            self._logout_btn.setStyleSheet(secondary_button_qss("4px 18px"))
        if getattr(self, "_mijia_btn", None) is not None:
            self._mijia_btn.setStyleSheet(secondary_button_qss("4px 18px"))
            self._mijia_btn.setIcon(
                qta.icon("mdi.qrcode-scan", color=SiColors.TEXT_SECONDARY))
            self._mijia_btn.setIconSize(QSize(16, 16))
        if getattr(self, "_update_btn", None) is not None:
            self._update_btn.setStyleSheet(secondary_button_qss("3px 14px"))
            self._update_btn.setIcon(
                qta.icon("mdi.update", color=SiColors.TEXT_SECONDARY))
            self._update_btn.setIconSize(QSize(14, 14))
        if getattr(self, "_version_label", None) is not None:
            self._version_label.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                " font-size: 9pt;")
        # 状态徽标与状态文案重绘
        if getattr(self, "_status_label", None) is not None:
            self.set_status(self._status_label.text())
        self._refresh_counts()
