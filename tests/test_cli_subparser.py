"""
P1-2 回归测试：5 个独立 main() 合并到 cli.py

- 7 个顶层 subcommand 都可识别（analyze/screen/score/workflow/diagnose/watchlist/sync）
- watchlist 5 个子动作（add/remove/list/scan/report）
- sync 4 个子动作（init/sync/status/stk-factor）
- 顶层 subcommand 缺失时 exit 非 0
- 全局 prog = "zt"
"""

import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_zt(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "modules.cli", *args],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(PROJECT_ROOT),
    )


@pytest.mark.parametrize("options,selected", [([], "US"), (["--market", "US"], "US"), (["--market", "CN"], "CN")])
def test_cli_binds_market_and_restores_calling_context(temp_db, monkeypatch, options, selected):
    from modules import cli
    from modules.database import get_db_path, get_market, market_context

    observed = []
    monkeypatch.setattr(cli, "cmd_watchlist", lambda args: observed.append((get_market(), get_db_path())))
    monkeypatch.setattr(sys, "argv", ["zt", *options, "watchlist", "list"])
    expected_path = get_db_path(selected)
    with market_context("CN"):
        cli.main()
        assert get_market() == "CN"
    assert observed == [(selected, expected_path)]
    assert get_market() == "US"


def test_cli_invalid_market_rejected_before_handler(temp_db, monkeypatch):
    from modules import cli

    calls = []
    monkeypatch.setattr(cli, "cmd_watchlist", lambda args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["zt", "--market", "EU", "watchlist", "list"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert not calls


@pytest.mark.parametrize("entry", ["cli", "data_sync"])
@pytest.mark.parametrize("action", ["init", "sync", "stk-factor"])
def test_cn_legacy_sync_entry_refuses_unready_writes(entry, action, monkeypatch):
    from modules import cli, data_sync
    from modules.database import get_db_path, get_market, market_context

    monkeypatch.setattr(data_sync, "DataSyncer", lambda: pytest.fail("不得初始化旧Tushare客户端"))
    argv = (["zt", "--market", "CN", "sync", action] if entry == "cli"
            else ["data-sync", action, "--market", "CN"])
    monkeypatch.setattr(sys, "argv", argv)
    with market_context("US"), pytest.raises(ValueError, match="CN.*尚未就绪"):
        (cli if entry == "cli" else data_sync).main()
    assert get_market() == "US"
    assert not get_db_path("CN").exists()


@pytest.mark.parametrize("entry", ["cli", "data_sync"])
@pytest.mark.parametrize("single,skip", [(True, False), (True, True), (False, False)])
def test_us_sync_entry_uses_workspace_adapter(temp_db, entry, single, skip, monkeypatch):
    from modules import cli, data_sync
    from modules.database import get_market, market_context
    from api.services import sync_service

    calls = []
    monkeypatch.setattr(data_sync, "DataSyncer", lambda: pytest.fail("不得初始化旧Tushare客户端"))
    monkeypatch.setattr(sync_service, "collect_us_hk_codes", lambda: ["AAPL.US", "00700.HK"])
    monkeypatch.setattr(data_sync, "sync_workspace_stocks", lambda codes, days, mode: calls.append((get_market(), codes, mode, days)) or {})
    argv = (["zt", "sync", "sync"] + (["AAPL.US"] if single else []) if entry == "cli"
            else ["data-sync", "sync"] + (["--ts_code", "AAPL.US"] if single else []))
    monkeypatch.setattr(sys, "argv", argv + (["--skip-indicators"] if skip else []))
    with market_context("CN"):
        (cli if entry == "cli" else data_sync).main()
        assert get_market() == "CN"
    assert calls == [("US", ["AAPL.US"] if single else ["AAPL.US", "00700.HK"],
                      "daily_and_compute" if single and not skip else "daily", 730)]


@pytest.mark.parametrize("entry", ["cli", "data_sync"])
def test_us_sync_status_needs_no_tushare(temp_db, entry, monkeypatch, capsys):
    from modules import cli, data_sync

    monkeypatch.setattr(data_sync, "DataSyncer", lambda: pytest.fail("不得初始化旧Tushare客户端"))
    monkeypatch.setattr(sys, "argv", ["zt", "sync", "status"] if entry == "cli" else ["data-sync", "status"])
    (cli if entry == "cli" else data_sync).main()
    assert "US" in capsys.readouterr().out


# ==================== 顶层 subcommand ====================

EXPECTED_TOP_COMMANDS = ["analyze", "screen", "score", "workflow", "diagnose", "watchlist", "sync"]


def test_top_level_help_lists_all_seven_commands():
    """zt --help 必须列出 7 个顶层 subcommand"""
    result = run_zt("--help")
    assert result.returncode == 0
    for cmd in EXPECTED_TOP_COMMANDS:
        assert cmd in result.stdout, f"--help 缺 {cmd}"


@pytest.mark.parametrize("cmd", EXPECTED_TOP_COMMANDS)
def test_each_top_command_has_help(cmd):
    """每个顶层 subcommand 必须支持 --help"""
    result = run_zt(cmd, "--help")
    assert (
        result.returncode == 0
    ), f"{cmd} --help exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"


def test_missing_command_exits_nonzero():
    """不传任何 subcommand 应该 exit 非 0（required=True）"""
    result = run_zt()
    assert result.returncode != 0
    # argparse 应该打印 usage + 错误到 stderr
    assert "usage:" in result.stderr or "usage:" in result.stdout


def test_prog_is_zt():
    """顶层 ArgumentParser 的 prog 必须是 zt"""
    result = run_zt("--help")
    assert "usage: zt " in result.stdout


# ==================== watchlist 子动作 ====================

EXPECTED_WL_ACTIONS = ["add", "remove", "list", "scan", "report"]


def test_watchlist_help_lists_all_five_actions():
    """zt watchlist --help 必须列出 5 个 action（含新增 report）"""
    result = run_zt("watchlist", "--help")
    assert result.returncode == 0
    for action in EXPECTED_WL_ACTIONS:
        assert action in result.stdout, f"watchlist --help 缺 {action}"


# ==================== sync 子动作 ====================

EXPECTED_SYNC_ACTIONS = ["init", "sync", "status", "stk-factor"]


def test_sync_help_lists_all_four_actions():
    """zt sync --help 必须列出 4 个 action"""
    result = run_zt("sync", "--help")
    assert result.returncode == 0
    for action in EXPECTED_SYNC_ACTIONS:
        assert action in result.stdout, f"sync --help 缺 {action}"


def test_sync_init_help():
    """zt sync init --help 必须 exit 0"""
    result = run_zt("sync", "init", "--help")
    assert result.returncode == 0


def test_sync_sync_help():
    """zt sync sync --help 必须 exit 0"""
    result = run_zt("sync", "sync", "--help")
    assert result.returncode == 0
    # ts_code 是位置参数（nargs='?'），不是 --ts_code flag
    # 其余是 flag
    for flag in ("ts_code", "--days", "--indicators", "--skip-indicators"):
        assert flag in result.stdout, f"sync sync --help 缺 {flag}"


def test_sync_stk_factor_help():
    """zt sync stk-factor --help 必须 exit 0"""
    result = run_zt("sync", "stk-factor", "--help")
    assert result.returncode == 0


# ==================== screen 11 种 strategy 仍被接受 ====================

STRATEGY_ALIAS = {
    "B1": "b1",
    "B2": "b2_breakout",
    "B3": "b3_consensus",
    "完美图形": "perfect",
    "超级B1": "super_b1",
    "长安战法": "changan",
    "建仓波": "build_wave",
    "吸筹": "xishou",
    "安全": "safe",
    "超跌": "oversold",
    "突破": "breakout",
}


@pytest.mark.parametrize("strategy", STRATEGY_ALIAS.keys())
def test_screen_accepts_all_strategies_via_cli(strategy):
    """screen --help 应该列出所有 11 种 strategy"""
    result = run_zt("screen", "--help")
    assert result.returncode == 0
    assert strategy in result.stdout, f"screen --help 缺 {strategy}"


# ==================== analyze / diagnose / score / workflow 必要 flag ====================


def test_analyze_help_has_required_args():
    result = run_zt("analyze", "--help")
    assert result.returncode == 0
    assert "ts_code" in result.stdout
    assert "--days" in result.stdout


def test_diagnose_help_has_required_args():
    result = run_zt("diagnose", "--help")
    assert result.returncode == 0
    assert "ts_code" in result.stdout
    assert "--days" in result.stdout


def test_score_help_has_ts_code():
    result = run_zt("score", "--help")
    assert result.returncode == 0
    assert "ts_code" in result.stdout


def test_workflow_help_works():
    """workflow 无参数，但 --help 应该 exit 0"""
    result = run_zt("workflow", "--help")
    assert result.returncode == 0


# ==================== zt 整体可用性 smoke ====================


def test_zt_help_does_not_crash():
    """最简单 smoke：zt --help 完整跑通"""
    result = run_zt("--help")
    assert result.returncode == 0
    # 应该看到 epilog 中的示例
    assert "zt analyze" in result.stdout
    assert "zt sync" in result.stdout
