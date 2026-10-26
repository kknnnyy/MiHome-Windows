# SPDX-License-Identifier: GPL-3.0-or-later
"""插件系统与美的平台回归测试（离屏、无网络）。

覆盖：
1. 插件发现/清单校验/用户目录覆盖；
2. HA 常量替身（任意属性可用、可作 dict 键）；
3. 美的映射表解释器：用**真实 T0xAC 数据表**断言能解释出
   电源/模式/风速/预设/摆风/温度/辅热与开关实体，状态行含室内温度；
4. 异步桥基本可用；
5. 美的平台通过插件机制注册进注册表；
6. 面板对话框的关闭竞态（开着面板时关掉，异步结果回来不得崩）；
7. 一键更新落到用户目录的协议层覆盖内置副本（且不可用时回退）。

用法: python -m tests.plugin_test
"""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from tests._isolate import isolate

isolate()

import importlib
import time

from app.core.models import ControlSpec, DeviceInfo
from app.core.platforms import PlatformRegistry
from app.core.platforms.base import PlatformBase
from app.plugins import loader
from app.plugins.midea import adapter as midea_adapter
from app.plugins.midea import ha_shim


# ---------- 1. HA 常量替身 ----------

def test_ha_shim() -> None:
    ha_shim.install()
    from homeassistant.const import Platform, UnitOfTemperature

    # 任意属性都可用，且同名相等、可作 dict 键
    assert Platform.CLIMATE == Platform.CLIMATE
    assert Platform.CLIMATE != Platform.FAN
    table = {Platform.CLIMATE: "climate", Platform.FAN: "fan"}
    assert table[Platform.CLIMATE] == "climate"
    assert "CLIMATE" in repr(Platform.CLIMATE)
    assert "CELSIUS" in repr(UnitOfTemperature.CELSIUS)
    # 幂等
    ha_shim.install()
    print("1. HA 常量替身 OK")


# ---------- 2. 插件发现与清单 ----------

def test_plugin_discovery() -> None:
    plugins = {info.id: info for info in loader.discover()}
    assert "midea" in plugins, f"未发现美的插件: {list(plugins)}"
    info = plugins["midea"]
    assert info.name == "美的"
    assert info.version == "1.0.0"
    assert info.entry == "adapter:MideaPlatform"
    assert info.upstream.get("repo") == "sususweet/midea-meiju-codec"
    assert info.upstream.get("version") == "v0.4.19"
    assert info.path.is_dir()
    # vendored 协议层在位
    assert (info.path / "vendor" / "core" / "cloud.py").is_file()
    assert (info.path / "vendor" / "device_mapping" / "T0xAC.py").is_file()
    assert (info.path / "vendor" / "NOTICE.txt").is_file()
    print("2. 插件发现/清单/vendored 协议层 OK")


def test_manifest_validation() -> None:
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 缺字段 -> None
        bad = root / "bad"
        bad.mkdir()
        (bad / "plugin.json").write_text(json.dumps({"id": "bad"}),
                                         encoding="utf-8")
        assert loader._read_manifest(bad) is None
        # entry 格式错 -> None
        bad2 = root / "bad2"
        bad2.mkdir()
        (bad2 / "plugin.json").write_text(json.dumps(
            {"id": "bad2", "name": "x", "version": "1", "entry": "no_colon"}),
            encoding="utf-8")
        assert loader._read_manifest(bad2) is None
        # 合法清单
        good = root / "good"
        good.mkdir()
        (good / "plugin.json").write_text(json.dumps(
            {"id": "good", "name": "好", "version": "1.0",
             "entry": "adapter:SomePlatform"}), encoding="utf-8")
        manifest = loader._read_manifest(good)
        assert manifest and manifest["id"] == "good"
        # 目录无清单 -> 扫描不到
        (root / "empty").mkdir()
        found = loader._scan(root, "bundled")
        assert set(found) == {"bad2", "good"} or set(found) == {"good"}
    print("3. 清单校验 OK")


