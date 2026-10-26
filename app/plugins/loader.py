# SPDX-License-Identifier: GPL-3.0-or-later
"""插件加载器：发现 → 校验 → 导入 → 注册；GitHub 一键更新。

目录约定：
- 内置插件：``app/plugins/<id>/``（随程序分发）
- 用户插件：``%LOCALAPPDATA%\\MiHome-Windows\\plugins\\<id>/``
  （更新后的副本落在这里，覆盖内置；打包版无需管理员权限）

单个插件损坏（清单缺失/导入异常）只跳过并记录，绝不影响其他平台
与主程序启动。
"""

import importlib
import importlib.machinery
import importlib.util
import json
import logging
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import requests

from app.core import _json_store
from app.core.platforms.base import PlatformBase

logger = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).resolve().parent
# 用户级插件目录（更新后的副本）：与 settings.json 等同目录，
# 打包版无需管理员权限即可写入
USER_DIR = _json_store.data_dir() / "plugins"
_GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
_DOWNLOAD_TIMEOUT = (15, 120)


@dataclass
class PluginInfo:
    """插件清单（内存形态）。"""

    id: str
    name: str
    version: str
    entry: str
    path: Path
    source: str                       # "bundled" | "user"
    description: str = ""
    icon_name: str = ""
    python_deps: list[str] = field(default_factory=list)
    upstream: dict = field(default_factory=dict)

    @property
    def upstream_version(self) -> str:
        return str(self.upstream.get("version") or "")


def _read_manifest(plugin_dir: Path) -> dict | None:
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("插件清单读取失败 %s: %s", manifest_path, exc)
        return None
    if not isinstance(data, dict):
        return None
    # 必填字段校验
    for key in ("id", "name", "version", "entry"):
        if not data.get(key):
            logger.warning("插件清单缺少字段 %s: %s", key, manifest_path)
            return None
    entry = str(data["entry"])
    if ":" not in entry:
        logger.warning("插件 entry 格式应为 模块:类名: %s", entry)
        return None
    return data


def _scan(root: Path, source: str) -> dict[str, PluginInfo]:
    found: dict[str, PluginInfo] = {}
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        data = _read_manifest(child)
        if data is None:
            continue
        found[str(data["id"])] = PluginInfo(
            id=str(data["id"]),
            name=str(data["name"]),
            version=str(data["version"]),
            entry=str(data["entry"]),
            path=child,
            source=source,
            description=str(data.get("description") or ""),
            icon_name=str(data.get("icon_name") or ""),
            python_deps=list(data.get("python_deps") or []),
            upstream=dict(data.get("upstream") or {}),
        )
    return found


def _bundled_roots() -> list[Path]:
    """内置插件根目录候选（按优先级）。

    源码模式下 ``BUNDLED_DIR`` 就是源码目录，直接用；
    打包版（Nuitka standalone）模块被编译进 exe，``__file__`` 可能是
    编译期的源码路径（用户机器上不存在）或 dist 下的虚拟路径，此时以
    **exe 同目录的 app/plugins** 为准——build.ps1 会把整棵插件树作为数据
    复制过去（plugin.json / NOTICE.txt 等非代码文件必须来自磁盘）。
    """
    roots: list[Path] = []
    try:
        exe_root = Path(sys.executable).resolve().parent / "app" / "plugins"
    except Exception:  # pragma: no cover - sys.executable 异常时退回源码目录
        exe_root = None
    if exe_root is not None and exe_root.is_dir() and exe_root != BUNDLED_DIR:
        roots.append(exe_root)
    roots.append(BUNDLED_DIR)
    return roots


def discover() -> list[PluginInfo]:
    """发现全部插件；同 id 用户目录覆盖内置。"""
    plugins: dict[str, PluginInfo] = {}
    # 先扫打包产物目录（存在即优先），再扫源码目录，最后用户目录覆盖
    for root in _bundled_roots():
        plugins.update(_scan(root, "bundled"))
    plugins.update(_scan(USER_DIR, "user"))
    return sorted(plugins.values(), key=lambda p: p.id)


# 用户副本导入时用的顶层包名前缀。必须避开 ``app.plugins.*``：打包版
# （Nuitka）里那些模块是编译进 exe 的冻结模块，适配器里的
# ``from .vendor...`` 会按全名命中冻结表，用户目录的新协议层永远抢不到。
# 用一个冻结表里没有的顶层名，协议层就从用户目录加载。
USER_PKG_PREFIX = "_mihome_plugin_"


