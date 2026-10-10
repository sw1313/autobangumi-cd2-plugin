"""Install the CloudDrive2 gRPC client outside the plugin directory.

AutoBangumi 4.x rejects native extensions anywhere under the plugin root.
The client libraries are downloaded into the config directory instead, so a
copy of this repository can install them on startup without shipping wheels.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGES = ("grpcio", "protobuf", "typing_extensions", "clouddrive2-client")


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_home() -> Path:
    override = os.environ.get("CD2_RUNTIME_HOME")
    if override:
        return Path(override)
    root = plugin_root()
    if root.parent.name == "local" and root.parent.parent.name == "plugins":
        return root.parent.parent.parent / "cd2-runtime"
    config = Path(os.environ.get("AB_CONFIG_DIR", "/app/config"))
    if config.is_dir():
        return config / "cd2-runtime"
    return root.parent / "cd2-runtime"


def _python_dir() -> Path:
    return runtime_home() / "py"


def activate() -> None:
    """Make a previously installed runtime visible to this process."""
    py_dir = _python_dir()
    if (py_dir / "grpc").is_dir() and str(py_dir) not in sys.path:
        sys.path.insert(0, str(py_dir))


def _imports() -> bool:
    try:
        import grpc  # noqa: F401
        from clouddrive2_client import CloudDriveClient  # noqa: F401
    except ImportError:
        for name in list(sys.modules):
            if name == "grpc" or name.startswith(("grpc.", "clouddrive2_client")):
                del sys.modules[name]
        return False
    return True


def _arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def _musl() -> bool:
    return os.path.exists(f"/lib/ld-musl-{_arch()}.so.1")


def _wheel_url(package: str, py_tag: str, arch: str, musl: bool) -> str:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=60) as response:
        files = json.load(response)["urls"]
    family = "musllinux" if musl else "manylinux"

    def matches(name: str) -> bool:
        if not name.endswith(".whl"):
            return False
        if "py3-none-any" in name:
            return True
        return arch in name and family in name and (py_tag in name or "abi3" in name)

    matches_found = [item["url"] for item in files if matches(item["filename"])]
    if not matches_found:
        raise RuntimeError(f"No {family} wheel for {package} ({py_tag}, {arch})")
    preferred = [url for url in matches_found if py_tag in url]
    return preferred[0] if preferred else matches_found[0]


def _extract_wheel(url: str, dest: Path) -> None:
    name = url.rsplit("/", 1)[-1]
    archive_path = dest / name
    logger.info("CD2 正在下载 %s", name)
    urllib.request.urlretrieve(url, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(dest)
    archive_path.unlink()


def _install_wheels() -> None:
    dest = _python_dir()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    musl = _musl()
    arch = _arch()
    for package in _PACKAGES:
        _extract_wheel(_wheel_url(package, py_tag, arch, musl), dest)


def _uv_install() -> bool:
    uv = shutil.which("uv")
    for candidate in ("/usr/local/bin/uv", "/opt/uv/bin/uv"):
        if uv:
            break
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            uv = candidate
    if not uv:
        return False
    dest = _python_dir()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(dest),
            "-r",
            str(requirements),
        ],
        check=True,
    )
    return True


def ensure() -> None:
    """Download the gRPC client when this interpreter cannot import it yet."""
    activate()
    if _imports():
        return
    home = runtime_home()
    logger.info("CD2 客户端库未安装，正在下载到 %s", home)
    try:
        try:
            installed = _uv_install()
        except (OSError, subprocess.CalledProcessError):
            logger.warning("CD2 使用 uv 安装失败，改为直接下载轮子", exc_info=True)
            installed = False
        if not installed:
            _install_wheels()
        activate()
    except Exception:
        logger.exception("CD2 客户端库安装失败")
        return
    if not _imports():
        logger.error("CD2 客户端库已下载到 %s，但当前进程仍无法导入", home)
