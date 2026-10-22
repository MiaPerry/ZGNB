import ReactECharts from 'echarts-for-react';
import type { KlineChart as KlineDataType } from '../../api/types';
import { SIGNAL_COLORS } from '../../lib/constants';
import { assetPriceUnit, formatVolumeAxis, formatNumber } from '../../lib/formatters';
import type { WorkspaceMarket } from '../../stores/appStore';

interface Props {
  data: KlineDataType;
  market: WorkspaceMarket;
  height?: number;
}

export default function KlineChart({ data, market, height = 820 }: Props) {
  const { dates, ohlc, volumes, pct_chgs, overlays, signal_markers, kdj, macd, brick } = data;
  const priceUnit = assetPriceUnit(market, data.ts_code, data.asset_type, data.currency);
  const priceTooltip = {
    valueFormatter: (value: number | string | (number | string)[]) => {
      const format = (item: number | string) => typeof item === 'number' ? `${formatNumber(item)} ${priceUnit}` : item;
      return Array.isArray(value) ? value.map(format).join(' / ') : format(value);
    },
  };

  const upColor = '#ef4444';
  const downColor = '#22c55e';

  // 取每个 series 的最后一个非 null 值，用作右侧点位标签
  const lastValid = (arr: (number | null)[]): number | null => {
    for (let i = arr.length - 1; i >= 0; i--) {
      if (arr[i] !== null && arr[i] !== undefined) return arr[i];
    }
    return null;
  };

  const lastWhite = lastValid(overlays.white_line);
  const lastYellow = lastValid(overlays.yellow_line);
  const lastBbi = lastValid(overlays.bbi);
  const lastDate = dates[dates.length - 1];

  // 构建 markPoint 数据
  const buyMarkers = signal_markers
    .filter((m) => m.action === 'BUY')
    .map((m) => ({
      name: m.type,
      coord: [m.date, m.price],
      value: m.type,
      itemStyle: { color: SIGNAL_COLORS[m.type] || SIGNAL_COLORS.BUY },
    }));

  const sellMarkers = signal_markers
    .filter((m) => m.action === 'SELL')
    .map((m) => ({
      name: m.type,
      coord: [m.date, m.price],
      value: m.type,
      itemStyle: { color: SIGNAL_COLORS[m.type] || SIGNAL_COLORS.SELL },
    }));

  const option = {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#1a2236',
      borderColor: '#2a3a52',
      textStyle: { color: '#e2e8f0', fontSize: 11 },
    },
    legend: {
      data: ['K线', '白线', '黄线', 'BBI', '布林上', '布林中', '布林下'],
      top: 0,
      textStyle: { color: '#94a3b8', fontSize: 11 },
      itemWidth: 14,
      itemHeight: 2,
    },
    grid: [
      { left: 60, right: 100, top: 30, height: '34%' },
      { left: 60, right: 20, top: '38%', height: '11%' },
      { left: 60, right: 20, top: '53%', height: '13%' },
      { left: 60, right: 20, top: '69%', height: '13%' },
      { left: 60, right: 20, top: '85%', height: '12%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        gridIndex: 0,
        axisLine: { lineStyle: { color: '#2a3a52' } },
        axisLabel: { color: '#64748b', fontSize: 10 },
        splitLine: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 1,
        axisLine: { lineStyle: { color: '#2a3a52' } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 2,
        axisLine: { lineStyle: { color: '#2a3a52' } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 3,
        axisLine: { lineStyle: { color: '#2a3a52' } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 4,
        axisLine: { lineStyle: { color: '#2a3a52' } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        gridIndex: 0,
        name: priceUnit,
        nameTextStyle: { color: '#64748b', fontSize: 10 },
        splitLine: { lineStyle: { color: '#1e293b' } },
        axisLabel: { color: '#64748b', fontSize: 10 },
        axisLine: { lineStyle: { color: '#2a3a52' } },
      },
      {
        // 成交量 Y 轴 — 万 / 亿 缩写
        scale: true,
        gridIndex: 1,
        splitLine: { show: false },
        axisLabel: {
          color: '#64748b',
          fontSize: 10,
          formatter: (v: number) => formatVolumeAxis(v),
        },
        axisLine: { lineStyle: { color: '#2a3a52' } },
      },
      {
        scale: true,
        gridIndex: 2,
        splitLine: { lineStyle: { color: '#1e293b' } },
        axisLabel: { color: '#64748b', fontSize: 10 },
        axisLine: { lineStyle: { color: '#2a3a52' } },
      },
      {
        scale: true,
        gridIndex: 3,
        splitLine: { lineStyle: { color: '#1e293b' } },
        axisLabel: { color: '#64748b', fontSize: 10 },
        axisLine: { lineStyle: { color: '#2a3a52' } },
      },
      {
        scale: true,
        gridIndex: 4,
        splitLine: { lineStyle: { color: '#1e293b' } },
        axisLabel: { color: '#64748b', fontSize: 10 },
        axisLine: { lineStyle: { color: '#2a3a52' } },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2, 3, 4], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2, 3, 4], bottom: 5, height: 15, borderColor: '#2a3a52', fillerColor: 'rgba(245,158,11,0.1)', textStyle: { color: '#64748b' } },
    ],
    series: [
      {
        name: 'K线',
        tooltip: priceTooltip,
        type: 'candlestick',
        data: ohlc,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: upColor,
          color0: downColor,
          borderColor: upColor,
          borderColor0: downColor,
        },
        markPoint: {
          symbol: 'triangle',
          symbolSize: 10,
          data: [
            ...buyMarkers.map((m) => ({
              ...m,
              symbol: 'triangle',
              symbolRotate: 0,
              symbolOffset: [0, 10],
            })),
            ...sellMarkers.map((m) => ({
              ...m,
              symbol: 'triangle',
              symbolRotate: 180,
              symbolOffset: [0, -10],
            })),
          ],
          label: { show: false },
        },
      },
      // 白线 (EMA(EMA(C,10),10)) - 短期动能线
      {
        name: '白线',
        tooltip: priceTooltip,
        type: 'line',
        data: overlays.white_line,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 2, color: '#ffffff' },
        symbol: 'none',
        // 右侧点位标签
        markPoint: lastWhite !== null ? {
          symbol: 'roundRect',
          symbolSize: [76, 18],
          symbolOffset: [44, 0],
          data: [{
            coord: [lastDate, lastWhite],
            value: `${formatNumber(lastWhite)} ${priceUnit}`,
            itemStyle: { color: '#0b0f19', borderColor: '#ffffff', borderWidth: 1 },
            label: { color: '#ffffff', fontSize: 10, fontWeight: 'bold' },
          }],
        } : undefined,
      },
      // 黄线 ((MA14+MA28+MA57+MA114)/4) - 多空生命线
      {
        name: '黄线',
        tooltip: priceTooltip,
        type: 'line',
        data: overlays.yellow_line,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 2, color: '#fbbf24' },
        symbol: 'none',
        markPoint: lastYellow !== null ? {
          symbol: 'roundRect',
          symbolSize: [76, 18],
          symbolOffset: [44, 0],
          data: [{
            coord: [lastDate, lastYellow],
            value: `${formatNumber(lastYellow)} ${priceUnit}`,
            itemStyle: { color: '#0b0f19', borderColor: '#fbbf24', borderWidth: 1 },
            label: { color: '#fbbf24', fontSize: 10, fontWeight: 'bold' },
          }],
        } : undefined,
      },
      // BBI 多空指数
      {
        name: 'BBI',
        tooltip: priceTooltip,
        type: 'line',
        data: overlays.bbi,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 1.5, color: '#06b6d4', type: 'dashed' },
        symbol: 'none',
        markPoint: lastBbi !== null ? {
          symbol: 'roundRect',
          symbolSize: [76, 18],
          symbolOffset: [44, 0],
          data: [{
            coord: [lastDate, lastBbi],
            value: `${formatNumber(lastBbi)} ${priceUnit}`,
            itemStyle: { color: '#0b0f19', borderColor: '#06b6d4', borderWidth: 1 },
            label: { color: '#06b6d4', fontSize: 10, fontWeight: 'bold' },
          }],
        } : undefined,
      },
      // 布林上轨
      {
        name: '布林上',
        tooltip: priceTooltip,
        type: 'line',
        data: overlays.boll_upper,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 1, color: '#a855f7', type: 'dotted', opacity: 0.6 },
        symbol: 'none',
      },
      // 布林中轨
      {
        name: '布林中',
        tooltip: priceTooltip,
        type: 'line',
        data: overlays.boll_mid,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 1, color: '#a855f7', type: 'dotted', opacity: 0.6 },
        symbol: 'none',
      },
      // 布林下轨
      {
        name: '布林下',
        tooltip: priceTooltip,
        type: 'line',
        data: overlays.boll_lower,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 1, color: '#a855f7', type: 'dotted', opacity: 0.6 },
        symbol: 'none',
      },
      // 成交量
      {
        name: data.asset_type === 'index' ? '成交量（源数据）' : '成交量（股）',
        type: 'bar',
        data: volumes.map((v, i) => ({
          value: v,
          itemStyle: { color: pct_chgs[i] >= 0 ? `${upColor}80` : `${downColor}80` },
        })),
        xAxisIndex: 1,
        yAxisIndex: 1,
      },
      // KDJ
      {
        name: 'K',
        type: 'line',
        data: kdj.k,
        xAxisIndex: 2,
        yAxisIndex: 2,
        smooth: true,
        lineStyle: { width: 1, color: '#f59e0b' },
        symbol: 'none',
      },
      {
        name: 'D',
        type: 'line',
        data: kdj.d,
        xAxisIndex: 2,
        yAxisIndex: 2,
        smooth: true,
        lineStyle: { width: 1, color: '#3b82f6' },
        symbol: 'none',
      },
      {
        name: 'J',
        type: 'line',
        data: kdj.j,
        xAxisIndex: 2,
        yAxisIndex: 2,
        smooth: true,
        lineStyle: { width: 1, color: '#a855f7' },
        symbol: 'none',
      },
      // MACD
      {
        name: 'DIF',
        type: 'line',
        data: macd.dif,
        xAxisIndex: 3,
        yAxisIndex: 3,
        smooth: true,
        lineStyle: { width: 1, color: '#f59e0b' },
        symbol: 'none',
      },
      {
        name: 'DEA',
        type: 'line',
        data: macd.dea,
        xAxisIndex: 3,
        yAxisIndex: 3,
        smooth: true,
        lineStyle: { width: 1, color: '#3b82f6' },
        symbol: 'none',
      },
      {
        name: 'MACD',
        type: 'bar',
        data: macd.hist.map((v) => ({
          value: v,
          itemStyle: { color: (v ?? 0) >= 0 ? `${upColor}80` : `${downColor}80` },
        })),
        xAxisIndex: 3,
        yAxisIndex: 3,
      },
      // 砖型图 - 红绿柱子
      {
        name: '砖型图',
        type: 'bar',
        data: (brick.values || []).map((v, i) => {
          const color = (brick.colors || [])[i];
          const barColor = color === 1 ? upColor : color === -1 ? downColor : '#475569';
          return {
            value: v,
            itemStyle: { color: barColor },
          };
        }),
        xAxisIndex: 4,
        yAxisIndex: 4,
        barWidth: '60%',
      },
      // 砖型图 - 折线叠加
      {
        name: '砖值',
        type: 'line',
        data: brick.values,
        xAxisIndex: 4,
        yAxisIndex: 4,
        smooth: true,
        lineStyle: { width: 2, color: '#f59e0b' },
        symbol: 'none',
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} notMerge />;
}
