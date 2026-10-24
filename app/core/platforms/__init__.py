# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""多平台设备管理模块。

- ``base``     平台抽象基类与 PlatformError
- ``mijia``    米家平台（MijiaService 适配）
- ``rmtsw``    开机卡平台（逆向协议实现）
- ``registry`` 多平台注册表（界面层唯一入口）

新增平台：继承 ``PlatformBase`` 实现接口，并在 ``PlatformRegistry``
构造处注册，即可获得主界面、托盘、桌面挂件、定时任务等全部入口的
设备与控制能力。
"""

from app.core.platforms.base import PlatformBase, PlatformError
from app.core.platforms.registry import PlatformRegistry

__all__ = ["PlatformBase", "PlatformError", "PlatformRegistry"]
