"""Install the CloudDrive2 gRPC client outside the plugin directory.

AutoBangumi 4.x rejects native extensions anywhere under the plugin root.
The client libraries are downloaded into the config directory instead, so a
copy of this repository can install them on startup without shipping wheels.
"""

from __future__ import annotations

import ctypes
import gzip
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGES = ("grpcio", "protobuf", "typing_extensions", "clouddrive2-client")
_ALPINE_LIBS = ("libgcc_s.so.1", "libstdc++.so.6")


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


def _lib_dir() -> Path:
    return runtime_home() / "lib"


def activate() -> None:
    """Make a previously installed runtime visible to this process."""
    lib_dir = _lib_dir()
    if lib_dir.is_dir():
        for name in _ALPINE_LIBS:
            path = lib_dir / name
            if path.is_file():
                ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
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


def _alpine_version() -> str | None:
    path = Path("/etc/os-release")
    if not path.is_file():
        return None
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    if values.get("ID") != "alpine":
        return None
    version = values.get("VERSION_ID", "")
    parts = version.split(".")
    if len(parts) < 2:
        return None
    return f"v{parts[0]}.{parts[1]}"


def _needs_libstdcxx() -> bool:
    if not _musl() or os.path.exists("/usr/lib/libstdc++.so.6"):
        return False
    return not all((_lib_dir() / name).is_file() for name in _ALPINE_LIBS)


def _apk_names(version: str, arch: str) -> dict[str, str]:
    url = f"https://dl-cdn.alpinelinux.org/alpine/{version}/main/{arch}/APKINDEX.tar.gz"
    with urllib.request.urlopen(url, timeout=60) as response:
        blob = response.read()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        text = archive.extractfile("APKINDEX").read().decode()
    wanted = {"libgcc": "", "libstdc++": ""}
    current: dict[str, str] = {}
    for line in text.splitlines() + [""]:
        if line == "":
            name = current.get("P")
            if name in wanted and current.get("V"):
                wanted[name] = f"{name}-{current['V']}.apk"
            current = {}
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            current[key] = value
    missing = [name for name, filename in wanted.items() if not filename]
    if missing:
        raise RuntimeError(f"Alpine index has no package for {', '.join(missing)}")
    return wanted


def _extract_apk(blob: bytes, dest: Path) -> None:
    pos = 0
    while True:
        start = blob.find(b"\x1f\x8b", pos)
        if start < 0:
            return
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(blob[start:])) as handle:
                payload = handle.read()
            archive = tarfile.open(fileobj=io.BytesIO(payload))
        except (OSError, tarfile.TarError, EOFError):
            pos = start + 2
            continue
        for member in archive.getmembers():
            base = Path(member.name).name
            if base not in _ALPINE_LIBS and not base.startswith("libstdc++.so.6."):
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            filename = "libstdc++.so.6" if "libstdc++" in base else base
            (dest / filename).write_bytes(source.read())
        pos = start + 2


def _install_alpine_libs() -> None:
    if not _needs_libstdcxx():
        return
    version = _alpine_version()
    if version is None:
        raise RuntimeError("libstdc++ is missing and this is not Alpine")
    dest = _lib_dir()
    dest.mkdir(parents=True, exist_ok=True)
    arch = _arch()
    for filename in _apk_names(version, arch).values():
        url = f"https://dl-cdn.alpinelinux.org/alpine/{version}/main/{arch}/{filename}"
        logger.info("CD2 正在下载 %s", filename)
        with urllib.request.urlopen(url, timeout=60) as response:
            _extract_apk(response.read(), dest)
    if _needs_libstdcxx():
        raise RuntimeError("Alpine libstdc++ was downloaded but the libraries are missing")


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
        _install_alpine_libs()
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
