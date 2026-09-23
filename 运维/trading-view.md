
可以，TradingView 用 **Pine Script 自定义指标**就能画出同样的黄白双线，无需购买别人发布的指标。

## 1. 打开编辑器
建议使用**电脑网页版或桌面端**：

1. 打开股票的完整图表，切换到 **日线（1D）**。
2. 点击右侧工具栏的 **「Pine」**，打开 Pine 编辑器。旧版界面可能在底部。
3. 新建一个指标脚本，把默认代码全部替换为下面这段。

## 2. 粘贴双线代码

```pine
//@version=6
indicator("Z哥双线：白线与大哥线", shorttitle="ZGSX", overlay=true)

// 白线：两次10周期指数平滑
whiteLine = ta.ema(ta.ema(close, 10), 10)

// 黄线：四个周期的简单均线取平均
yellowLine = (ta.sma(close, 14) + ta.sma(close, 28) + ta.sma(close, 57) + ta.sma(close, 114)) / 4

plot(whiteLine, title="白线", color=color.white, linewidth=2)
plot(yellowLine, title="黄线（大哥线）", color=color.yellow, linewidth=2)
```

点击 **「保存」**，再点击 **「添加到图表 / Add to chart」**。不需要点击“发布脚本”。

你会看到两条线直接叠加在 K 线上：
- **白线**：短期趋势线。
- **黄线**：较中长期趋势线，又叫大哥线。

## 3. 设置时注意
- **使用普通蜡烛 K 线**，不要用 Heikin Ashi（平均 K 线），否则计算采用的收盘价会不同。
- 日线上参数代表交易日；切成周线或小时线，参数就变成相应周期。
- 黄线需要至少 **114 根 K 线**才会出现。
- 白底看不清白线，可在指标旁的 **齿轮 → 样式**里换颜色。
- 与富途对图时，保持相同股票、周期、复权和交易时段设置。

这段脚本严格按前面介绍的公式绘制，**只显示趋势线，不会自动下单，也不代表金叉必涨、死叉必跌**。

官方入口说明：[如何使用 Pine 编辑器](https://www.tradingview.com/support/solutions/43000763320-how-to-work-with-pine-editor/)。