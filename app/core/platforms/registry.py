# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""多平台注册表：界面层与设备交互的唯一入口。

职责：
1. 持有全部已注册平台（米家 + 开机卡，新增平台在此登记）；
2. 按 did 前缀把操作路由到对应平台（米家 did 不带前缀，保持历史
   持久化数据兼容；其他平台 did 形如 ``<platform_id>:<原生ID>``）；
3. 对界面层暴露统一的设备列表 / 开关 / 指令接口，米家专属能力
   （属性面板、场景、图标、耗材）委托给米家平台。

新增一个平台只需两步：实现 PlatformBase 子类，并在
``PlatformRegistry.__init__`` 里注册。
"""

import logging
from typing import TYPE_CHECKING

from app.core.models import ActionInfo, ControlSpec, DeviceInfo, SceneInfo
from app.core.platforms.base import PlatformBase, PlatformError

if TYPE_CHECKING:
    from .mijia import MijiaPlatform
    from .rmtsw import RmtswPlatform

logger = logging.getLogger(__name__)


class PlatformRegistry:
    """多平台设备管理门面；所有方法均为阻塞式，须放后台线程执行。"""

    def __init__(self, mijia_service=None):
        from .rmtsw import RmtswPlatform

        self._platforms: list[PlatformBase] = []
        self._mijia = None
        if mijia_service is not None:
            # 惰性导入：米家平台依赖 mijiaAPI，未安装（如单元测试只验
            # 开源卡平台）时其余平台仍可正常工作
            from .mijia import MijiaPlatform

            self._mijia = MijiaPlatform(mijia_service)
            self._platforms.append(self._mijia)
        self._rmtsw = RmtswPlatform()
        self._platforms.append(self._rmtsw)
        self._load_plugins()

    def _load_plugins(self) -> None:
        """注册插件平台（美的等）。单个插件失败不影响其他平台。"""
        from app.plugins import load_platforms

        for platform in load_platforms():
            logger.info("已注册插件平台: %s", platform.platform_id)
            self._platforms.append(platform)

    # ---------- 平台登记与查询 ----------

    @property
    def platforms(self) -> list[PlatformBase]:
        return list(self._platforms)

    def active_platform_ids(self) -> set[str]:
        """当前已登录的平台 ID 集合（本地判定，不发网络请求）。

        界面层用它把未登录平台的设备（含缓存里的旧设备）挡在列表与
        筛选器之外——用户没登录的平台不该出现在家庭/房间下拉里。
        """
        active: set[str] = set()
        for platform in self._platforms:
            try:
                if platform.session_active():
                    active.add(platform.platform_id)
            except Exception as exc:
                logger.warning("读取平台 %s 登录态失败: %s",
                               platform.platform_id, exc)
        return active

    @property
    def mijia(self) -> "MijiaPlatform":
        if self._mijia is None:
            raise PlatformError("米家平台不可用")
        return self._mijia

    @property
    def rmtsw(self) -> "RmtswPlatform":
        return self._rmtsw

    def platform_of(self, did: str) -> PlatformBase | None:
        """按 did 前缀找到设备所属平台；找不到返回 None。"""
        text = str(did)
        for platform in self._platforms:
            prefix = f"{platform.platform_id}:"
            if text.startswith(prefix):
                return platform
        # 无前缀 did 归属第一个"原生 did"平台（米家）
        for platform in self._platforms:
            if platform.platform_id == "mijia":
                return platform
        return None

    def _require(self, did: str) -> PlatformBase:
        platform = self.platform_of(did)
        if platform is None:
            raise PlatformError(f"找不到设备 {did} 所属的平台")
        return platform

    def platform_status(self) -> list[dict]:
        """多平台管理界面的数据源：各平台登录态与设备数。"""
        result = []
        for platform in self._platforms:
            logged_in = False
            status = "未登录"
            count = 0
            try:
                logged_in = platform.session_active()
                status = platform.status_text()
                if logged_in:
                    count = len(platform.list_devices())
            except Exception as exc:  # 单个平台异常不拖垮整个面板
                logger.warning("读取平台 %s 状态失败: %s", platform.platform_id, exc)
                status = f"状态获取失败：{exc}"
            result.append({
                "platform_id": platform.platform_id,
                "display_name": platform.display_name,
                "icon_name": platform.icon_name,
                "description": platform.description,
                "logged_in": logged_in,
                "status": status,
                "device_count": count,
            })
        return result

    def silent_auto_login(self) -> None:
        """启动时对非交互平台做静默登录（米家走扫码流程，不在此列）。"""
        for platform in self._platforms:
            if platform.platform_id == "mijia":
                continue
            try:
                platform.auto_login()
            except Exception as exc:
                logger.info("平台 %s 静默登录失败: %s",
                            platform.platform_id, exc)

    # ---------- 设备列表与开关（跨平台） ----------

    def list_devices(self) -> list[DeviceInfo]:
        """合并全部已登录平台的设备列表。

        单个平台失败只记录并跳过其设备：米家列表不能因为开机卡
        会话失效而整体消失。
        """
        result: list[DeviceInfo] = []
        for platform in self._platforms:
            try:
                if not platform.session_active():
                    continue
                result.extend(platform.list_devices())
            except Exception as exc:
                logger.warning("获取平台 %s 设备列表失败: %s",
                               platform.platform_id, exc)
        return result

    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        """批量读电源状态：按平台分组后分别请求，结果合并。"""
        groups: dict[str, list[str]] = {}
        for did in dids:
            platform = self.platform_of(did)
            key = platform.platform_id if platform else "?"
            groups.setdefault(key, []).append(did)
        result: dict[str, bool | None] = {did: None for did in dids}
        for key, group in groups.items():
            platform = next(
                (p for p in self._platforms if p.platform_id == key), None)
            if platform is None:
                continue
            # 未登录的平台不发起请求：轮询会 3 秒一次，让离线平台反复
            # 打无效请求只会把日志刷满（缓存里的旧设备 did 仍在列表时）
            if not platform.session_active():
                continue
            try:
                result.update(platform.power_states(group))
            except Exception as exc:
                logger.warning("读取平台 %s 开关状态失败: %s", key, exc)
        return result

    def power_state(self, did: str) -> bool | None:
        return self.power_states([did]).get(did)

    def power_state_known(self, did: str) -> bool:
        """None 是否代表"确认无电源能力"（供主界面决定是否重试）。"""
        platform = self.platform_of(did)
        if platform is None:
            return True
        try:
            return platform.power_state_known(did)
        except Exception:
            return True

    def set_power_state(self, did: str, state: bool) -> None:
        self._require(did).set_power_state(did, state)

    def toggle_power(self, did: str) -> bool:
        return self._require(did).toggle_power(did)

    # ---------- 指令式平台（开机卡）详情面板 ----------

    def capabilities(self, did: str) -> list[ActionInfo]:
        return self._require(did).capabilities(did)

    def execute_action(self, did: str, action: str) -> None:
        self._require(did).execute_action(did, action)

    def device_status(self, did: str) -> dict[str, str]:
        return self._require(did).device_status(did)

    # ---------- 结构化控制（美的空调等） ----------

    def device_controls(self, did: str) -> list[ControlSpec]:
        return self._require(did).device_controls(did)

    def set_control(self, did: str, key: str, value) -> None:
        self._require(did).set_control(did, key, value)

    # ---------- 米家专属能力（委托） ----------

    def _mijia_service(self):
        return self.mijia.service

    def login_status(self) -> bool:
        return self._mijia is not None and self._mijia.is_logged_in()

    def qr_login_begin(self) -> dict | None:
        return self._mijia_service().qr_login_begin()

    def qr_login_wait(self, login_data: dict) -> None:
        self._mijia_service().qr_login_wait(login_data)

    def api_version(self) -> str:
        return self._mijia_service().api_version()

    def device_detail(self, did: str):
        return self._mijia_service().device_detail(did)

    def read_prop(self, did: str, name: str):
        return self._mijia_service().read_prop(did, name)

    def read_props(self, did: str, names: list[str]) -> dict:
        return self._mijia_service().read_props(did, names)

    def write_prop(self, did: str, name: str, value) -> None:
        self._mijia_service().write_prop(did, name, value)

    def run_action(self, did: str, name: str, params=None) -> None:
        self._mijia_service().run_action(did, name, params)

    def read_metrics(self, dids: list[str]) -> dict[str, str | None]:
        return self._mijia_service().read_metrics(dids)

    def scene_list(self, home_name: str | None = None) -> list[SceneInfo]:
        return self._mijia_service().scene_list(home_name)

    def run_scene(self, scene: SceneInfo) -> None:
        self._mijia_service().run_scene(scene)

    def icon_urls(self, models: list[str]) -> dict[str, str | None]:
        return self._mijia_service().icon_urls(models)

    def icon_file(self, model: str, url: str):
        return self._mijia_service().icon_file(model, url)

    def has_product_page_name(self, model: str) -> bool:
        return self._mijia_service().has_product_page_name(model)

    def cached_product_page_names(self, models: list[str]) -> dict[str, str]:
        return self._mijia_service().cached_product_page_names(models)

    def product_page_name(self, model: str) -> str | None:
        return self._mijia_service().product_page_name(model)

    def localized_product_names(self, dids: list[str],
                                names: dict[str, str]) -> dict[str, str]:
        return self._mijia_service().localized_product_names(dids, names)

    def spec_has_published_functions(self, spec_type: str) -> bool | None:
        return self._mijia_service().spec_has_published_functions(spec_type)

    def consumable_items(self) -> list[dict]:
        return self._mijia_service().consumable_items()