def test_load_platforms_registers_midea() -> None:
    platforms = {p.platform_id: p for p in loader.load_platforms()}
    assert "midea" in platforms
    assert platforms["midea"].supports_password_login is True
    assert platforms["midea"].session_active() is False
    print("4. 插件平台导入 OK")


# ---------- 3. 美的映射表解释器（真实数据表） ----------

def _t0xac_mapping():
    module = importlib.import_module(
        "app.plugins.midea.vendor.device_mapping.T0xAC")
    return midea_adapter.select_mapping(
        module.DEVICE_MAPPING, 0xAC, sn8="00000Q1F")


def test_build_controls_from_real_table() -> None:
    mapping = _t0xac_mapping()
    assert mapping, "T0xAC 默认映射未选中"
    attributes = {
        "power": "on", "mode": "cool", "wind_speed": 60,
        "temperature": 26, "indoor_temperature": 29.5,
        "wind_swing_lr": "off", "wind_swing_ud": "off",
        "eco": "off", "comfort_power_save": "off", "cool_power_saving": 0,
        "strong_wind": "off", "ptc": "off",
    }
    controls = {c.key: c for c in
                midea_adapter.build_controls(mapping, attributes, True)}
    # 电源
    assert controls["power"].kind == "switch"
    assert controls["power"].value is True
    # 模式：hvac_modes 的 cool 与当前属性吻合
    assert controls["mode"].kind == "enum"
    assert controls["mode"].value == "cool"
    assert ("cool", "制冷") in controls["mode"].options
    # 风速：wind_speed=60 -> medium
    assert controls["fan_mode"].value == "medium"
    # 预设：全 off -> none
    assert controls["preset"].value == "none"
    # 摆风：两个 swing 都 off -> off
    assert controls["swing"].value == "off"
    # 设定温度
    temp = controls["target_temp"]
    assert temp.kind == "range" and temp.value == 26.0
    assert temp.minimum == 17.0 and temp.maximum == 30.0
    # 辅热
    assert controls["aux_heat"].kind == "switch"
    assert controls["aux_heat"].value is False
    # 开关实体（fresh_air_remove_odor / dry / prevent_straight_wind / aux_heat）
    assert "fresh_air_remove_odor" in controls
    assert controls["fresh_air_remove_odor"].kind == "switch"
    # 离线时全部置灰
    offline = midea_adapter.build_controls(mapping, attributes, False)
    assert all(not c.enabled for c in offline)
    print("5. 美的映射表 -> 控制项（真实 T0xAC） OK")


def test_build_status_rows_from_real_table() -> None:
    mapping = _t0xac_mapping()
    rows = midea_adapter.build_status_rows(
        mapping, {"indoor_temperature": 29.5, "indoor_humidity": 55,
                  "real_time_power_value": 12.3})
    assert rows.get("室内温度") == "29.5°C", rows
    assert rows.get("室内湿度") == "55%", rows
    assert rows.get("实时功率") in ("12.3W", "12.3"), rows
    print("6. 美的映射表 -> 状态行 OK")


def test_select_mapping_priority() -> None:
    # subtype 优先于 default
    table = {
        "default": {"marker": "default"},
        ("subtype", "524"): {"marker": "subtype"},
    }
    assert midea_adapter.select_mapping(table, 0xAC, subtype=524)[
               "marker"] == "subtype"
    assert midea_adapter.select_mapping(table, 0xAC, subtype=999)[
               "marker"] == "default"
    # sn8 规则
    table2 = {"default": {"marker": "default"},
              "00000Q1F": {"marker": "sn8"}}
    assert midea_adapter.select_mapping(table2, 0xAC, sn8="00000Q1F")[
               "marker"] == "sn8"
    # 空表
    assert midea_adapter.select_mapping({}, 0xAC) == {}
    print("7. 映射选择优先级 OK")


def test_power_keys_from_real_table() -> None:
    mapping = _t0xac_mapping()
    power_attr, rationale = midea_adapter._power_keys(mapping)
    assert power_attr == "power"
    assert rationale == ["off", "on"]
    print("8. 电源键识别 OK")


# ---------- 4. 异步桥与平台装配 ----------

