import type { WorkspaceMarket } from '../stores/appStore.ts';

export function assetPriceUnit(market: WorkspaceMarket, code: string, assetType?: string, currency?: string): string {
  if (assetType === 'index') return '点';
  if (market === 'CN') return 'CNY';
  return currency === 'HKD' || code.endsWith('.HK') ? 'HKD' : 'USD';
}

export function formatAssetPrice(n: number | null | undefined, market: WorkspaceMarket, code: string, assetType?: string, currency?: string): string {
  if (n == null || !Number.isFinite(n)) return '--';
  return `${formatNumber(n)} ${assetPriceUnit(market, code, assetType, currency)}`;
}

/** 兼容既有统计数值，US混合币种不标记为统一币种收益。 */
export function formatTradeTotal(value: number | null | undefined, market: WorkspaceMarket): string {
  if (value == null || !Number.isFinite(value)) return '--';
  return market === 'CN' ? formatAssetPrice(value, market, '') : `${formatNumber(value)}（原币数值，未换算）`;
}

/** 点评只突出数字，单位保留源文；任何模型文本先转义再生成受控标记。 */
export function renderCommentaryInline(text: string): string {
  const escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  return escaped.split(/(\*\*[^\n]+?\*\*)/g).map((part) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return `<strong class="text-accent-gold font-bold">${part.slice(2, -2)}</strong>`;
    }
    return part.replace(/[+-]?\d+(?:\.\d+)?%|\d+\.\d{2,}/g, (value) => {
      const color = value.endsWith('%') ? 'cyan' : /\.\d{3,}$/.test(value) ? 'purple' : 'gold';
      return `<span class="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded font-mono tabular-nums text-[11px] font-bold bg-accent-${color}/15 text-accent-${color} border border-accent-${color}/30">${value}</span>`;
    });
  }).join('').replace(/\n/g, '<br/>');
}

// 数字格式化
export function formatNumber(n: number, decimals = 2): string {
  if (n === null || n === undefined) return '--';
  return n.toFixed(decimals);
}

// 价格格式化（带 ¥ 前缀）
export function formatPrice(n: number | null | undefined): string {
  if (n === null || n === undefined) return '--';
  return `¥${n.toFixed(2)}`;
}

// 百分比格式化
export function formatPct(n: number, decimals = 2): string {
  if (n === null || n === undefined) return '--';
  return `${n >= 0 ? '+' : ''}${n.toFixed(decimals)}%`;
}

/** 策略模拟的不可计算值不能被零值或常规缺失占位符掩盖。 */
export function formatBacktestMetric(n: number | null, kind: 'number' | 'return' | 'win_rate' | 'drawdown' = 'number'): string {
  if (n === null || !Number.isFinite(n)) return '不可计算';
  if (kind === 'win_rate') return `${(n * 100).toFixed(1)}%`;
  if (kind === 'return') return formatPct(n);
  if (kind === 'drawdown') return formatPct(-Math.abs(n));
  return formatNumber(n);
}

// 涨跌颜色 class
export function pctColor(n: number): string {
  if (n > 0) return 'text-up';
  if (n < 0) return 'text-down';
  return 'text-text-secondary';
}

// 大数字缩写
export function formatVolume(n: number): string {
  if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(2)}万`;
  return n.toFixed(0);
}

/**
 * 用于 ECharts Y 轴的成交量格式化（更紧凑，无小数）
 * 3500000 → "350万"   12000000000 → "12亿"
 */
export function formatVolumeAxis(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${(n / 1e8).toFixed(abs >= 1e10 ? 0 : 1)}亿`;
  if (abs >= 1e4) return `${(n / 1e4).toFixed(0)}万`;
  return n.toFixed(0);
}

// 评分星级
export function ratingStars(rating: string): string {
  const match = rating.match(/★+/);
  return match ? match[0] : '☆☆☆☆☆';
}

// 风险等级颜色
export function riskColor(level: string): string {
  const map: Record<string, string> = {
    LOW: '#22c55e',
    MEDIUM: '#f59e0b',
    HIGH: '#f97316',
    CRITICAL: '#ef4444',
  };
  return map[level] || '#64748b';
}