def _drop_modules(prefix: str) -> None:
    """清掉顶层包 prefix 及其子模块的导入缓存（回滚用）。"""
    dotted = f"{prefix}."
    for name in [n for n in sys.modules
                 if n == prefix or n.startswith(dotted)]:
        del sys.modules[name]


def _bundled_plugin_dir(plugin_id: str) -> Path | None:
    """内置插件目录（给副本提供适配器代码：一键更新只替换协议层）。"""
    for root in _bundled_roots():
        candidate = root / plugin_id
        if (candidate / "plugin.json").is_file():
            return candidate
    return None


def _sync_builtin_code(info: PluginInfo, bundled: Path | None) -> None:
    """把内置插件的适配器源码同步进用户目录（一键更新只替换协议层）。

    打包版里内置插件目录是冻结包的路径，Nuitka 不允许从那里 import 源码
    （实测 find_spec 返回 None），所以把顶层 .py 复制成用户目录里的普通
    文件，用户副本才是自洽的：协议层取更新后的，适配器取内置这份。
    """
    if bundled is None:
        return
    for src in sorted(bundled.glob("*.py")):
        try:
            shutil.copy2(src, info.path / src.name)
        except OSError as exc:
            logger.info("插件 %s 同步适配器源码失败 %s: %s",
                        info.id, src.name, exc)


def _import_user_plugin(info: PluginInfo, module_name: str):
    """导入用户目录里的插件副本，让"一键更新"真正生效。

    用户副本只含 ``vendor/`` 与 ``plugin.json``（更新的只是上游协议层）。
    这里合成一个顶层包 ``<前缀><id>`` 指向用户目录：

    - 包名不在打包版（Nuitka）的冻结表里，全部从磁盘加载，绕开模块表；
    - 协议层与设备映射表 T0xXX 取用户目录那份 → 更新生效；
    - 适配器（``adapter.py`` 等）由 _sync_builtin_code 同步自内置目录。

    返回 entry 模块；不可用（已导入过 / 加载失败）返回 None，由调用方
    回退到内置副本——宁可用旧协议层，也不能让平台整个消失。
    """
    top = f"{USER_PKG_PREFIX}{info.id}"
    if top in sys.modules:
        return None
    _sync_builtin_code(info, _bundled_plugin_dir(info.id))
    spec = importlib.machinery.ModuleSpec(top, None)
    spec.submodule_search_locations = [str(info.path)]
    sys.modules[top] = importlib.util.module_from_spec(spec)
    try:
        module = importlib.import_module(f"{top}.{module_name}")
    except Exception as exc:
        _drop_modules(top)
        logger.warning("插件 %s 的用户副本加载失败: %s", info.id, exc)
        return None
    logger.info("插件 %s 使用用户目录协议层: %s", info.id, info.path)
    return module


def _import_platform(info: PluginInfo) -> PlatformBase:
    """按 entry（模块:类名）导入并实例化平台。

    用户目录插件（一键更新后的副本）优先，失败回退内置副本。
    """
    module_name, class_name = info.entry.split(":", 1)
    module = None
    if info.source == "user":
        module = _import_user_plugin(info, module_name)
        if module is None:
            logger.info("插件 %s 回退内置副本", info.id)
    if module is None:
        module = importlib.import_module(
            f"{__package__}.{info.id}.{module_name}")
    platform_cls = getattr(module, class_name, None)
    if platform_cls is None:
        raise AttributeError(f"插件 {info.id} 的 {info.entry} 不存在")
    platform = platform_cls()
    if not isinstance(platform, PlatformBase):
        raise TypeError(f"插件 {info.id} 的 {class_name} 不是 PlatformBase 子类")
    return platform


def load_platforms() -> list[PlatformBase]:
    """导入全部可用插件平台；失败跳过。"""
    platforms: list[PlatformBase] = []
    for info in discover():
        try:
            platforms.append(_import_platform(info))
        except Exception as exc:
            logger.warning("插件 %s 加载失败，已跳过: %s", info.id, exc)
    return platforms


def check_update(info: PluginInfo) -> dict | None:
    """查询 GitHub 最新 release；有更新返回详情，否则 None。"""
    repo = info.upstream.get("repo")
    if not repo:
        return None
    url = _GITHUB_API.format(repo=repo)
    try:
        resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT,
                            headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            logger.info("插件 %s 更新检查失败: HTTP %s", info.id, resp.status_code)
            return None
        release = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.info("插件 %s 更新检查失败: %s", info.id, exc)
        return None
    latest = str(release.get("tag_name") or "")
    if not latest or latest == info.upstream_version:
        return None
    return {
        "current": info.upstream_version,
        "latest": latest,
        "zip_url": release.get("zipball_url") or "",
        "notes": str(release.get("name") or latest),
    }


