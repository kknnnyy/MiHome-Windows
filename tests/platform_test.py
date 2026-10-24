# SPDX-License-Identifier: GPL-3.0-or-later
"""多平台管理与开机卡平台回归测试（离屏、无网络）。

覆盖：
1. DeviceInfo 多平台字段与 native() 语义；
2. 开机卡协议客户端：clientInit 必带 SoftPass、错误码中文化、
   sendTask 指令码映射、DeviceRuntimeInfo 二次解析；
3. 开机卡平台：did 前缀唯一性、电源状态被动上报、登录/会话持久化；
4. PlatformRegistry：按 did 前缀路由、多平台设备合并、可扩展注册。

用法: python -m tests.platform_test
"""

import base64
import json

from tests._isolate import isolate

isolate()

import requests

from app.core import platform_store
from app.core.models import ActionInfo, DeviceInfo
from app.core.platforms import PlatformRegistry
from app.core.platforms.base import PlatformBase, PlatformError
from app.core.platforms import rmtsw as rmtsw_mod
from app.core.platforms.rmtsw import RmtswClient, RmtswPlatform


# ---------- 假的开机卡云端：拦截 requests.Session.post ----------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeCloud:
    """按 CMD 应答的假云端；记录全部请求体供断言。"""

    def __init__(self):
        self.calls: list[tuple[int, dict]] = []
        self.user = {"UserId": 41012, "UserName": "kknnnyy"}
        self.devices = [
            {"DeviceId": 139713, "DeviceName": "客厅电脑", "IsDeviceOnline": True,
             "DeviceFirmVer": "1.2.3",
             "DeviceRuntimeInfo": json.dumps(
                 {"rssi": -55, "pm": 1, "fsm": 3, "pwr": 1, "lock": 0, "apo": 1})},
            {"DeviceId": 64052, "DeviceName": "机房电脑", "IsDeviceOnline": False,
             "DeviceRuntimeInfo": json.dumps({"rssi": -90, "pwr": 0})},
        ]
        self.fail_login = False

    def post(self, session, url, data=None, **kwargs):
        # URL 必须与逆向 demo 逐字符一致：?t= 少一个字符（曾把
        # test-app-intf-type 误写为 test-apa-intf-type）服务端就返回
        # HTTP 200 的 HTML 错误页，线上表现为「无法解析的内容」
        assert url == rmtsw_mod.API_URL, f"URL 与 demo 不一致: {url}"
        t_param = url.split("?t=", 1)[1]
        assert base64.b64decode(t_param).decode() == "test-app-intf-type", (
            f"t 参数解码错误: {base64.b64decode(t_param).decode()}")
        body = json.loads(data.decode("utf-8"))
        assert set(body) == {"CMD", "Data"}, f"请求体字段不符: {body}"
        cmd = body["CMD"]
        self.calls.append((cmd, body["Data"]))
        # clientInit 时下发 PHPSESSID，模拟真实服务端行为
        if cmd == 1:
            session.cookies.set("PHPSESSID", "fake-session-id",
                                domain="api.rmtsw.siwiot.com", path="/")
        if cmd == 4 and self.fail_login:
            return _FakeResponse({"CMD": 1000000001, "ErrCode": -2102,
                                  "ErrMsg": "用户未注册"})
        if cmd == 51:
            return _FakeResponse({"CMD": 1000000001, "Data": {"TaskList": [
                {"DeviceId": "139713", "TaskId": 27609804, "Code": 0, "Desc": ""}]}})
        if cmd == 9:
            return _FakeResponse({"CMD": 1000000001,
                                  "Data": {"DeviceList": self.devices}})
        return _FakeResponse({"CMD": 1000000001, "Data": self.user})


def _patch_cloud(monkeypatch_holder: list, cloud: FakeCloud) -> None:
    """把假云端装到 requests.Session.post 上，返回还原函数。"""
    original = requests.Session.post
    requests.Session.post = lambda self, url, data=None, **kw: cloud.post(
        self, url, data=data, **kw)
    monkeypatch_holder.append(original)


def _unpatch(monkeypatch_holder: list) -> None:
    if monkeypatch_holder:
        requests.Session.post = monkeypatch_holder.pop()


# ---------- 1. 数据模型 ----------

def test_device_info_fields() -> None:
    mijia = DeviceInfo(did="123", name="灯", model="yeelink.light",
                       home_name="家", room_name="客厅", online=True)
    assert mijia.platform == "mijia" and mijia.native_id == ""
    assert mijia.native() == "123"

    card = DeviceInfo(did="rmtsw:139713", name="客厅电脑", model="rmtsw.powercard",
                      home_name="开机卡", room_name="开机卡", online=True,
                      platform="rmtsw", native_id="139713")
    assert card.native() == "139713"
    print("1. DeviceInfo 多平台字段 OK")


# ---------- 2. 协议客户端 ----------

