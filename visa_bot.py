import asyncio
import calendar as cal_mod
import json
import os
from collections import defaultdict
from datetime import date
from html import escape

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
CONSULATES = {
    "SYDNEY": "Sydney",
    "MELBOURNE": "Melbourne",
    "PERTH": "Perth",
}
API_URL_TEMPLATE = (
    "https://migratemate.co/api/visa-processing/interview-slots"
    "?consulate={consulate}"
)
BLOG_URL = "https://migratemate.co/blog/e3-visa-appointment-calendar"
POLL_INTERVAL = 300  # seconds between polls (5 minutes)


async def solve_vercel_challenge(page: Page):
    """Navigate to the blog page and wait for Vercel's JS challenge to resolve."""
    await page.goto(BLOG_URL, wait_until="networkidle")
    for _ in range(20):
        title = await page.title()
        if "Security Checkpoint" not in title and "Vercel" not in title:
            print(f"Challenge solved. Page title: {title}")
            return
        await page.wait_for_timeout(1500)
    raise ValueError("Vercel challenge did not resolve after waiting")


async def fetch_dates(page: Page, consulate: str) -> dict:
    """Fetch one consulate using the browser's Vercel session cookies."""
    if consulate not in CONSULATES:
        raise ValueError(f"Unsupported consulate: {consulate}")

    api_url = API_URL_TEMPLATE.format(consulate=consulate)
    result = await page.evaluate("""
        async (url) => {
            const resp = await fetch(url);
            return { status: resp.status, body: await resp.text() };
        }
    """, api_url)
    status = result["status"]
    body = result["body"]
    if status != 200 or not body.strip().startswith("{"):
        print(
            f"fetch_dates[{consulate}]: unexpected response "
            f"(status={status}, length={len(body)})"
        )
        print(f"fetch_dates[{consulate}]: body preview: {body[:500]}")
        raise ValueError(f"API returned non-JSON response (status={status})")

    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("API returned JSON that is not an object")
    interview_dates = payload.get("interview_dates")
    updated_at = payload.get("updated_at")
    if not isinstance(interview_dates, list) or not all(
        isinstance(value, str) for value in interview_dates
    ):
        raise ValueError("API returned invalid interview_dates")
    if not isinstance(updated_at, str) or not updated_at:
        raise ValueError("API returned invalid updated_at")
    try:
        for value in interview_dates:
            date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("API returned an invalid interview date") from exc

    run_id = payload.get("run_id", "N/A")
    print(
        f"fetch_dates[{consulate}]: run_id={run_id}, "
        f"keys={list(payload.keys())}"
    )
    return payload


async def fetch_consulates(page: Page, consulates=None) -> tuple[dict, dict]:
    """Fetch consulates sequentially so one shared Playwright page stays safe."""
    payloads = {}
    errors = {}
    if consulates is None:
        consulates = CONSULATES
    for consulate in consulates:
        try:
            payloads[consulate] = await fetch_dates(page, consulate)
        except Exception as exc:
            errors[consulate] = exc
    return payloads, errors


def snapshot_from_payload(payload: dict) -> dict:
    """Convert an API payload into the in-memory state used by the bot."""
    return {
        "dates": set(payload["interview_dates"]),
        "updated_at": payload["updated_at"],
        "run_id": payload.get("run_id", "N/A"),
    }


def render_calendars(date_strings: set[str]) -> str:
    """Render a set of ISO date strings as monthly calendar grids.
    Available dates show as numbers, other days show as dots."""
    by_month: dict[tuple[int, int], set[int]] = defaultdict(set)
    for d in date_strings:
        parsed = date.fromisoformat(d)
        by_month[(parsed.year, parsed.month)].add(parsed.day)

    cal = cal_mod.TextCalendar(firstweekday=0)
    blocks = []

    for (year, month) in sorted(by_month):
        avail = by_month[(year, month)]
        title = f"{cal_mod.month_abbr[month]} {year}"
        lines = [title.center(20), "Mo Tu We Th Fr Sa Su"]

        for week in cal.monthdayscalendar(year, month):
            cells = []
            for day in week:
                if day == 0:
                    cells.append("\u00a0\u00a0")
                elif day in avail:
                    cells.append(f"{day:>2}")
                else:
                    cells.append(" ·")
            lines.append(" ".join(cells).rstrip())

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def format_dates_msg(
    dates: set[str],
    updated_at: str,
    run_id: str = "N/A",
    consulate: str = "SYDNEY",
) -> str:
    ts = escape(updated_at[:16].replace("T", " "))
    city = CONSULATES[consulate]
    if dates:
        availability = (
            f"📅 <b>{len(dates)} dates available:</b>\n\n"
            f"<pre>{render_calendars(dates)}</pre>"
        )
    else:
        availability = "📅 <b>No dates currently available.</b>"
    return (
        f"🇦🇺 <b>{city} E-3 Visa Update</b>\n"
        f"<i>as of {ts} UTC</i>\n"
        f"<i>run_id: {escape(str(run_id))}</i>\n\n"
        f"{availability}\n\n"
        f'🔗 <a href="https://www.ustraveldocs.com/">Book now</a>'
    )


