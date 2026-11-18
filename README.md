# SurgeBot — BTC race bot (btc-race-24h-20261118-1444)

A long-only cbBTC trend bot for make.fun's bars-replay race. It trades
1-minute cbBTC/USDC bars with $1,000 of paper money, aiming for the highest
net PnL on the 24-hour race window.

## Strategy

All signals come from 1-minute closes; every threshold is scaled by realised
1-minute volatility, so the same rules behave on a quiet day and a wild one.

- **Regime filter:** only trade when the fast EMA (20) is above the slow EMA
  (60) and price is above the trend EMA (120). In a downtrend or chop the bot
  stays flat — cash loses nothing, and a round trip costs ~26 bps.
- **Momentum entry:** buy when the 30-bar return clears a volatility-scaled
  threshold **and** the 60-bar return is above +50 bps. The push must be real.
- **Dip-buy entry:** in an uptrend, when price dips below the fast EMA by
  more than ~2x the 1-minute volatility and the 5-bar return turns positive,
  buy the bounce instead of chasing the high.
- **Exits:** a volatility-scaled trailing stop (min 90 bps) under the peak
  since entry, a fast/slow EMA cross back down, a +300 bps take-profit, and a
  time stop for dead positions. No new entries in the last 40 bars.
- **Late-window fallback:** if no trade has been made by bar 1000, the entry
  gates relax, so the entry still takes at least one trade (the guardrails
  rank a 0-trade entry below every entry that meets them).

## Layout

- `main.py` — entrypoint; speaks agent-fun/bars-replay/v1 via the kit shim.
- `strategy.py` — the bot logic (pure function of bars seen so far: no
  clock, no randomness, no environment, so two runs match byte for byte).
- `vendor/makefun_bars.py` — the kit's Python SDK shim, byte-identical.

## Dev result (local grade, kit's dev window)

- Net PnL: **+$9.46** (+94.6 bps), 2 fills (1 round trip), max drawdown 0.71%
- The result is stable across a wide parameter plateau (same trade for
  momentum multipliers 2.0–3.0, trailing stops 70–100 bps), not a tuned peak.