def test_async_bridge() -> None:
    import asyncio

    bridge = midea_adapter._AsyncBridge()

    async def _add(a, b):
        await asyncio.sleep(0)
        return a + b

    assert bridge.run(lambda: _add(2, 3), timeout=5) == 5
    print("9. 异步事件循环桥 OK")


def test_registry_with_midea_plugin() -> None:
    # 不传 mijia_service：验证插件平台与内置平台合并路由
    registry = PlatformRegistry()
    ids = [p.platform_id for p in registry.platforms]
    assert "rmtsw" in ids and "midea" in ids, ids
    # did 前缀路由
    assert registry.platform_of("midea:123").platform_id == "midea"
    assert registry.platform_of("rmtsw:1").platform_id == "rmtsw"
    assert registry.platform_of("123") is None
    # 未登录时 power_states 不发起请求，直接 None
    states = registry.power_states(["midea:123"])
    assert states == {"midea:123": None}
    # 结构化控制委托：桩平台验证 device_controls/set_control 路由
    class _Stub(PlatformBase):
        platform_id = "stub"
        display_name = "桩"

        def session_active(self):
            return True

        def device_controls(self, did):
            return [ControlSpec(key="power", label="电源", kind="switch",
                                value=True)]

        def set_control(self, did, key, value):
            self.last = (did, key, value)

    stub = _Stub()
    registry._platforms.append(stub)
    controls = registry.device_controls("stub:9")
    assert len(controls) == 1 and controls[0].key == "power"
    registry.set_control("stub:9", "power", False)
    assert stub.last == ("stub:9", "power", False)
    print("10. 注册表插件合并/路由/控制委托 OK")


def test_power_panel_close_race() -> None:
    """开着电源面板时关掉，异步快照结果回来不得崩。

    锁死一次真实事故：打包版里快速开关设备面板，快照结果到达时
    PowerButton 的 C++ 对象已删除，回调直接 RuntimeError。
    """
    from PySide6.QtWidgets import QApplication

    from app.core.jobs import JobExecutor
    from app.ui.power_card_panel import PowerCardDialog

    app = QApplication.instance() or QApplication([])

    class _SlowPlatform(PlatformBase):
        platform_id = "slow"
        display_name = "慢平台"

        def session_active(self):
            return True

        def power_states(self, dids):
            return {did: True for did in dids}

        def device_status(self, did):
            return {}

        def capabilities(self, did):
            return []

        def device_controls(self, did):
            return []

    jobs = JobExecutor()
    registry = PlatformRegistry()
    registry._platforms.append(_SlowPlatform())
    device = DeviceInfo(did="slow:1", name="慢设备", model="m", home_name="家",
                        room_name="客厅", online=True, platform="slow",
                        native_id="1")
    panel = PowerCardDialog(registry, jobs, device)
    panel.show()
    panel.load()
    panel.close()
    panel.deleteLater()          # 让 C++ 对象真正被销毁
    deadline = time.time() + 3
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    jobs.shutdown(wait=2)
    print("11. 面板关闭竞态无崩溃 OK")


# ---------- 5. 美的登录流（假云端，锁死手机提醒风暴回归） ----------

class _FakeMideaCloud:
    """记录调用的假美居云端。"""

    def __init__(self, account, password, *, login_ok=True,
                 homes_ok=True, list_home_ok=True):
        self._account = account
        self._password = password
        self._login_ok = login_ok
        self._homes_ok = homes_ok
        self._list_home_ok = list_home_ok
        self.login_calls = 0
        self.list_home_calls = 0
        self.imported = None
        self._nickname = "测试用户"
        self._access_token = None

    @property
    def nickname(self):
        return self._nickname

    async def login(self):
        self.login_calls += 1
        if not self._login_ok:
            return False
        self._access_token = "token-1"
        return True

    async def list_home(self):
        self.list_home_calls += 1
        if not self._list_home_ok:
            return None
        return {1: "我家"}

    def import_session_payload(self, payload):
        self.imported = payload

    def export_session_payload(self):
        return {"access_token": self._access_token}


class _SyncBridge:
    """测试用同步桥：直接跑协程。"""

    def run(self, factory, timeout=30.0):
        import asyncio

        return asyncio.run(factory())


