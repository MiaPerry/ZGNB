"""发布包只携带功能文件，不携带各环境的业务数据和秘密。"""

import zipfile

import pytest


def test_backend_package_excludes_runtime_data_and_configuration(tmp_path):
    from scripts.deploy_package import build_package

    source = tmp_path / "source"
    files = {
        "api/main.py": "# API",
        "modules/tracking_tables.sql": "SELECT 1;",
        "requirements.txt": "curl_cffi>=0.7.0",
        "SKILL.md": "股票分析运行时模板",
        "data/nasdaq100.json": "[]",
        "data/stock_data.db": "本地数据库",
        "data/stock_data.db-wal": "未合并日志",
        "data/stock_data.db.bak": "备份",
        ".env": "SECRET=private",
        ".env.production": "SECRET=private",
        "modules/.env": "SECRET=private",
        "scripts/proxy.yaml": "订阅信息",
        "scripts/id_rsa": "密钥",
        "modules/__pycache__/secret.pyc": "缓存",
    }
    for name, content in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    archive = tmp_path / "backend.zip"
    build_package(source, archive, "backend")
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {
            "api/main.py",
            "modules/tracking_tables.sql",
            "requirements.txt",
            "data/nasdaq100.json",
            "SKILL.md",
        }


@pytest.mark.parametrize("unsafe", [".env", "data/stock_data.db", "../outside.py", "/api/main.py"])
def test_apply_rejects_entire_package_before_writing(tmp_path, unsafe):
    from scripts.deploy_package import apply_package

    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("api/main.py", "新代码")
        package.writestr(unsafe, "禁止发布")
    destination = tmp_path / "server"
    destination.mkdir()

    with pytest.raises(ValueError):
        apply_package(archive, destination, "backend")
    assert list(destination.iterdir()) == []


def test_apply_preserves_server_database_and_environment(tmp_path):
    from scripts.deploy_package import apply_package

    destination = tmp_path / "server"
    (destination / "data").mkdir(parents=True)
    database = destination / "data" / "stock_data.db"
    environment = destination / ".env"
    database.write_bytes(b"server-only-data")
    environment.write_bytes(b"server-only-config")
    archive = tmp_path / "backend.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("api/main.py", "新代码")

    apply_package(archive, destination, "backend")
    assert database.read_bytes() == b"server-only-data"
    assert environment.read_bytes() == b"server-only-config"
    assert (destination / "api" / "main.py").read_text(encoding="utf-8") == "新代码"


def test_frontend_package_excludes_private_files_and_preserves_server_data(tmp_path):
    from scripts.deploy_package import apply_package, build_package

    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("index.html", "app.js", "app.css", ".env", "stock_data.db", "clash.json", "id_rsa", "site.key"):
        (dist / name).write_text("test", encoding="utf-8")
    archive = tmp_path / "frontend.zip"
    assert build_package(dist, archive, "frontend") == ["webroot/app.css", "webroot/app.js", "webroot/index.html"]
    destination = tmp_path / "server"
    destination.mkdir()
    config = destination / ".env"
    config.write_bytes(b"server-only")
    apply_package(archive, destination, "frontend")
    assert (destination / "webroot" / "index.html").exists()
    assert config.read_bytes() == b"server-only"


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "damaged"])
def test_bad_package_does_not_update_any_code(tmp_path, kind):
    import stat
    from scripts.deploy_package import apply_package

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("api/main.py", "new-code")
        if kind == "duplicate":
            with pytest.warns(UserWarning):
                package.writestr("api/main.py", "duplicate-code")
        elif kind == "symlink":
            entry = zipfile.ZipInfo("api/linked.py")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(entry, "../.env")
        else:
            package.writestr("api/second.py", "damaged-content")
    if kind == "damaged":
        archive.write_bytes(archive.read_bytes().replace(b"damaged-content", b"broken!-content"))
    destination = tmp_path / "server"
    (destination / "api").mkdir(parents=True)
    code = destination / "api" / "main.py"
    code.write_bytes(b"old-code")
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        apply_package(archive, destination, "backend")
    assert code.read_bytes() == b"old-code"


@pytest.mark.parametrize("payload, expected", [(b'{"status":"ok"}', 0), (b'{"status":"failed"}', 1), (b"invalid", 1)])
def test_remote_health_check_uses_loopback_without_environment_proxy(monkeypatch, payload, expected):
    from io import BytesIO
    from unittest.mock import Mock
    from scripts import deploy_package as tool
    import urllib.request

    handler = Mock()
    proxy_factory = Mock(return_value=handler)
    opener = Mock()
    opener.open.return_value = BytesIO(payload)
    factory = Mock(return_value=opener)
    monkeypatch.setattr(urllib.request, "ProxyHandler", proxy_factory)
    monkeypatch.setattr(urllib.request, "build_opener", factory)
    assert tool.main(["health"]) == expected
    proxy_factory.assert_called_once_with({})
    factory.assert_called_once_with(handler)
    opener.open.assert_called_once_with("http://127.0.0.1:8000/api/v1/system/health", timeout=15)
