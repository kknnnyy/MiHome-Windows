# SPDX-License-Identifier: GPL-3.0-or-later
"""Home Assistant 常量替身。

上游 device_mapping / const 的数据表只从 homeassistant.* import **枚举
常量**（Platform.CLIMATE、UnitOfTemperature.CELSIUS……），本身不调用 HA
任何功能。本模块在 sys.modules 里放置"取什么属性都返回占位对象"的假
模块，让这些数据表无需 Home Assistant 即可导入。

设计取舍（对应插件动态更新目标）：占位对象对任意属性都返回新的占位
对象，上游新增常量/新增组件模块不会让导入失败——比逐个枚举真实常量
更耐更新。代价是这些常量没有真实语义，适配层（adapter.py）按占位
对象的名字末段（如 "CLIMATE"/"CELSIUS"）做识别。

仅在真实 homeassistant 未安装时生效；装了 HA 的环境尊重真实模块。
"""

import sys
import types


class _Placeholder:
    """任意属性都返回新占位对象；可按名字相等/哈希（可作 dict 键）。"""

    __slots__ = ("_name",)

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attr: str) -> "_Placeholder":
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _Placeholder(f"{self._name}.{attr}")

    def __repr__(self) -> str:
        return self._name

    def __str__(self) -> str:
        return self._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __eq__(self, other) -> bool:
        return isinstance(other, _Placeholder) and other._name == self._name

    def __call__(self, *args, **kwargs) -> "_Placeholder":
        return self


def _make_module(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module

    def _getattr(attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _Placeholder(f"{name}.{attr}")

    module.__dict__["__getattr__"] = _getattr
    # 让 from X import * 也能拿到占位（数据表不会这么用，保险起见）
    module.__dict__["__all__"] = []
    return module


# 上游用到的 homeassistant 模块路径；多放几个常见路径，
# 上游将来新增 import 时无需改这里
_HA_MODULES = (
    "homeassistant",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.exceptions",
    "homeassistant.util",
    "homeassistant.util.json",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.event",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.switch",
    "homeassistant.components.binary_sensor",
    "homeassistant.components.fan",
    "homeassistant.components.humidifier",
    "homeassistant.components.climate",
    "homeassistant.components.light",
    "homeassistant.components.number",
    "homeassistant.components.select",
    "homeassistant.components.lock",
    "homeassistant.components.cover",
    "homeassistant.components.vacuum",
    "homeassistant.components.water_heater",
    "homeassistant.components.button",
    "homeassistant.components.text",
)

_installed = False


def install() -> None:
    """安装替身（幂等）。真实 HA 存在时整体跳过。"""
    global _installed
    if _installed:
        return
    try:
        import importlib.util

        if importlib.util.find_spec("homeassistant") is not None:
            return  # 真实 HA 在场，尊重它
    except (ImportError, ValueError):
        pass
    for name in _HA_MODULES:
        _make_module(name)
    _installed = True