def _make_platform(monkey_clouds: list, **cloud_kwargs) -> "midea_adapter.MideaPlatform":
    platform = midea_adapter.MideaPlatform()
    platform._bridge = _SyncBridge()

    def _fake_make_cloud(account, password, server="美的美居"):
        cloud = _FakeMideaCloud(account, password, **cloud_kwargs)
        monkey_clouds.append(cloud)
        return cloud

    platform._make_cloud = _fake_make_cloud
    return platform


def test_midea_login_success_and_reuse() -> None:
    """登录成功后会话落盘：下次 auto_login 走本地会话，不再真实登录。"""
    from app.core import platform_store

    clouds: list = []
    platform = _make_platform(clouds)
    platform.login("13800138000", "pw", remember=True)
    assert platform.session_active() is True
    assert clouds[0].login_calls == 1
    assert clouds[0].list_home_calls == 1   # 登录后校验了一次
    # 会话与账号都已落盘
    session = platform_store.load_session("midea")
    assert session.get("payload", {}).get("access_token") == "token-1"
    assert session.get("account") == "13800138000"
    account = platform_store.load_account("midea")
    assert account.get("username") == "13800138000"
    # 新实例 auto_login：恢复本地会话，零次真实登录
    clouds2: list = []
    platform2 = _make_platform(clouds2)
    assert platform2.auto_login() is True
    assert clouds2[0].login_calls == 0
    assert clouds2[0].imported is not None
    platform2.logout()
    print("12. 美的登录成功与会话复用 OK")


def test_midea_login_pending_confirmation() -> None:
    """登录后业务接口 40002（待手机确认）：明确报错，不纳入登录态，
    且不连环重登（会话仍落盘以便恢复）。"""
    from app.core import platform_store

    clouds: list = []
    platform = _make_platform(clouds, list_home_ok=False)
    try:
        platform.login("13800138000", "pw")
        raise AssertionError("校验失败应当抛 PlatformError")
    except midea_adapter.PlatformError as exc:
        assert "待确认" in str(exc), exc
    assert platform.session_active() is False
    assert clouds[0].login_calls == 1          # 只登了一次，没有连环重登
    assert clouds[0].list_home_calls == 1
    # 会话已落盘：下次可从本地会话恢复
    assert platform_store.load_session("midea").get("payload")
    platform_store.clear_session("midea")
    platform_store.clear_account("midea")
    print("13. 美的登录待确认不连环重登 OK")


def test_midea_auto_login_circuit_breaker() -> None:
    """自动登录失败后进入静默期：轮询反复调用也不会再次真实登录。"""
    from app.core import platform_store

    clouds: list = []
    platform = _make_platform(clouds, login_ok=False)
    platform_store.save_account("midea", "13800138000", "wrong")
    assert platform.auto_login() is False
    assert clouds[0].login_calls == 1
    # 冷却期内再次调用：不再发起登录
    assert platform.auto_login() is False
    assert platform.auto_login() is False
    assert len(clouds) == 1 and clouds[0].login_calls == 1
    # 冷却过期后放行：再次尝试（新建 cloud 实例）
    platform._auto_login_blocked_until = 0.0
    assert platform.auto_login() is False
    assert len(clouds) == 2 and clouds[1].login_calls == 1
    platform_store.clear_account("midea")
    print("14. 美的自动登录断路器 OK")


def test_midea_power_state_known() -> None:
    """None 的语义：有电源键=读取失败可重试；无电源键=确认无能力。"""
    from types import SimpleNamespace

    platform = midea_adapter.MideaPlatform()
    platform._devices["midea:1"] = SimpleNamespace(power_attr="power")
    platform._devices["midea:2"] = SimpleNamespace(power_attr=None)
    assert platform.power_state_known("midea:1") is False   # 可重试
    assert platform.power_state_known("midea:2") is True    # 确认无能力
    assert platform.power_state_known("midea:999") is False  # 未加载，可重试
    print("15. 美的电源状态 None 语义 OK")


