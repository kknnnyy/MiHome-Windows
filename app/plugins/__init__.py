# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""多平台插件系统。

每个设备平台是一个插件目录（``plugin.json`` 清单 + 适配器 + 可选的
vendored 协议层），由 loader 发现并注册进 PlatformRegistry：

- 内置插件随程序分发（``app/plugins/<id>/``）
- 更新后的副本落在用户数据目录（``%LOCALAPPDATA%\\MiHome-Windows\\
  plugins/<id>/``），覆盖内置，支持 GitHub 一键更新

新增平台 = 新建一个插件目录，无需改动主程序。
"""

from app.plugins.loader import (
    PluginInfo,
    apply_update,
    check_update,
    discover,
    load_platforms,
)

__all__ = [
    "PluginInfo",
    "apply_update",
    "check_update",
    "discover",
    "load_platforms",
]
