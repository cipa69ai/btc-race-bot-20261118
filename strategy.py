"""SurgeBot: a long-only cbBTC trend bot for the bars-replay race.

Design (all signals from 1-minute closes, all thresholds scaled by realised
1-minute volatility, so the same rules behave on a quiet day and a wild one):

* Regime: only trade when the fast EMA (12) sits above the slow EMA (48) and
  price sits above the trend EMA (120). In a downtrend or chop we stay flat:
  cash loses nothing and a round trip costs ~26 bps, so no edge means no trade.
* Momentum entry: buy when the 15-bar return clears a volatility-scaled
  threshold while the regime is up. The push must be real, not one flat bar
  ticking up.
* Dip-buy entry: in an up regime, when price dips below the fast EMA by more
  than ~2x the 1-minute volatility and the 5-bar return turns positive again,
  buy the bounce instead of chasing the high.
* Exits: a volatility-scaled trailing stop under the peak since entry, a
  fast/slow EMA cross back down, a take-profit, and a time stop for dead
  positions. No new entries in the last 40 bars of the window.

Everything is a pure function of the bars seen so far: no clock, no random
numbers, no environment. Two runs on the same data produce the same orders.
"""

SYMBOL = "CBBTC"

# --- tunable parameters (defaults tuned on the dev window) ---
EMA_FAST = 20
EMA_SLOW = 60
EMA_TREND = 120
VOL_WIN = 60            # bars for realised 1-minute volatility
MOM_WIN = 30            # bars for the momentum return
DIP_WIN = 5             # bars for the dip-buy turn confirmation
MOM_MULT = 2.5          # momentum entry: r30 > MOM_MULT * vol * sqrt(30)
MOM_MIN_BPS = 10        # ... and at least this many bps outright
TREND_R60_MIN_BPS = 50  # momentum entry also needs the 60-bar return above this
DIP_MULT = 2.0          # dip-buy: price > DIP_MULT * vol below the fast EMA
DIP_TURN_BPS = 4        # ... with the 5-bar return back above this
TRAIL_MULT = 3.0        # trailing stop: TRAIL_MULT * vol below the peak
TRAIL_MIN_BPS = 90      # ... and at least this many bps
TAKE_PROFIT_BPS = 300   # sell the whole position this far above entry
TIME_STOP_BARS = 300    # exit a position held this long without a real gain
TIME_STOP_GAIN_BPS = 10
COOLDOWN_BARS = 30      # bars to wait after an exit before re-entering
WARMUP_BARS = 130       # bars of history needed before the first entry
LAST_ENTRY_BAR = 1400   # no new entries at or after this seq
FALLBACK_BAR = 1000     # if no trade yet by this bar, relax the entry gates
FALLBACK_MOM_MULT = 1.2  # (so the entry still takes at least one trade: the
FALLBACK_R60_BPS = 0    # guardrails rank a 0-trade entry below every trader)
BUY_SLIP_BPS = 50       # cap what a buy may pay above the latest close
SELL_SLIP_BPS = 100     # exits matter more than price: wider cap for sells
MIN_CASH_UNITS = 2_000_000  # need at least 2 USDC of cash to bother buying


