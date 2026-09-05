import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("CHAT_ID", "123456")

import visa_bot


def payload(dates, run_id="test-run", updated_at="2026-09-05T12:00:00Z"):
    return {
        "interview_dates": dates,
        "updated_at": updated_at,
        "run_id": run_id,
    }


def snapshot(dates, run_id="old-run", updated_at="2026-09-05T11:00:00Z"):
    return visa_bot.snapshot_from_payload(payload(dates, run_id, updated_at))


class FakePage:
    def __init__(self, responses):
        self.responses = {
            consulate: list(values) if isinstance(values, list) else [values]
            for consulate, values in responses.items()
        }
        self.urls = []

    async def evaluate(self, script, url):
        self.urls.append(url)
        consulate = url.rsplit("=", 1)[1]
        values = self.responses[consulate]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return {"status": 200, "body": json.dumps(value)}


def make_app(page, snapshots):
    return SimpleNamespace(
        bot_data={
            "page": page,
            "snapshots": snapshots,
            "fetch_lock": asyncio.Lock(),
        },
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_all_consulates_in_stable_order(self):
        page = FakePage(
            {
                "SYDNEY": payload(["2026-09-10"], "sydney-run"),
                "MELBOURNE": payload(["2026-10-12"], "melbourne-run"),
                "PERTH": payload(["2026-09-08"], "perth-run"),
            }
        )

        results, errors = await visa_bot.fetch_consulates(page)

        self.assertEqual(errors, {})
        self.assertEqual(list(results), ["SYDNEY", "MELBOURNE", "PERTH"])
        self.assertEqual(
            page.urls,
            [
                visa_bot.API_URL_TEMPLATE.format(consulate="SYDNEY"),
                visa_bot.API_URL_TEMPLATE.format(consulate="MELBOURNE"),
                visa_bot.API_URL_TEMPLATE.format(consulate="PERTH"),
            ],
        )

    async def test_one_failure_does_not_block_other_consulates(self):
        page = FakePage(
            {
                "SYDNEY": payload(["2026-09-10"]),
                "MELBOURNE": RuntimeError("Melbourne unavailable"),
                "PERTH": payload(["2026-09-08"]),
            }
        )

        results, errors = await visa_bot.fetch_consulates(page)

        self.assertEqual(set(results), {"SYDNEY", "PERTH"})
        self.assertEqual(set(errors), {"MELBOURNE"})
        self.assertEqual(len(page.urls), 3)

    async def test_rejects_unknown_consulate_and_invalid_dates(self):
        page = FakePage({"SYDNEY": payload(["not-a-date"])})

        with self.assertRaisesRegex(ValueError, "Unsupported consulate"):
            await visa_bot.fetch_dates(page, "BRISBANE")
        with self.assertRaisesRegex(ValueError, "invalid interview date"):
            await visa_bot.fetch_dates(page, "SYDNEY")


class FormattingTests(unittest.TestCase):
    def test_messages_name_each_consulate(self):
        for consulate, city in visa_bot.CONSULATES.items():
            with self.subTest(consulate=consulate):
                full = visa_bot.format_dates_msg(
                    {"2026-09-10"},
                    "2026-09-05T12:00:00Z",
                    "run-id",
                    consulate,
                )
                change = visa_bot.format_change_msg(
                    {"2026-09-10"},
                    set(),
                    {"2026-09-10"},
                    "2026-09-05T12:00:00Z",
                    "run-id",
                    consulate,
                )
                self.assertIn(f"{city} E-3 Visa Update", full)
                self.assertIn(f"{city} E-3 Visa Update", change)

    def test_empty_snapshot_is_reported_as_valid_zero_availability(self):
        message = visa_bot.format_dates_msg(
            set(), "2026-09-05T12:00:00Z", consulate="PERTH"
        )

        self.assertIn("Perth", message)
        self.assertIn("No dates currently available", message)
        self.assertNotIn("<pre>", message)

    def test_run_id_is_html_escaped(self):
        message = visa_bot.format_dates_msg(
            set(), "2026-09-05T12:00:00Z", "<unsafe>", "MELBOURNE"
        )

        self.assertIn("&lt;unsafe&gt;", message)
        self.assertNotIn("<unsafe>", message)


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_changes_are_compared_and_alerted_per_consulate(self):
        page = FakePage(
            {
                "SYDNEY": payload(["2026-09-10"], "new-sydney"),
                "MELBOURNE": payload(["2026-10-12"], "new-melbourne"),
                "PERTH": payload([], "new-perth"),
            }
        )
        app = make_app(
            page,
            {
                "SYDNEY": snapshot([]),
                "MELBOURNE": snapshot(["2026-10-12"]),
                "PERTH": snapshot(["2026-09-08"]),
            },
        )

        await visa_bot.poll_and_notify(app)

        self.assertEqual(app.bot.send_message.await_count, 2)
        messages = [
            call.kwargs["text"] for call in app.bot.send_message.await_args_list
        ]
        self.assertIn("Sydney E-3 Visa Update", messages[0])
        self.assertIn("New slots", messages[0])
        self.assertIn("Perth E-3 Visa Update", messages[1])
        self.assertIn("Gone", messages[1])
        self.assertIn("No dates currently available", messages[1])
        self.assertEqual(app.bot_data["snapshots"]["SYDNEY"]["dates"], {"2026-09-10"})
        self.assertEqual(app.bot_data["snapshots"]["MELBOURNE"]["dates"], {"2026-10-12"})
        self.assertEqual(app.bot_data["snapshots"]["PERTH"]["dates"], set())

    async def test_failed_consulate_keeps_its_last_good_snapshot(self):
        page = FakePage(
            {
                "SYDNEY": payload(["2026-09-10"], "new-sydney"),
                "MELBOURNE": RuntimeError("temporary failure"),
                "PERTH": payload(["2026-09-08"], "new-perth"),
            }
        )
        old_melbourne = snapshot(["2026-10-12"], "old-melbourne")
        app = make_app(
            page,
            {
                "SYDNEY": snapshot(["2026-09-10"]),
                "MELBOURNE": old_melbourne,
                "PERTH": snapshot(["2026-09-08"]),
            },
        )

        with patch.object(
            visa_bot, "solve_vercel_challenge", new=AsyncMock()
        ) as solve:
            await visa_bot.poll_and_notify(app)

        app.bot.send_message.assert_not_awaited()
        self.assertIs(app.bot_data["snapshots"]["MELBOURNE"], old_melbourne)
        self.assertEqual(
            app.bot_data["snapshots"]["SYDNEY"]["run_id"], "new-sydney"
        )
        self.assertEqual(app.bot_data["snapshots"]["PERTH"]["run_id"], "new-perth")
        solve.assert_awaited_once_with(page)

    async def test_first_success_after_missing_startup_sends_full_snapshot(self):
        page = FakePage(
            {
                "SYDNEY": payload([], "sydney-run"),
                "MELBOURNE": payload(["2026-10-12"], "melbourne-run"),
                "PERTH": payload([], "perth-run"),
            }
        )
        app = make_app(
            page,
            {
                "SYDNEY": snapshot([]),
                "PERTH": snapshot([]),
            },
        )

        await visa_bot.poll_and_notify(app)

        app.bot.send_message.assert_awaited_once()
        message = app.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Melbourne E-3 Visa Update", message)
        self.assertNotIn("New slots", message)
        self.assertEqual(
            app.bot_data["snapshots"]["MELBOURNE"]["dates"], {"2026-10-12"}
        )


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_dates_reports_valid_empty_snapshots_for_all_cities(self):
        reply_text = AsyncMock()
        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
        context = SimpleNamespace(
            bot_data={
                "snapshots": {
                    consulate: snapshot([]) for consulate in visa_bot.CONSULATES
                }
            }
        )

        await visa_bot.cmd_dates(update, context)

        self.assertEqual(reply_text.await_count, 3)
        messages = [call.args[0] for call in reply_text.await_args_list]
        self.assertEqual(
            [
                city
                for city in visa_bot.CONSULATES.values()
                if any(city in message for message in messages)
            ],
            ["Sydney", "Melbourne", "Perth"],
        )
        self.assertTrue(
            all("No dates currently available" in message for message in messages)
        )

    async def test_ping_reports_all_cities_and_preserves_failed_city_cache(self):
        page = FakePage(
            {
                "SYDNEY": payload(["2026-09-10"], "sydney-new"),
                "MELBOURNE": RuntimeError("temporary failure"),
                "PERTH": payload(["2026-09-08"], "perth-new"),
            }
        )
        old_melbourne = snapshot(["2026-10-12"], "melbourne-old")
        sent = SimpleNamespace(edit_text=AsyncMock())
        reply_text = AsyncMock(return_value=sent)
        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
        context = SimpleNamespace(
            bot_data={
                "page": page,
                "snapshots": {"MELBOURNE": old_melbourne},
                "fetch_lock": asyncio.Lock(),
            }
        )

        with patch.object(
            visa_bot, "solve_vercel_challenge", new=AsyncMock()
        ):
            await visa_bot.cmd_ping(update, context)

        self.assertEqual(reply_text.await_count, 3)
        self.assertIn("Sydney E-3 Visa Update", sent.edit_text.await_args.args[0])
        self.assertIn(
            "Melbourne E-3 Visa Update", reply_text.await_args_list[1].args[0]
        )
        self.assertIn(
            "Could not fetch fresh data", reply_text.await_args_list[1].args[0]
        )
        self.assertIn("Perth E-3 Visa Update", reply_text.await_args_list[2].args[0])
        self.assertIs(context.bot_data["snapshots"]["MELBOURNE"], old_melbourne)

    async def test_ping_only_commits_a_city_after_its_response_is_delivered(self):
        page = FakePage(
            {
                "SYDNEY": payload(["2026-09-10"], "sydney-new"),
                "MELBOURNE": payload(["2026-10-12"], "melbourne-new"),
                "PERTH": payload(["2026-09-08"], "perth-new"),
            }
        )
        old_snapshots = {
            "SYDNEY": snapshot([], "sydney-old"),
            "MELBOURNE": snapshot([], "melbourne-old"),
            "PERTH": snapshot([], "perth-old"),
        }
        sent = SimpleNamespace(edit_text=AsyncMock())
        reply_text = AsyncMock(
            side_effect=[sent, RuntimeError("Telegram send failed"), sent]
        )
        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
        context = SimpleNamespace(
            bot_data={
                "page": page,
                "snapshots": dict(old_snapshots),
                "fetch_lock": asyncio.Lock(),
            }
        )

        await visa_bot.cmd_ping(update, context)

        self.assertEqual(
            context.bot_data["snapshots"]["SYDNEY"]["run_id"], "sydney-new"
        )
        self.assertIs(
            context.bot_data["snapshots"]["MELBOURNE"],
            old_snapshots["MELBOURNE"],
        )
        self.assertEqual(
            context.bot_data["snapshots"]["PERTH"]["run_id"], "perth-new"
        )

    async def test_overlapping_pings_serialize_page_access(self):
        class SlowPage:
            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def evaluate(self, script, url):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.005)
                self.active -= 1
                return {"status": 200, "body": json.dumps(payload([]))}

        def update_for_ping():
            sent = SimpleNamespace(edit_text=AsyncMock())
            return SimpleNamespace(
                message=SimpleNamespace(reply_text=AsyncMock(return_value=sent))
            )

        page = SlowPage()
        context = SimpleNamespace(
            bot_data={
                "page": page,
                "snapshots": {},
                "fetch_lock": asyncio.Lock(),
            }
        )

        await asyncio.gather(
            visa_bot.cmd_ping(update_for_ping(), context),
            visa_bot.cmd_ping(update_for_ping(), context),
        )

        self.assertEqual(page.max_active, 1)


if __name__ == "__main__":
    unittest.main()
