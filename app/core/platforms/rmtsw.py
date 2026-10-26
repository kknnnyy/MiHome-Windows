# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""梦叶 / rmtsw / siwiot 远程开机卡平台（协议逆向成果的集成）。

云端接口为明文 JSON、无签名：请求体 ``{"CMD":<int>,"Data":{...}}``，
鉴权靠服务端下发的 PHPSESSID Cookie。登录前必须先 clientInit，其
Data 里的 SoftPass 是 App 硬编码密钥（缺失会一直返回 -1001）。
设备电源状态（pwr）随设备列表被动上报，控制走 sendTask。

协议细节（命令码、错误码、字段含义）见逆向文档
D:\\codes\\pycharm\\jsmyapi\\PROTOCOL.md，本文件与其保持同步。
"""

import json
import logging

import requests

from app.core import platform_store
from app.core.models import ActionInfo, DeviceInfo
from app.core.platforms.base import (
    CaptchaRequired,
    PlatformBase,
    PlatformError,
)

logger = logging.getLogger(__name__)

# 云端端点：?t= 是 base64 的 "test-app-intf-type"（与逆向 demo 逐字符
# 一致，曾因少一个 p 写成 test-apa-intf-type，服务端返回 HTTP 200 的
# HTML 错误页导致 JSON 解析失败——测试里已加断言锁死该值）
API_URL = ("https://api.rmtsw.siwiot.com/client_api/intf.php"
           "?t=dGVzdC1hcHAtaW50Zi10eXBl")
SOFT_TYPE = 7
SOFT_VER = "1.4.0.513"
# App 内硬编码的客户端密钥（RmtswApi.clientInit 字面量）
SOFT_PASS = "503a81b6222702f09e51bf7ab966b926"
_COOKIE_NAME = "PHPSESSID"
_COOKIE_DOMAIN = "api.rmtsw.siwiot.com"

# 命令码（RmtswApiDef）
_CMD_CLIENT_INIT = 1
_CMD_LOGIN = 4
_CMD_LOGOUT = 5
_CMD_GET_USER_INFO = 6
_CMD_GET_DEVICELIST = 9
_CMD_SEND_TASK = 51
_CMD_GET_VCODE = 101           # 取图形验证码（GET_VCODE_IMG）
_GET_VCODE_TYPE = 2            # GET_VCODE_IMG 的验证码类型
_ERR_NEED_VCODE = -2109        # 登录返回此码即需图形验证码

# 任务指令（Task 类）；TaskType 固定 1（控制）
_TASK_TYPE_CONTROL = 1
_TASK_CMD = {
    "poweron": 1,     # 开机
    "poweroff": 2,    # 硬件强关（切断 ATX 电源）
    "softpwroff": 3,  # 软关机（通知系统正常关机后再断电）
    "reboot": 4,      # 重启
    "lock": 5,        # 锁定
    "unlock": 6,      # 解锁
    "apo_ena": 7,     # 来电自启 开
    "apo_dis": 8,     # 来电自启 关
}
_ACTION_LABELS = {
    "poweron": "开机",
    "poweroff": "关机",
    "softpwroff": "软关机",
    "reboot": "重启",
    "lock": "锁定",
    "unlock": "解锁",
    "apo_ena": "来电自启 开",
    "apo_dis": "来电自启 关",
}

# 设备在 DeviceInfo 里的固定归属：开机卡没有家庭/房间概念
_HOME_NAME = "开机卡"
_MODEL = "rmtsw.powercard"
# 跨平台 did 前缀（与 DeviceInfo.platform 对应）
_PREFIX = "rmtsw:"

# 错误码 -> 中文含义（实测/常量表；未列出的按服务端 ErrMsg 展示）
_ERROR_HINTS = {
    -1001: "客户端版本无效",
    -1011: "客户端未初始化",
    -1014: "用户未登录",
    -2109: "登录需要图形验证码",
    -2102: "验证码错误",
    -4003: "任务不存在",
    -5102: "设备离线",
}


class RmtswClient:
    """开机卡云端 HTTP 客户端（明文 JSON + PHPSESSID 会话）。"""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "okhttp/3.12.0",
        })
        self._initialized = False

    # ---------- 底层请求 ----------

    def _request_raw(self, cmd: int, data: dict) -> dict:
        """发送一次命令并返回完整 payload（含 ErrCode/ErrMsg/Data）。

        不抛异常、不解析错误码，交由调用方按业务处理：login 需区分
        -2109 触发验证码，get_captcha 需取 Data.VCodeImg。
        """
        body = json.dumps({"CMD": cmd, "Data": data}, ensure_ascii=False)
        try:
            resp = self._session.post(API_URL, data=body.encode("utf-8"),
                                      timeout=25)
        except requests.RequestException as exc:
            raise PlatformError(f"开机卡云端连接失败：{exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            raise PlatformError(
                f"开机卡云端返回了无法解析的内容（HTTP {resp.status_code}）")
        if not isinstance(payload, dict):
            raise PlatformError("开机卡云端返回格式异常")
        return payload

    def _request(self, cmd: int, data: dict) -> dict:
        """发送一次命令并返回 Data；错误（ErrCode!=0）抛 PlatformError。"""
        payload = self._request_raw(cmd, data)
        code = payload.get("ErrCode")
        if code:
            hint = _ERROR_HINTS.get(code)
            message = payload.get("ErrMsg") or hint or "未知错误"
            if hint and hint not in message:
                message = f"{message}（{hint}）"
            raise PlatformError(f"开机卡云端错误（错误码 {code}）：{message}")
        data_field = payload.get("Data")
        return data_field if isinstance(data_field, dict) else {}

    def client_init(self) -> None:
        """客户端初始化：下发会话 Cookie，必须先于其他命令调用。"""
        self._request(_CMD_CLIENT_INIT, {
            "SoftwareType": SOFT_TYPE,
            "SoftwareVer": SOFT_VER,
            "SoftPass": SOFT_PASS,
        })
        self._initialized = True

    def ensure_initialized(self) -> None:
        if not self._initialized:
            self.client_init()

    # ---------- 会话持久化 ----------

    def cookie_value(self) -> str:
        value = self._session.cookies.get(_COOKIE_NAME)
        return value or ""

    def restore_cookie(self, value: str) -> None:
        if value:
            self._session.cookies.set(
                _COOKIE_NAME, value, domain=_COOKIE_DOMAIN, path="/")

    # ---------- 业务命令 ----------

    def login(self, username: str, password: str,
              vcode_id: str = "", vcode: str = "") -> dict:
        """账号密码登录，成功返回用户信息（UserId/UserName/…）。

        vcode_id/vcode 为图形验证码（多次失败触发 -2109 后由 get_captcha
        取得）。登录返回 -2109 时抛 CaptchaRequired，界面层取图后重试。
        """
        self.client_init()
        payload = self._request_raw(_CMD_LOGIN, {
            "UserName": username,
            "UserPass": password,
            "VCodeId": vcode_id,
            "VCode": vcode,
        })
        code = payload.get("ErrCode")
        if code == _ERR_NEED_VCODE:
            raise CaptchaRequired("登录需要图形验证码，请填写图中字符")
        if code:
            hint = _ERROR_HINTS.get(code)
            message = payload.get("ErrMsg") or hint or "未知错误"
            if hint and hint not in message:
                message = f"{message}（{hint}）"
            raise PlatformError(f"开机卡云端错误（错误码 {code}）：{message}")
        data_field = payload.get("Data")
        return data_field if isinstance(data_field, dict) else {}

    def get_captcha(self) -> dict:
        """取登录图形验证码，返回 {vcode_id, vcode_img(data URI)}。

        vcode_img 已拼好 ``data:image/jpeg;base64,`` 前缀，可直接喂给 Qt
        图片控件（base64 解码后）或 ``<img>``。

        注意：该接口服务端即便成功也常返回非 0 的 ErrCode，因此只要
        ``Data.VCodeImg`` 存在即视为成功（与 jsmyapi 的宽松判定一致）。
        另外取图发生在登录失败之后，client_init 已执行过一次，这里再
        调用即便返回非 0 也忽略（会话已由首次 init 建立，无需重连）。
        """
        try:
            self.client_init()
        except PlatformError:
            pass
        payload = self._request_raw(_CMD_GET_VCODE,
                                    {"VCodeType": _GET_VCODE_TYPE})
        data = payload.get("Data")
        if not isinstance(data, dict) or not data.get("VCodeImg"):
            code = payload.get("ErrCode")
            hint = _ERROR_HINTS.get(code)
            message = payload.get("ErrMsg") or hint or "服务端未返回验证码图片"
            if hint and hint not in message:
                message = f"{message}（{hint}）"
            raise PlatformError(f"获取验证码失败：{message}")
        vcode_id = data.get("VCodeId") or ""
        img = data.get("VCodeImg") or ""
        return {"vcode_id": vcode_id,
                "vcode_img": f"data:image/jpeg;base64,{img}"}

    def get_user_info(self) -> dict:
        self.ensure_initialized()
        return self._request(_CMD_GET_USER_INFO, {})

    def logout(self) -> None:
        try:
            self.ensure_initialized()
            self._request(_CMD_LOGOUT, {})
        except PlatformError:
            pass  # 退出登录失败不影响本地清理
        finally:
            self._session.cookies.clear()

    def get_device_list(self) -> list[dict]:
        self.ensure_initialized()
        data = self._request(_CMD_GET_DEVICELIST, {"WithGroup": True})
        items = data.get("DeviceList")
        if not isinstance(items, list):
            # 响应结构历史上稳定；异常时把所有列表字段都找一遍兜底
            for value in data.values():
                if isinstance(value, list):
                    items = value
                    break
        return [item for item in (items or []) if isinstance(item, dict)]

    def send_task(self, device_id: int, action: str,
                  timeout: int = 15) -> dict:
        """下发一条控制指令；Code==0 即受理成功，否则抛 PlatformError。"""
        cmd = _TASK_CMD.get(action)
        if cmd is None:
            raise PlatformError(f"开机卡不支持指令 {action}")
        self.ensure_initialized()
        data = self._request(_CMD_SEND_TASK, {"TaskList": [{
            "DeviceId": device_id,
            "TaskType": _TASK_TYPE_CONTROL,
            "TaskCmd": cmd,
            "TaskData": "",
            "TaskExecTimeout": timeout,
        }]})
        results = data.get("TaskList") if isinstance(data, dict) else None
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict) and first.get("Code") not in (0, None):
                raise PlatformError(
                    f"指令下发失败（{first.get('Desc') or '未知错误'}，"
                    f"错误码 {first.get('Code')}）")
        return data


class RmtswPlatform(PlatformBase):
    """开机卡平台：账号密码登录，设备电源状态随列表被动上报。"""

    platform_id = "rmtsw"
    display_name = "开机卡"
    icon_name = "mdi.power"
    description = ("梦叶 / rmtsw / siwiot 远程开机卡：账号密码登录，"
                   "远程控制电脑开关机、重启与锁定")
    supports_password_login = True

    def __init__(self):
        self._client = RmtswClient()
        # 跨平台 did -> (原生 DeviceId, 云端原始设备 dict)
        self._devices: dict[str, tuple[int, dict]] = {}
        self._user_name: str | None = None

    # ---------- 登录态 ----------

    def _apply_session(self, session: dict) -> bool:
        """把持久化的会话装回客户端；无效返回 False。"""
        cookie = session.get("phpsessid")
        if not cookie:
            return False
        self._client.restore_cookie(str(cookie))
        try:
            info = self._client.get_user_info()
        except PlatformError:
            return False
        if not info.get("UserId"):
            return False
        self._user_name = str(info.get("UserName") or "")
        return True

    def auto_login(self) -> bool:
        """启动静默登录：先试本地会话，再试保存的凭据。"""
        if self._apply_session(platform_store.load_session(self.platform_id)):
            logger.info("开机卡: 已从本地会话恢复登录（%s）", self._user_name)
            return True
        account = platform_store.load_account(self.platform_id)
        if account.get("username") and account.get("password"):
            try:
                self.login(account["username"], account["password"])
                logger.info("开机卡: 已用保存的账号自动登录")
                return True
            except PlatformError as exc:
                logger.info("开机卡: 自动登录失败（%s），等待手动登录", exc)
        return False

    def is_logged_in(self) -> bool:
        if not self._user_name:
            if not self._apply_session(platform_store.load_session(
                    self.platform_id)):
                return False
        try:
            info = self._client.get_user_info()
        except PlatformError:
            return False
        return bool(info.get("UserId"))

    def session_active(self) -> bool:
        # 开机卡登录态在会话内自持：合并设备列表时不重复打联网校验
        return self._user_name is not None

    def login(self, username: str, password: str,
              remember: bool = False, vcode_id: str = "",
              vcode: str = "") -> None:
        username = (username or "").strip()
        if not username or not password:
            raise PlatformError("请输入开机卡账号和密码")
        info = self._client.login(username, password,
                                   vcode_id=vcode_id, vcode=vcode)
        if not info.get("UserId"):
            raise PlatformError("登录失败：云端未返回用户信息")
        self._user_name = str(info.get("UserName") or username)
        self._persist_session(info)
        if remember:
            platform_store.save_account(self.platform_id, username, password)
        else:
            platform_store.clear_account(self.platform_id)

    def logout(self) -> None:
        self._client.logout()
        self._user_name = None
        self._devices.clear()
        platform_store.clear_session(self.platform_id)

    def status_text(self) -> str:
        if self._user_name:
            return f"已登录：{self._user_name}"
        return "未登录"

    def _persist_session(self, info: dict) -> None:
        platform_store.save_session(self.platform_id, {
            "phpsessid": self._client.cookie_value(),
            "user_id": info.get("UserId"),
            "user_name": self._user_name,
        })

    # ---------- 设备 ----------

    @staticmethod
    def _uid(device_id: int) -> str:
        return f"{_PREFIX}{device_id}"

    @staticmethod
    def _native_id(uid: str) -> int:
        try:
            return int(str(uid).split(":", 1)[-1])
        except (TypeError, ValueError):
            raise PlatformError(f"无法解析开机卡设备 ID：{uid}")

    @staticmethod
    def _runtime_info(raw: dict) -> dict:
        """DeviceRuntimeInfo 是字符串里再套一层 JSON，需二次解析。"""
        text = raw.get("DeviceRuntimeInfo")
        if not text:
            return {}
        try:
            info = json.loads(text)
        except (TypeError, ValueError):
            return {}
        return info if isinstance(info, dict) else {}

    def _refresh_devices(self) -> None:
        """拉取设备列表并重建 did -> (原生 ID, 原始 dict) 索引。"""
        if not self._user_name and not self.is_logged_in():
            raise PlatformError("开机卡未登录，请先在「多平台管理」中登录")
        devices: dict[str, tuple[int, dict]] = {}
        for raw in self._client.get_device_list():
            try:
                device_id = int(raw["DeviceId"])
            except (KeyError, TypeError, ValueError):
                continue
            devices[self._uid(device_id)] = (device_id, raw)
        self._devices = devices

    def list_devices(self) -> list[DeviceInfo]:
        self._refresh_devices()
        result: list[DeviceInfo] = []
        for uid, (device_id, raw) in self._devices.items():
            result.append(DeviceInfo(
                did=uid,
                name=str(raw.get("DeviceName") or f"开机卡 {device_id}"),
                model=_MODEL,
                home_name=_HOME_NAME,
                room_name=str(raw.get("GroupName") or _HOME_NAME),
                online=bool(raw.get("IsDeviceOnline")),
                platform=self.platform_id,
                native_id=str(device_id),
            ))
        return sorted(result, key=lambda d: (not d.online, d.name))

    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        """电源状态随设备列表被动上报：一次列表请求换全部设备状态。"""
        if not dids:
            return {}
        self._refresh_devices()
        result: dict[str, bool | None] = {}
        for uid in dids:
            entry = self._devices.get(uid)
            if entry is None:
                result[uid] = None
                continue
            runtime = self._runtime_info(entry[1])
            pwr = runtime.get("pwr")
            result[uid] = bool(pwr) if pwr in (0, 1, True, False) else None
        return result

    def set_power_state(self, did: str, state: bool) -> None:
        device_id = self._native_id(did)
        self._client.send_task(device_id, "poweron" if state else "poweroff")

    def snapshot(self, did: str) -> tuple[bool | None, dict[str, str]]:
        """一次列表刷新同时拿到电源状态与状态行（省掉一次请求）。"""
        self._refresh_devices()
        entry = self._devices.get(did)
        if entry is None:
            return None, {}
        runtime = self._runtime_info(entry[1])
        pwr = runtime.get("pwr")
        state = bool(pwr) if pwr in (0, 1, True, False) else None
        return state, self.device_status(did)

    # ---------- 指令面板 ----------

    def capabilities(self, did: str) -> list[ActionInfo]:
        return [ActionInfo(name=action, desc=label)
                for action, label in _ACTION_LABELS.items()]

    def execute_action(self, did: str, action: str) -> None:
        device_id = self._native_id(did)
        self._client.send_task(device_id, action)

    def device_status(self, did: str) -> dict[str, str]:
        """详情面板状态行：目标电脑电源、开机卡状态、信号与开关。"""
        entry = self._devices.get(did)
        if entry is None:
            self._refresh_devices()
            entry = self._devices.get(did)
        if entry is None:
            return {}
        _device_id, raw = entry
        runtime = self._runtime_info(raw)
        online = bool(raw.get("IsDeviceOnline"))
        pwr = runtime.get("pwr")
        rows = {
            "目标电脑电源": (
                "开启" if pwr else "关闭") if pwr in (0, 1, True, False) else "未知",
            "开机卡状态": "在线" if online else "离线",
        }
        if "rssi" in runtime:
            rows["WiFi 信号"] = f"{runtime['rssi']} dBm"
        if "lock" in runtime:
            rows["锁定"] = "已锁定" if runtime["lock"] else "未锁定"
        if "apo" in runtime:
            rows["来电自启"] = "开启" if runtime["apo"] else "关闭"
        if raw.get("DeviceFirmVer"):
            rows["固件版本"] = str(raw["DeviceFirmVer"])
        return rows