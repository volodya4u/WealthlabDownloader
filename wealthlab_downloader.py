from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


API_BASE_URL = "https://api.bybit.com"
KLINE_PATH = "/v5/market/kline"
INSTRUMENT_PATH = "/v5/market/instruments-info"
ONE_MINUTE_MS = 60_000
MAX_BARS_PER_REQUEST = 1_000
CSV_HEADER = ("DateTime", "Open", "High", "Low", "Close", "Volume")


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    status: str
    contract_type: str
    quote_coin: str
    settle_coin: str
    launch_time_ms: int


@dataclass(frozen=True)
class Kline:
    start_time_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str


@dataclass(frozen=True)
class DownloadResult:
    symbol: str
    path: Path
    rows_written: int
    gaps_detected: int
    resumed: bool


@dataclass(frozen=True)
class IntervalSpec:
    label: str
    api_value: str
    duration_ms: int | None


SUPPORTED_INTERVALS = (
    IntervalSpec("1m", "1", ONE_MINUTE_MS),
    IntervalSpec("3m", "3", 3 * ONE_MINUTE_MS),
    IntervalSpec("5m", "5", 5 * ONE_MINUTE_MS),
    IntervalSpec("15m", "15", 15 * ONE_MINUTE_MS),
    IntervalSpec("30m", "30", 30 * ONE_MINUTE_MS),
    IntervalSpec("1h", "60", 60 * ONE_MINUTE_MS),
    IntervalSpec("2h", "120", 120 * ONE_MINUTE_MS),
    IntervalSpec("4h", "240", 240 * ONE_MINUTE_MS),
    IntervalSpec("6h", "360", 360 * ONE_MINUTE_MS),
    IntervalSpec("12h", "720", 720 * ONE_MINUTE_MS),
    IntervalSpec("1d", "D", 24 * 60 * ONE_MINUTE_MS),
    IntervalSpec("1w", "W", 7 * 24 * 60 * ONE_MINUTE_MS),
    IntervalSpec("1M", "M", None),
)
INTERVALS_BY_LABEL = {interval.label: interval for interval in SUPPORTED_INTERVALS}
INTERVAL_HINT = ", ".join(interval.label for interval in SUPPORTED_INTERVALS)


def parse_interval(value: str) -> IntervalSpec:
    text = value.strip()
    key = text if text == "1M" else text.lower()
    interval = INTERVALS_BY_LABEL.get(key)
    if interval is None:
        raise argparse.ArgumentTypeError(
            f"invalid interval {value!r}. Supported Bybit intervals: {INTERVAL_HINT}"
        )
    return interval


def floor_to_interval_ms(value_ms: int, interval: IntervalSpec) -> int:
    dt = datetime.fromtimestamp(value_ms / 1_000, tz=timezone.utc)
    if interval.label == "1M":
        return int(datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp() * 1_000)
    if interval.label == "1w":
        day_start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        monday = day_start.timestamp() * 1_000 - dt.weekday() * 24 * 60 * ONE_MINUTE_MS
        return int(monday)
    assert interval.duration_ms is not None
    return value_ms - value_ms % interval.duration_ms


def advance_intervals_ms(value_ms: int, interval: IntervalSpec, count: int) -> int:
    if count < 0:
        raise ValueError("count cannot be negative")
    if interval.label != "1M":
        assert interval.duration_ms is not None
        return value_ms + interval.duration_ms * count

    dt = datetime.fromtimestamp(value_ms / 1_000, tz=timezone.utc)
    month_index = dt.year * 12 + (dt.month - 1) + count
    year, month_zero_based = divmod(month_index, 12)
    result = datetime(year, month_zero_based + 1, 1, tzinfo=timezone.utc)
    return int(result.timestamp() * 1_000)


def interval_count_between(start_ms: int, end_ms: int, interval: IntervalSpec) -> int:
    if end_ms <= start_ms:
        return 0
    if interval.label != "1M":
        assert interval.duration_ms is not None
        return (end_ms - start_ms) // interval.duration_ms
    start = datetime.fromtimestamp(start_ms / 1_000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1_000, tz=timezone.utc)
    return max(0, (end.year - start.year) * 12 + end.month - start.month)


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000)


def parse_utc(value: str) -> int:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("date/time cannot be empty")
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        timestamp_ms = int(dt.timestamp() * 1_000)
    except (ValueError, OverflowError, OSError) as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date/time {value!r}. Use YYYY-MM-DD or ISO-8601, "
            "for example 2024-01-01 or 2024-01-01T12:30:00Z"
        ) from exc
    return timestamp_ms


