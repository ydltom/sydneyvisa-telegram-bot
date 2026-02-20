import asyncio
import calendar as cal_mod
import json
import os
from collections import defaultdict
from datetime import date
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API_URL = "https://migratemate.co/api/visa-processing/interview-slots?consulate=SYDNEY"
BLOG_URL = "https://migratemate.co/blog/e3-visa-appointment-calendar"
POLL_INTERVAL = 300  # seconds between polls (5 minutes)


async def fetch_dates(page: Page) -> dict:
    response = await page.goto(API_URL, wait_until="networkidle")
    body = await response.text()
    return json.loads(body)


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
                    cells.append("  ")
                elif day in avail:
                    cells.append(f"{day:>2}")
                else:
                    cells.append(" ·")
            lines.append(" ".join(cells).rstrip())

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def format_dates_msg(dates: set[str], updated_at: str) -> str:
    ts = updated_at[:16].replace("T", " ")
    calendars = render_calendars(dates)
    return (
        f"🇦🇺 <b>Sydney E-3 Visa Update</b>\n"
        f"<i>as of {ts} UTC</i>\n\n"
        f"📅 <b>{len(dates)} dates available:</b>\n\n"
        f"<pre>{calendars}</pre>\n\n"
        f'🔗 <a href="https://www.ustraveldocs.com/">Book now</a>'
    )


def format_change_msg(new: set[str], gone: set[str], all_dates: set[str], updated_at: str) -> str:
    ts = updated_at[:16].replace("T", " ")
    lines = [
        f"🇦🇺 <b>Sydney E-3 Visa Update</b>",
        f"<i>as of {ts} UTC</i>",
    ]
    if new:
        lines.append(f"\n✅ <b>New slots:</b> {', '.join(sorted(new))}")
    if gone:
        lines.append(f"\n❌ <b>Gone:</b> {', '.join(sorted(gone))}")
    calendars = render_calendars(all_dates)
    lines.append(f"\n📅 <b>{len(all_dates)} dates available:</b>\n\n<pre>{calendars}</pre>")
    lines.append(f'\n🔗 <a href="https://www.ustraveldocs.com/">Book now</a>')
    return "\n".join(lines)


PARSE_MODE = "HTML"


# ── Command handlers ──────────────────────────────────────────────

async def cmd_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the last-known available dates (cached)."""
    last_dates = context.bot_data.get("last_dates", set())
    last_updated = context.bot_data.get("last_updated")
    if not last_dates:
        await update.message.reply_text("No data yet — waiting for first poll.")
        return
    await update.message.reply_text(
        format_dates_msg(last_dates, last_updated),
        parse_mode=PARSE_MODE,
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch fresh data from the API right now."""
    page = context.bot_data["page"]
    sent = await update.message.reply_text("Fetching fresh data...")
    try:
        data = await fetch_dates(page)
        current_dates = set(data["interview_dates"])
        updated_at = data["updated_at"]
        context.bot_data["last_dates"] = current_dates
        context.bot_data["last_updated"] = updated_at
        await sent.edit_text(
            format_dates_msg(current_dates, updated_at),
            parse_mode=PARSE_MODE,
        )
    except Exception as e:
        await sent.edit_text(f"Error fetching data: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Sydney E-3 Visa Bot</b>\n\n"
        "/dates — show last-known available dates\n"
        "/ping  — fetch fresh data right now\n"
        "/help  — show this message\n\n"
        f"Auto-polls every {POLL_INTERVAL // 60} minutes and alerts on changes.",
        parse_mode=PARSE_MODE,
    )


# ── Background polling ────────────────────────────────────────────

async def poll_and_notify(app: Application):
    page = app.bot_data["page"]
    try:
        data = await fetch_dates(page)
        current_dates = set(data["interview_dates"])
        updated_at = data["updated_at"]
        last_dates = app.bot_data.get("last_dates", set())

        ts = updated_at[:16].replace("T", " ")

        if current_dates != last_dates and last_dates:
            new = current_dates - last_dates
            gone = last_dates - current_dates
            if new or gone:
                parts = []
                if new:
                    parts.append(f"+{len(new)} new: {', '.join(sorted(new))}")
                if gone:
                    parts.append(f"-{len(gone)} gone: {', '.join(sorted(gone))}")
                print(f"[{ts}] CHANGE — {len(current_dates)} dates | {' | '.join(parts)}")
                msg = format_change_msg(new, gone, current_dates, updated_at)
                await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode=PARSE_MODE)
        else:
            nearest = min(sorted(current_dates)) if current_dates else "none"
            print(f"[{ts}] No change — {len(current_dates)} dates | nearest: {nearest}")

        app.bot_data["last_dates"] = current_dates
        app.bot_data["last_updated"] = updated_at

    except Exception as e:
        print(f"Poll error: {e}")
        try:
            print("Re-solving Vercel challenge...")
            await page.goto(BLOG_URL, wait_until="networkidle")
            print("Challenge re-solved.")
        except Exception as inner:
            print(f"Re-solve failed: {inner}")


# ── Main ──────────────────────────────────────────────────────────

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        browser_ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        )
        page = await browser_ctx.new_page()

        print("Solving Vercel challenge...")
        await page.goto(BLOG_URL, wait_until="networkidle")
        print("Challenge passed.")

        data = await fetch_dates(page)
        initial_dates = set(data["interview_dates"])
        updated_at = data["updated_at"]
        print(f"Loaded {len(initial_dates)} dates.\n")

        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.bot_data.update({
            "page": page,
            "last_dates": initial_dates,
            "last_updated": updated_at,
        })

        app.add_handler(CommandHandler("dates", cmd_dates))
        app.add_handler(CommandHandler("ping", cmd_ping))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("start", cmd_help))

        async with app:
            startup_msg = format_dates_msg(initial_dates, updated_at)
            await app.bot.send_message(chat_id=CHAT_ID, text=startup_msg, parse_mode=PARSE_MODE)

            await app.updater.start_polling()
            await app.start()
            print("Bot running. Commands: /dates, /ping, /help\n")

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
