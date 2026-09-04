import asyncio
import calendar as cal_mod
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

DEFAULT_API_URL = (
    "https://migratemate.co/api/visa-processing/interview-slots?consulate=SYDNEY"
)
API_URL = os.getenv("MIGRATEMATE_API_URL", DEFAULT_API_URL)
PARSE_MODE = "HTML"


def positive_number(name: str, default: str, converter):
    raw_value = os.getenv(name, default)
    try:
        value = converter(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw_value!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


POLL_INTERVAL = positive_number("POLL_INTERVAL", "300", int)
REQUEST_TIMEOUT = positive_number("REQUEST_TIMEOUT", "30", float)
MAX_RETRY_INTERVAL = positive_number("MAX_RETRY_INTERVAL", "3600", int)
MANUAL_FETCH_COOLDOWN = positive_number("MANUAL_FETCH_COOLDOWN", "60", int)


class UpstreamUnavailable(RuntimeError):
    """The appointment data source could not provide a usable response."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class PollOutcome:
    succeeded: bool
    retry_after: int | None = None


def compute_retry_delay(failure_count: int, retry_after: int | None) -> float:
    """Return capped client backoff, while honoring a longer server delay."""
    exponential_delay = min(
        MAX_RETRY_INTERVAL,
        POLL_INTERVAL * (2 ** min(max(0, failure_count - 1), 8)),
    )
    jitter = random.uniform(0, min(30, exponential_delay * 0.1))
    client_delay = min(MAX_RETRY_INTERVAL, exponential_delay + jitter)
    return max(client_delay, retry_after or 0)


def build_api_headers() -> dict[str, str]:
    """Build honest request headers, with an optional owner-provided bypass token."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "sydneyvisa-telegram-bot/1.0",
    }
    bypass_secret = os.getenv("MIGRATEMATE_VERCEL_BYPASS_SECRET")
    if bypass_secret:
        parsed_url = urlparse(API_URL)
        hostname = (parsed_url.hostname or "").lower()
        if parsed_url.scheme != "https" or hostname not in {
            "migratemate.co",
            "www.migratemate.co",
        }:
            raise RuntimeError(
                "MIGRATEMATE_VERCEL_BYPASS_SECRET may only be sent over HTTPS "
                "to migratemate.co"
            )
        headers["x-vercel-protection-bypass"] = bypass_secret
    return headers


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return min(86_400, max(0, int(value)))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = int((retry_at - datetime.now(timezone.utc)).total_seconds())
            return min(86_400, max(0, seconds))
        except (TypeError, ValueError, OverflowError):
            return None


def validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise UpstreamUnavailable("data source returned JSON that is not an object")

    dates = payload.get("interview_dates")
    updated_at = payload.get("updated_at")
    if not isinstance(dates, list) or not all(isinstance(item, str) for item in dates):
        raise UpstreamUnavailable("data source response has invalid interview_dates")
    if not isinstance(updated_at, str) or not updated_at:
        raise UpstreamUnavailable("data source response has invalid updated_at")

    try:
        for item in dates:
            parsed_date = date.fromisoformat(item)
            if parsed_date.isoformat() != item:
                raise ValueError
        parsed_updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if parsed_updated_at.tzinfo is None or parsed_updated_at.utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise UpstreamUnavailable(
            "data source response contains an invalid date"
        ) from exc

    normalized_payload = dict(payload)
    normalized_payload["updated_at"] = (
        parsed_updated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    normalized_payload["run_id"] = str(payload.get("run_id", "N/A"))[:100]
    return normalized_payload


async def fetch_dates(client: httpx.AsyncClient) -> dict:
    """Fetch and validate the latest appointment data."""
    try:
        response = await client.get(API_URL)
    except httpx.TimeoutException as exc:
        raise UpstreamUnavailable("data source request timed out") from exc
    except httpx.RequestError as exc:
        raise UpstreamUnavailable(
            f"could not reach data source ({type(exc).__name__})"
        ) from exc

    body_preview = response.text[:1_000].lower()
    is_vercel_challenge = (
        response.headers.get("x-vercel-mitigated", "").lower() == "challenge"
        or "vercel security checkpoint" in body_preview
        or "we're verifying your browser" in body_preview
    )
    if is_vercel_challenge:
        raise UpstreamUnavailable(
            f"data source blocked automation with a Vercel challenge "
            f"(HTTP {response.status_code})",
            retry_after=parse_retry_after(response.headers.get("retry-after")),
        )
    if response.status_code != 200:
        raise UpstreamUnavailable(
            f"data source returned HTTP {response.status_code}",
            retry_after=parse_retry_after(response.headers.get("retry-after")),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamUnavailable("data source returned a non-JSON response") from exc

    payload = validate_payload(payload)
    run_id = str(payload.get("run_id", "N/A"))[:100]
    print(f"Fetched appointment data: run_id={run_id!r}")
    return payload


def render_calendars(date_strings: set[str]) -> str:
    """Render ISO dates as monthly grids, with available days shown as numbers."""
    by_month: dict[tuple[int, int], set[int]] = defaultdict(set)
    for value in date_strings:
        parsed = date.fromisoformat(value)
        by_month[(parsed.year, parsed.month)].add(parsed.day)

    cal = cal_mod.TextCalendar(firstweekday=0)
    blocks = []

    for year, month in sorted(by_month):
        available = by_month[(year, month)]
        title = f"{cal_mod.month_abbr[month]} {year}"
        lines = [title.center(20), "Mo Tu We Th Fr Sa Su"]

        for week in cal.monthdayscalendar(year, month):
            cells = []
            for day in week:
                if day == 0:
                    cells.append("\u00a0\u00a0")
                elif day in available:
                    cells.append(f"{day:>2}")
                else:
                    cells.append(" ·")
            lines.append(" ".join(cells).rstrip())

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def format_dates_msg(dates: set[str], updated_at: str, run_id: object = "N/A") -> str:
    timestamp = escape(updated_at[:16].replace("T", " "))
    safe_run_id = escape(str(run_id))
    if dates:
        availability = (
            f"📅 <b>{len(dates)} dates available:</b>\n\n"
            f"<pre>{render_calendars(dates)}</pre>"
        )
    else:
        availability = "📅 <b>No dates currently available.</b>"
    return (
        "🇦🇺 <b>Sydney E-3 Visa Update</b>\n"
        f"<i>as of {timestamp} UTC</i>\n"
        f"<i>run_id: {safe_run_id}</i>\n\n"
        f"{availability}\n\n"
        '<a href="https://www.ustraveldocs.com/">Book now</a>'
    )


def format_change_msg(
    new: set[str],
    gone: set[str],
    all_dates: set[str],
    updated_at: str,
    run_id: object = "N/A",
) -> str:
    timestamp = escape(updated_at[:16].replace("T", " "))
    lines = [
        "🇦🇺 <b>Sydney E-3 Visa Update</b>",
        f"<i>as of {timestamp} UTC</i>",
        f"<i>run_id: {escape(str(run_id))}</i>",
    ]
    if new:
        lines.append(f"\n✅ <b>New slots:</b> {', '.join(sorted(new))}")
    if gone:
        lines.append(f"\n❌ <b>Gone:</b> {', '.join(sorted(gone))}")
    if all_dates:
        calendars = render_calendars(all_dates)
        lines.append(
            f"\n📅 <b>{len(all_dates)} dates available:</b>\n\n<pre>{calendars}</pre>"
        )
    else:
        lines.append("\n📅 <b>No dates currently available.</b>")
    lines.append('\n<a href="https://www.ustraveldocs.com/">Book now</a>')
    return "\n".join(lines)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def schedule_retry(bot_data: dict, delay: float) -> None:
    """Keep the longest active retry delay in monotonic and wall-clock form."""
    now = time.monotonic()
    target = max(bot_data.get("retry_not_before_monotonic", 0.0), now + delay)
    bot_data["retry_not_before_monotonic"] = target
    remaining = max(0, target - now)
    bot_data["next_retry_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=remaining)
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def retry_wait_seconds(bot_data: dict) -> int:
    remaining = bot_data.get("retry_not_before_monotonic", 0.0) - time.monotonic()
    return max(0, math.ceil(remaining))


def manual_fetch_wait_seconds(bot_data: dict) -> int:
    retry_wait = retry_wait_seconds(bot_data)
    cooldown_remaining = (
        bot_data.get("last_manual_fetch_at", 0.0)
        + MANUAL_FETCH_COOLDOWN
        - time.monotonic()
    )
    return max(retry_wait, max(0, math.ceil(cooldown_remaining)))


def record_fetch_error(bot_data: dict, error: Exception) -> str:
    message = (
        str(error) if isinstance(error, UpstreamUnavailable) else type(error).__name__
    )
    bot_data["last_error"] = message
    bot_data["last_error_at"] = utc_timestamp()
    bot_data["consecutive_errors"] = bot_data.get("consecutive_errors", 0) + 1
    bot_data["retry_after"] = (
        error.retry_after if isinstance(error, UpstreamUnavailable) else None
    )
    return message


def store_payload(bot_data: dict, payload: dict) -> set[str]:
    current_dates = set(payload["interview_dates"])
    bot_data["last_dates"] = current_dates
    bot_data["last_updated"] = payload["updated_at"]
    bot_data["last_run_id"] = payload.get("run_id", "N/A")
    bot_data["last_success_at"] = utc_timestamp()
    bot_data["last_error"] = None
    bot_data["last_error_at"] = None
    bot_data["consecutive_errors"] = 0
    bot_data["retry_after"] = None
    bot_data["next_retry_at"] = None
    bot_data["retry_not_before_monotonic"] = 0.0
    return current_dates


async def enqueue_payload_notification(bot_data: dict, payload: dict) -> set[str]:
    """Queue the first snapshot or a change without conflating delivery and fetches."""
    current_dates = set(payload["interview_dates"])
    previous_dates = bot_data.get("last_enqueued_dates")
    updated_at = payload["updated_at"]
    run_id = payload.get("run_id", "N/A")
    timestamp = updated_at[:16].replace("T", " ")
    message = None

    if previous_dates is None:
        print(f"[{timestamp}] Loaded {len(current_dates)} dates (run_id={run_id!r}).")
        message = format_dates_msg(current_dates, updated_at, run_id)
    elif current_dates != previous_dates:
        new = current_dates - previous_dates
        gone = previous_dates - current_dates
        parts = []
        if new:
            parts.append(f"+{len(new)} new: {', '.join(sorted(new))}")
        if gone:
            parts.append(f"-{len(gone)} gone: {', '.join(sorted(gone))}")
        print(
            f"[{timestamp}] run_id={run_id!r} CHANGE — {len(current_dates)} dates | "
            f"{' | '.join(parts)}"
        )
        message = format_change_msg(new, gone, current_dates, updated_at, run_id)
    else:
        nearest = min(current_dates) if current_dates else "none"
        print(
            f"[{timestamp}] run_id={run_id!r} No change — "
            f"{len(current_dates)} dates | nearest: {nearest}"
        )

    async with bot_data["notification_lock"]:
        if message:
            notifications = bot_data["pending_notifications"]
            notifications.append(message)
            if len(notifications) > 20:
                notifications.pop(0)
                print("Dropped oldest pending Telegram notification (queue limit: 20).")
        bot_data["last_enqueued_dates"] = current_dates
    return current_dates


async def flush_pending_notifications(bot, bot_data: dict) -> None:
    """Deliver queued channel alerts independently of upstream availability."""
    async with bot_data["notification_lock"]:
        while bot_data["pending_notifications"]:
            try:
                await bot.send_message(
                    chat_id=bot_data["notification_chat_id"],
                    text=bot_data["pending_notifications"][0],
                    parse_mode=PARSE_MODE,
                )
                bot_data["pending_notifications"].pop(0)
                bot_data["notification_error"] = None
                bot_data["notification_error_at"] = None
            except asyncio.CancelledError:
                raise
            except InvalidToken:
                raise
            except Exception as exc:
                error_name = type(exc).__name__
                bot_data["notification_error"] = error_name
                bot_data["notification_error_at"] = utc_timestamp()
                print(f"Telegram notification failed: {error_name}")
                break


async def safe_edit(message, text: str, parse_mode: str | None = None) -> None:
    """Edit a command response without changing upstream health state on failure."""
    try:
        await message.edit_text(text, parse_mode=parse_mode)
    except asyncio.CancelledError:
        raise
    except InvalidToken:
        raise
    except Exception as exc:
        print(f"Telegram command response failed: {type(exc).__name__}")


# ── Command handlers ──────────────────────────────────────────────


async def cmd_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the last-known available dates."""
    last_dates = context.bot_data.get("last_dates")
    if last_dates is None:
        message = "No appointment snapshot yet — the bot is still retrying."
        if context.bot_data.get("last_error"):
            message += f"\nLast error: {context.bot_data['last_error']}"
        await update.message.reply_text(message)
        return
    await update.message.reply_text(
        format_dates_msg(
            last_dates,
            context.bot_data["last_updated"],
            context.bot_data.get("last_run_id", "N/A"),
        ),
        parse_mode=PARSE_MODE,
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch fresh data from the API right now."""
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if chat_id != str(context.bot_data["command_chat_id"]):
        await update.message.reply_text("This command is not authorized in this chat.")
        return

    wait_seconds = manual_fetch_wait_seconds(context.bot_data)
    if wait_seconds:
        await update.message.reply_text(
            f"Please wait {wait_seconds}s before requesting another refresh."
        )
        return

    sent = await update.message.reply_text("Fetching fresh data...")
    async with context.bot_data["fetch_lock"]:
        wait_seconds = manual_fetch_wait_seconds(context.bot_data)
        if wait_seconds:
            await safe_edit(
                sent, f"Please wait {wait_seconds}s before requesting another refresh."
            )
            return
        context.bot_data["last_manual_fetch_at"] = time.monotonic()
        try:
            data = await fetch_dates(context.bot_data["client"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = record_fetch_error(context.bot_data, exc)
            delay = compute_retry_delay(
                context.bot_data["consecutive_errors"],
                exc.retry_after if isinstance(exc, UpstreamUnavailable) else None,
            )
            schedule_retry(context.bot_data, delay)
            print(f"Manual fetch failed: {message}")
            error_text = (
                f"Data source unavailable: {message}\n"
                "The bot is still running and will retry."
            )
            data = None

        if data is not None:
            current_dates = store_payload(context.bot_data, data)
            await enqueue_payload_notification(context.bot_data, data)

    if data is None:
        await safe_edit(sent, error_text)
        return

    await flush_pending_notifications(context.bot, context.bot_data)
    await safe_edit(
        sent,
        format_dates_msg(
            current_dates,
            data["updated_at"],
            data.get("run_id", "N/A"),
        ),
        parse_mode=PARSE_MODE,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report worker and upstream health without triggering a request."""
    lines = ["Bot: running"]
    if context.bot_data.get("last_error"):
        lines.extend(
            [
                "Data source: unavailable",
                f"Last error: {context.bot_data['last_error']}",
                f"Error time: {context.bot_data['last_error_at']}",
                f"Consecutive errors: {context.bot_data['consecutive_errors']}",
            ]
        )
        if context.bot_data.get("next_retry_at"):
            lines.append(f"Next retry: {context.bot_data['next_retry_at']}")
    elif context.bot_data.get("last_success_at"):
        lines.extend(
            [
                "Data source: reachable",
                f"Last success: {context.bot_data['last_success_at']}",
            ]
        )
    else:
        lines.append("Data source: waiting for first check")
    pending_count = len(context.bot_data.get("pending_notifications", []))
    if context.bot_data.get("notification_error"):
        lines.extend(
            [
                "Telegram delivery: degraded",
                f"Last delivery error: {context.bot_data['notification_error']}",
                f"Delivery error time: {context.bot_data['notification_error_at']}",
                f"Pending alerts: {pending_count}",
            ]
        )
    elif pending_count:
        lines.append(f"Telegram delivery: {pending_count} alert(s) pending")
    else:
        lines.append("Telegram delivery: healthy")
    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Sydney E-3 Visa Bot</b>\n\n"
        "/dates — show last-known available dates\n"
        "/ping — fetch fresh data right now\n"
        "/status — show bot and data-source health\n"
        "/help — show this message\n\n"
        f"Auto-polls every {POLL_INTERVAL // 60} minutes and alerts on changes.",
        parse_mode=PARSE_MODE,
    )


# ── Background polling ────────────────────────────────────────────


async def poll_and_notify(app: Application) -> PollOutcome:
    """Run one poll. Upstream failures are recorded and never stop the worker."""
    await flush_pending_notifications(app.bot, app.bot_data)
    try:
        async with app.bot_data["fetch_lock"]:
            wait_seconds = retry_wait_seconds(app.bot_data)
            if wait_seconds:
                print(f"Data-source retry deferred for another {wait_seconds}s.")
                return PollOutcome(succeeded=False, retry_after=wait_seconds)
            data = await fetch_dates(app.bot_data["client"])
            store_payload(app.bot_data, data)
            await enqueue_payload_notification(app.bot_data, data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = record_fetch_error(app.bot_data, exc)
        print(
            f"Poll failed ({app.bot_data['consecutive_errors']} consecutive): {message}"
        )
        retry_after = exc.retry_after if isinstance(exc, UpstreamUnavailable) else None
        return PollOutcome(succeeded=False, retry_after=retry_after)

    await flush_pending_notifications(app.bot, app.bot_data)

    return PollOutcome(succeeded=True)


# ── Main ──────────────────────────────────────────────────────────


def required_environment() -> tuple[str, str]:
    missing = [name for name in ("TELEGRAM_TOKEN", "CHAT_ID") if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    return os.environ["TELEGRAM_TOKEN"], os.environ["CHAT_ID"]


async def main():
    telegram_token, chat_id = required_environment()
    command_chat_id = os.getenv("COMMAND_CHAT_ID") or chat_id
    timeout = httpx.Timeout(REQUEST_TIMEOUT)

    async with httpx.AsyncClient(
        headers=build_api_headers(),
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        app = Application.builder().token(telegram_token).build()
        app.bot_data.update(
            {
                "client": client,
                "fetch_lock": asyncio.Lock(),
                "notification_lock": asyncio.Lock(),
                "notification_chat_id": chat_id,
                "command_chat_id": command_chat_id,
                "last_dates": None,
                "last_enqueued_dates": None,
                "last_updated": None,
                "last_run_id": "N/A",
                "last_success_at": None,
                "last_error": None,
                "last_error_at": None,
                "consecutive_errors": 0,
                "retry_after": None,
                "next_retry_at": None,
                "retry_not_before_monotonic": 0.0,
                "last_manual_fetch_at": 0.0,
                "pending_notifications": [],
                "notification_error": None,
                "notification_error_at": None,
            }
        )

        app.add_handler(CommandHandler("dates", cmd_dates))
        app.add_handler(CommandHandler("ping", cmd_ping))
        app.add_handler(CommandHandler("status", cmd_status))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("start", cmd_help))

        async with app:
            updater_started = False
            app_started = False
            try:
                await app.updater.start_polling()
                updater_started = True
                await app.start()
                app_started = True
                print("Bot running. Commands: /dates, /ping, /status, /help")

                while True:
                    outcome = await poll_and_notify(app)
                    if outcome.succeeded:
                        delay = POLL_INTERVAL
                    else:
                        failure_count = app.bot_data["consecutive_errors"]
                        delay = compute_retry_delay(
                            failure_count,
                            outcome.retry_after,
                        )
                        schedule_retry(app.bot_data, delay)
                        print(f"Next data-source retry in {delay:.0f}s.")
                    await asyncio.sleep(delay)
            finally:
                if updater_started:
                    await app.updater.stop()
                if app_started:
                    await app.stop()


def run() -> int:
    try:
        asyncio.run(main())
    except InvalidToken:
        print(
            "Fatal: Telegram rejected TELEGRAM_TOKEN. "
            "Check the Railway variable and restart the service."
        )
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