def format_wealthlab_datetime(
    start_time_ms: int, timestamp_mode: str, interval: IntervalSpec
) -> str:
    timestamp_ms = start_time_ms
    if timestamp_mode == "end":
        timestamp_ms = advance_intervals_ms(start_time_ms, interval, 1)
    dt = datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if symbol.startswith("BYBIT:"):
        symbol = symbol[6:]
    if symbol.endswith(".P"):
        symbol = symbol[:-2]
    if not re.fullmatch(r"[A-Z0-9]+USDT", symbol) or not symbol[:-4]:
        raise argparse.ArgumentTypeError(
            f"invalid currency pair {value!r}. Expected a Bybit USDT perpetual "
            "symbol such as ETHUSDT or BYBIT:ETHUSDT.P"
        )
    return symbol


def parse_positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("value must be a positive finite number")
    return number


def parse_nonnegative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative finite number")
    return number


def parse_nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    return number


def api_get(
    path: str,
    params: dict[str, Any],
    *,
    timeout_seconds: float,
    retries: int,
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE_URL}{path}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "WealthLab-Bybit-History-Downloader/1.0",
        },
    )

    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            ret_code = int(payload.get("retCode", -1))
            if ret_code == 0:
                return payload
            message = str(payload.get("retMsg", "Unknown Bybit API error"))
            if ret_code not in {10000, 10006}:
                raise DownloadError(f"Bybit API error {ret_code}: {message}")
            last_error = DownloadError(f"Bybit API error {ret_code}: {message}")
        except DownloadError:
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc

        if attempt < retries:
            time.sleep(min(30.0, 1.5 * (2**attempt)))

    raise DownloadError(f"request failed after {retries + 1} attempts: {url}") from last_error


def get_instrument(
    symbol: str, *, timeout_seconds: float, retries: int
) -> Instrument:
    payload = api_get(
        INSTRUMENT_PATH,
        {"category": "linear", "symbol": symbol},
        timeout_seconds=timeout_seconds,
        retries=retries,
    )
    items = payload.get("result", {}).get("list", [])
    exact = next((item for item in items if item.get("symbol") == symbol), None)
    if exact is None:
        raise DownloadError(
            f"invalid currency pair {symbol!r}: an active Bybit USDT linear "
            "perpetual contract was not found"
        )

    instrument = Instrument(
        symbol=str(exact.get("symbol", "")),
        status=str(exact.get("status", "")),
        contract_type=str(exact.get("contractType", "")),
        quote_coin=str(exact.get("quoteCoin", "")),
        settle_coin=str(exact.get("settleCoin", "")),
        launch_time_ms=int(exact.get("launchTime", 0)),
    )
    if instrument.status != "Trading":
        raise DownloadError(
            f"currency pair {symbol!r} is not currently tradable on Bybit "
            f"(status: {instrument.status!r})"
        )
    if instrument.contract_type != "LinearPerpetual":
        raise DownloadError(
            f"{symbol}: expected LinearPerpetual, got {instrument.contract_type!r}"
        )
    if instrument.quote_coin != "USDT" or instrument.settle_coin != "USDT":
        raise DownloadError(
            f"{symbol}: expected USDT quote/settlement, got "
            f"{instrument.quote_coin}/{instrument.settle_coin}"
        )
    return instrument


def get_klines(
    symbol: str,
    start_ms: int,
    end_exclusive_ms: int,
    interval: IntervalSpec,
    *,
    timeout_seconds: float,
    retries: int,
) -> list[Kline]:
    if end_exclusive_ms <= start_ms:
        return []
    payload = api_get(
        KLINE_PATH,
        {
            "category": "linear",
            "symbol": symbol,
            "interval": interval.api_value,
            "start": start_ms,
            "end": end_exclusive_ms - 1,
            "limit": MAX_BARS_PER_REQUEST,
        },
        timeout_seconds=timeout_seconds,
        retries=retries,
    )

    raw_rows = payload.get("result", {}).get("list", [])
    rows: dict[int, Kline] = {}
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) < 7:
            raise DownloadError(f"{symbol}: malformed kline returned by Bybit: {raw!r}")
        start_time_ms = int(raw[0])
        if not (start_ms <= start_time_ms < end_exclusive_ms):
            continue
        if start_time_ms % ONE_MINUTE_MS != 0:
            raise DownloadError(
                f"{symbol}: kline timestamp is not minute-aligned: {start_time_ms}"
            )
        rows[start_time_ms] = Kline(
            start_time_ms=start_time_ms,
            open=str(raw[1]),
            high=str(raw[2]),
            low=str(raw[3]),
            close=str(raw[4]),
            volume=str(raw[5]),
        )
    return [rows[key] for key in sorted(rows)]