def test_client_protocol() -> None:
    holder: list = []
    cloud = FakeCloud()
    _patch_cloud(holder, cloud)
    try:
        client = RmtswClient()
        # 未初始化直接调业务命令会先自动 clientInit（带 SoftPass）
        info = client.login("13800138000", "pw")
        assert info["UserId"] == 41012, info
        init_call = cloud.calls[0]
        assert init_call[0] == 1, "第一个命令必须是 clientInit"
        assert init_call[1]["SoftPass"] == rmtsw_mod.SOFT_PASS
        assert init_call[1]["SoftwareType"] == 7
        login_call = cloud.calls[1]
        assert login_call[0] == 4
        assert login_call[1] == {"UserName": "13800138000", "UserPass": "pw",
                                 "VCodeId": "", "VCode": ""}
        assert client.cookie_value() == "fake-session-id"

        # 指令码映射：全部 8 个动作都必须带对 TaskCmd
        for action, expect in (("poweron", 1), ("poweroff", 2),
                               ("softpwroff", 3), ("reboot", 4), ("lock", 5),
                               ("unlock", 6), ("apo_ena", 7), ("apo_dis", 8)):
            client.send_task(139713, action)
            cmd, data = cloud.calls[-1]
            assert cmd == 51
            task = data["TaskList"][0]
            assert task["TaskCmd"] == expect, (action, task)
            assert task["TaskType"] == 1 and task["DeviceId"] == 139713

        # 错误码中文化：-2102 不在提示表里，按服务端 ErrMsg 展示
        cloud.fail_login = True
        client2 = RmtswClient()
        try:
            client2.login("x", "y")
            raise AssertionError("登录失败应当抛 PlatformError")
        except PlatformError as exc:
            assert "用户未注册" in str(exc) and "-2102" in str(exc), exc
    finally:
        _unpatch(holder)
    print("2. 协议客户端（SoftPass/指令码/错误码） OK")


# ---------- 3. 开机卡平台 ----------

def _logged_in_platform(cloud: FakeCloud, holder: list) -> RmtswPlatform:
    platform = RmtswPlatform()
    platform.login("13800138000", "pw", remember=True)
    return platform


def test_platform_devices_and_power() -> None:
    holder: list = []
    cloud = FakeCloud()
    _patch_cloud(holder, cloud)
    try:
        platform = _logged_in_platform(cloud, holder)
        assert platform.status_text() == "已登录：kknnnyy"

        devices = platform.list_devices()
        assert [d.did for d in devices] == ["rmtsw:139713", "rmtsw:64052"]
        first = devices[0]
        assert first.native_id == "139713" and first.platform == "rmtsw"
        assert first.home_name == "开机卡" and first.room_name == "开机卡"
        assert first.online is True and devices[1].online is False

        # 电源状态：DeviceRuntimeInfo 内嵌 JSON 二次解析，pwr 为电源态
        states = platform.power_states(["rmtsw:139713", "rmtsw:64052",
                                        "rmtsw:不存在"])
        assert states == {"rmtsw:139713": True, "rmtsw:64052": False,
                          "rmtsw:不存在": None}

        # 面板快照：一次列表请求同时拿到状态与状态行
        state, rows = platform.snapshot("rmtsw:139713")
        assert state is True
        assert rows["目标电脑电源"] == "开启"
        assert rows["开机卡状态"] == "在线"
        assert rows["WiFi 信号"] == "-55 dBm"
        assert rows["来电自启"] == "开启"
        assert rows["固件版本"] == "1.2.3"

        # 控制指令
        before = len(cloud.calls)
        platform.set_power_state("rmtsw:139713", False)
        cmd, data = cloud.calls[-1]
        assert cmd == 51 and data["TaskList"][0]["TaskCmd"] == 2  # 硬件关机
        platform.execute_action("rmtsw:139713", "softpwroff")
        assert cloud.calls[-1][1]["TaskList"][0]["TaskCmd"] == 3
        assert len(cloud.calls) - before == 2

        caps = platform.capabilities("rmtsw:139713")
        assert [c.name for c in caps] == [
            "poweron", "poweroff", "softpwroff", "reboot",
            "lock", "unlock", "apo_ena", "apo_dis"]
        assert all(isinstance(c, ActionInfo) for c in caps)

        # 离线设备指令：服务端 -5102 应中文化
        cloud.devices[1]["IsDeviceOnline"] = True
        try:
            platform.set_power_state("rmtsw:64052", True)
        except PlatformError:
            pass  # 假云端不模拟 -5102，这里只验证不炸
    finally:
        _unpatch(holder)
    print("3. 开机卡平台（设备/电源状态/指令） OK")


