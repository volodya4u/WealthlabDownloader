# Bybit 1-minute data for Wealth-Lab

`wealthlab_downloader.py` downloads public Last Traded Price OHLCV bars
for Bybit USDT linear perpetual contracts. It does not require an API key.

The default start is `2024-01-01 00:00:00 UTC`. For a contract listed later,
the downloader automatically starts at the Bybit launch time. One symbol is
downloaded per script run.

## Run

Download or update one symbol:

```powershell
python .\wealthlab_downloader.py --symbol BTCUSDT
```

By default, an existing CSV is resumed from its final closed minute. Use
`--force` only when you intentionally want to replace it:

```powershell
python .\wealthlab_downloader.py --symbol BTCUSDT --force
```

Files are written to `Bybit_1m`, one per symbol, for example:

```text
Bybit_1m\BTCUSDT.csv
```

## Wealth-Lab ASCII DataSet

Create an ASCII DataSet using these columns:

```text
DateTime, Open, High, Low, Close, Volume
```

Use:

- Scale: `1 Minute`
- Time zone: `UTC`
- Market hours: `24/7`
- Holidays: none
- One file per symbol

The CSV timestamps are already converted from Bybit's start-of-bar timestamp
to Wealth-Lab's end-of-bar convention. Therefore, leave Wealth-Lab's
`Adjust bar timestamps from start-of-bar to end-of-bar` option **unchecked**.

Wealth-Lab can then compress the 1-minute history to 60-minute or 120-minute
bars for the strategy backtests.

## Useful options

```text
--start 2024-01-01       UTC start
--end 2026-01-01         exclusive UTC end
--output-dir PATH        output directory
--timestamp end          Wealth-Lab convention (default)
--force                  replace instead of resume
--timeout 30             HTTP timeout
--retries 5              transient-error retries
--pause 0.05             pause between API requests
```

Only fully closed candles are downloaded. The script validates that every
symbol is an active `LinearPerpetual` quoted and settled in USDT, removes
duplicates returned by the API, and reports missing one-minute intervals
without inventing synthetic candles.