def _extract_upstream_paths(zip_path: Path, extract_dir: Path,
                            paths: list[str]) -> Path:
    """从 GitHub zipball 中取出配置的上游路径，返回协议层根目录。

    zipball 顶层是 ``<repo>-<sha>/``，用 paths 的前缀定位。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        top = next((n.split("/", 1)[0] for n in names if "/" in n), "")
        wanted: list[str] = []
        for rel in paths:
            prefix = f"{top}/{rel}".rstrip("/")
            hit = any(n == prefix or n.startswith(prefix + "/") for n in names)
            if not hit:
                raise FileNotFoundError(f"上游包内找不到 {rel}")
            wanted.append(prefix)
        for member in names:
            for prefix in wanted:
                if member == prefix or member.startswith(prefix + "/"):
                    rel_inside = member[len(prefix):].lstrip("/")
                    target = extract_dir / rel_inside if rel_inside else None
                    if target is None:
                        continue
                    if member.endswith("/"):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    break
    return extract_dir


def apply_update(info: PluginInfo, zip_url: str) -> str:
    """下载上游 zip 并替换用户目录下的协议层，返回新版本号。

    流程：下载 → 临时目录解出 upstream.paths → 导入自测（失败即放弃，
    不碰现有文件）→ 原子替换 vendor/ 与 plugin.json。
    """
    paths = list(info.upstream.get("paths") or [])
    if not paths or not zip_url:
        raise ValueError("插件未配置 upstream.paths 或更新地址")
    resp = requests.get(zip_url, timeout=_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(resp.content)
        zip_path = Path(tmp.name)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            new_vendor = _extract_upstream_paths(
                zip_path, Path(tmp_dir) / "vendor", paths)
            # 自测：新协议层必须能导入（HA 替身 + importlib）
            _selftest_import(info, new_vendor)
            # 落位：用户目录 <id>/ 下替换 vendor/ 与 plugin.json
            user_plugin_dir = USER_DIR / info.id
            user_plugin_dir.mkdir(parents=True, exist_ok=True)
            target_vendor = user_plugin_dir / "vendor"
            backup = user_plugin_dir / ".vendor.bak"
            if backup.exists():
                shutil.rmtree(backup)
            if target_vendor.exists():
                shutil.move(str(target_vendor), str(backup))
            shutil.copytree(new_vendor, target_vendor)
            if backup.exists():
                shutil.rmtree(backup)
            # 更新清单里的 upstream.version 与插件版本
            manifest_path = info.path / "plugin.json"
            target_manifest = user_plugin_dir / "plugin.json"
            if manifest_path.is_file() and not target_manifest.is_file():
                shutil.copy2(manifest_path, target_manifest)
            data = json.loads(target_manifest.read_text(encoding="utf-8"))
            latest = zip_url.rstrip("/").rsplit("/", 1)[-1]
            data.setdefault("upstream", {})["version"] = latest
            target_manifest.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return latest
    finally:
        zip_path.unlink(missing_ok=True)


def _selftest_import(info: PluginInfo, vendor_dir: Path) -> None:
    """把候选 vendor 目录当作包临时导入一遍，验证协议层自洽。"""
    package_root = vendor_dir.parent.name
    with tempfile.TemporaryDirectory() as tmp_dir:
        staging = Path(tmp_dir) / info.id
        staging.mkdir(parents=True)
        shutil.copytree(vendor_dir, staging / "vendor")
        (staging / "__init__.py").write_text("", encoding="utf-8")
        import sys
        sys.path.insert(0, tmp_dir)
        module_name = f"{info.id}.vendor"
        try:
            importlib.import_module(module_name)
            cloud = importlib.import_module(f"{module_name}.core.cloud")
            assert hasattr(cloud, "MeijuCloud"), "协议层缺少 MeijuCloud"
            mapping = importlib.import_module(
                f"{module_name}.device_mapping.T0xAC")
            assert hasattr(mapping, "DEVICE_MAPPING"), "设备映射表缺失"
        finally:
            sys.path.remove(tmp_dir)
            for name in [m for m in sys.modules if m.startswith(info.id)]:
                del sys.modules[name]
        del package_root
