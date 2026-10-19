"""战法信号联合复核（基线 b5c98b7）确定性缺陷的回归测试。

每个用例对应一个已用反例证实的工程缺陷；不涉及有版本争议的业务阈值。
"""

import pytest

import modules.indicators as indicators
import modules.strategies as strategies
from modules.indicators import DailyData, TradeSignal
from modules.indicators.core import precompute_macd_sequence
from modules.strategies import StrategySignal, StrategyType, Priority, _post_process_signals


def _rows(n: int, ts_code: str = "AAA.US") -> list[dict]:
    """生成 get_kline_data 同构的 dict 行（缓慢上涨带锯齿）。"""
    rows = []
    prev_close = 100.0
    prev_vol = 1000.0
    for i in range(n):
        close = 100 + i * 0.5 + (1.0 if i % 3 == 0 else -0.5)
        vol = 1000.0 + (i % 5) * 50
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}",
                "open": close - 0.2,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "vol": vol,
                "amount": close * vol,
                "pct_chg": (close / prev_close - 1) * 100,
                "prev_close": prev_close,
                "prev_vol": prev_vol,
                "is_rise": close > prev_close,
                "is_beidou": False,
                "is_suoliang": False,
                "is_jiayin": False,
                "is_yinxian": close < prev_close,
                "is_fangliang_yinxian": False,
            }
        )
        prev_close, prev_vol = close, vol
    return rows


def _daily(n: int, close=100.0, vol=1000.0, ts_code: str = "AAA.US") -> list[DailyData]:
    return [
        DailyData(
            ts_code=ts_code,
            trade_date=f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}",
            open=close,
            high=close + 1,
            low=close - 1,
            close=close + (i % 3) * 0.1,
            vol=vol,
            amount=close * vol,
            pct_chg=0.0,
            prev_close=close,
        )
        for i in range(n)
    ]


def _run_all(monkeypatch, n: int = 40, **patches) -> list[StrategySignal]:
    monkeypatch.setattr(strategies, "get_kline_data", lambda ts_code, days=120: _rows(n))
    for name, fn in patches.items():
        monkeypatch.setattr(indicators, name, fn)
    return strategies.detect_all_strategies("AAA.US", days=n)


# ==================== MACD DIF 日期对齐 ====================


class TestMacdDifAlignment:
    def test_precomputed_dif_matches_same_day(self, monkeypatch):
        captured = {}

        def spy_s2(klines, index, *a, **k):
            captured["klines"] = klines
            return None

        monkeypatch.setattr(strategies, "detect_s2", spy_s2)
        _run_all(monkeypatch, n=80)

        klines = captured["klines"]
        expected, _, _ = precompute_macd_sequence(klines)
        for idx in (25, 40, 79):
            assert klines[idx].macd_dif == pytest.approx(expected[idx])

    def test_dynamic_fallback_matches_same_day(self):
        from modules.strategies.core import _get_macd_dif

        klines = _daily(60)
        for i, k in enumerate(klines):
            k.close = 100 + i * 0.7
        expected, _, _ = precompute_macd_sequence(klines)

        assert _get_macd_dif(klines, 59) == pytest.approx(expected[59])
        assert klines[30].macd_dif == pytest.approx(expected[30])

    def test_divergence_window_uses_kline_dates(self):
        from modules.indicators.price_patterns.complex_patterns import detect_divergence

        klines = _daily(80)
        for k in klines:
            k.close = 100.0
        # calculate_macd 的 DIF 从第 25 根开始；裁剪序列下标 5 = K 线下标 30（位于比较窗口内）
        dif_list = [1.0] * 55
        dif_list[5] = 10.0
        dif_list[-1] = 5.0

        assert detect_divergence(klines, dif_list)["is_top_divergence"] is True


# ==================== 关键K及其他来源不冒用 B1/B2 ====================


def _only(signals, prefix):
    hits = [s for s in signals if s.description.startswith(prefix)]
    assert len(hits) == 1, [s.description for s in signals]
    return hits[0]


class TestSignalSourceLabels:
    def test_plain_bearish_key_candle_is_sell(self, monkeypatch):
        key = {"is_key": True, "type": "关键阴", "direction": "顶部/滞涨阴", "confidence": 0.75}
        sig = _only(_run_all(monkeypatch, detect_key_candle=lambda k: key), "关键K")
        assert sig.action == "SELL"
        assert sig.strategy == StrategyType.S1

    def test_bullish_key_candle_is_not_b1_buy(self, monkeypatch):
        key = {"is_key": True, "type": "关键阳突破", "direction": "向上突破", "confidence": 0.90}
        sig = _only(_run_all(monkeypatch, detect_key_candle=lambda k: key), "关键K")
        assert sig.strategy == StrategyType.KEY_CANDLE
        assert sig.action == "WATCH"
        assert "B1" in sig.description

    def test_macd_dead_trap_has_own_label(self, monkeypatch):
        sig = _only(
            _run_all(monkeypatch, detect_macd_trap=lambda dif, dea: {"is_dead_trap": True}),
            "MACD 死叉多",
        )
        assert sig.strategy == StrategyType.MACD_DEAD_TRAP
        assert sig.action == "BUY"

    def test_yueyueyushi_has_own_label(self, monkeypatch):
        ready = {"is_ready": True, "confidence": 0.7, "desc": "跃跃欲试：测试"}
        sig = _only(_run_all(monkeypatch, detect_yueyueyushi=lambda k: ready), "跃跃欲试")
        assert sig.strategy == StrategyType.YUEYUE
        assert sig.action == "BUY"

    def test_build_wave_has_own_label(self, monkeypatch):
        wave = {"wave": "建仓波", "confidence": 0.6, "stats": {"gain_pct": 10}}
        sig = _only(_run_all(monkeypatch, detect_three_waves=lambda k: wave), "三波理论·建仓波")
        assert sig.strategy == StrategyType.WAVE_BUILD
        assert sig.action == "BUY"