def format_change_msg(
    new: set[str],
    gone: set[str],
    all_dates: set[str],
    updated_at: str,
    run_id: str = "N/A",
    consulate: str = "SYDNEY",
) -> str:
    ts = escape(updated_at[:16].replace("T", " "))
    city = CONSULATES[consulate]
    lines = [
        f"🇦🇺 <b>{city} E-3 Visa Update</b>",
        f"<i>as of {ts} UTC</i>",
        f"<i>run_id: {escape(str(run_id))}</i>",
    ]
    if new:
        lines.append(f"\n✅ <b>New slots:</b> {', '.join(sorted(new))}")
    if gone:
        lines.append(f"\n❌ <b>Gone:</b> {', '.join(sorted(gone))}")
    if all_dates:
        calendars = render_calendars(all_dates)
        lines.append(
            f"\n📅 <b>{len(all_dates)} dates available:</b>"
            f"\n\n<pre>{calendars}</pre>"
        )
    else:
        lines.append("\n📅 <b>No dates currently available.</b>")
    lines.append(f'\n🔗 <a href="https://www.ustraveldocs.com/">Book now</a>')
    return "\n".join(lines)


PARSE_MODE = "HTML"


# ── Command handlers ──────────────────────────────────────────────

async def cmd_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the last-known availability for every monitored consulate."""
    snapshots = context.bot_data.get("snapshots", {})
    if not snapshots:
        await update.message.reply_text(
            "No data yet for Sydney, Melbourne, or Perth — "
            "waiting for a successful poll."
        )
        return

    for consulate, city in CONSULATES.items():
        snapshot = snapshots.get(consulate)
        if snapshot is None:
            await update.message.reply_text(
                f"No data yet for {city} — waiting for a successful poll."
            )
            continue
        await update.message.reply_text(
            format_dates_msg(
                snapshot["dates"],
                snapshot["updated_at"],
                snapshot["run_id"],
                consulate,
            ),
            parse_mode=PARSE_MODE,
        )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch fresh data for every monitored consulate right now."""
    page = context.bot_data["page"]
    snapshots = context.bot_data["snapshots"]
    fetch_lock = context.bot_data["fetch_lock"]
    sent = await update.message.reply_text("Fetching fresh data...")

    async with fetch_lock:
        payloads, errors = await fetch_consulates(page)
        for index, (consulate, city) in enumerate(CONSULATES.items()):
            current = None
            if consulate in payloads:
                current = snapshot_from_payload(payloads[consulate])
                response = format_dates_msg(
                    current["dates"],
                    current["updated_at"],
                    current["run_id"],
                    consulate,
                )
            else:
                error = escape(str(errors[consulate]))
                response = (
                    f"⚠️ <b>{city} E-3 Visa Update</b>\n"
                    f"Could not fetch fresh data: {error}"
                )

            try:
                if index == 0:
                    await sent.edit_text(response, parse_mode=PARSE_MODE)
                else:
                    await update.message.reply_text(
                        response, parse_mode=PARSE_MODE
                    )
            except Exception as exc:
                print(f"Telegram command response error [{city}]: {exc}")
                continue

            if current is not None:
                snapshots[consulate] = current

        if errors:
            failed = ", ".join(CONSULATES[code] for code in errors)
            print(f"Re-solving Vercel challenge after failures for: {failed}")
            try:
                await solve_vercel_challenge(page)
            except Exception as exc:
                print(f"Re-solve failed: {exc}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Australian E-3 Visa Bot</b>\n\n"
        "Monitors Sydney, Melbourne, and Perth.\n\n"
        "/dates — show last-known dates for all locations\n"
        "/ping  — fetch all locations right now\n"
        "/help  — show this message\n\n"
        f"Auto-polls every {POLL_INTERVAL // 60} minutes and alerts on changes.",
        parse_mode=PARSE_MODE,
    )


# ── Background polling ────────────────────────────────────────────

