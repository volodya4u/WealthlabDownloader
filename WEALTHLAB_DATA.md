# Bybit historical data for Wealth-Lab

`wealthlab_downloader.py` downloads public Last Traded Price OHLCV bars
for Bybit USDT linear perpetual contracts. It does not require an API key.

The start date is a required input parameter. For a contract listed later,
the downloader automatically starts at the Bybit launch time. One symbol is
downloaded per script run.

## Run

Download or update one symbol:

```powershell
python .\wealthlab_downloader.py --symbol BTCUSDT --start 2024-01-01
```

The default interval is `1m`. Select another supported interval with
`--interval`:

```powershell
python .\wealthlab_downloader.py --symbol BTCUSDT --start 2024-01-01 --interval 1h
```

Supported Bybit intervals:

```text
1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d, 1w, 1M
```

Values such as `10m` are not supported by Bybit. The script rejects an
invalid interval before making an API request and displays the valid options.

## Input validation

The downloader validates every command-line parameter and exits with a clear
error instead of starting a download when an input is invalid:

- `--symbol` must have the form `ETHUSDT` or `BYBIT:ETHUSDT.P`. The program
  then asks Bybit to confirm that it is an active USDT linear perpetual.
- `--start` and `--end` must be real calendar dates in `YYYY-MM-DD` or
  ISO-8601 format. Future dates are rejected.
- `--start` must precede `--end`, and the range must contain at least one
  complete candle of the selected interval.
- `--interval` and `--timestamp` accept only the documented choices.
- `--timeout` must be positive; `--retries` and `--pause` cannot be negative.
- An existing `--output-dir` path must be a directory.

For example, `ETHUSDC`, `ETH/USD`, `2024-02-30`, a future date, or an unknown
Bybit pair will produce an error message and no candle download will begin.

By default, an existing CSV is resumed from its final closed minute. Use
`--force` only when you intentionally want to replace it:

```powershell
python .\wealthlab_downloader.py --symbol BTCUSDT --start 2024-01-01 --force
```

Files are written to `Bybit_data`, with the interval in the filename:

```text
Bybit_data\BTCUSDT_1m.csv
Bybit_data\BTCUSDT_1h.csv
```

## Wealth-Lab ASCII DataSet

Create an ASCII DataSet using these columns:

```text
DateTime, Open, High, Low, Close, Volume
```

Use:

- Scale: the same interval selected with `--interval`
- Time zone: `UTC`
- Market hours: `24/7`
- Holidays: none
- One file per symbol

The CSV timestamps are already converted from Bybit's start-of-bar timestamp
to Wealth-Lab's end-of-bar convention. Therefore, leave Wealth-Lab's
`Adjust bar timestamps from start-of-bar to end-of-bar` option **unchecked**.

With the default `1m` data, Wealth-Lab can compress the history to 60-minute
or 120-minute bars for the strategy backtests.

## Useful options

```text
--start 2024-01-01       required UTC start
--interval 1m            candle interval (default: 1m)
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
duplicates returned by the API, and reports missing selected intervals
without inventing synthetic candles.
