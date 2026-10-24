# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""多平台账号与会话的本地持久化。

与 settings.json / tray.json 同目录（见 _json_store），两个文件：

- ``platforms.json``  各平台保存的登录凭据（勾选"记住账号密码"时写入）。
  明文存储与托盘配置同级：本机桌面应用的本地数据目录
  （%LOCALAPPDATA%\\MiHome-Windows\\），不随账号同步、不上传。
- ``platform_sessions.json``  各平台的会话票据（如开机卡的 PHPSESSID），
  用于重启后免重复登录；失效时平台自行重新登录。

任何读写异常都按"无数据"处理：持久化只是便利性，不应阻塞主流程。
"""

from app.core import _json_store

_ACCOUNTS_FILE = "platforms.json"
_SESSIONS_FILE = "platform_sessions.json"


def _read(path_name: str) -> dict:
    raw = _json_store.read_json(_json_store.data_file(path_name), {})
    return raw if isinstance(raw, dict) else {}


def _write(path_name: str, data: dict) -> None:
    _json_store.write_json(_json_store.data_file(path_name), data)


# ---------- 登录凭据 ----------

def load_account(platform_id: str) -> dict:
    """读取某平台保存的账号密码；未保存返回空 dict。"""
    entry = _read(_ACCOUNTS_FILE).get(platform_id)
    return dict(entry) if isinstance(entry, dict) else {}


def save_account(platform_id: str, username: str, password: str) -> None:
    """保存账号密码（用户勾选"记住账号密码"时调用）。"""
    data = _read(_ACCOUNTS_FILE)
    data[platform_id] = {"username": username, "password": password}
    _write(_ACCOUNTS_FILE, data)


def clear_account(platform_id: str) -> None:
    """清除某平台保存的账号密码（取消勾选或退出登录时调用）。"""
    data = _read(_ACCOUNTS_FILE)
    if platform_id in data:
        del data[platform_id]
        _write(_ACCOUNTS_FILE, data)


# ---------- 会话票据 ----------

def load_session(platform_id: str) -> dict:
    """读取某平台的会话票据；无则返回空 dict。"""
    entry = _read(_SESSIONS_FILE).get(platform_id)
    return dict(entry) if isinstance(entry, dict) else {}


def save_session(platform_id: str, session: dict) -> None:
    data = _read(_SESSIONS_FILE)
    data[platform_id] = session
    _write(_SESSIONS_FILE, data)


def clear_session(platform_id: str) -> None:
    data = _read(_SESSIONS_FILE)
    if platform_id in data:
        del data[platform_id]
        _write(_SESSIONS_FILE, data)