def test_platform_card_login_controls() -> None:
    """卡片登录控件显隐：未登录只显示登录，已登录只显示退出。"""
    from PySide6.QtWidgets import QApplication

    from app.core.jobs import JobExecutor
    from app.ui.platform_manager import PlatformManagerDialog

    app = QApplication.instance() or QApplication([])
    jobs = JobExecutor()
    registry = PlatformRegistry()          # rmtsw + midea（无米家）
    dlg = PlatformManagerDialog(registry, jobs)
    for platform_id in ("rmtsw", "midea"):
        card = dlg._cards[platform_id]
        # 对话框未 show 时 isVisible() 恒为 False，用 isVisibleTo(卡片)
        # 判断控件自身是否被显式隐藏
        assert card._login_btn.isVisibleTo(card) is True, platform_id
        assert card._logout_btn.isVisibleTo(card) is False, platform_id
        assert card._user_edit.isVisibleTo(card) is True, platform_id
        # 登录后：表单与登录按钮隐藏，退出登录可见
        card.set_logged_in(True)
        assert card._login_btn.isVisibleTo(card) is False, platform_id
        assert card._user_edit.isVisibleTo(card) is False, platform_id
        assert card._logout_btn.isVisibleTo(card) is True, platform_id
        # 再登出：回到登录态
        card.set_logged_in(False)
        assert card._login_btn.isVisibleTo(card) is True, platform_id
        assert card._logout_btn.isVisibleTo(card) is False, platform_id
    dlg.close()
    dlg.deleteLater()
    app.processEvents()
    jobs.shutdown(wait=2)
    print("16. 平台卡片登录控件显隐 OK")


def test_mijia_card_button_text() -> None:
    """米家卡片按钮文案随登录态变化（扫码登录 / 切换账号）。"""
    from PySide6.QtWidgets import QApplication

    from app.core.jobs import JobExecutor
    from app.core.service import MijiaService
    from app.ui.platform_manager import PlatformManagerDialog

    app = QApplication.instance() or QApplication([])
    jobs = JobExecutor()
    registry = PlatformRegistry(MijiaService())
    dlg = PlatformManagerDialog(registry, jobs)
    card = dlg._cards["mijia"]
    # 先显式置态再断言切换（构造时文案取决于本机是否已登录）
    card.set_logged_in(False)
    assert card._mijia_btn.text() == "扫码登录"
    card.set_logged_in(True)
    assert card._mijia_btn.text() == "切换账号"
    card.set_logged_in(False)
    assert card._mijia_btn.text() == "扫码登录"
    dlg.close()
    dlg.deleteLater()
    app.processEvents()
    jobs.shutdown(wait=2)
    print("17. 米家卡片按钮文案 OK")


