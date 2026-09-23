"""PowerShell 发布流程测试：所有外部程序均替换为记录调用的假实现。"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_server.ps1"


def run_script(tmp_path, flags="", failure="", script=SCRIPT):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("未安装 PowerShell")
    log = tmp_path / "commands.log"
    key = tmp_path / "key"
    key.write_text("测试密钥占位，不会建立连接", encoding="utf-8")
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        """$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
function Record-Call($name, $arguments) {
    Add-Content -LiteralPath $env:TEST_COMMAND_LOG -Value ($name + ' ' + ($arguments -join ' '))
    $global:LASTEXITCODE = 0
    if ($env:TEST_FAIL_AT -and (($env:TEST_FAIL_AT -eq $name) -or (($arguments -join ' ').Contains($env:TEST_FAIL_AT)))) { $global:LASTEXITCODE = 23 }
}
function python { Record-Call 'python' $args }
function npm.cmd { Record-Call 'npm' $args }
function ssh { Record-Call 'ssh' $args }
function scp { Record-Call 'scp' $args }
function Start-Sleep { }
function tar { throw '测试禁止旧打包命令' }
function curl.exe { throw '测试禁止真实健康检查' }
"""
        + f"& '{script}' -Server 'deploy@example.invalid' -SshKey '{key}' {flags}\nexit $LASTEXITCODE\n",
        encoding="utf-8-sig",
    )
    env = {**os.environ, "TEST_COMMAND_LOG": str(log), "TEST_FAIL_AT": failure}
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(harness)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )
    calls = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    return result, calls


def test_dry_run_has_no_external_commands(tmp_path):
    result, calls = run_script(tmp_path, "-DryRun")
    assert result.returncode == 0, result.stderr
    assert "DRY_RUN" in result.stdout
    assert calls == []


def test_legacy_db_option_is_rejected_before_any_action(tmp_path):
    result, calls = run_script(tmp_path, "-Db")
    assert result.returncode != 0
    assert "DATA_IMPORT_DISABLED" in result.stderr + result.stdout
    assert calls == []


@pytest.mark.parametrize("failure", ["npm", "python", "scp"])
def test_failed_step_prevents_remote_apply_and_restart(tmp_path, failure):
    result, calls = run_script(tmp_path, failure=failure)
    assert result.returncode != 0
    assert not any("systemctl restart" in call or " apply " in call for call in calls)
    if failure in ("npm", "python"):
        assert not any(call.startswith(("ssh ", "scp ")) for call in calls)


def test_backend_release_only_sends_packages_and_helper(tmp_path):
    result, calls = run_script(tmp_path, "-Backend")
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("npm ") for call in calls)
    assert any("systemctl restart" in call for call in calls)
    uploads = [call for call in calls if call.startswith("scp ")]
    assert len(uploads) == 2
    assert all(".env" not in call and "stock_data.db" not in call for call in uploads)
    assert all("StrictHostKeyChecking=no" not in call for call in calls)
    assert all('"' not in call for call in calls if call.startswith("ssh "))
    assert any("deploy_package.py' health" in call for call in calls)


def test_frontend_release_does_not_restart_backend(tmp_path):
    result, calls = run_script(tmp_path, "-Frontend")
    assert result.returncode == 0, result.stderr
    assert any(call.startswith("npm ") for call in calls)
    assert not any("systemctl restart" in call or "backend.zip" in call for call in calls)


@pytest.mark.parametrize("failure", ["ssh", " apply ", "systemctl restart", " health"])
def test_remote_errors_are_not_reported_as_success(tmp_path, failure):
    result, calls = run_script(tmp_path, "-Backend", failure=failure)
    assert result.returncode != 0
    if failure in ("ssh", " apply "):
        assert not any("systemctl restart" in call for call in calls)


@pytest.mark.parametrize("flags", ["-RemoteDir '/.'", "-RemoteDir '//'", "-TypoOption", "-RemoteDir '/www/../'"])
def test_unsafe_or_unknown_parameters_fail_before_commands(tmp_path, flags):
    result, calls = run_script(tmp_path, flags)
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize("flags", ["-DryRun", "-Db"])
def test_existing_outer_entry_forwards_safely(tmp_path, flags):
    wrapper = SCRIPT.parents[4] / "update_server.ps1"
    if not wrapper.exists():
        pytest.skip("此检出没有外层兼容入口")
    result, calls = run_script(tmp_path, flags, script=wrapper)
    assert calls == []
    if flags == "-Db":
        assert result.returncode != 0
        assert "DATA_IMPORT_DISABLED" in result.stderr + result.stdout
    else:
        assert result.returncode == 0
        assert "DRY_RUN" in result.stdout
