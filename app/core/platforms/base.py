# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""多平台设备管理的抽象基类。

界面层与持久化数据只认这里的接口与 DeviceInfo，不关心背后是米家云端
还是开机卡云端。新增一个平台 = 继承 PlatformBase 实现各方法，并在
PlatformRegistry 里注册（见 registry.py），主界面、托盘、桌面挂件、
定时任务等全部入口自动获得该平台的设备与控制能力。

约定：
- did 是跨平台唯一键，由注册表按平台前缀保证唯一（米家保持原生 did
  不变，兼容既有持久化数据）；
- 所有方法均为阻塞式同步调用，必须经 JobExecutor 放后台线程执行；
- 失败一律抛 PlatformError（携带用户可读的中文信息）。
"""

from app.core.models import ActionInfo, ControlSpec, DeviceInfo


class PlatformError(Exception):
    """携带用户可直接阅读的中文信息，界面层直接展示 message 即可。"""


class PlatformBase:
    """设备平台接口。子类按需覆写；不支持的能力给出安全默认值。"""

    # ---- 平台元数据（界面展示用） ----
    platform_id: str = ""
    display_name: str = ""
    icon_name: str = "mdi.devices"          # qtawesome 图标名
    description: str = ""
    # 是否有手动场景（米家有；场景选项卡与场景视图依赖它为 True）
    supports_scenes: bool = False
    # 是否提供温湿度等环境读数（卡片副标题用）
    supports_metrics: bool = False
    # 是否提供属性式详情面板（米家的 prop/action 面板；开机卡这类
    # 指令式平台返回 False，界面改走指令按钮面板）
    supports_props: bool = False
    # 是否走账号密码登录（多平台管理渲染账号/密码/记住表单）；
    # 米家为扫码登录（False），开机卡/美的等为 True
    supports_password_login: bool = False

    # ---- 登录态 ----

    def is_logged_in(self) -> bool:
        """当前是否已登录（可能触发一次轻量联网校验）。"""
        return False

    def session_active(self) -> bool:
        """本会话是否已登录（廉价判断，不发起网络请求）。

        列表合并等高频路径用它避免每轮刷新都打一次联网校验；
        需要确认票据是否仍然有效时用 is_logged_in。
        """
        return self.is_logged_in()

    def login(self, **credentials) -> None:
        """登录；credentials 由各平台自定（如用户名密码）。"""
        raise NotImplementedError

    def logout(self) -> None:
        """退出登录并清理本地会话。"""
        raise NotImplementedError

    def status_text(self) -> str:
        """一行中文状态描述（多平台管理界面展示）。"""
        return "未登录"

    def auto_login(self) -> bool:
        """启动时的静默登录：本地会话或已保存的凭据有效则恢复登录态。

        返回是否处于已登录状态。默认只做一次联网校验。
        """
        return self.is_logged_in()

    # ---- 设备 ----

    def list_devices(self) -> list[DeviceInfo]:
        """拉取该平台全部设备（未登录时返回空列表或抛 PlatformError）。"""
        return []

    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        """批量读取设备电源状态；did -> True/False/None（无能力或读不到）。

        dids 为跨平台 did（DeviceInfo.did），由平台内部转换为原生 ID。
        """
        return {}

    def power_state_known(self, did: str) -> bool:
        """power_states 返回 None 时，是否代表"确认无电源控制能力"。

        True（默认，如米家按 spec 判定、开机卡设备无 pwr 字段）：
        主界面不再重试，电源钮保持隐藏；
        False（如美的登录态异常导致读取失败）：主界面删除缓存值，
        下一轮轮询重试，恢复后电源钮正常出现。
        """
        return True

    def power_state(self, did: str) -> bool | None:
        return self.power_states([did]).get(did)

    def set_power_state(self, did: str, state: bool) -> None:
        """设置电源开关状态。"""
        raise NotImplementedError

    def toggle_power(self, did: str) -> bool:
        """切换电源状态并返回新状态。"""
        current = self.power_state(did)
        if current is None:
            raise PlatformError("设备不支持开关控制或已离线")
        self.set_power_state(did, not current)
        return not current

    # ---- 详情面板 ----

    def capabilities(self, did: str) -> list[ActionInfo]:
        """指令式平台提供的操作列表（开机卡：开机/关机/重启…）。"""
        return []

    def execute_action(self, did: str, action: str) -> None:
        """执行 capabilities 返回的某个指令。"""
        raise PlatformError(f"该平台不支持指令 {action}")

    def device_status(self, did: str) -> dict[str, str]:
        """详情面板附加状态行（键为展示名，值为状态文案）。"""
        return {}

    # ---------- 结构化控制（空调温度/模式/风速等） ----------

    def device_controls(self, did: str) -> list[ControlSpec]:
        """详情面板的结构化控制项；指令式平台（开机卡）返回空列表。"""
        return []

    def set_control(self, did: str, key: str, value) -> None:
        """设置 device_controls 返回的某个控制项。"""
        raise PlatformError(f"该平台不支持控制项 {key}")

    def snapshot(self, did: str) -> tuple[bool | None, dict[str, str]]:
        """一次性读取 (电源状态, 状态行)；默认分两次调用。"""
        return self.power_state(did), self.device_status(did)
