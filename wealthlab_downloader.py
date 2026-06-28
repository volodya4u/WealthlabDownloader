from __future__ import annotations

import argparse
import csv
import json
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


def floor_to_minute_ms(value_ms: int) -> int:
    return value_ms - value_ms % ONE_MINUTE_MS


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
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date/time {value!r}; use YYYY-MM-DD or ISO-8601"
        ) from exc
    return int(dt.timestamp() * 1_000)


def format_wealthlab_datetime(start_time_ms: int, timestamp_mode: str) -> str:
    timestamp_ms = start_time_ms
    if timestamp_mode == "end":
        timestamp_ms += ONE_MINUTE_MS
    dt = datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if symbol.startswith("BYBIT:"):
        symbol = symbol[6:]
    if symbol.endswith(".P"):
        symbol = symbol[:-2]
    if not symbol or not symbol.replace("-", "").isalnum():
        raise argparse.ArgumentTypeError(f"invalid Bybit symbol: {value!r}")
    return symbol


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
            "User-Agent": "WealthLab-Bybit-1m-Downloader/1.0",
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
        raise DownloadError(f"{symbol}: linear instrument not found on Bybit")

    instrument = Instrument(
        symbol=str(exact.get("symbol", "")),
        status=str(exact.get("status", "")),
        contract_type=str(exact.get("contractType", "")),
        quote_coin=str(exact.get("quoteCoin", "")),
        settle_coin=str(exact.get("settleCoin", "")),
        launch_time_ms=int(exact.get("launchTime", 0)),
    )
    if instrument.status != "Trading":
        raise DownloadError(f"{symbol}: instrument status is {instrument.status!r}")
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
            "interval": "1",
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


def resume_start_ms(path: Path, timestamp_mode: str) -> int | None:
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
    # End-of-bar 00:01 means that the next bar starts at 00:01. With
    # start-of-bar timestamps, the next bar starts one minute later.
    if timestamp_mode == "start":
        value_ms += ONE_MINUTE_MS
    return value_ms


def count_gaps(rows: Iterable[Kline], previous_start_ms: int | None) -> tuple[int, int | None]:
    gaps = 0
    previous = previous_start_ms
    for row in rows:
        if previous is not None and row.start_time_ms > previous + ONE_MINUTE_MS:
            gaps += (row.start_time_ms - previous) // ONE_MINUTE_MS - 1
        previous = row.start_time_ms
    return gaps, previous


def download_symbol(
    symbol: str,
    requested_start_ms: int,
    end_exclusive_ms: int,
    output_dir: Path,
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
    path = output_dir / f"{symbol}.csv"
    resumed = validate_or_create_csv(path, force=force)

    launch_ms = floor_to_minute_ms(instrument.launch_time_ms)
    effective_start_ms = max(requested_start_ms, launch_ms)
    existing_next_ms = resume_start_ms(path, timestamp_mode)
    if existing_next_ms is not None:
        effective_start_ms = max(effective_start_ms, existing_next_ms)

    if effective_start_ms >= end_exclusive_ms:
        print(f"{symbol}: already up to date -> {path}")
        return DownloadResult(symbol, path, 0, 0, resumed)

    total_minutes = (end_exclusive_ms - effective_start_ms) // ONE_MINUTE_MS
    cursor_ms = effective_start_ms
    rows_written = 0
    gaps_detected = 0
    previous_start_ms: int | None = None
    chunk_number = 0
    print(
        f"{symbol}: downloading {total_minutes:,} closed 1m bars "
        f"from {format_wealthlab_datetime(effective_start_ms, 'start')} UTC"
    )

    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        while cursor_ms < end_exclusive_ms:
            window_end_ms = min(
                cursor_ms + MAX_BARS_PER_REQUEST * ONE_MINUTE_MS,
                end_exclusive_ms,
            )
            klines = get_klines(
                symbol,
                cursor_ms,
                window_end_ms,
                timeout_seconds=timeout_seconds,
                retries=retries,
            )
            chunk_gaps, previous_start_ms = count_gaps(klines, previous_start_ms)
            gaps_detected += chunk_gaps
            for bar in klines:
                writer.writerow(
                    (
                        format_wealthlab_datetime(bar.start_time_ms, timestamp_mode),
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
                    f"through {format_wealthlab_datetime(cursor_ms, 'start')} UTC"
                )
            if pause_seconds > 0 and cursor_ms < end_exclusive_ms:
                time.sleep(pause_seconds)

    return DownloadResult(symbol, path, rows_written, gaps_detected, resumed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download closed 1-minute Last Traded Price candles for Bybit USDT "
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
        default=Path("Bybit_1m"),
        help="Output directory (default: ./Bybit_1m).",
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
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Retries after transient API/network errors (default: 5).",
    )
    parser.add_argument(
        "--pause",
        type=float,
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
    end_exclusive_ms = (
        floor_to_minute_ms(utc_now_ms()) if args.end is None else floor_to_minute_ms(args.end)
    )
    start_ms = floor_to_minute_ms(args.start)

    if start_ms >= end_exclusive_ms:
        parser.error("--start must be earlier than --end/current closed minute")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    if args.pause < 0:
        parser.error("--pause cannot be negative")

    print("Bybit source: linear USDT perpetual Last Traded Price klines")
    print(f"Symbol: {symbol}")
    print(f"Output: {args.output_dir.resolve()}")
    print(f"Timestamp convention: {args.timestamp}-of-bar UTC")

    try:
        result = download_symbol(
            symbol,
            start_ms,
            end_exclusive_ms,
            args.output_dir,
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
        f"{result.gaps_detected:,} missing minute(s) detected | {result.path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
