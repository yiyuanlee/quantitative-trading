import asyncio
import sys
import io
from datetime import datetime, time as dt_time
import pytz

# --- 修复 Windows 终端乱码与事件循环问题 ---
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import *
import ib_insync.util as util
import pandas as pd
import numpy as np
import logging

# --- 配置区 ---
TWS_HOST = '127.0.0.1'
TWS_PORT = 7497  
CLIENT_ID = 1    

# 默认观察名单
FALLBACK_TICKERS = ['QQQ', 'VOO', 'AMD', 'NVDA', 'AAPL', 'MSFT', 'TSLA']

# 策略参数 (V9.3 优化)
FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
RSI_ENTRY = 45   # 提高入场门槛，确保动能更强
RISK_PER_TRADE = 0.02 
MAX_POSITIONS = 8     # 资金充足，增加同时持仓数
TAKE_PROFIT = 0.015   # 增加 1.5% 固定止盈，锁定日内短线利润

# --- 风控参数 ---
MIN_PRICE = 5.0        
MAX_SHARES = 50000     
MAX_DOLLAR_VALUE = 50000 

# 设置日志
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger()

class HighFreqIntradayV9:
    def __init__(self):
        self.ib = IB()
        self.active_tickers = []
        self.pending_tickers = set() # 追踪处理中的标的，防止重复下单

    def connect(self):
        try:
            self.ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
            logger.info("✅ 成功连接到 IBKR！V9.3 卓越执行版就绪。")
        except Exception as e:
            logger.error(f"❌ 连接失败: {e}")
            exit()

    def is_market_open(self):
        """严格检查美股常规交易时段 (9:30 - 16:00 EST)"""
        tz = pytz.timezone('US/Eastern')
        now = datetime.now(tz)
        if now.weekday() >= 5: return False # 周末不交易
        
        start_time = dt_time(9, 30)
        end_time = dt_time(15, 50) # 提前10分钟停止新开仓
        return start_time <= now.time() <= end_time

    def get_dynamic_universe(self):
        """扫描涨幅前20的活跃股票"""
        try:
            sub = ScannerSubscription(
                instrument='STK', 
                locationCode='STK.US.MAJOR', 
                scanCode='TOP_PERC_GAIN' 
            )
            tag_values = [TagValue('priceAbove', str(MIN_PRICE))]
            scan_data = self.ib.reqScannerData(sub, scannerSubscriptionFilterOptions=tag_values)
            
            if not scan_data:
                return FALLBACK_TICKERS
                
            raw_tickers = [cd.contractDetails.contract.symbol for cd in scan_data[:20]]
            new_tickers = [t for t in raw_tickers if not any(x in t for x in [' RT', ' WS'])]
            return new_tickers
        except Exception as e:
            logger.error(f"❌ 扫描出错: {e}")
            return FALLBACK_TICKERS

    def get_intraday_indicators(self, bars):
        df = util.df(bars)
        if df is None or df.empty: return None
        
        df['ema_fast'] = df['close'].ewm(span=FAST_EMA).mean()
        df['ema_slow'] = df['close'].ewm(span=SLOW_EMA).mean()
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean().replace(0, 0.001)
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # VWAP
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3
        df['tpv'] = df['tp'] * df['volume']
        df['vwap'] = df['tpv'].cumsum() / df['volume'].cumsum()
        
        return df.iloc[-1]

    def trade_logic(self):
        if not self.is_market_open():
            logger.info("💤 当前非美股交易时段，系统静默中...")
            return

        self.active_tickers = self.get_dynamic_universe()
        
        summary = self.ib.accountSummary()
        net_liq = float([i.value for i in summary if i.tag == 'NetLiquidation'][0])
        
        positions = self.ib.positions()
        pos_symbols = {p.contract.symbol: p for p in positions}
        
        # 清理已完成的 pending 状态
        current_orders = self.ib.openOrders()
        self.pending_tickers = {o.symbol for o in current_orders}

        for symbol in self.active_tickers:
            if len(pos_symbols) >= MAX_POSITIONS and symbol not in pos_symbols:
                continue
            
            if symbol in self.pending_tickers:
                continue # 如果已经有未成交订单，跳过

            contract = Stock(symbol, 'SMART', 'USD')
            self.ib.qualifyContracts(contract)
            
            bars = self.ib.reqHistoricalData(
                contract, endDateTime='', durationStr='1 D',
                barSizeSetting='1 min', whatToShow='ADJUSTED_LAST', useRTH=True)
            
            if not bars or len(bars) < SLOW_EMA: continue
            
            latest = self.get_intraday_indicators(bars)
            curr_price = bars[-1].close
            
            if symbol in pos_symbols:
                p = pos_symbols[symbol]
                avg_cost = p.avgCost
                
                # 离场逻辑 1: 固定止盈
                if curr_price > avg_cost * (1 + TAKE_PROFIT):
                    logger.info(f"💰 [{symbol}] 达到止盈目标，落袋为安。")
                    self.ib.placeOrder(contract, MarketOrder('SELL', abs(p.position), tif='DAY'))
                
                # 离场逻辑 2: 趋势转弱 (跌破 VWAP 或 EMA 死叉)
                elif curr_price < latest['vwap'] or latest['ema_fast'] < latest['ema_slow']:
                    logger.info(f"🚨 [{symbol}] 趋势破坏，即时平仓。")
                    self.ib.placeOrder(contract, MarketOrder('SELL', abs(p.position), tif='DAY'))
            else:
                # 入场逻辑
                if (curr_price > latest['vwap'] and 
                    latest['ema_fast'] > latest['ema_slow'] and 
                    latest['rsi'] > RSI_ENTRY):
                    
                    risk_amt = net_liq * RISK_PER_TRADE
                    size = int(risk_amt / (curr_price * 0.05)) 
                    size = min(size, MAX_SHARES)
                    if size * curr_price > MAX_DOLLAR_VALUE:
                        size = int(MAX_DOLLAR_VALUE / curr_price)

                    if size > 0:
                        logger.info(f"🔥 [{symbol}] 捕捉动能！买入 {size} 股，价格: {curr_price:.2f}")
                        self.ib.placeOrder(contract, MarketOrder('BUY', size, tif='DAY'))

    def run(self):
        self.connect()
        while True:
            try:
                self.trade_logic()
                self.ib.sleep(20) # 提高扫描频率到 20 秒
            except Exception as e:
                logger.error(f"⚠️ 异常: {e}")
                self.ib.sleep(10)

if __name__ == "__main__":
    system = HighFreqIntradayV9()
    system.run()