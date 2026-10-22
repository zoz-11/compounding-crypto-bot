# Walk-forward validation

Honest out-of-sample testing for the bot's strategy families. Optimizes on a
45-day in-sample window, tests the chosen parameters once on the next 15 days
the optimizer never saw, rolls forward, and chains the blind results.

```bash
cd bot/walkforward
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 walkforward.py BTCUSDT 365   # writes ../../public/results.json
```

Then run the Next.js app and open `/results`.

Strategies: **A** RSI mean-reversion DCA (the family in `bot/strategy.py`),
**B** EMA trend-following with regime filter, **C** regime-switching combo.

Data: Binance public endpoint, no API key. If the fetch fails the loader falls
back to a synthetic series and labels every output as a code test.

Rule of thumb: only a strategy that is positive out-of-sample across most
folds, with enough trades, earns a Testnet run. Fees are 0.1%/side.
