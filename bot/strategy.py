"""Shared strategy logic: RSI mean-reversion DCA with equity-fraction sizing.

Used by both backtest.py (simulation) and bot.py (testnet/live).
All sizing is a function of CURRENT equity -> compounding is automatic.
"""

# ---- Tunables (kept in one place) ----
RSI_PERIOD = 14
RSI_BUY = 32.0          # enter when RSI crosses below this
BASE_FRACTION = 0.50    # % of current quote equity used for the base buy
SAFETY_DROP = 0.03      # DCA safety order if price falls 3% below base entry
TAKE_PROFIT = 0.02      # sell all at +2% above average entry
STOP_LOSS = 0.04        # sell all at -4% below average entry
CIRCUIT_BREAKER = 0.35  # halt bot if equity falls 35% below session start
FEE = 0.001             # 0.1% taker fee per side (Binance spot default)


def rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI. Returns None until enough data."""
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


class Position:
    """Tracks an open position (base buy + optional one safety order)."""

    def __init__(self):
        self.qty = 0.0
        self.cost = 0.0        # total quote spent (incl. fees)
        self.base_price = None # price of the initial entry
        self.safety_used = False

    @property
    def open(self):
        return self.qty > 0

    @property
    def avg_entry(self):
        return self.cost / self.qty if self.qty else 0.0

    def add_fill(self, qty, price):
        self.cost += qty * price * (1 + FEE)
        self.qty += qty
        if self.base_price is None:
            self.base_price = price


def decide(pos, price, rsi_prev, rsi_now, quote_balance):
    """Pure decision function. Returns (action, quote_amount_or_None).

    action in {"BUY_BASE", "BUY_SAFETY", "SELL_TP", "SELL_SL", "HOLD"}.
    quote_amount is how much quote currency (USDT) to spend on a buy.
    """
    if pos.open:
        if price >= pos.avg_entry * (1 + TAKE_PROFIT):
            return "SELL_TP", None
        if price <= pos.avg_entry * (1 - STOP_LOSS):
            return "SELL_SL", None
        if not pos.safety_used and price <= pos.base_price * (1 - SAFETY_DROP):
            return "BUY_SAFETY", quote_balance * 0.98  # spend the rest, keep fee dust
        return "HOLD", None

    # no position: enter on RSI crossing down through the threshold
    if rsi_prev is not None and rsi_now is not None:
        if rsi_prev >= RSI_BUY and rsi_now < RSI_BUY:
            equity = quote_balance  # flat, so equity == quote balance
            return "BUY_BASE", equity * BASE_FRACTION
    return "HOLD", None
