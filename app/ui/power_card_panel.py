# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""指令式平台的设备控制面板（开机卡 / 美的等通用）。

米家设备走属性式工作台面板（WorkbenchPanel），开机卡、美的这类平台
走本面板：

- 电源主按钮（三态）
- 结构化控制项（ControlSpec）：空调的温度滑杆、模式/风速/预设下拉、
  开关实体的开关——由各平台从自己的设备模型解释生成
- 指令按钮（平台 capabilities 提供的一次性指令）
- 设备状态行（平台 device_status 提供）

所有操作经 PlatformRegistry 路由到对应平台，面板本身不感知协议。
"""

import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import shiboken6

from app.core.models import ControlSpec, DeviceInfo
from app.core.platforms import PlatformRegistry
from app.ui.overlay_dialog import OverlayDialog
from app.ui.power_button import PowerButton
from app.ui.si_theme import SiColors, secondary_button_qss
from app.ui.toast import Toast

_PANEL_W = 460
_POWER_BTN_SIZE = 52


class PowerCardDialog(OverlayDialog):
    """指令式平台控制面板：电源主按钮 + 控制项 + 指令 + 状态行。"""

    def __init__(self, registry: PlatformRegistry, jobs,
                 device: DeviceInfo, parent=None):
        super().__init__(parent)
        self._registry = registry
        self._jobs = jobs
        self._device = device
        self._did = device.did
        self._busy_action: str | None = None
        self._busy_control: str | None = None
        self._online = device.online
        self._controls: list[tuple[ControlSpec, object]] = []

        self.setWindowTitle(device.name)

        outer = QVBoxLayout(self._panel)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(14)

        # ---- 标题栏 ----
        head = QHBoxLayout()
        head.setSpacing(10)
        icon = QLabel()
        icon.setFixedSize(28, 28)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(qta.icon("mdi.power", color=SiColors.THEME).pixmap(24, 24))
        head.addWidget(icon)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self._title = QLabel(device.name)
        self._title.setFont(QFont("Microsoft YaHei UI", 14, QFont.Weight.DemiBold))
        title_col.addWidget(self._title)
        self._subtitle = QLabel(f"{device.platform} · 远程控制")
        title_col.addWidget(self._subtitle)
        head.addLayout(title_col, stretch=1)
        head.addWidget(self._make_close_button(),
                       alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        # ---- 电源主按钮区 ----
        power_row = QHBoxLayout()
        power_row.setSpacing(16)
        self._power_btn = PowerButton(_POWER_BTN_SIZE, icon_size=30)
        self._power_btn.setToolTip("电源开关")
        self._power_btn.clicked.connect(self._on_power_clicked)
        power_row.addWidget(self._power_btn)
        power_col = QVBoxLayout()
        power_col.setSpacing(2)
        self._power_label = QLabel("电源：读取中…")
        self._power_label.setFont(
            QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
        power_col.addWidget(self._power_label)
        self._power_hint = QLabel()
        power_col.addWidget(self._power_hint)
        power_row.addLayout(power_col, stretch=1)
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setFixedHeight(30)
        self._refresh_btn.clicked.connect(self.load)
        power_row.addWidget(self._refresh_btn,
                            alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(power_row)

        # ---- 状态行 ----
        self._status_grid = QGridLayout()
        self._status_grid.setHorizontalSpacing(12)
        self._status_grid.setVerticalSpacing(6)
        outer.addLayout(self._status_grid)

        # ---- 结构化控制项 ----
        self._controls_title = QLabel("设备控制")
        outer.addWidget(self._controls_title)
        self._controls_grid = QGridLayout()
        self._controls_grid.setHorizontalSpacing(10)
        self._controls_grid.setVerticalSpacing(8)
        outer.addLayout(self._controls_grid)

        # ---- 指令按钮 ----
        self._actions_title = QLabel("控制指令")
        outer.addWidget(self._actions_title)
        self._actions_grid = QGridLayout()
        self._actions_grid.setHorizontalSpacing(10)
        self._actions_grid.setVerticalSpacing(10)
        outer.addLayout(self._actions_grid)
        self._action_buttons: dict[str, QPushButton] = {}
        self._build_actions()

        outer.addStretch(1)

        self._apply_styles()
        self._apply_online_state()

    # ---------- 构建 ----------

    def _build_actions(self) -> None:
        capabilities = self._registry.capabilities(self._did)
        if not capabilities:
            self._actions_title.hide()
            return
        columns = 2
        for index, action in enumerate(capabilities):
            button = QPushButton(action.desc)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(34)
            button.setToolTip(f"下发「{action.desc}」指令")
            button.clicked.connect(
                lambda _, name=action.name: self._on_action_clicked(name))
            self._action_buttons[action.name] = button
            self._actions_grid.addWidget(button, index // columns,
                                         index % columns)

    def _build_controls(self, specs: list[ControlSpec]) -> None:
        """按 ControlSpec 渲染控制项（滑杆/下拉/开关）。"""
        # 清空旧控件
        while self._controls_grid.count():
            item = self._controls_grid.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        self._controls.clear()
        if not specs:
            self._controls_title.hide()
            return
        self._controls_title.show()
        for row, spec in enumerate(specs):
            label = QLabel(spec.label)
            label.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
                " font-size: 9pt;")
            self._controls_grid.addWidget(label, row, 0)
            widget = self._make_control_widget(spec)
            self._controls_grid.addWidget(widget, row, 1)
            self._controls.append((spec, widget))

    def _make_control_widget(self, spec: ControlSpec):
        if spec.kind == "switch":
            button = QPushButton()
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(30)
            button.setCheckable(True)
            # 阻塞信号：setChecked 会发 toggled，构造期不能当成用户操作
            button.blockSignals(True)
            button.setChecked(bool(spec.value))
            button.blockSignals(False)
            self._style_switch_button(button, bool(spec.value))
            button.toggled.connect(
                lambda checked, s=spec: self._on_control_changed(s, checked))
            return button

        if spec.kind == "enum":
            combo = QComboBox()
            combo.setFixedHeight(30)
            combo.setCursor(Qt.CursorShape.PointingHandCursor)
            # 先填充再连接：addItem 会改当前索引并发信号
            combo.blockSignals(True)
            for value, text in spec.options:
                combo.addItem(text, value)
            if spec.value is not None:
                index = combo.findData(spec.value)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.blockSignals(False)
            combo.currentIndexChanged.connect(
                lambda _, s=spec, c=combo: self._on_control_changed(
                    s, c.currentData()))
            return combo

        # range：滑杆 + 数值
        minimum = int(spec.minimum or 0)
        maximum = int(spec.maximum or 100)
        step = spec.step or 1
        steps = max(1, int(round((maximum - minimum) / step)))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setFixedHeight(28)
        slider.setCursor(Qt.CursorShape.PointingHandCursor)
        slider.setRange(0, steps)
        slider.setSingleStep(1)
        slider.setPageStep(max(1, steps // 10))
        value_label = QLabel()
        value_label.setFixedWidth(64)
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter)

        def _fmt(raw: int) -> str:
            actual = minimum + raw * step
            text = f"{actual:g}"
            return f"{text}{spec.unit}" if spec.unit else text

        current = spec.value if spec.value is not None else minimum
        slider.blockSignals(True)
        slider.setValue(max(0, min(steps, int(round(
            (float(current) - minimum) / step)))))
        slider.blockSignals(False)
        value_label.setText(_fmt(slider.value()))
        slider.valueChanged.connect(lambda raw: value_label.setText(_fmt(raw)))
        slider.sliderReleased.connect(
            lambda s=spec, sl=slider: self._on_control_changed(
                s, minimum + sl.value() * step))
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(slider, stretch=1)
        wrap.addWidget(value_label)
        container = QWidget()
        container.setLayout(wrap)
        return container

    def _style_switch_button(self, button: QPushButton, checked: bool) -> None:
        button.setText("已开启" if checked else "已关闭")
        color = SiColors.THEME if checked else SiColors.TEXT_SECONDARY
        button.setStyleSheet(
            f"QPushButton {{ background: {SiColors.SURFACE};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 8px;"
            f" color: {color}; padding: 4px 16px; font-size: 9pt; }}"
            f"QPushButton:hover {{ border-color: {SiColors.THEME}; }}")

    # ---------- 数据加载 ----------

    def _alive(self) -> bool:
        """异步回调到达时对话框可能已被关闭（曾真实崩溃：快速开关面板
        时快照结果回来，PowerButton 的 C++ 对象已删除）。"""
        return shiboken6.isValid(self) and not self._closing

    def load(self) -> None:
        """后台读取电源状态、状态行与控制项（一次快照拿全）。"""
        self._power_btn.set_busy(True)
        self._jobs.submit(
            self._snapshot,
            on_success=self._on_snapshot,
            on_error=self._on_load_error,
        )

    def _snapshot(self):
        platform = self._registry.platform_of(self._did)
        snapshot = getattr(platform, "snapshot", None)
        if callable(snapshot):
            state, rows = snapshot(self._did)
        else:
            state = self._registry.power_state(self._did)
            rows = self._registry.device_status(self._did)
        controls = self._registry.device_controls(self._did)
        return state, rows, controls

    def _on_snapshot(self, snapshot) -> None:
        if not self._alive():
            return
        state, rows, controls = snapshot
        self._power_btn.set_busy(False)
        self._power_btn.set_state(state)
        if state is None:
            self._power_label.setText("电源：未知")
        else:
            self._power_label.setText(f"电源：{'开启' if state else '关闭'}")
        self._render_status_rows(rows)
        self._build_controls(controls)
        online = rows.get("开机卡状态")
        if online:
            self._online = online == "在线"
        self._apply_online_state()

    def _on_load_error(self, error: Exception) -> None:
        if not self._alive():
            return
        self._power_btn.set_busy(False)
        self._power_label.setText("电源：读取失败")
        self._power_hint.setText(str(error))
        self._power_hint.setStyleSheet(
            f"color: {SiColors.ERROR_TEXT}; background: transparent;"
            " font-size: 9pt;")

    def _render_status_rows(self, rows: dict) -> None:
        # 清空旧行
        while self._status_grid.count():
            item = self._status_grid.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        for index, (key, value) in enumerate(rows.items()):
            key_label = QLabel(key)
            key_label.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                " font-size: 9pt;")
            value_label = QLabel(str(value))
            value_label.setStyleSheet(
                f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
                " font-size: 9pt;")
            self._status_grid.addWidget(key_label, index, 0)
            self._status_grid.addWidget(value_label, index, 1)
        if not rows:
            hint = QLabel("暂无状态信息")
            hint.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                " font-size: 9pt;")
            self._status_grid.addWidget(hint, 0, 0)

    # ---------- 交互 ----------

    def _apply_online_state(self) -> None:
        online = self._online
        self._power_btn.set_online(online)
        for button in self._action_buttons.values():
            button.setEnabled(online and self._busy_control is None)
        for spec, widget in self._controls:
            widget.setEnabled(online and self._busy_control is None)
        if online:
            self._power_hint.setText("设备在线，可下发指令")
            self._power_hint.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
                " font-size: 9pt;")
        else:
            self._power_hint.setText("设备离线，暂不能下发指令")
            self._power_hint.setStyleSheet(
                f"color: {SiColors.ERROR_TEXT}; background: transparent;"
                " font-size: 9pt;")

    def _on_power_clicked(self) -> None:
        if not self._online:
            return
        current = self._power_btn.state()
        target = not current if current is not None else True
        # 乐观翻转：先切到目标状态，失败回滚
        self._power_btn.set_busy(True)
        self._power_btn.set_state(target)
        self._jobs.submit(
            lambda: self._registry.set_power_state(self._did, target),
            on_success=lambda _: self._on_power_done(target),
            on_error=lambda exc: self._on_power_failed(exc, current),
        )

    def _on_power_done(self, state: bool) -> None:
        if not self._alive():
            return
        self._power_btn.set_busy(False)
        self._power_btn.set_state(state)
        self._power_label.setText(f"电源：{'开启' if state else '关闭'}")
        Toast.info(self, f"已向「{self._device.name}」下发"
                         f"{'开机' if state else '关机'}指令", 2500)

    def _on_power_failed(self, error: Exception, revert_to: bool | None) -> None:
        if not self._alive():
            return
        self._power_btn.set_busy(False)
        if revert_to is not None:
            self._power_btn.set_state(revert_to)
            self._power_label.setText(
                f"电源：{'开启' if revert_to else '关闭'}")
        Toast.info(self, f"指令失败：{error}", 4000)

    def _on_control_changed(self, spec: ControlSpec, value) -> None:
        if not self._online or self._busy_control is not None:
            return
        self._busy_control = spec.key
        self._apply_online_state()
        self._jobs.submit(
            lambda: self._registry.set_control(self._did, spec.key, value),
            on_success=lambda _: self._on_control_done(spec),
            on_error=lambda exc: self._on_control_failed(spec, exc),
        )

    def _on_control_done(self, spec: ControlSpec) -> None:
        if not self._alive():
            return
        self._busy_control = None
        self._apply_online_state()
        Toast.info(self, f"已设置{spec.label}", 2000)
        # 控制后设备状态会变，顺手刷新一次面板
        self.load()

    def _on_control_failed(self, spec: ControlSpec,
                           error: Exception) -> None:
        if not self._alive():
            return
        self._busy_control = None
        self._apply_online_state()
        Toast.info(self, f"{spec.label}设置失败：{error}", 4000)
        # 失败后重拉一次，把控件恢复到云端真实状态
        self.load()

    def _on_action_clicked(self, action: str) -> None:
        if self._busy_action is not None:
            return
        button = self._action_buttons.get(action)
        label = button.text() if button is not None else action
        if not self._online:
            Toast.info(self, "设备离线，暂不能下发指令", 3000)
            return
        self._busy_action = action
        if button is not None:
            button.setEnabled(False)
        self._jobs.submit(
            lambda: self._registry.execute_action(self._did, action),
            on_success=lambda _: self._on_action_done(label),
            on_error=lambda exc: self._on_action_failed(action, label, exc),
        )

    def _on_action_done(self, label: str) -> None:
        if not self._alive():
            return
        self._busy_action = None
        self._apply_online_state()
        Toast.info(self, f"已向「{self._device.name}」下发{label}指令", 2500)
        # 指令可能改变设备状态，顺手刷新一次
        self.load()

    def _on_action_failed(self, action: str, label: str,
                          error: Exception) -> None:
        if not self._alive():
            return
        self._busy_action = None
        self._apply_online_state()
        Toast.info(self, f"{label}指令失败：{error}", 4000)

    # ---------- 样式与布局 ----------

    def _apply_styles(self) -> None:
        self._title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        self._controls_title.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        self._actions_title.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        self._refresh_btn.setStyleSheet(secondary_button_qss("4px 14px"))
        for button in self._action_buttons.values():
            button.setStyleSheet(secondary_button_qss("4px 14px"))
        self._power_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._apply_online_state()

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()
        self._render_status_rows(self._current_rows())

    def _current_rows(self) -> dict:
        rows: dict[str, str] = {}
        for row in range(self._status_grid.rowCount()):
            key_item = self._status_grid.itemAtPosition(row, 0)
            value_item = self._status_grid.itemAtPosition(row, 1)
            if key_item is None or value_item is None:
                continue
            key_widget = key_item.widget()
            value_widget = value_item.widget()
            if key_widget is None or value_widget is None:
                continue
            rows[key_widget.text()] = value_widget.text()
        return rows

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().resizeEvent(event)
        self._place_overlay()
        pw = min(_PANEL_W, max(320, self.width() - 40))
        ph = min(620, self.height() - 40)
        x = (self.width() - pw) // 2
        y = (self.height() - ph) // 2
        self._panel.setGeometry(x, y, pw, ph)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        self._center_on_screen(_PANEL_W, 620)
        self.raise_()
        self._fade_in()
