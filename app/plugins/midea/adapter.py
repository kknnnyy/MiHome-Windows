# SPDX-License-Identifier: GPL-3.0-or-later
"""美的平台适配器：把上游协议层（vendor/）桥接到 PlatformBase。

架构要点：
- 上游 core/ 是 aiohttp **异步**客户端；本类是同步阻塞式（与开机卡
  平台一致），由 _AsyncBridge 维护一条专用事件循环线程承接；
- 设备控制复用上游 MiedaDevice（纯云端模式：不下载 Lua 编解码器，
  状态查询与控制均走美居云端接口，lua_file=None 即生效）；
- device_mapping 数据表经 ha_shim 的常量替身导入，由本文件解释成
  ControlSpec / 状态行；上游更新映射表无需改这里；
- 登录态（accessToken 等）经 export/import_session_payload 落到
  platform_store，重启后免重新登录。
"""

import asyncio
import atexit
import importlib
import logging
import threading
import time

from . import ha_shim

ha_shim.install()  # 必须在 import vendor.* 之前（const/mapping 依赖 HA 常量替身）

from .vendor.core.cloud import MeijuCloud, MSmartHomeCloud  # noqa: E402
from .vendor.core.device import MiedaDevice  # noqa: E402
from .vendor.device_mapping._cloud_stats import (  # noqa: E402
    merge_cloud_stats_mapping,
)

from app.core import platform_store  # noqa: E402
from app.core.models import ActionInfo, ControlSpec, DeviceInfo  # noqa: E402
from app.core.platforms.base import PlatformBase, PlatformError  # noqa: E402

logger = logging.getLogger(__name__)

_PREFIX = "midea:"
_HOME_NAME = "美的家居"
_MODEL_FALLBACK = "midea.device"
# 设备状态缓存时长：主界面轮询 3 秒一次，美的云端按设备逐个查询较慢，
# 做 TTL 避免同一轮询周期内重复请求
_STATE_TTL = 15.0
_DEFAULT_SERVER = "美的美居"
# 自动登录失败后的静默期：防止轮询/刷新反复触发真实登录
# （每次真实登录都会在用户手机上弹登录提醒，曾把用户打到报警）
_AUTO_LOGIN_COOLDOWN = 180.0
# 登录态联网校验的缓存时长（多平台管理打开时校验一次即可）
_LOGIN_CHECK_TTL = 60.0

# 常见模式/风速名的中文展示（映射表只有英文键，HA 的翻译文件不随插件分发）
_LABELS = {
    "off": "关闭", "on": "开启",
    "cool": "制冷", "heat": "制热", "auto": "自动", "dry": "除湿",
    "fan_only": "送风", "fan": "送风",
    "silent": "静音", "low": "低风", "medium": "中风", "medium_low": "中低风",
    "medium_high": "中高风", "high": "高风", "full": "最大风", "max": "最大风",
    "eco": "节能", "comfort": "舒适", "sleep": "睡眠", "boost": "强劲",
    "strong_wind": "强劲风", "none": "无",
    "both": "上下左右", "horizontal": "左右", "vertical": "上下",
    "heat_exchange": "热交换", "smooth_in": "微风进风", "rough_in": "强风进风",
    "smooth_out": "微风排风", "rough_out": "强风排风", "innercycle": "内循环",
}
_SENSOR_LABELS = {
    "indoor_temperature": "室内温度", "indoor_humidity": "室内湿度",
    "outdoor_temperature": "室外温度", "real_time_power_value": "实时功率",
    "real_time_power": "实时功率", "total_energy": "累计电量",
    "mode": "运行模式", "wind_speed": "风速", "temperature": "设定温度",
    "filter_life": "滤网寿命", "pm25": "PM2.5", "co2": "CO₂",
    "tvoc": "TVOC", "humidity": "湿度", "water_level": "水位",
    "running_status": "运行状态", "error_code": "故障代码",
}


# ---------------------------------------------------------------- 事件循环桥

class _AsyncBridge:
    """专用事件循环线程：把 aiohttp 异步调用桥接成同步阻塞调用。"""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="midea-async")
        self._thread.start()
        if not self._ready.wait(5):
            raise PlatformError("美的平台后台事件循环启动失败")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, factory, timeout: float = 30.0):
        """在循环线程里执行 coroutine 工厂返回的协程，阻塞等结果。"""
        if not self._loop.is_running():
            raise PlatformError("美的平台后台事件循环已停止")
        future = asyncio.run_coroutine_threadsafe(factory(), self._loop)
        return future.result(timeout)


# ---------------------------------------------------------------- 映射表解释

def _last_name(value) -> str:
    """HA 常量替身对象/字符串 -> 名字末段（CLIMATE / CELSIUS / % ...）。"""
    if isinstance(value, str):
        return value.rsplit(".", 1)[-1]
    return repr(value).rsplit(".", 1)[-1]


_UNIT_TEXTS = {
    "CELSIUS": "°C", "FAHRENHEIT": "°F", "KELVIN": "K",
    "WATT": "W", "PERCENTAGE": "%", "PPM": "ppm",
    "MINUTES": "分钟", "SECONDS": "秒", "HOURS": "小时",
    "LUX": "lx", "DECIBEL": "dB",
}


