#!/usr/bin/env python3
"""发布包允许名单：只交付功能文件，永不覆盖数据库和环境私有配置。"""

import argparse
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import urllib.request
import zipfile

_BACKEND_TYPES = {
    "api": {".py"},
    "modules": {".py", ".sql"},
    "scripts": {".py", ".ps1"},
    "rules": {".yaml", ".yml", ".md"},
    "knowledge": {".md"},
}
_STATIC_FILES = {"requirements.txt", "pyproject.toml", "SKILL.md", "data/nasdaq100.json"}
_FRONTEND_TYPES = {
    ".html",
    ".js",
    ".css",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".json",
    ".txt",
}
_PRIVATE_PATTERNS = (
    "*.db*",
    "*.sqlite*",
    "*.bak*",
    "*.backup*",
    "*.log",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*subscription*",
    "clash*",
    "mihomo*",
    "proxy.*",
)


def allowed_file(name: str, kind: str) -> bool:
    """两端使用相同规则；拒绝路径穿越、私有配置和运行数据。"""
    if kind not in ("backend", "frontend"):
        raise ValueError("发布类型必须是 backend 或 frontend")
    parts = name.split("/")
    if (
        "\\" in name
        or ":" in name
        or any(not part or part.startswith(".") or part.endswith((".", " ")) or part == "__pycache__" for part in parts)
    ):
        return False
    if any(fnmatch.fnmatch(part.lower(), pattern) for part in parts for pattern in _PRIVATE_PATTERNS):
        return False
    path = PurePosixPath(name)
    if kind == "frontend":
        return len(parts) >= 2 and parts[0] == "webroot" and path.suffix.lower() in _FRONTEND_TYPES
    return name in _STATIC_FILES or (len(parts) >= 2 and path.suffix.lower() in _BACKEND_TYPES.get(parts[0], set()))


def build_package(source: Path, archive: Path, kind: str) -> list[str]:
    """后端从项目根目录打包；前端从 dist 打包并加 webroot 前缀。"""
    source, archive = Path(source).resolve(), Path(archive).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"发布源目录不存在：{source}")
    names = []
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for base, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ("node_modules", "__pycache__", "venv", "backups")
                and not (Path(base) / d).is_symlink()
            ]
            for filename in sorted(files):
                path = Path(base) / filename
                relative = path.relative_to(source).as_posix()
                name = f"webroot/{relative}" if kind == "frontend" else relative
                if path.is_symlink() or not allowed_file(name, kind):
                    continue
                package.write(path, name)
                names.append(name)
    if not names:
        raise ValueError("发布包为空，停止发布")
    return sorted(names)


def apply_package(archive: Path, destination: Path, kind: str) -> list[str]:
    """先完整校验并暂存，再逐个原子替换允许的文件；不清理其他文件。"""
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [entry.filename for entry in entries]
        if not entries or len(set(names)) != len(names):
            raise ValueError("发布包为空或包含重复文件")
        for entry in entries:
            if not allowed_file(entry.filename, kind) or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("发布包包含禁止的文件或路径")
            target = destination / entry.filename
            if not target.resolve().is_relative_to(destination):
                raise ValueError("目标路径越过发布目录")
            current = target
            while current != destination:
                if current.is_symlink():
                    raise ValueError("发布目标不能经过符号链接")
                current = current.parent
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".release-", dir=destination) as staging:
            for entry in entries:
                pending = Path(staging) / entry.filename
                pending.parent.mkdir(parents=True, exist_ok=True)
                pending.write_bytes(package.read(entry))
            for name in names:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(Path(staging) / name, target)
    return names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "apply", "health"))
    parser.add_argument("--kind", choices=("backend", "frontend"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "health":
            # 经 SSH 在服务器回环地址检查，绕过服务器环境代理，不携带网站登录凭据。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:8000/api/v1/system/health", timeout=15) as response:
                result = json.load(response)
            if not isinstance(result, dict) or result.get("status") != "ok":
                raise ValueError("后端健康检查未通过")
            print("HEALTH_OK")
            return 0
        if args.kind is None or args.archive is None:
            parser.error("build/apply 需要 --kind 和 --archive")
        if args.action == "build":
            if args.source is None:
                parser.error("build 需要 --source")
            names = build_package(args.source, args.archive, args.kind)
        else:
            if args.destination is None:
                parser.error("apply 需要 --destination")
            names = apply_package(args.archive, args.destination, args.kind)
        print(f"{args.action}: {len(names)} 个功能文件")
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"发布包处理失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
