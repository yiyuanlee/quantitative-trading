"""
AlphaFlow - 统一回测入口
===========================
所有回测通过这一个脚本运行，输出汇总表格 + Equity Curve 图。
用法: python backtest_main.py
"""

import backtrader as bt
import yfinance as yf
import pandas as pd
import numpy as np
import sys
import io
import yaml
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from datetime import datetime
from pathlib import Path

# --- 解决 Windows 终端中文显示 ---
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# --- 颜色配置 ---
GREEN = '#10B981'
RED   = '#EF4444'
ACCENT = '#6C63FF'
BG     = '#0D1117'


def load_config():
    with open('config.yaml', 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ============================================================
# 策略逻辑 (V8.1)
# ============================================================
class AlphaFlowStrategy(bt.Strategy):
    params = dict(
        fast_period=10, slow_period=25, trend_period=200,
        rsi_period=14, rsi_upper=65,
        adx_period=14, adx_threshold=20,
        atr_period=14, atr_multiplier=2.5,
        trailing_stop=0.12,
        risk_per_trade=0.015, index_multiplier=3.0,
        printlog=False,
    )

    def log(self, txt, dt=None):
        if self.params.printlog:
            dt = dt or self.datas[0].datetime.date(0)
            print(f'{dt.isoformat()} {txt}')

    def __init__(self):
        self.inds = {}
        for d in self.datas:
            self.inds[d] = {
                'ema_fast':  bt.indicators.EMA(d, period=self.p.fast_period),
                'ema_slow':  bt.indicators.EMA(d, period=self.p.slow_period),
                'ema_trend': bt.indicators.EMA(d, period=self.p.trend_period),
                'rsi':       bt.indicators.RSI(d, period=self.p.rsi_period),
                'atr':       bt.indicators.ATR(d, period=self.p.atr_period),
                'adx':       bt.indicators.ADX(d, period=self.p.adx_period),
                'crossover': bt.indicators.CrossOver(
                    bt.indicators.EMA(d, period=self.p.fast_period),
                    bt.indicators.EMA(d, period=self.p.slow_period)
                ),
                'stop_price':    None,
                'highest_price': None,
            }

    def notify_order(self, order):
        if order.status in [order.Completed]:
            d = order.data
            if order.isbuy():
                self.inds[d]['stop_price']    = order.executed.price - self.inds[d]['atr'][0] * self.p.atr_multiplier
                self.inds[d]['highest_price']   = order.executed.price
            else:
                self.inds[d]['stop_price']    = None
                self.inds[d]['highest_price'] = None

    def next(self):
        for d in self.datas:
            pos  = self.getposition(d)
            ind  = self.inds[d]
            ev   = ind['ema_fast'][0] - ind['ema_slow'][0]

            if pos:
                ind['highest_price'] = max(ind['highest_price'], d.close[0])
                # ATR 止损
                if d.close[0] < ind['stop_price']:
                    self.close(d)
                    continue
                # Trailing Stop
                if d.close[0] < ind['highest_price'] * (1.0 - self.p.trailing_stop):
                    self.close(d)
                    continue
                # 死叉离场
                if ind['crossover'] < 0:
                    self.close(d)

            else:
                trend_ok  = d.close[0] > ind['ema_trend'][0]
                cross_ok  = ind['crossover'] > 0
                rsi_ok    = ind['rsi'][0] < self.p.rsi_upper
                adx_ok    = ind['adx'][0] > self.p.adx_threshold

                if trend_ok and cross_ok and rsi_ok and adx_ok:
                    total_value = self.broker.getvalue()
                    risk_mult   = self.p.index_multiplier if d._name in ['QQQ', 'VOO'] else 1.0
                    risk_amount = total_value * self.p.risk_per_trade * risk_mult
                    atr_stop    = ind['atr'][0] * self.p.atr_multiplier
                    if atr_stop <= 0:
                        continue
                    size = int(risk_amount / atr_stop)
                    if size <= 0:
                        continue
                    self.buy(d, size=size)


# ============================================================
# 下载数据
# ============================================================
def fetch_data(ticker, start, end):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        df['openinterest'] = 0
        return df
    except Exception as e:
        print(f'  [!] {ticker} 下载失败: {e}')
        return None


# ============================================================
# 单标的回测
# ============================================================
def backtest_ticker(ticker, config):
    p = config['strategy']
    cash = config['backtest']['initial_cash']
    commission = config['backtest']['commission']
    start = config['backtest']['start_date']
    end   = config['backtest']['end_date']

    df = fetch_data(ticker, start, end)
    if df is None or len(df) < 60:
        return None

    cerebro = bt.Cerebro()
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission)
    # 允许做多
    cerebro.addsizer(bt.sizers.FixedSize)

    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data, name=ticker)

    cerebro.addstrategy(
        AlphaFlowStrategy,
        fast_period=p.get('fast_period', 10),
        slow_period=p.get('slow_period', 25),
        trend_period=p.get('trend_period', 200),
        rsi_period=p.get('rsi_period', 14),
        rsi_upper=p.get('rsi_upper', 65),
        adx_period=p.get('adx_period', 14),
        adx_threshold=p.get('adx_threshold', 20),
        atr_period=p.get('atr_period', 14),
        atr_multiplier=p.get('atr_multiplier', 2.5),
        trailing_stop=p.get('trailing_stop', 0.12),
        risk_per_trade=config['risk'].get('risk_per_trade', 0.015),
        index_multiplier=config['risk'].get('index_multiplier', 3.0),
    )

    initial = cerebro.broker.getvalue()
    cerebro.run()
    final   = cerebro.broker.getvalue()
    ret     = (final - initial) / initial * 100

    return {
        'ticker':         ticker,
        'return':         ret,
        'final_value':    final,
        'trades':         sum(1 for p in cerebro.broker.orders if p.execstatus in [bt.Order.Executed]),
    }


