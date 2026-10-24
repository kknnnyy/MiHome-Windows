# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""米家平台：把既有 MijiaService 适配为 PlatformBase 接口。

米家是多平台体系里的第一个平台，其设备 did 保持云端原生值不变
（不带前缀），托盘/挂件/定时任务等历史持久化数据因此完全兼容。
"""

from app.core.models import DeviceInfo
from app.core.platforms.base import PlatformBase
from app.core.service import MijiaService


class MijiaPlatform(PlatformBase):
    """米家平台适配层；登录走扫码，由 LoginDialog 驱动。"""

    platform_id = "mijia"
    display_name = "米家"
    icon_name = "mdi.home-assistant"
    description = "米家 APP 账号（扫码登录）：设备、手动场景、小爱语音"
    supports_scenes = True
    supports_metrics = True
    supports_props = True

    def __init__(self, service: MijiaService):
        self._service = service

    @property
    def service(self) -> MijiaService:
        """底层米家服务（属性式详情面板、图标、耗材等米家专属能力）。"""
        return self._service

    # ---------- 登录态 ----------

    def is_logged_in(self) -> bool:
        return self._service.login_status()

    def status_text(self) -> str:
        return "已登录（扫码）" if self.is_logged_in() else "未登录"

    # ---------- 设备 ----------

    def list_devices(self) -> list[DeviceInfo]:
        # MijiaService 产出的 DeviceInfo 默认 platform="mijia"、native_id=""
        return self._service.list_devices()

    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        return self._service.power_states(dids)

    def set_power_state(self, did: str, state: bool) -> None:
        self._service.set_power_state(did, state)

    def toggle_power(self, did: str) -> bool:
        return self._service.toggle_power(did)
