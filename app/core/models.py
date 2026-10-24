# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""界面层专用的数据模型。

刻意与 mijiaAPI 的原始 dict / 对象结构解耦：上游返回格式变化时只需调整
core.service 适配层，界面层永远只依赖这里的稳定类型。
"""

from dataclasses import dataclass, field


@dataclass
class DeviceInfo:
    """设备列表条目，只保留界面展示需要的最小字段集。

    spec_type 是云端下发的设备规格 URN（可能为空：手表、第三方牙刷
    等无公开规格的设备），上游 5.x 按它拉取设备规格，取代旧版的 model。

    多平台扩展（见 app/core/platforms）：platform 标识设备所属平台
    （"mijia" 米家 / "rmtsw" 开机卡…），did 是跨平台唯一键（非米家
    平台的 did 带平台前缀，如 "rmtsw:139713"），native_id 是平台内部
    的设备 ID（空串表示与 did 相同，米家设备即是如此）。托盘、挂件、
    定时任务等持久化数据一律以 did 为键，天然兼容多平台。
    """

    did: str
    name: str
    model: str
    home_name: str
    room_name: str
    online: bool
    spec_type: str = ""
    platform: str = "mijia"
    native_id: str = ""

    def native(self) -> str:
        """平台内部设备 ID：调用平台接口时传这个值。"""
        return self.native_id or self.did


@dataclass
class PropInfo:
    """单个属性的元数据，控件工厂据此决定生成哪种控件。

    range 来自米家 spec 的 valueRange；value_list 为枚举选项，
    每项含 value/description，可能附带 desc_zh_cn 中文描述。
    """

    name: str
    desc: str
    type: str  # bool / int / uint / float / string
    readable: bool
    writable: bool
    range: tuple | None = None  # (min, max, step)
    value_list: list | None = None


@dataclass
class ActionInfo:
    name: str
    desc: str


@dataclass
class ControlSpec:
    """详情面板里的一个结构化控制项（空调温度/模式/风速、开关实体等）。

    kind:
      - "switch"  布尔开关（value 为平台原生真假值）
      - "range"   数值滑杆（minimum/maximum/step/unit 生效）
      - "enum"    下拉选择（options 为 [(值, 展示名), ...]）

    key 是平台内部属性键（美的即设备属性名或语义键），由平台的
    set_control 负责翻译成真正的控制指令。
    """

    key: str
    label: str
    kind: str                                   # switch | range | enum
    value: object = None                        # 当前值（平台原生形态）
    options: list = field(default_factory=list)  # [(值, 展示名), ...]
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    enabled: bool = True                        # 离线/不可控时置灰


@dataclass
class DeviceDetail:
    """点击某台设备后加载的控制面板所需全部信息。"""

    did: str
    name: str
    model: str
    props: list[PropInfo]
    actions: list[ActionInfo]


@dataclass
class SceneInfo:
    """米家手动场景（米家 APP → 智能 → 手动控制 中创建的项）。

    home_id 是执行场景时上游 run_scene 所需的家庭 ID；
    home_name 为家庭显示名（卡片副标题用），为空表示归属未知。
    """

    scene_id: str
    name: str
    home_id: str
    home_name: str = ""


def is_speaker(device: DeviceInfo) -> bool:
    """小爱音箱判定：音频控制栏、语音入口共用同一标准。

    用包含匹配而非限定 xiaomi 前缀，第三方音箱（模型含
    wifispeaker）同样具备音量/静音与文本指令能力。
    """
    return "wifispeaker" in device.model