# ==================== 后处理：去重与截断 ====================


def _sig(date, strategy=StrategyType.B1, conf=0.5, desc="B1买点", action="BUY"):
    return StrategySignal(
        ts_code="AAA.US",
        trade_date=date,
        strategy=strategy,
        confidence=conf,
        description=desc,
        action=action,
        priority=Priority.OPPORTUNITY,
    )


class TestPostProcess:
    def test_keeps_latest_three_per_type(self):
        signals = [_sig("20260104", conf=0.5), _sig("20260103", conf=0.6),
                   _sig("20260102", conf=0.7), _sig("20260101", conf=0.8)]
        dates = [s.trade_date for s in _post_process_signals(signals)]
        assert dates == ["20260104", "20260103", "20260102"]

    def test_different_sources_same_day_not_merged(self):
        signals = [_sig("20260101", conf=0.6, desc="B1买点 J=-12"),
                   _sig("20260101", conf=0.85, desc="其他来源")]
        assert len(_post_process_signals(signals)) == 2

    def test_exact_duplicate_merged(self):
        signals = [_sig("20260101", conf=0.6), _sig("20260101", conf=0.7)]
        out = _post_process_signals(signals)
        assert len(out) == 1 and out[0].confidence == 0.7


# ==================== 出货五式计数 ====================


class TestChuhuoPatternCount:
    TWO = {"total_score": 1.0, "patterns": [{"confidence": 0.9}, {"confidence": 0.8}], "is_selling": True}
    NONE = {"total_score": 0, "patterns": []}

    def test_kirin_paifa_counts_patterns(self, monkeypatch):
        import modules.indicators.kirin_detector as kd

        klines = _daily(60)
        monkeypatch.setattr(kd, "detect_chuhuo_wushi", lambda k: self.NONE)
        base = kd.detect_kirin_stage(klines)["scores"]["paifa"]
        monkeypatch.setattr(kd, "detect_chuhuo_wushi", lambda k: self.TWO)
        assert kd.detect_kirin_stage(klines)["scores"]["paifa"] == base + 30

    def test_s3_counts_patterns(self, monkeypatch):
        from modules.strategies.sell_signals import detect_s3

        klines = _daily(20)
        s1 = klines[10]
        s1.open, s1.high, s1.low, s1.close, s1.vol = 105.0, 106.0, 98.0, 99.0, 1000.0
        s1.is_fangliang_yinxian = True
        today = klines[19]
        today.close, today.vol, today.pct_chg = 104.0, 500.0, 0.0

        monkeypatch.setattr(indicators, "detect_chuhuo_wushi", lambda k: self.NONE)
        assert detect_s3(klines, 19).confidence == 0.7
        monkeypatch.setattr(indicators, "detect_chuhuo_wushi", lambda k: self.TWO)
        sig = detect_s3(klines, 19)
        assert sig.confidence == 0.85
        assert "2项" in sig.description


# ==================== TradeSignal MACD 一票否决 ====================


class TestMacdVeto:
    def _run(self, monkeypatch, macd_signals):
        import modules.indicators.volume_patterns as vp

        monkeypatch.setattr(vp, "detect_macd_signals", lambda *a: macd_signals)
        return vp.detect_trade_signal(_daily(40))

    def test_veto_does_not_block_sell(self, monkeypatch):
        assert self._run(monkeypatch, {"macd_veto": True, "is_top_divergence": True}) == TradeSignal.S2

    def test_veto_still_blocks_buy(self, monkeypatch):
        assert self._run(monkeypatch, {"macd_veto": True, "is_dead_fake": True}) == TradeSignal.WATCH


# ==================== 四块砖翻绿文案 ====================


def test_brick_flip_green_reports_previous_red_count(monkeypatch):
    import modules.indicators.price_patterns.brick as brick

    values = [10, 11, 12, 13, 14, 13]
    monkeypatch.setattr(brick, "calculate_brick_value", lambda sub: values[len(sub) - 8])
    result = brick.detect_four_brick_system(_daily(13))

    assert result["is_brick_flip_green"] is True
    assert result["brick_consecutive"] == 1
    assert "连续红砖4块后翻绿" in result["brick_action_desc"]


# ==================== 麒麟会白黄线数据不足 ====================


def test_kirin_white_yellow_unknown_when_insufficient_history():
    from modules.indicators.kirin_detector import detect_kirin_stage

    result = detect_kirin_stage(_daily(80))
    assert result["indicators"]["white_above_yellow"] is None


# ==================== 跃跃欲试：巨量按事件日前10日均量 ====================


def test_yueyueyushi_uses_each_day_prior_average():
    from modules.indicators.price_patterns.complex_patterns import detect_yueyueyushi

    klines = _daily(30, vol=100.0)
    for i in (12, 14, 16):
        klines[i].open, klines[i].close, klines[i].vol = 99.5, 100.5, 300.0
    for i in range(20, 30):
        klines[i].vol = 200.0

    result = detect_yueyueyushi(klines)
    assert result["is_ready"] is True
    assert result["count"] == 3


# ==================== 图表信号标记价格 ====================


def test_chart_markers_fall_back_to_close():
    from api.services.stock_service import _build_signal_markers

    signals = [_sig("20260102"), _sig("20260109")]
    markers = _build_signal_markers(signals, ["20260101", "20260102"], [10.0, 11.0])
    assert markers == [{"date": "20260102", "type": "B1", "price": 11.0, "action": "BUY"}]