class SurgeBot:
    """Stateful strategy. One instance per run; on_batch answers every batch."""

    def __init__(self):
        self.closes = []          # closes seen so far (ints, price units)
        self.ema_fast = None
        self.ema_slow = None
        self.ema_trend = None
        self.abs_rets = []        # |1-minute simple returns| for volatility
        self.in_position = False
        self.traded = False       # any fill observed this run
        self.peak = 0.0           # highest close since the position opened
        self.entry_price = 0.0    # cost-implied entry price (price units)
        self.held_bars = 0
        self.cooldown = 0

    # -- indicator helpers -------------------------------------------------
    @staticmethod
    def _ema(prev, price, n):
        alpha = 2.0 / (n + 1)
        return price if prev is None else prev + alpha * (price - prev)

    def _ret(self, n):
        """Simple return over the last n bars (fraction), or None."""
        if len(self.closes) <= n:
            return None
        old = self.closes[-1 - n]
        return self.closes[-1] / old - 1.0 if old else None

    def _vol(self):
        """Mean |1-minute return| over the last VOL_WIN bars (fraction)."""
        if not self.abs_rets:
            return 0.0
        window = self.abs_rets[-VOL_WIN:]
        return sum(window) / len(window)

    # -- main entry point --------------------------------------------------
    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        close = state.close(SYMBOL)
        if close is None:
            return []
        self._observe(close)
        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding else 0

        if qty > 0:
            if not self.in_position:
                # Position just appeared (our buy filled): anchor tracking.
                self.in_position = True
                self.traded = True
                self.peak = float(close)
                self.held_bars = 0
                self.entry_price = self._entry_price(state, holding)
            else:
                self.held_bars += 1
                if close > self.peak:
                    self.peak = float(close)
            return self._maybe_exit(state, holding, close)

        # Flat: reset tracking, then maybe enter.
        if self.in_position:
            self.in_position = False
            self.cooldown = COOLDOWN_BARS
        if self.cooldown > 0:
            self.cooldown -= 1
            return []
        return self._maybe_enter(state, close)

    # -- internals ---------------------------------------------------------
    def _observe(self, close):
        if self.closes:
            prev = self.closes[-1]
            if prev:
                self.abs_rets.append(abs(close / prev - 1.0))
        self.closes.append(close)
        price = float(close)
        self.ema_fast = self._ema(self.ema_fast, price, EMA_FAST)
        self.ema_slow = self._ema(self.ema_slow, price, EMA_SLOW)
        self.ema_trend = self._ema(self.ema_trend, price, EMA_TREND)

    def _entry_price(self, state, holding):
        """Cost-implied average entry price from the position's cost."""
        if holding.qty <= 0 or holding.cost <= 0:
            return float(self.closes[-1])
        sym = state.symbols[SYMBOL]
        scale = state.hello["capital"]["quote_scale"]
        # value(qty, price) = qty*price*10**scale // 10**(qty_scale+price_scale)
        # -> price = cost * 10**(qty_scale+price_scale) / (qty * 10**scale)
        num = holding.cost * 10 ** (sym["qty_scale"] + sym["price_scale"])
        den = holding.qty * 10 ** scale
        return num / den if den else float(self.closes[-1])

    def _regime_up(self, close):
        return (
            self.ema_fast is not None
            and self.ema_slow is not None
            and self.ema_trend is not None
            and self.ema_fast > self.ema_slow
            and close > self.ema_trend
        )

    def _maybe_enter(self, state, close):
        if len(self.closes) < WARMUP_BARS:
            return []
        if state.seq >= LAST_ENTRY_BAR:
            return []
        if state.cash < MIN_CASH_UNITS:
            return []
        if not self._regime_up(close):
            return []
        vol = self._vol()
        if vol <= 0:
            return []
        price = float(close)
        # Late-window fallback: if we still have no trade, relax the gates so
        # the entry takes at least one shot (a 0-trade entry ranks below every
        # entry that meets the guardrails, whatever its PnL).
        relaxed = state.seq >= FALLBACK_BAR and not self.traded
        mom_mult = FALLBACK_MOM_MULT if relaxed else MOM_MULT
        r60_min = FALLBACK_R60_BPS if relaxed else TREND_R60_MIN_BPS
        mom = self._ret(MOM_WIN)
        mom_gate = max(MOM_MIN_BPS / 10000.0, mom_mult * vol * (MOM_WIN ** 0.5))
        r60 = self._ret(60)
        trend_ok = r60 is not None and r60 > r60_min / 10000.0
        momentum_ok = mom is not None and mom > mom_gate and trend_ok
        # Dip-buy: price fell below the fast EMA and is turning back up.
        # Only in a genuine uptrend (same 60-bar filter as momentum).
        dip = (self.ema_fast - price) / price if price else 0.0
        turn = self._ret(DIP_WIN)
        dip_ok = (
            trend_ok
            and dip > DIP_MULT * vol
            and turn is not None
            and turn > DIP_TURN_BPS / 10000.0
        )
        if not (momentum_ok or dip_ok):
            return []
        spend = state.cash  # full size: one position, long only
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": spend,
            "max_slippage_bps": BUY_SLIP_BPS,
        }]

    def _maybe_exit(self, state, holding, close):
        price = float(close)
        vol = self._vol()
        # Trailing stop under the peak since entry.
        trail = max(TRAIL_MIN_BPS / 10000.0, TRAIL_MULT * vol)
        if price < self.peak * (1.0 - trail):
            return self._sell_all(holding)
        # Momentum broke down: fast EMA crossed back under the slow EMA.
        if self.ema_fast is not None and self.ema_slow is not None:
            if self.ema_fast < self.ema_slow:
                return self._sell_all(holding)
        # Take profit.
        if self.entry_price > 0:
            gain = price / self.entry_price - 1.0
            if gain > TAKE_PROFIT_BPS / 10000.0:
                return self._sell_all(holding)
            # Time stop: held too long with no real gain.
            if self.held_bars >= TIME_STOP_BARS and gain < TIME_STOP_GAIN_BPS / 10000.0:
                return self._sell_all(holding)
        return []

    @staticmethod
    def _sell_all(holding):
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": holding.qty,
            "max_slippage_bps": SELL_SLIP_BPS,
        }]