def read_last_csv_row(path: Path) -> list[str] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        block_size = 8_192
        data = b""
        position = end
        while position > 0 and data.count(b"\n") < 2:
            take = min(block_size, position)
            position -= take
            handle.seek(position)
            data = handle.read(take) + data
    lines = [line for line in data.decode("utf-8-sig").splitlines() if line.strip()]
    if not lines:
        return None
    row = next(csv.reader([lines[-1]]))
    if tuple(row) == CSV_HEADER:
        return None
    return row


def validate_or_create_csv(path: Path, *, force: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if force and path.exists():
        path.unlink()
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerow(CSV_HEADER)
        return False

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle), None)
    if tuple(header or ()) != CSV_HEADER:
        raise DownloadError(
            f"{path}: unexpected CSV header {header!r}; expected {list(CSV_HEADER)!r}"
        )
    return read_last_csv_row(path) is not None


def resume_start_ms(
    path: Path, timestamp_mode: str, interval: IntervalSpec
) -> int | None:
    row = read_last_csv_row(path)
    if row is None:
        return None
    if len(row) != len(CSV_HEADER):
        raise DownloadError(f"{path}: malformed last CSV row: {row!r}")
    try:
        timestamp = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise DownloadError(f"{path}: invalid last DateTime value {row[0]!r}") from exc
    value_ms = int(timestamp.timestamp() * 1_000)
    # With an end-of-bar timestamp, the next bar starts at that timestamp.
    # With a start-of-bar timestamp, advance by one selected interval.
    if timestamp_mode == "start":
        value_ms = advance_intervals_ms(value_ms, interval, 1)
    return value_ms


def count_gaps(
    rows: Iterable[Kline],
    previous_start_ms: int | None,
    interval: IntervalSpec,
) -> tuple[int, int | None]:
    gaps = 0
    previous = previous_start_ms
    for row in rows:
        if previous is not None:
            expected = advance_intervals_ms(previous, interval, 1)
            if row.start_time_ms > expected:
                gaps += max(
                    0,
                    interval_count_between(previous, row.start_time_ms, interval) - 1,
                )
        previous = row.start_time_ms
    return gaps, previous


