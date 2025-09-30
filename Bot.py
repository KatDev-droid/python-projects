# mt5_checker_gui_multi.py
"""
MT5 Multi-Symbol Checklist Monitor
- H1: RSI(5) levels 10/50/90, Fisher Transform(period=10)
- Detect SR breach, RSI leaving Fisher behind, H1 closes < 50 in specific hours
- Then monitor M15 for retrace to 50 EMA after previous H1 close below 50
- Multi-symbol GUI tabs with checkboxes + log + notifications
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import threading
from datetime import datetime, timedelta
import ta
import tkinter as tk
from tkinter import ttk

# optional toast (Windows notifications)
try:
    from win10toast import ToastNotifier
    toaster = ToastNotifier()
except Exception:
    toaster = None

# ----------------- CONFIG -----------------
ACCOUNT = 0           # put your account number if you want
PASSWORD = ""         # password
SERVER = ""           # broker server
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]  # add more pairs here
H1_LOOKBACK = 120
FISHER_PERIOD = 10
RSI_PERIOD = 5
FISHER_RSI_GAP = 0.6
SR_LOOKBACK = 20
M15_LOOKBACK = 200
EMA50_M15_PERIOD = 50
SMA200_PERIOD = 200
SMA400_PERIOD = 400
BOLL_WINDOW = 20
CHECK_INTERVAL = 30   # seconds
# ------------------------------------------

# Fisher Transform
def fisher_transform(series, period=10):
    price = series.values
    n = len(price)
    val = np.zeros(n)
    filt = np.zeros(n)
    fisher = np.full(n, np.nan)
    prev_val = 0.0
    for i in range(n):
        if i < period:
            continue
        window = price[i - period + 1:i + 1]
        max_h = np.max(window)
        min_l = np.min(window)
        if max_h - min_l == 0:
            raw = 0.0
        else:
            raw = 0.33 * 2 * ((price[i] - min_l) / (max_h - min_l) - 0.5) + 0.67 * prev_val
        raw = np.clip(raw, -0.999, 0.999)
        prev_val = raw
        fisher_val = 0.5 * np.log((1 + raw) / (1 - raw))
        filt[i] = 0.5 * fisher_val + 0.5 * filt[i - 1] if i > period else fisher_val
        fisher[i] = filt[i]
    return pd.Series(fisher, index=series.index)

# MT5 helpers
def mt5_connect():
    if not mt5.initialize():
        print("mt5.initialize() failed:", mt5.last_error())
        return False
    if ACCOUNT and PASSWORD and SERVER:
        ok = mt5.login(ACCOUNT, password=PASSWORD, server=SERVER)
        if not ok:
            print("mt5.login() failed:", mt5.last_error())
    else:
        print("No login credentials in config — make sure MT5 terminal is logged in manually.")
    return True

def mt5_shutdown():
    try:
        mt5.shutdown()
    except Exception:
        pass

def fetch_ohlc(symbol, timeframe, n):
    utc_to = datetime.utcnow()
    minutes_per_bar = {
        mt5.TIMEFRAME_M1: 1, mt5.TIMEFRAME_M5: 5, mt5.TIMEFRAME_M15: 15,
        mt5.TIMEFRAME_H1: 60, mt5.TIMEFRAME_D1: 1440
    }.get(timeframe, 60)
    utc_from = utc_to - timedelta(minutes=n * minutes_per_bar)
    rates = mt5.copy_rates_range(symbol, timeframe, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    return df

# Indicator calculators
def compute_h1_indicators(df_h1):
    df = df_h1.copy()
    df['rsi'] = ta.momentum.RSIIndicator(close=df['close'], window=RSI_PERIOD).rsi()
    df['fisher'] = fisher_transform(df['close'], period=FISHER_PERIOD)
    return df

def compute_m15_indicators(df_m15):
    df = df_m15.copy()
    df['ema50'] = df['close'].ewm(span=EMA50_M15_PERIOD, adjust=False).mean()
    df['sma200'] = df['close'].rolling(window=SMA200_PERIOD).mean()
    df['sma400'] = df['close'].rolling(window=SMA400_PERIOD).mean()
    bb = ta.volatility.BollingerBands(close=df['close'], window=BOLL_WINDOW, window_dev=2)
    df['bb_h'] = bb.bollinger_hband()
    df['bb_l'] = bb.bollinger_lband()
    return df

# Minor SR detection
def detect_minor_sr_breach(df_h1, lookback=SR_LOOKBACK):
    if len(df_h1) < lookback + 2:
        return (None, None, 0.0)
    recent = df_h1.iloc[-lookback-1:-1]
    prev_high = recent['high'].max()
    prev_low = recent['low'].min()
    last = df_h1.iloc[-1]
    small_pct = 0.0005
    if last['high'] > prev_high * (1 + small_pct):
        return ('resistance', float(prev_high), float(last['high'] - prev_high))
    if last['low'] < prev_low * (1 - small_pct):
        return ('support', float(prev_low), float(prev_low - last['low']))
    return (None, None, 0.0)

# Checklist per symbol
class ChecklistMonitor:
    def __init__(self, symbol, parent_notebook):
        self.symbol = symbol
        self.h1_sr_breached = False
        self.h1_left_fisher = False
        self.h1_last_close_below_50 = False
        self.h1_time_ok = False
        self.m15_retrace_ok = False

        # GUI tab
        self.frame = ttk.Frame(parent_notebook, padding=10)
        parent_notebook.add(self.frame, text=symbol)
        self.vars = {}
        labels = [
            "H1: SR breach",
            "H1: RSI leaves Fisher",
            "H1: RSI close < 50",
            "M15: Retrace to 50 EMA (200/400 side + BB ok)",
            "ALL conditions met"
        ]
        for i, txt in enumerate(labels):
            var = tk.IntVar(value=0)
            chk = ttk.Checkbutton(self.frame, text=txt, variable=var)
            chk.grid(row=i, column=0, sticky="w", pady=2)
            self.vars[i] = var
        self.status = tk.Text(self.frame, height=10, width=70)
        self.status.grid(row=len(labels), column=0, pady=6)
        btn = ttk.Button(self.frame, text="Start Monitoring", command=self.start_thread)
        btn.grid(row=len(labels)+1, column=0, pady=4)

    def log(self, *args):
        t = datetime.utcnow().strftime("%H:%M:%S")
        msg = " ".join(str(a) for a in args)
        self.status.insert("end", f"[{t}] {msg}\n")
        self.status.see("end")

    def notify(self, title, message):
        self.log(title, "-", message)
        if toaster:
            try:
                toaster.show_toast(f"{self.symbol} - {title}", message, threaded=True, duration=5)
            except Exception:
                pass

    def start_thread(self):
        threading.Thread(target=self.monitor_loop, daemon=True).start()
        self.log("Started monitoring...")

    def monitor_loop(self):
        while True:
            try:
                df_h1 = fetch_ohlc(self.symbol, mt5.TIMEFRAME_H1, H1_LOOKBACK)
                if df_h1 is None:
                    self.log("No H1 data yet")
                    time.sleep(CHECK_INTERVAL)
                    continue
                hin = compute_h1_indicators(df_h1)
                sr_type, _, _ = detect_minor_sr_breach(df_h1)
                self.h1_sr_breached = bool(sr_type)
                self.vars[0].set(1 if self.h1_sr_breached else 0)

                last_h1 = hin.iloc[-1]
                gap = last_h1['rsi'] - last_h1['fisher']
                self.h1_left_fisher = abs(gap) >= FISHER_RSI_GAP
                self.vars[1].set(1 if self.h1_left_fisher else 0)

                self.h1_last_close_below_50 = last_h1['rsi'] < 50
                self.vars[2].set(1 if self.h1_last_close_below_50 else 0)

                hour = last_h1.name.hour
                self.h1_time_ok = (9 <= hour <= 10) or (14 <= hour <= 17)

                if self.h1_sr_breached and self.h1_left_fisher and self.h1_last_close_below_50 and self.h1_time_ok:
                    self.notify("H1", "Conditions met, checking M15...")
                    self.monitor_m15()
                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                self.log("Error:", e)
                time.sleep(CHECK_INTERVAL)

    def monitor_m15(self):
        df_m15 = fetch_ohlc(self.symbol, mt5.TIMEFRAME_M15, M15_LOOKBACK)
        if df_m15 is None:
            return
        m15 = compute_m15_indicators(df_m15)
        last = m15.iloc[-1]
        price, ema50 = last['close'], last['ema50']
        sma200, sma400 = last['sma200'], last['sma400']
        bb_h, bb_l = last['bb_h'], last['bb_l']
        if np.isnan([ema50, sma200, sma400, bb_h, bb_l]).any():
            return
        sma_side_ok = sma200 > sma400
        not_out_bands = bb_l < price < bb_h
        touched_ema = abs(price - ema50) <= 0.0004
        if sma_side_ok and not_out_bands and touched_ema:
            self.vars[3].set(1)
            self.vars[4].set(1)
            self.notify("M15", "Retrace confirmed, ALL conditions met!")

# -------------------- main --------------------
def main():
    if not mt5_connect():
        print("MT5 failed to init. Log into your terminal first.")
        return
    root = tk.Tk()
    root.title("MT5 Multi-Symbol Checklist")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    monitors = [ChecklistMonitor(sym, notebook) for sym in SYMBOLS]
    root.mainloop()
    mt5_shutdown()

if __name__ == "__main__":
    main()