def _unit_text(raw) -> str:
    """映射表的单位字段 -> 展示文本。

    单位可能是 HA 枚举替身（UnitOfTemperature.CELSIUS），也可能是
    普通字符串（"%"）；都统一成可读文本。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return _UNIT_TEXTS.get(_last_name(raw), "")


def _label_of(key: str) -> str:
    return _LABELS.get(key, _SENSOR_LABELS.get(key, key))


def _rationale_of(entity_cfg: dict, mapping: dict) -> list:
    """布尔值表示：实体级 rationale 覆盖映射级；缺省 ["off", "on"]。"""
    rationale = entity_cfg.get("rationale") or mapping.get("rationale")
    if isinstance(rationale, list) and len(rationale) >= 2:
        return rationale
    return ["off", "on"]


def _to_bool(value, rationale: list) -> bool | None:
    if value is None:
        return None
    if value in (rationale[1], 1, "1", True):
        return True
    if value in (rationale[0], 0, "0", False):
        return False
    return None


def _as_number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def select_mapping(device_mappings: dict, device_type: int, sn8: str = "",
                   subtype=None, category=None) -> dict:
    """复刻上游 get_device_mapping 的选择优先级。

    subtype 精确匹配 -> sn8 规则匹配 -> default_{category} -> default。
    """
    if not device_mappings:
        return {}
    result = None
    if subtype is not None:
        subtype_str = str(subtype)
        for key, config in device_mappings.items():
            if (isinstance(key, tuple) and len(key) == 2
                    and key[0] == "subtype" and str(key[1]) == subtype_str):
                result = config
                break
    if result is None and sn8:
        import re

        for key, config in device_mappings.items():
            if (key == sn8
                    or (isinstance(key, tuple) and sn8 in key)
                    or (isinstance(key, str) and re.match(key, sn8))):
                result = config
                break
    if result is None and category:
        category_key = f"default_{str(category).replace('-', '_')}"
        if category_key in device_mappings:
            result = device_mappings[category_key]
    if result is None and "default" in device_mappings:
        result = device_mappings["default"]
    return result or {}


def _match_option(control: dict, attributes: dict) -> bool:
    """枚举选项的控制字典是否与当前属性吻合（HA 同款反向匹配）。"""
    for key, value in control.items():
        if attributes.get(key) != value:
            return False
    return True


def _attr_of(entity_cfg: dict, entity_key: str) -> str:
    attr = entity_cfg.get("attribute")
    return str(attr) if isinstance(attr, str) and attr else entity_key


def build_controls(mapping: dict, attributes: dict,
                   online: bool) -> list[ControlSpec]:
    """把映射表 entities 解释成详情面板的结构化控制项。

    同名键去重（先到先得）：映射表里 CLIMATE 的 aux_heat（读 ptc
    属性）与 SWITCH 实体的 aux_heat 同名，以先解释出的为准，避免
    语义冲突的控制项互相覆盖。
    """
    controls: list[ControlSpec] = []
    by_key: dict[str, ControlSpec] = {}
    entities = mapping.get("entities") or {}
    if not isinstance(entities, dict):
        return controls

    for platform_key, platform_cfg in entities.items():
        platform = _last_name(platform_key)
        if not isinstance(platform_cfg, dict):
            continue
        for entity_key, cfg in platform_cfg.items():
            if not isinstance(cfg, dict):
                continue
            try:
                for control in _entity_controls(platform, entity_key, cfg,
                                                mapping, attributes, online):
                    existing = by_key.get(control.key)
                    if existing is None:
                        by_key[control.key] = control
                    elif (existing.value is None
                          and control.value is not None):
                        # 同名键冲突（如 FAN 的 power 与 CLIMATE 的
                        # power）：保留能解析出当前值的那个
                        by_key[control.key] = control
            except Exception as exc:  # 单个实体解释失败不拖垮整台设备
                logger.debug("美的: 实体 %s/%s 解释失败: %s",
                             platform, entity_key, exc)
    return list(by_key.values())


def _entity_controls(platform: str, entity_key: str, cfg: dict,
                     mapping: dict, attributes: dict,
                     online: bool) -> list[ControlSpec]:
    controls: list[ControlSpec] = []
    enabled = online

    if platform == "CLIMATE":
        # 电源
        power_attr = cfg.get("power")
        if isinstance(power_attr, str):
            rationale = _rationale_of(cfg, mapping)
            controls.append(ControlSpec(
                key="power", label="电源", kind="switch",
                value=_to_bool(attributes.get(power_attr), rationale),
                enabled=enabled))
        # 模式
        modes = cfg.get("hvac_modes")
        if isinstance(modes, dict) and modes:
            current = next((name for name, ctrl in modes.items()
                            if isinstance(ctrl, dict)
                            and _match_option(ctrl, attributes)), None)
            controls.append(ControlSpec(
                key="mode", label="模式", kind="enum", value=current,
                options=[(name, _label_of(name)) for name in modes],
                enabled=enabled))
        # 风速
        fan_modes = cfg.get("fan_modes")
        if isinstance(fan_modes, dict) and fan_modes:
            current = next((name for name, ctrl in fan_modes.items()
                            if isinstance(ctrl, dict)
                            and _match_option(ctrl, attributes)), None)
            controls.append(ControlSpec(
                key="fan_mode", label="风速", kind="enum", value=current,
                options=[(name, _label_of(name)) for name in fan_modes],
                enabled=enabled))
        # 预设（eco/睡眠等）
        presets = cfg.get("preset_modes")
        if isinstance(presets, dict) and presets:
            current = next((name for name, ctrl in presets.items()
                            if isinstance(ctrl, dict)
                            and _match_option(ctrl, attributes)), None)
            controls.append(ControlSpec(
                key="preset", label="预设", kind="enum", value=current,
                options=[(name, _label_of(name)) for name in presets],
                enabled=enabled))
        # 摆风
        swing = cfg.get("swing_modes")
        if isinstance(swing, dict) and swing:
            current = next((name for name, ctrl in swing.items()
                            if isinstance(ctrl, dict)
                            and _match_option(ctrl, attributes)), None)
            controls.append(ControlSpec(
                key="swing", label="摆风", kind="enum", value=current,
                options=[(name, _label_of(name)) for name in swing],
                enabled=enabled))
        # 设定温度
        temp_attr = cfg.get("target_temperature")
        if isinstance(temp_attr, list) and temp_attr:
            temp_attr = temp_attr[0]
        if isinstance(temp_attr, str):
            step = 1.0 if "WHOLE" in repr(cfg.get("precision")) else 0.5
            controls.append(ControlSpec(
                key="target_temp", label="设定温度", kind="range",
                value=_as_number(attributes.get(temp_attr)),
                minimum=float(cfg.get("min_temp", 16)),
                maximum=float(cfg.get("max_temp", 30)),
                step=step, unit="°C", enabled=enabled))
        # 辅热
        aux_attr = cfg.get("aux_heat")
        if isinstance(aux_attr, str):
            rationale = _rationale_of(cfg, mapping)
            controls.append(ControlSpec(
                key="aux_heat", label="辅助加热", kind="switch",
                value=_to_bool(attributes.get(aux_attr), rationale),
                enabled=enabled))

    elif platform == "DEHUMIDIFIER":
        # 与 CLIMATE 同构（target_humidity 等键名按映射表实际内容解释）
        power_attr = cfg.get("power")
        if isinstance(power_attr, str):
            rationale = _rationale_of(cfg, mapping)
            controls.append(ControlSpec(
                key="power", label="电源", kind="switch",
                value=_to_bool(attributes.get(power_attr), rationale),
                enabled=enabled))
        for modes_key, label in (("modes", "模式"), ("hvac_modes", "模式")):
            modes = cfg.get(modes_key)
            if isinstance(modes, dict) and modes:
                current = next((name for name, ctrl in modes.items()
                                if isinstance(ctrl, dict)
                                and _match_option(ctrl, attributes)), None)
                controls.append(ControlSpec(
                    key="mode", label=label, kind="enum", value=current,
                    options=[(name, _label_of(name)) for name in modes],
                    enabled=enabled))
                break
        humidity_attr = cfg.get("target_humidity")
        if isinstance(humidity_attr, str):
            controls.append(ControlSpec(
                key="target_humidity", label="设定湿度", kind="range",
                value=_as_number(attributes.get(humidity_attr)),
                minimum=float(cfg.get("min_humidity", 30)),
                maximum=float(cfg.get("max_humidity", 80)),
                step=1.0, unit="%", enabled=enabled))

    elif platform == "FAN":
        power_attr = cfg.get("power")
        if isinstance(power_attr, str):
            rationale = _rationale_of(cfg, mapping)
            controls.append(ControlSpec(
                key="power", label="电源", kind="switch",
                value=_to_bool(attributes.get(power_attr), rationale),
                enabled=enabled))
        presets = cfg.get("preset_modes")
        if isinstance(presets, dict) and presets:
            current = next((name for name, ctrl in presets.items()
                            if isinstance(ctrl, dict)
                            and _match_option(ctrl, attributes)), None)
            controls.append(ControlSpec(
                key="preset", label="模式", kind="enum", value=current,
                options=[(name, _label_of(name)) for name in presets],
                enabled=enabled))
        speeds = cfg.get("speeds")
        if isinstance(speeds, list) and speeds:
            # speeds: [{"wind_speed": 1}, ...] -> 按数值分档
            options = []
            current = None
            for item in speeds:
                if not isinstance(item, dict):
                    continue
                for attr, value in item.items():
                    options.append((value, f"{value} 档"))
                    if attributes.get(attr) == value:
                        current = value
            if options:
                controls.append(ControlSpec(
                    key="fan_speed", label="风速档位", kind="enum",
                    value=current, options=options, enabled=enabled))
        oscillate = cfg.get("oscillate")
        if isinstance(oscillate, str):
            rationale = _rationale_of(cfg, mapping)
            controls.append(ControlSpec(
                key="oscillate", label="摆风", kind="switch",
                value=_to_bool(attributes.get(oscillate), rationale),
                enabled=enabled))

    elif platform == "SWITCH":
        rationale = _rationale_of(cfg, mapping)
        label = str(cfg.get("name") or _label_of(entity_key))
        controls.append(ControlSpec(
            key=entity_key, label=label, kind="switch",
            value=_to_bool(attributes.get(entity_key), rationale),
            enabled=enabled))

    elif platform == "NUMBER":
        attr = _attr_of(cfg, entity_key)
        controls.append(ControlSpec(
            key=attr, label=_label_of(entity_key), kind="range",
            value=_as_number(attributes.get(attr)),
            minimum=float(cfg.get("min", 0)),
            maximum=float(cfg.get("max", 100)),
            step=float(cfg.get("step", 1)),
            unit=_unit_text(cfg.get("unit_of_measurement")),
            enabled=enabled))

    elif platform == "SELECT":
        options_cfg = cfg.get("options")
        options: list[tuple[object, str]] = []
        current = None
        if isinstance(options_cfg, dict):
            for name, ctrl in options_cfg.items():
                options.append((name, _label_of(name)))
                if isinstance(ctrl, dict) and _match_option(ctrl, attributes):
                    current = name
        elif isinstance(options_cfg, list):
            for item in options_cfg:
                if isinstance(item, dict) and "value" in item:
                    value = item["value"]
                    label = str(item.get("label") or value)
                    options.append((value, label))
                    if attributes.get(entity_key) == value:
                        current = value
        if options:
            controls.append(ControlSpec(
                key=entity_key, label=_label_of(entity_key), kind="enum",
                value=current, options=options, enabled=enabled))

    elif platform == "LIGHT":
        power_attr = cfg.get("power")
        if isinstance(power_attr, str):
            rationale = _rationale_of(cfg, mapping)
            controls.append(ControlSpec(
                key="power", label="电源", kind="switch",
                value=_to_bool(attributes.get(power_attr), rationale),
                enabled=enabled))
        brightness = cfg.get("brightness")
        if isinstance(brightness, dict):
            for attr, span in brightness.items():
                if isinstance(span, list) and len(span) == 2:
                    controls.append(ControlSpec(
                        key=attr, label="亮度", kind="range",
                        value=_as_number(attributes.get(attr)),
                        minimum=float(span[0]), maximum=float(span[1]),
                        step=1.0, unit="%", enabled=enabled))
        presets = cfg.get("preset_modes")
        if isinstance(presets, dict) and presets:
            current = next((name for name, ctrl in presets.items()
                            if isinstance(ctrl, dict)
                            and _match_option(ctrl, attributes)), None)
            controls.append(ControlSpec(
                key="preset", label="灯光模式", kind="enum", value=current,
                options=[(name, _label_of(name)) for name in presets],
                enabled=enabled))

    return controls


def build_status_rows(mapping: dict, attributes: dict) -> dict[str, str]:
    """状态行：SENSOR 实体 +  climate 的当前温度等只读信息。"""
    rows: dict[str, str] = {}
    entities = mapping.get("entities") or {}
    if not isinstance(entities, dict):
        return rows
    for platform_key, platform_cfg in entities.items():
        if _last_name(platform_key) != "SENSOR" or not isinstance(
                platform_cfg, dict):
            continue
        for entity_key, cfg in platform_cfg.items():
            if not isinstance(cfg, dict):
                continue
            value = attributes.get(entity_key)
            if value is None:
                continue
            unit = _unit_text(cfg.get("unit_of_measurement"))
            text = f"{value}{unit}" if unit else str(value)
            rows[_SENSOR_LABELS.get(entity_key, entity_key)] = text
    return rows


# ---------------------------------------------------------------- 设备条目

class _DeviceEntry:
    """一台美的设备：云端原始信息 + 上游 MiedaDevice + 映射表。"""

    def __init__(self, appliance_code: int, info: dict, device: MiedaDevice,
                 mapping: dict, home_name: str = ""):
        self.appliance_code = appliance_code
        self.info = info
        self.device = device
        self.mapping = mapping
        self.home_name = home_name
        self.room_name = ""
        # 电源属性键与布尔表示（解释自映射表）
        self.power_attr, self.rationale = _power_keys(mapping)

    @property
    def device_type(self) -> str:
        return f"T0x{self.device.device_type:02X}"


def _power_keys(mapping: dict) -> tuple[str | None, list]:
    """从映射表找**主电源**属性键（面板主电源按钮用）。

    优先级 CLIMATE > DEHUMIDIFIER > FAN > LIGHT > WATER_HEATER > 任意：
    T0xAC（新风空调）同时有 FAN（新风机）与 CLIMATE（空调）两个
    电源键，主电源应取空调那个；纯风扇类设备则回落到 FAN。
    """
    entities = mapping.get("entities") or {}
    rationale = mapping.get("rationale")
    if not (isinstance(rationale, list) and len(rationale) >= 2):
        rationale = ["off", "on"]

    def _from(cfg: dict) -> tuple[str, list]:
        entity_rationale = cfg.get("rationale")
        if isinstance(entity_rationale, list) and len(entity_rationale) >= 2:
            return cfg["power"], entity_rationale
        return cfg["power"], rationale

    priority = ("CLIMATE", "DEHUMIDIFIER", "FAN", "LIGHT", "WATER_HEATER")
    for want in priority:
        for platform_key, platform_cfg in entities.items():
            if _last_name(platform_key) != want or not isinstance(
                    platform_cfg, dict):
                continue
            for cfg in platform_cfg.values():
                if isinstance(cfg, dict) and isinstance(cfg.get("power"), str):
                    return _from(cfg)
    # 兜底：任意带电源键的实体
    for platform_cfg in entities.values():
        if not isinstance(platform_cfg, dict):
            continue
        for cfg in platform_cfg.values():
            if isinstance(cfg, dict) and isinstance(cfg.get("power"), str):
                return _from(cfg)
    return None, rationale


def _preset_attribute_keys(mapping: dict) -> set[str]:
    """复刻上游：预置 attributes 键，保证未刷新前 set_attribute 也认得。"""
    keys: set[str] = set(mapping.get("centralized") or [])
    entities = mapping.get("entities") or {}
    if not isinstance(entities, dict):
        return keys
    for platform_key, platform_cfg in entities.items():
        if not isinstance(platform_cfg, dict):
            continue
        platform = _last_name(platform_key)
        for entity_key, cfg in platform_cfg.items():
            if not isinstance(cfg, dict):
                continue
            for k in ("power", "aux_heat", "current_temperature",
                      "target_temperature", "oscillate", "min_temp",
                      "max_temp"):
                value = cfg.get(k)
                if isinstance(value, str):
                    keys.add(value)
                elif isinstance(value, list):
                    keys.update(v for v in value if isinstance(v, str))
            for map_key in ("hvac_modes", "preset_modes", "swing_modes",
                            "fan_modes", "operation_list", "options"):
                maps = cfg.get(map_key)
                if isinstance(maps, dict):
                    for cond in maps.values():
                        if isinstance(cond, dict):
                            keys.update(cond.keys())
            if platform in ("SENSOR", "BINARY_SENSOR", "SWITCH", "FAN",
                            "SELECT", "VACUUM", "LOCK", "TEXT"):
                keys.add(entity_key)
    return keys


class _RoomAwareMeijuCloud(MeijuCloud):
    """带出房间名的 list_appliances（上游实现丢弃了 room 名）。

    与上游 v0.4.19 的响应结构保持一致；解析失败时回退上游实现，
    上游变更接口时不致于完全不可用。
    """

    async def list_appliances(self, home_id):
        try:
            self._homegroup_id = str(home_id)
            response = await self._api_request(
                endpoint="/v1/appliance/home/list/get",
                data={"homegroupId": home_id})
            if not response:
                return None
            appliances = {}
            for home in response.get("homeList") or []:
                for room in home.get("roomList") or []:
                    room_name = room.get("name") or ""
                    for appliance in room.get("applianceList") or []:
                        info = {
                            "name": appliance.get("name"),
                            "type": int(appliance.get("type"), 16),
                            "sn": (self._security.aes_decrypt(
                                appliance.get("sn"))
                                if appliance.get("sn") else ""),
                            "sn8": appliance.get("sn8") or "00000000",
                            "category": appliance.get("category"),
                            "smart_product_id": appliance.get(
                                "smartProductId", "0"),
                            "model_number": appliance.get(
                                "modelNumber", "0"),
                            "manufacturer_code": appliance.get(
                                "enterpriseCode", "0000"),
                            "model": appliance.get("productModel"),
                            "online": appliance.get("onlineStatus") == "1",
                            "room": room_name,
                        }
                        if not info.get("model"):
                            info["model"] = info["sn8"]
                        appliances[int(appliance["applianceCode"])] = info
            return appliances
        except Exception as exc:
            logger.info("美的: 房间名解析失败，回退上游列表 (%s)", exc)
            return await super().list_appliances(home_id)


# ---------------------------------------------------------------- 平台

class MideaPlatform(PlatformBase):
    """美的平台（美的美居云端）：空调等设备的远程控制。"""

    platform_id = "midea"
    display_name = "美的"
    icon_name = "mdi.air-conditioner"
    description = ("美的美居云端（美的美居 App 账号）：空调等设备的"
                   "开关、温度、模式、风速远程控制")
    supports_metrics = False
    supports_props = False        # 走指令 + 结构化控制面板
    supports_password_login = True

    def __init__(self):
        self._bridge = _AsyncBridge()
        self._session = None
        self._cloud = None
        self._user_name: str | None = None
        self._devices: dict[str, _DeviceEntry] = {}
        self._homes: dict[int, str] = {}
        self._state_cache: dict[str, tuple[float, dict]] = {}
        # 自动登录失败静默期截止时刻（monotonic）；登录态联网校验缓存
        self._auto_login_blocked_until = 0.0
        self._login_checked_at = 0.0
        self._login_checked_ok = False
        # 进程退出时兜底关闭 aiohttp 会话，避免 "Unclosed client session"
        atexit.register(self._close_session_sync)

    # ---------- 登录态 ----------

    def _ensure_session(self):
        """在事件循环线程里创建 aiohttp 会话（惰性，仅一次）。"""
        if self._session is not None:
            return self._session

        async def _create():
            import aiohttp

            return aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30))

        self._session = self._bridge.run(_create)
        return self._session

    def _make_cloud(self, account: str, password: str,
                    server: str = _DEFAULT_SERVER):
        session = self._ensure_session()
        if server == "MSmartHome":
            return MSmartHomeCloud(
                cloud_name=server, session=session,
                account=account, password=password)
        return _RoomAwareMeijuCloud(
            cloud_name=_DEFAULT_SERVER, session=session,
            account=account, password=password)

    def session_active(self) -> bool:
        return self._cloud is not None

    def is_logged_in(self) -> bool:
        """联网校验登录态（带缓存）。

        每次校验都可能触发 vendor 的 token 失效自愈重登（= 用户手机上
        弹登录提醒），因此 60 秒内复用上次结果，不反复打扰云端与用户。
        """
        if self._cloud is None:
            return False
        now = time.monotonic()
        if now - self._login_checked_at < _LOGIN_CHECK_TTL:
            return self._login_checked_ok
        try:
            homes = self._bridge.run(self._cloud.list_home, timeout=25)
            ok = homes is not None
        except Exception:
            ok = False
        self._login_checked_at = now
        self._login_checked_ok = ok
        return ok

    def auto_login(self) -> bool:
        """启动静默登录：本地会话 -> 保存的账号密码。

        关键点：
        - 构造云端对象必须带**真实密码**——美的 token 失效（40002）时
          vendor 的自动重登用的是 cloud._password，占位符会让自愈永远失败；
        - 失败后进入静默期：轮询与刷新会反复调到这里，每次都真实登录
          会在用户手机上连环弹提醒（曾真实发生）；
        - 登录成功后重置静默期与校验缓存。
        """
        now = time.monotonic()
        if now < self._auto_login_blocked_until:
            return False

        session = platform_store.load_session(self.platform_id)
        payload = session.get("payload")
        account = platform_store.load_account(self.platform_id)
        username = str(account.get("username") or session.get("account") or "")
        password = str(account.get("password") or "")
        server = str(session.get("server") or _DEFAULT_SERVER)

        if payload and username:
            try:
                cloud = self._make_cloud(username, password or username, server)
                cloud.import_session_payload(payload)
                # 预置冷却：本地会话过期时不让 vendor 连环重登（每次都在
                # 手机上弹提醒），改为快速失败后走下面的账号密码路径，
                # 由我们控制只登一次
                self._arm_relogin_cooldown(cloud)
                homes = self._bridge.run(cloud.list_home, timeout=25)
                if homes is not None:
                    self._cloud = cloud
                    self._user_name = str(
                        session.get("user_name") or cloud.nickname or username)
                    self._auto_login_blocked_until = 0.0
                    self._login_checked_at = now
                    self._login_checked_ok = True
                    logger.info("美的: 已从本地会话恢复登录（%s）", self._user_name)
                    return True
                logger.info("美的: 本地会话已过期，回退账号密码登录")
            except Exception as exc:
                logger.info("美的: 本地会话恢复失败（%s），回退账号密码", exc)
            platform_store.clear_session(self.platform_id)

        if username and password:
            try:
                self.login(username, password)
                self._auto_login_blocked_until = 0.0
                logger.info("美的: 已用保存的账号自动登录")
                return True
            except PlatformError as exc:
                logger.info("美的: 自动登录失败（%s），%ds 内不再重试",
                            exc, int(_AUTO_LOGIN_COOLDOWN))
                self._auto_login_blocked_until = now + _AUTO_LOGIN_COOLDOWN
        return False

    def login(self, username: str, password: str,
              remember: bool = False, server: str = _DEFAULT_SERVER) -> None:
        username = (username or "").strip()
        if not username or not password:
            raise PlatformError("请输入美的账号和密码")
        cloud = self._make_cloud(username, password, server)
        try:
            ok = self._bridge.run(cloud.login, timeout=60)
        except Exception as exc:
            logger.warning("美的: 登录请求异常: %s", exc)
            raise PlatformError(f"美的云端登录失败：{exc}") from exc
        if not ok:
            logger.warning("美的: 账号 %s 登录被云端拒绝", username)
            raise PlatformError(
                "美的云端登录失败：请检查账号密码；若账号开启了短信/图形"
                "验证，请先在美的美居 App 内关闭后重试")
        self._cloud = cloud
        self._user_name = str(cloud.nickname or username)
        logger.info("美的: 登录成功（%s）", self._user_name)
        self._auto_login_blocked_until = 0.0
        self._login_checked_at = time.monotonic()
        self._login_checked_ok = True
        # 登录后先校验一次业务接口。新设备首次登录部分账号需手机确认，
        # 未确认时业务接口返回 40002，vendor 会触发自动重登（每次都在
        # 用户手机上弹提醒，最多连 3 次）。这里把计数与时间戳预置成
        # "刚登过、即将冷却"，让后台重登进入 60 秒冷却，同时把问题
        # 明确暴露给用户，而不是在后台连环打扰。
        self._arm_relogin_cooldown(cloud)
        homes = None
        try:
            homes = self._bridge.run(cloud.list_home, timeout=25)
        except Exception as exc:
            logger.info("美的: 登录后校验家庭列表失败: %s", exc)
        # 登录本身已成功：先落盘会话，校验失败时下次可从本地会话恢复，
        # 不必再登一次（少弹一次手机提醒）
        self._persist_session(username, server)
        if remember:
            platform_store.save_account(self.platform_id, username, password)
        else:
            platform_store.clear_account(self.platform_id)
        if homes is None:
            # 校验失败：不纳入登录态（避免后台反复重登），但把问题明确
            # 暴露给用户，而不是在后台连环重登打扰
            self._cloud = None
            self._user_name = None
            self._login_checked_ok = False
            self._close_session_sync()
            raise PlatformError(
                "美的云端登录待确认：请在「美的美居」App 里确认本次登录"
                "（或检查网络），完成后重试")

    def logout(self) -> None:
        self._cloud = None
        self._user_name = None
        self._devices.clear()
        self._state_cache.clear()
        self._login_checked_ok = False
        platform_store.clear_session(self.platform_id)
        self._close_session_sync()

    def _close_session_sync(self) -> None:
        """同步等待 aiohttp 会话关闭（登出/退出时调用，幂等）。"""
        session, self._session = self._session, None
        if session is None or session.closed:
            return
        try:
            self._bridge.run(session.close, timeout=5)
        except Exception as exc:  # 关闭失败不影响主流程
            logger.debug("美的: 关闭 HTTP 会话失败: %s", exc)

    @staticmethod
    def _arm_relogin_cooldown(cloud) -> None:
        """让 vendor 的 token 失效自动重登进入冷却（见 login() 注释）。

        预置重试计数到阈值、并把最近重登时间设为现在：vendor 见到
        40002 时会判定"刚登过、还在冷却"从而跳过重登。属性名随上游
        可能变化，用防御式 setattr，失败也不影响主流程。
        """
        try:
            setattr(cloud, "_token_invalid_retry_count", 2)
            setattr(cloud, "_last_relogin_attempt", time.monotonic())
        except Exception as exc:  # pragma: no cover - 上游改名时兜底
            logger.debug("美的: 预置重登冷却失败: %s", exc)

    def status_text(self) -> str:
        if self._user_name:
            return f"已登录：{self._user_name}"
        return "未登录"

    def _persist_session(self, account: str, server: str) -> None:
        try:
            payload = self._cloud.export_session_payload()
        except Exception as exc:  # 会话导出失败不阻断登录流程
            logger.info("美的: 会话导出失败（%s）", exc)
            payload = {}
        platform_store.save_session(self.platform_id, {
            "payload": payload,
            "account": account,
            "server": server,
            "user_name": self._user_name,
        })

    # ---------- 设备 ----------

    @staticmethod
    def _uid(appliance_code: int) -> str:
        return f"{_PREFIX}{appliance_code}"

    @staticmethod
    def _code_of(uid: str) -> int:
        try:
            return int(str(uid).split(":", 1)[-1])
        except (TypeError, ValueError):
            raise PlatformError(f"无法解析美的设备 ID：{uid}")

    def _require_cloud(self):
        if self._cloud is None and not self.auto_login():
            raise PlatformError("美的未登录，请先在「多平台管理」中登录")
        return self._cloud

    def _build_device(self, appliance_code: int, info: dict,
                      home_name: str) -> _DeviceEntry:
        """构造云端设备并加载映射表（复刻上游初始化配方）。"""
        device = MiedaDevice(
            name=str(info.get("name") or f"美的设备 {appliance_code}"),
            device_id=appliance_code,
            device_type=int(info.get("type") or 0),
            ip_address=None,
            port=None,
            token=None,
            key=None,
            connected=bool(info.get("online", True)),
            protocol=int(info.get("protocol") or 2),
            model=info.get("model"),
            subtype=info.get("model_number"),
            manufacturer_code=info.get("manufacturer_code"),
            category=info.get("category"),
            sn=info.get("sn"),
            sn8=info.get("sn8"),
            lua_file=None,          # 纯云端模式：不下载 Lua 编解码器
            cloud=self._cloud,
        )
        mapping = self._load_mapping(device, info)
        try:
            device.set_queries(mapping.get("queries") or [{}])
            device.set_cloud_queries(mapping.get("cloud_queries") or {})
            device.set_centralized(mapping.get("centralized") or [])
            device.set_calculate(mapping.get("calculate") or {})
        except Exception as exc:
            logger.info("美的: 设备 %s 映射应用失败: %s", appliance_code, exc)
        # 默认值与预置键（保证未刷新前控制也认得这些属性）
        default_values: dict = {}
        for platform_cfg in (mapping.get("entities") or {}).values():
            if not isinstance(platform_cfg, dict):
                continue
            for entity_key, cfg in platform_cfg.items():
                if isinstance(cfg, dict) and "default_value" in cfg:
                    default_values[_attr_of(cfg, entity_key)] = cfg[
                        "default_value"]
        try:
            device.set_default_values(default_values)
        except Exception:
            pass
        for key in _preset_attribute_keys(mapping):
            device.attributes.setdefault(key, None)
        entry = _DeviceEntry(appliance_code, info, device, mapping, home_name)
        entry.room_name = str(info.get("room") or home_name or _HOME_NAME)
        return entry

    def _load_mapping(self, device: MiedaDevice, info: dict) -> dict:
        """按设备类型导入映射表并做 subtype/sn8 选择与云统计合并。"""
        device_type = device.device_type
        try:
            module = importlib.import_module(
                f"{__package__}.vendor.device_mapping.T0x{device_type:02X}")
        except ImportError:
            logger.info("美的: 无 T0x%02X 映射表，按通用设备处理",
                        device_type)
            return {}
        device_mappings = getattr(module, "DEVICE_MAPPING", {}) or {}
        mapping = select_mapping(
            device_mappings, device_type,
            sn8=str(info.get("sn8") or ""),
            subtype=device.subtype, category=device.category)
        try:
            mapping = merge_cloud_stats_mapping(mapping, device_type) or mapping
        except Exception as exc:
            logger.debug("美的: 云统计映射合并跳过 (%s)", exc)
        return mapping

    def _refresh_devices(self) -> None:
        cloud = self._require_cloud()
        homes = self._bridge.run(cloud.list_home, timeout=30)
        if not homes:
            self._devices.clear()
            return
        self._homes = {int(k): str(v) for k, v in homes.items()}
        devices: dict[str, _DeviceEntry] = {}
        for home_id, home_name in self._homes.items():
            appliances = self._bridge.run(
                lambda hid=home_id: cloud.list_appliances(hid), timeout=40)
            if not appliances:
                continue
            for code, info in appliances.items():
                entry = self._build_device(int(code), info, home_name)
                devices[self._uid(int(code))] = entry
        self._devices = devices

    def list_devices(self) -> list[DeviceInfo]:
        self._refresh_devices()
        result: list[DeviceInfo] = []
        for uid, entry in self._devices.items():
            info = entry.info
            result.append(DeviceInfo(
                did=uid,
                name=str(info.get("name") or f"美的设备 {entry.appliance_code}"),
                model=str(info.get("model") or entry.device_type),
                home_name=entry.home_name or _HOME_NAME,
                room_name=entry.room_name or _HOME_NAME,
                online=bool(info.get("online")),
                platform=self.platform_id,
                native_id=str(entry.appliance_code),
            ))
        return sorted(result, key=lambda d: (not d.online, d.name))

    def _entry(self, did: str) -> _DeviceEntry:
        entry = self._devices.get(did)
        if entry is None:
            self._refresh_devices()
            entry = self._devices.get(did)
        if entry is None:
            raise PlatformError(f"设备 {did} 不在美的账号下或已删除")
        return entry

    def _refresh_device(self, entry: _DeviceEntry) -> dict:
        """带 TTL 的设备状态刷新；返回 attributes 深拷贝。"""
        import copy

        cached = self._state_cache.get(self._uid(entry.appliance_code))
        now = time.monotonic()
        if cached and now - cached[0] < _STATE_TTL:
            return cached[1]
        self._bridge.run(entry.device.refresh_status, timeout=45)
        snapshot = copy.deepcopy(entry.device.attributes)
        self._state_cache[self._uid(entry.appliance_code)] = (now, snapshot)
        return snapshot

    def _attributes(self, did: str) -> tuple[_DeviceEntry, dict]:
        entry = self._entry(did)
        return entry, self._refresh_device(entry)

    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        result: dict[str, bool | None] = {did: None for did in dids}
        for did in dids:
            try:
                entry, attributes = self._attributes(did)
                if entry.power_attr is None:
                    continue
                result[did] = _to_bool(
                    attributes.get(entry.power_attr), entry.rationale)
            except Exception as exc:
                logger.info("美的: 读取 %s 电源状态失败: %s", did, exc)
        return result

    def power_state_known(self, did: str) -> bool:
        """None 的语义：映射表里有电源键 -> 读取失败（可重试）；
        没有电源键（纯传感器类设备）-> 确认无能力（不重试）。"""
        entry = self._devices.get(did)
        if entry is None:
            return False  # 设备还没加载出来，允许主界面下轮重试
        return entry.power_attr is None

    def set_power_state(self, did: str, state: bool) -> None:
        entry = self._entry(did)
        if entry.power_attr is None:
            raise PlatformError("该设备不支持电源控制")
        value = entry.rationale[1] if state else entry.rationale[0]
        ok = self._bridge.run(
            lambda: entry.device.set_attribute(entry.power_attr, value),
            timeout=30)
        self._state_cache.pop(did, None)
        if ok is False:
            raise PlatformError("美的云端未确认电源指令，请稍后重试")

    def toggle_power(self, did: str) -> bool:
        current = self.power_state(did)
        if current is None:
            raise PlatformError("设备不支持开关控制或已离线")
        self.set_power_state(did, not current)
        return not current

    # ---------- 结构化控制 ----------

    def device_controls(self, did: str) -> list[ControlSpec]:
        entry, attributes = self._attributes(did)
        return build_controls(entry.mapping, attributes,
                              online=bool(entry.info.get("online", True)))

    def set_control(self, did: str, key: str, value) -> None:
        entry = self._entry(did)
        mapping = entry.mapping
        entities = mapping.get("entities") or {}

        if key == "power":
            if entry.power_attr is None:
                raise PlatformError("该设备不支持电源控制")
            attr_value = (entry.rationale[1] if _truthy(value)
                          else entry.rationale[0])
            ok = self._bridge.run(
                lambda: entry.device.set_attribute(entry.power_attr, attr_value),
                timeout=30)
        elif key in ("mode", "fan_mode", "preset", "swing"):
            control = _lookup_option(entities, key, value)
            if control is None:
                raise PlatformError(f"不支持的{key}取值：{value}")
            ok = self._bridge.run(
                lambda: entry.device.set_attributes(control), timeout=30)
        elif key == "target_temp":
            temp_attr = _climate_attr(mapping, "target_temperature")
            if temp_attr is None:
                raise PlatformError("该设备不支持设定温度")
            ok = self._bridge.run(
                lambda: entry.device.set_attribute(temp_attr, value),
                timeout=30)
        elif key == "target_humidity":
            hum_attr = _climate_attr(mapping, "target_humidity")
            if hum_attr is None:
                raise PlatformError("该设备不支持设定湿度")
            ok = self._bridge.run(
                lambda: entry.device.set_attribute(hum_attr, value),
                timeout=30)
        elif key == "aux_heat":
            attr = _climate_attr(mapping, "aux_heat")
            if attr is None:
                raise PlatformError("该设备不支持辅助加热")
            rationale = _rationale_of({}, mapping)
            attr_value = rationale[1] if _truthy(value) else rationale[0]
            ok = self._bridge.run(
                lambda: entry.device.set_attribute(attr, attr_value),
                timeout=30)
        elif key == "oscillate":
            attr = _fan_attr(mapping, "oscillate")
            if attr is None:
                raise PlatformError("该设备不支持摆风")
            rationale = _rationale_of({}, mapping)
            attr_value = rationale[1] if _truthy(value) else rationale[0]
            ok = self._bridge.run(
                lambda: entry.device.set_attribute(attr, attr_value),
                timeout=30)
        else:
            # 其余键即实体属性名（开关/数值/选择/灯光等）
            ok = self._bridge.run(
                lambda: entry.device.set_attribute(key, value), timeout=30)
        self._state_cache.pop(did, None)
        if ok is False:
            raise PlatformError(f"美的云端未确认「{key}」指令，请稍后重试")

    # ---------- 指令与状态 ----------

    def capabilities(self, did: str) -> list[ActionInfo]:
        # 美的的控制全部走结构化控制项（device_controls），无额外指令；
        # 状态刷新用面板右上角的「刷新」按钮
        return []

    def execute_action(self, did: str, action: str) -> None:
        raise PlatformError(f"美的不支持指令 {action}")

    def device_status(self, did: str) -> dict[str, str]:
        entry, attributes = self._attributes(did)
        rows = build_status_rows(entry.mapping, attributes)
        # 当前温度（climate 的 current_temperature）补进行
        temp_attr = _climate_attr(entry.mapping, "current_temperature")
        if temp_attr and attributes.get(temp_attr) is not None:
            rows.setdefault("当前温度", f"{attributes[temp_attr]}°C")
        rows.setdefault("设备类型", entry.device_type)
        return rows

    def snapshot(self, did: str) -> tuple[bool | None, dict[str, str]]:
        entry, attributes = self._attributes(did)
        state = None
        if entry.power_attr is not None:
            state = _to_bool(attributes.get(entry.power_attr), entry.rationale)
        return state, self.device_status(did)


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "on", "true", "yes")
    return bool(value)


def _climate_attr(mapping: dict, key: str) -> str | None:
    """从 climate 实体配置里取属性键（target_temperature 可能是列表）。"""
    for platform_cfg in (mapping.get("entities") or {}).values():
        if not isinstance(platform_cfg, dict):
            continue
        for cfg in platform_cfg.values():
            if not isinstance(cfg, dict) or key not in cfg:
                continue
            value = cfg[key]
            if isinstance(value, list) and value:
                value = value[0]
            if isinstance(value, str):
                return value
    return None


def _fan_attr(mapping: dict, key: str) -> str | None:
    for platform_cfg in (mapping.get("entities") or {}).values():
        if not isinstance(platform_cfg, dict):
            continue
        for cfg in platform_cfg.values():
            if isinstance(cfg, dict) and isinstance(cfg.get(key), str):
                return cfg[key]
    return None


def _lookup_option(entities: dict, key: str, value) -> dict | None:
    """按语义键（mode/fan_mode/preset/swing）找映射表里的控制字典。"""
    field = {"mode": "hvac_modes", "fan_mode": "fan_modes",
             "preset": "preset_modes", "swing": "swing_modes"}[key]
    for platform_cfg in entities.values():
        if not isinstance(platform_cfg, dict):
            continue
        for cfg in platform_cfg.values():
            if not isinstance(cfg, dict):
                continue
            options = cfg.get(field)
            if isinstance(options, dict) and value in options:
                control = options[value]
                if isinstance(control, dict):
                    return control
    return None