def download_symbol(
    symbol: str,
    requested_start_ms: int,
    end_exclusive_ms: int,
    output_dir: Path,
    interval: IntervalSpec,
    *,
    timestamp_mode: str,
    force: bool,
    timeout_seconds: float,
    retries: int,
    pause_seconds: float,
) -> DownloadResult:
    instrument = get_instrument(
        symbol, timeout_seconds=timeout_seconds, retries=retries
    )
    path = output_dir / f"{symbol}_{interval.label}.csv"
    resumed = validate_or_create_csv(path, force=force)

    launch_ms = floor_to_interval_ms(instrument.launch_time_ms, interval)
    effective_start_ms = max(requested_start_ms, launch_ms)
    existing_next_ms = resume_start_ms(path, timestamp_mode, interval)
    if existing_next_ms is not None:
        effective_start_ms = max(effective_start_ms, existing_next_ms)

    if effective_start_ms >= end_exclusive_ms:
        print(f"{symbol}: already up to date -> {path}")
        return DownloadResult(symbol, path, 0, 0, resumed)

    total_bars = interval_count_between(
        effective_start_ms, end_exclusive_ms, interval
    )
    cursor_ms = effective_start_ms
    rows_written = 0
    gaps_detected = 0
    previous_start_ms: int | None = None
    chunk_number = 0
    print(
        f"{symbol}: downloading approximately {total_bars:,} closed "
        f"{interval.label} bars from "
        f"{format_wealthlab_datetime(effective_start_ms, 'start', interval)} UTC"
    )

    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        while cursor_ms < end_exclusive_ms:
            window_end_ms = min(
                advance_intervals_ms(cursor_ms, interval, MAX_BARS_PER_REQUEST),
                end_exclusive_ms,
            )
            klines = get_klines(
                symbol,
                cursor_ms,
                window_end_ms,
                interval,
                timeout_seconds=timeout_seconds,
                retries=retries,
            )
            chunk_gaps, previous_start_ms = count_gaps(
                klines, previous_start_ms, interval
            )
            gaps_detected += chunk_gaps
            for bar in klines:
                writer.writerow(
                    (
                        format_wealthlab_datetime(
                            bar.start_time_ms, timestamp_mode, interval
                        ),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                    )
                )
            rows_written += len(klines)
            handle.flush()
            cursor_ms = window_end_ms
            chunk_number += 1

            if chunk_number == 1 or chunk_number % 50 == 0 or cursor_ms >= end_exclusive_ms:
                completed = min(100.0, 100.0 * (cursor_ms - effective_start_ms) / max(1, end_exclusive_ms - effective_start_ms))
                print(
                    f"{symbol}: {completed:6.2f}% | "
                    f"{rows_written:,} rows | "
                    f"through {format_wealthlab_datetime(cursor_ms, 'start', interval)} UTC"
                )
            if pause_seconds > 0 and cursor_ms < end_exclusive_ms:
                time.sleep(pause_seconds)

    return DownloadResult(symbol, path, rows_written, gaps_detected, resumed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download closed Last Traded Price candles for Bybit USDT "
            "linear perpetuals and save Wealth-Lab-compatible CSV files."
        )
    )
    parser.add_argument(
        "--symbol",
        required=True,
        type=normalize_symbol,
        help=(
            "One Bybit symbol to download. TradingView forms such as "
            "BYBIT:BTCUSDT.P are accepted."
        ),
    )
    parser.add_argument(
        "--interval",
        type=parse_interval,
        default=INTERVALS_BY_LABEL["1m"],
        metavar="INTERVAL",
        help=(
            "Candle interval (default: 1m). Supported: "
            f"{INTERVAL_HINT}."
        ),
    )
    parser.add_argument(
        "--start",
        type=parse_utc,
        required=True,
        help="Required UTC start, for example: 2024-01-01.",
    )
    parser.add_argument(
        "--end",
        type=parse_utc,
        help=(
            "Exclusive UTC end. Default: start of the current UTC minute, so only "
            "fully closed candles are downloaded."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("Bybit_data"),
        help="Output directory (default: ./Bybit_data).",
    )
    parser.add_argument(
        "--timestamp",
        choices=("end", "start"),
        default="end",
        help=(
            "CSV timestamp convention. 'end' matches Wealth-Lab's convention and "
            "is the default."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing symbol CSV files instead of resuming them.",
    )
    parser.add_argument(
        "--timeout",
        type=parse_positive_float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--retries",
        type=parse_nonnegative_int,
        default=5,
        help="Retries after transient API/network errors (default: 5).",
    )
    parser.add_argument(
        "--pause",
        type=parse_nonnegative_float,
        default=0.05,
        help="Pause between Kline requests in seconds (default: 0.05).",
    )
    return parser


def configure_console_streams() -> None:
    # Some Windows installations expose stdout/stderr as cp1252 even when the
    # working directory contains Cyrillic characters. Do not let a progress
    # message abort a long-running download.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    symbol = args.symbol
    interval = args.interval
    now_ms = utc_now_ms()

    if args.start >= now_ms:
        parser.error("--start cannot be in the future")
    if args.end is not None and args.end > now_ms:
        parser.error("--end cannot be in the future")

    end_exclusive_ms = (
        floor_to_interval_ms(now_ms, interval)
        if args.end is None
        else floor_to_interval_ms(args.end, interval)
    )
    start_ms = floor_to_interval_ms(args.start, interval)

    if start_ms >= end_exclusive_ms:
        parser.error(
            f"--start must be earlier than --end and the range must contain "
            f"at least one complete {interval.label} candle"
        )
    if args.output_dir.exists() and not args.output_dir.is_dir():
        parser.error(f"--output-dir is not a directory: {args.output_dir}")

    print("Bybit source: linear USDT perpetual Last Traded Price klines")
    print(f"Symbol: {symbol}")
    print(f"Interval: {interval.label} (Bybit API value: {interval.api_value})")
    print(f"Output: {args.output_dir.resolve()}")
    print(f"Timestamp convention: {args.timestamp}-of-bar UTC")

    try:
        result = download_symbol(
            symbol,
            start_ms,
            end_exclusive_ms,
            args.output_dir,
            interval,
            timestamp_mode=args.timestamp,
            force=args.force,
            timeout_seconds=args.timeout,
            retries=args.retries,
            pause_seconds=args.pause,
        )
    except (DownloadError, OSError) as exc:
        print(f"{symbol}: ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"{symbol}: done | {result.rows_written:,} new rows | "
        f"{result.gaps_detected:,} missing {interval.label} interval(s) detected | "
        f"{result.path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