# ============================================================
# 汇总所有标的
# ============================================================
def run_backtest(config):
    tickers = config['tickers']
    results = []

    print('\n' + '='*55)
    print('  AlphaFlow V8.1 回测中...')
    print('='*55)

    for t in tickers:
        r = backtest_ticker(t, config)
        if r:
            results.append(r)

    return results


# ============================================================
# 打印汇总表格
# ============================================================
def print_summary(results):
    if not results:
        print('No results.')
        return

    # 计算指标（简单估算，基于最终权益）
    # 夏普/回撤需要逐笔数据，这里用简化版
    for r in results:
        flag = '🟢' if r['return'] >= 0 else '🔴'

    print()
    print(f'{'标的':<8} {'收益率':>10} {'状态':>4}')
    print('-'*30)
    for r in results:
        flag = '🟢' if r['return'] >= 0 else '🔴'
        print(f'{r["ticker"]:<8} {r["return"]:>+8.2f}%  {flag}')
    print('-'*30)
    avg = np.mean([r['return'] for r in results])
    print(f'{'平均':<8} {avg:>+8.2f}%')

    # 找出最佳标的
    best = max(results, key=lambda x: x['return'])
    print(f'\n🏆 最佳标的: {best["ticker"]} ({best["return"]:+.2f}%)')


# ============================================================
# 生成 Equity Curve 图
# ============================================================
def plot_equity_curves(results, config):
    cash   = config['backtest']['initial_cash']
    tickers = [r['ticker'] for r in results]

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    colors = ['#6C63FF', '#10B981', '#F59E0B', '#EF4444', '#3B82F6', '#8B5CF6', '#EC4899', '#14B8A6']

    for i, r in enumerate(results):
        ret = r['return']
        final_val = cash * (1 + ret / 100)
        # 简化权益曲线：假设线性增长
        # 真实曲线需要逐日数据，这里用起始-终止线段表示
        label = f'{r["ticker"]} {ret:+.1f}%'
        ax.bar(i, ret, color=colors[i % len(colors)], label=label, width=0.6)

    ax.axhline(0, color='white', linewidth=0.5, linestyle='--')
    ax.set_xticks(range(len(tickers)))
    ax.set_xticklabels(tickers, color='white', fontsize=11)
    ax.tick_params(colors='white')
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.set_ylabel('收益率 (%)', color='white', fontsize=10)
    ax.set_title('AlphaFlow V8.1 回测收益对比', color='white', fontsize=13, pad=12)

    legend = ax.legend(
        loc='upper right', framealpha=0.2,
        labelcolor='white', fontsize=9,
        facecolor='#1C1C1C', edgecolor='none'
    )
    legend.get_frame().set_facecolor('#1C1C1C')

    for spine in ax.spines.values():
        spine.set_edgecolor('#333')
    ax.tick_params(colors='white')

    plt.tight_layout()
    out_path = Path('equity_curve.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight',
                facecolor=BG, edgecolor='none')
    plt.close()
    print(f'\n📈 Equity Curve 已保存: {out_path.resolve()}')


# ============================================================
# 主入口
# ============================================================
if __name__ == '__main__':
    config = load_config()
    results = run_backtest(config)
    print_summary(results)
    plot_equity_curves(results, config)
    print('\n✅ 回测完成！')