def test_platform_login_persistence() -> None:
    holder: list = []
    cloud = FakeCloud()
    _patch_cloud(holder, cloud)
    try:
        platform = _logged_in_platform(cloud, holder)
        # 凭据与会话都应落盘（remember=True）
        account = platform_store.load_account("rmtsw")
        assert account.get("username") == "13800138000"
        session = platform_store.load_session("rmtsw")
        assert session.get("phpsessid") == "fake-session-id"

        # 新实例：本地会话直接恢复登录
        fresh = RmtswPlatform()
        assert fresh.session_active() is False  # 尚未恢复
        assert fresh.auto_login() is True
        assert fresh.status_text() == "已登录：kknnnyy"

        # 退出登录：清理内存与磁盘会话；勾选过"记住账号密码"的凭据保留
        # （与网页版一致：下次打开登录框自动回填，取消勾选才清除）
        platform.logout()
        assert platform.session_active() is False
        assert platform_store.load_session("rmtsw") == {}
        assert platform_store.load_account("rmtsw").get("username") == "13800138000"
        platform_store.clear_account("rmtsw")
        assert platform_store.load_account("rmtsw") == {}
    finally:
        _unpatch(holder)
    print("4. 开机卡登录/会话持久化 OK")


def test_platform_offline_error() -> None:
    """会话过期后列表接口报 -1014，应转换成中文 PlatformError。"""
    holder: list = []

    def _post(session, url, data=None, **kw):
        body = json.loads(data.decode("utf-8"))
        if body["CMD"] == 1:
            session.cookies.set("PHPSESSID", "fake-session-id",
                                domain="api.rmtsw.siwiot.com", path="/")
            return _FakeResponse({"CMD": 1000000001})
        return _FakeResponse({"CMD": 1000000000, "ErrCode": -1014,
                              "ErrMsg": "user not login"})

    requests.Session.post = lambda self, url, data=None, **kw: _post(
        self, url, data=data, **kw)
    holder.append(requests.Session.post)
    try:
        platform = RmtswPlatform()
        # 模拟"内存里还是登录态、服务端会话已过期"
        platform._user_name = "kknnnyy"
        try:
            platform.list_devices()
            raise AssertionError("会话过期应当抛 PlatformError")
        except PlatformError as exc:
            assert "用户未登录" in str(exc), exc
    finally:
        _unpatch(holder)
    print("5. 会话过期错误中文化 OK")


# ---------- 4. 注册表与可扩展性 ----------

class _StubPlatform(PlatformBase):
    """模拟第三方平台：验证不改注册表逻辑也能接入。"""

    platform_id = "stub"
    display_name = "测试平台"
    supports_props = False

    def __init__(self):
        self._devices = [
            DeviceInfo(did="stub:1", name="桩设备", model="stub.model",
                       home_name="测试平台", room_name="测试平台", online=True,
                       platform="stub", native_id="1")]

    def session_active(self) -> bool:
        return True

    def list_devices(self):
        return list(self._devices)

    def power_states(self, dids):
        return {did: True for did in dids}

    def set_power_state(self, did, state):
        self.last_set = (did, state)


def test_registry_routing_and_merge() -> None:
    holder: list = []
    cloud = FakeCloud()
    _patch_cloud(holder, cloud)
    try:
        # 不传 mijia_service：无 mijiaAPI 的环境也能验证路由与合并
        registry = PlatformRegistry()
        rmtsw_platform = _logged_in_platform(cloud, holder)
        stub = _StubPlatform()
        # 用登录态的开机卡实例替换注册表默认实例，再挂上桩平台
        registry._platforms = [rmtsw_platform, stub]
        registry._rmtsw = rmtsw_platform

        assert registry.platform_of("rmtsw:139713") is rmtsw_platform
        assert registry.platform_of("stub:1") is stub
        assert registry.platform_of("12345") is None  # 无米家平台时无前缀 did 无归属

        # 合并列表：开机卡 + 桩平台
        merged = registry.list_devices()
        dids = {d.did for d in merged}
        assert dids == {"rmtsw:139713", "rmtsw:64052", "stub:1"}

        # 电源状态按平台分组后合并
        states = registry.power_states(
            ["rmtsw:139713", "stub:1", "rmtsw:64052"])
        assert states == {"rmtsw:139713": True, "stub:1": True,
                          "rmtsw:64052": False}

        # 路由：开机卡 did 走开机卡协议
        registry.set_power_state("rmtsw:139713", False)
        assert cloud.calls[-1][1]["TaskList"][0]["TaskCmd"] == 2
        registry.set_power_state("stub:1", False)
        assert stub.last_set == ("stub:1", False)

        # 未知 did：明确报错而不是静默失败
        try:
            registry.set_power_state("rmtsw:不存在", True)
            raise AssertionError("未知 did 应当抛 PlatformError")
        except PlatformError:
            pass

        # 平台状态面板数据
        status = {row["platform_id"]: row for row in registry.platform_status()}
        assert status["rmtsw"]["logged_in"] is True
        assert status["rmtsw"]["device_count"] == 2
        assert status["stub"]["display_name"] == "测试平台"
    finally:
        _unpatch(holder)
    print("6. 注册表路由/合并/可扩展 OK")


test_device_info_fields()
test_client_protocol()
test_platform_devices_and_power()
test_platform_login_persistence()
test_platform_offline_error()
test_registry_routing_and_merge()

print("\nPLATFORM REGRESSION ALL PASS")