def test_inactive_platform_devices_filtered() -> None:
    """未登录平台的设备不进主列表：家庭/房间下拉的数据源被过滤。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from app.ui.main_window import MainWindow

    class _StubActive(PlatformBase):
        platform_id = "stub"
        display_name = "桩平台"

        def session_active(self):
            return True

        def list_devices(self):
            return []

        def power_states(self, dids):
            return {did: None for did in dids}

    win = MainWindow()
    win._registry._platforms.append(_StubActive())
    devices = [
        DeviceInfo(did="rmtsw:1", name="开机卡电脑", model="m",
                   home_name="开机卡", room_name="开机卡", online=True,
                   platform="rmtsw", native_id="1"),
        DeviceInfo(did="stub:1", name="桩设备", model="m",
                   home_name="桩家庭", room_name="客厅", online=True,
                   platform="stub", native_id="1"),
    ]
    win._apply_devices(devices)
    # 未登录的 rmtsw 设备被过滤，已登录的桩平台设备保留
    assert {d.did for d in win._all_devices} == {"stub:1"}
    # 家庭下拉（_homes）只含活跃平台的家庭
    assert "开机卡" not in win._homes
    assert "桩家庭" in win._homes
    # 过滤发生 -> 置位静默登录后的重取标记
    assert win._pending_platform_reload is True
    # 空列表不置位
    win._pending_platform_reload = False
    win._apply_devices([])
    assert win._pending_platform_reload is False
    win._jobs.shutdown(wait=2)
    win.deleteLater()
    app.processEvents()
    print("18. 未登录平台设备过滤 OK")


def test_user_vendor_overrides_bundled() -> None:
    """一键更新落到用户目录的协议层必须真正生效。

    锁死：更新只替换 ``USER_DIR/<id>/vendor``，而导入一直走
    ``app.plugins.<id>.adapter``，它的 ``from .vendor...`` 恒解析到内置
    副本——更新下载完了却永远用不上。
    """
    import json
    import shutil
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        user_root = root / "plugins"
        plugin_dir = user_root / "midea"
        plugin_dir.mkdir(parents=True)
        # 模拟更新落位：整份协议层复制过去，并在 vendor 里打个标记
        shutil.copytree(loader.BUNDLED_DIR / "midea" / "vendor",
                        plugin_dir / "vendor")
        init_file = plugin_dir / "vendor" / "__init__.py"
        init_file.write_text(
            init_file.read_text(encoding="utf-8") + "\nMARKER = 'user'\n",
            encoding="utf-8")
        (plugin_dir / "plugin.json").write_text(json.dumps({
            "id": "midea", "name": "美的", "version": "1.0.0",
            "entry": "adapter:MideaPlatform",
            "upstream": {"repo": "sususweet/midea-meiju-codec",
                         "version": "v9.9.9"},
        }), encoding="utf-8")
        # 没有 vendor 的插件不应挂载
        bare = user_root / "bare"
        bare.mkdir()
        (bare / "plugin.json").write_text(json.dumps(
            {"id": "bare", "name": "x", "version": "1", "entry": "a:B"}),
            encoding="utf-8")

        old_dir = loader.USER_DIR
        loader.USER_DIR = user_root
        try:
            plugins = {info.id: info for info in loader.discover()}
            info = plugins["midea"]
            assert info.source == "user", info.source
            assert info.path == plugin_dir
            assert info.upstream_version == "v9.9.9"
            # 缺 entry 模块的用户副本：加载失败 -> None（调用方回退内置）
            assert loader._import_user_plugin(plugins["bare"], "a") is None

            top = f"{loader.USER_PKG_PREFIX}midea"
            loader._drop_modules(top)
            module = loader._import_user_plugin(info, "adapter")
            assert module is not None
            # 协议层与设备映射表取用户目录那份
            assert list(sys.modules[top].__path__)[0] == str(plugin_dir)
            vendor_mod = importlib.import_module(f"{top}.vendor")
            assert getattr(vendor_mod, "MARKER") == "user"
            cloud = importlib.import_module(f"{top}.vendor.core.cloud")
            assert Path(cloud.__file__).is_relative_to(plugin_dir), \
                cloud.__file__
            mapping = importlib.import_module(
                f"{top}.vendor.device_mapping.T0xAC")
            assert Path(mapping.__file__).is_relative_to(plugin_dir)
            # 适配器由内置目录同步进用户副本（打包版内置目录不可直接导入）
            assert Path(module.__file__).is_relative_to(plugin_dir)
        finally:
            loader._drop_modules(f"{loader.USER_PKG_PREFIX}midea")
            loader.USER_DIR = old_dir
    # 回滚后不留残余
    assert not [n for n in sys.modules
                if n.startswith(loader.USER_PKG_PREFIX)]
    print("19. 用户目录协议层覆盖内置 OK")


test_ha_shim()
test_plugin_discovery()
test_manifest_validation()
test_load_platforms_registers_midea()
test_build_controls_from_real_table()
test_build_status_rows_from_real_table()
test_select_mapping_priority()
test_power_keys_from_real_table()
test_async_bridge()
test_registry_with_midea_plugin()
test_power_panel_close_race()
test_midea_login_success_and_reuse()
test_midea_login_pending_confirmation()
test_midea_auto_login_circuit_breaker()
test_midea_power_state_known()
test_platform_card_login_controls()
test_mijia_card_button_text()
test_inactive_platform_devices_filtered()
test_user_vendor_overrides_bundled()

print("\nPLUGIN REGRESSION ALL PASS")