async def poll_and_notify(app: Application):
    page = app.bot_data["page"]
    snapshots = app.bot_data["snapshots"]
    fetch_lock = app.bot_data["fetch_lock"]

    async with fetch_lock:
        payloads, errors = await fetch_consulates(page)

        for consulate, city in CONSULATES.items():
            if consulate not in payloads:
                print(f"Poll error [{city}]: {errors[consulate]}")
                continue

            current = snapshot_from_payload(payloads[consulate])
            previous = snapshots.get(consulate)
            current_dates = current["dates"]
            updated_at = current["updated_at"]
            run_id = current["run_id"]
            ts = updated_at[:16].replace("T", " ")

            if previous is None:
                print(
                    f"[{ts}] [{city}] run_id={run_id} Initial snapshot — "
                    f"{len(current_dates)} dates"
                )
                msg = format_dates_msg(
                    current_dates, updated_at, run_id, consulate
                )
                try:
                    await app.bot.send_message(
                        chat_id=CHAT_ID, text=msg, parse_mode=PARSE_MODE
                    )
                except Exception as exc:
                    print(f"Telegram notification error [{city}]: {exc}")
                    continue
            elif current_dates != previous["dates"]:
                new = current_dates - previous["dates"]
                gone = previous["dates"] - current_dates
                parts = []
                if new:
                    parts.append(f"+{len(new)} new: {', '.join(sorted(new))}")
                if gone:
                    parts.append(f"-{len(gone)} gone: {', '.join(sorted(gone))}")
                print(
                    f"[{ts}] [{city}] run_id={run_id} CHANGE — "
                    f"{len(current_dates)} dates | {' | '.join(parts)}"
                )
                msg = format_change_msg(
                    new, gone, current_dates, updated_at, run_id, consulate
                )
                try:
                    await app.bot.send_message(
                        chat_id=CHAT_ID, text=msg, parse_mode=PARSE_MODE
                    )
                except Exception as exc:
                    print(f"Telegram notification error [{city}]: {exc}")
                    continue
            else:
                nearest = min(current_dates) if current_dates else "none"
                print(
                    f"[{ts}] [{city}] run_id={run_id} No change — "
                    f"{len(current_dates)} dates | nearest: {nearest}"
                )

            snapshots[consulate] = current

        if errors:
            failed = ", ".join(CONSULATES[code] for code in errors)
            print(f"Re-solving Vercel challenge after failures for: {failed}")
            try:
                await solve_vercel_challenge(page)
            except Exception as exc:
                print(f"Re-solve failed: {exc}")


# ── Main ──────────────────────────────────────────────────────────

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        browser_ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        )
        page = await browser_ctx.new_page()

        MAX_RETRIES = 10
        RETRY_DELAY = 15  # seconds
        initial_payloads = {}

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"Solving Vercel challenge (attempt {attempt}/{MAX_RETRIES})...")
                await solve_vercel_challenge(page)

                pending = [
                    consulate
                    for consulate in CONSULATES
                    if consulate not in initial_payloads
                ]
                payloads, errors = await fetch_consulates(page, pending)
                initial_payloads.update(payloads)

                for consulate, payload in payloads.items():
                    city = CONSULATES[consulate]
                    count = len(payload["interview_dates"])
                    run_id = payload.get("run_id", "N/A")
                    print(f"Loaded {count} {city} dates (run_id={run_id}).")

                if len(initial_payloads) == len(CONSULATES):
                    print("Loaded all consulates.\n")
                    break

                for consulate, error in errors.items():
                    print(f"Startup fetch failed [{CONSULATES[consulate]}]: {error}")
            except Exception as exc:
                print(f"Startup attempt {attempt} failed: {exc}")

            if attempt == MAX_RETRIES:
                if not initial_payloads:
                    print("All retries exhausted with no appointment data. Exiting.")
                    raise RuntimeError(
                        "Could not load any consulate appointment data"
                    )
                missing = ", ".join(
                    CONSULATES[consulate]
                    for consulate in CONSULATES
                    if consulate not in initial_payloads
                )
                print(
                    f"Startup retries exhausted for {missing}; "
                    "continuing with partial data."
                )
                break

            if len(initial_payloads) < len(CONSULATES):
                print(f"Retrying missing consulates in {RETRY_DELAY}s...")
                await asyncio.sleep(RETRY_DELAY)

        initial_snapshots = {
            consulate: snapshot_from_payload(payload)
            for consulate, payload in initial_payloads.items()
        }

        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.bot_data.update({
            "page": page,
            "snapshots": initial_snapshots,
            "fetch_lock": asyncio.Lock(),
        })

        app.add_handler(CommandHandler("dates", cmd_dates))
        app.add_handler(CommandHandler("ping", cmd_ping))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("start", cmd_help))

        async with app:
            for consulate, city in CONSULATES.items():
                snapshot = initial_snapshots.get(consulate)
                if snapshot is None:
                    startup_msg = (
                        f"⚠️ <b>{city} E-3 Visa Update</b>\n"
                        "No data available at startup; automatic polling will retry."
                    )
                else:
                    startup_msg = format_dates_msg(
                        snapshot["dates"],
                        snapshot["updated_at"],
                        snapshot["run_id"],
                        consulate,
                    )
                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    text=startup_msg,
                    parse_mode=PARSE_MODE,
                )

            await app.updater.start_polling()
            await app.start()
            print(
                "Bot running for Sydney, Melbourne, and Perth. "
                "Commands: /dates, /ping, /help\n"
            )

            try:
                while True:
                    await asyncio.sleep(POLL_INTERVAL)
                    await poll_and_notify(app)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                pass
            finally:
                await app.updater.stop()
                await app.stop()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
