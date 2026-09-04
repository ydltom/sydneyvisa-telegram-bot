import asyncio
import io
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import Forbidden, InvalidToken, TimedOut

import visa_bot


def make_client(responder):
    return httpx.AsyncClient(transport=httpx.MockTransport(responder))


def valid_payload(dates=None):
    return {
        "interview_dates": [] if dates is None else dates,
        "updated_at": "2026-09-04T20:00:00Z",
        "run_id": "test-run",
    }


def bot_data(client):
    return {
        "client": client,
        "fetch_lock": asyncio.Lock(),
        "notification_lock": asyncio.Lock(),
        "notification_chat_id": "123",
        "command_chat_id": "123",
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


class FetchDatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_vercel_challenge_is_classified_and_preserves_retry_after(self):
        def responder(request):
            return httpx.Response(
                429,
                request=request,
                headers={
                    "x-vercel-mitigated": "challenge",
                    "retry-after": "120",
                },
                text="Vercel Security Checkpoint",
            )

        async with make_client(responder) as client:
            with self.assertRaises(visa_bot.UpstreamUnavailable) as raised:
                await visa_bot.fetch_dates(client)

        self.assertIn("Vercel challenge", str(raised.exception))
        self.assertEqual(raised.exception.retry_after, 120)

    async def test_html_checkpoint_is_detected_without_special_header(self):
        for status_code in (200, 403):
            with self.subTest(status_code=status_code):

                def responder(request):
                    return httpx.Response(
                        status_code,
                        request=request,
                        text="<title>Vercel Security Checkpoint</title>",
                    )

                async with make_client(responder) as client:
                    with self.assertRaisesRegex(
                        visa_bot.UpstreamUnavailable, "Vercel challenge"
                    ):
                        await visa_bot.fetch_dates(client)

    async def test_valid_payload_is_returned(self):
        payload = valid_payload(["2026-09-30"])

        def responder(request):
            return httpx.Response(200, request=request, json=payload)

        async with make_client(responder) as client:
            self.assertEqual(await visa_bot.fetch_dates(client), payload)

    async def test_updated_at_is_normalized_to_utc(self):
        payload = {
            **valid_payload(),
            "updated_at": "2026-09-04T16:00:00-04:00",
        }

        def responder(request):
            return httpx.Response(200, request=request, json=payload)

        async with make_client(responder) as client:
            result = await visa_bot.fetch_dates(client)

        self.assertEqual(result["updated_at"], "2026-09-04T20:00:00Z")

    async def test_invalid_payload_is_rejected(self):
        invalid_payloads = (
            {"updated_at": "2026-09-04T20:00:00Z"},
            valid_payload(["09/30/2026"]),
            {**valid_payload(), "updated_at": "2026-09-04 20:00:00"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):

                def responder(request):
                    return httpx.Response(200, request=request, json=payload)

                async with make_client(responder) as client:
                    with self.assertRaises(visa_bot.UpstreamUnavailable):
                        await visa_bot.fetch_dates(client)


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_poll_is_non_fatal(self):
        def responder(request):
            return httpx.Response(
                429,
                request=request,
                headers={"x-vercel-mitigated": "challenge", "retry-after": "90"},
                text="Vercel Security Checkpoint",
            )

        async with make_client(responder) as client:
            app = SimpleNamespace(
                bot_data=bot_data(client),
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            outcome = await visa_bot.poll_and_notify(app)

        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.retry_after, 90)
        self.assertEqual(app.bot_data["consecutive_errors"], 1)
        self.assertIsNone(app.bot_data["last_dates"])
        app.bot.send_message.assert_not_awaited()

    async def test_empty_first_snapshot_is_valid_and_sent_once(self):
        def responder(request):
            return httpx.Response(200, request=request, json=valid_payload())

        async with make_client(responder) as client:
            app = SimpleNamespace(
                bot_data=bot_data(client),
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            first = await visa_bot.poll_and_notify(app)
            second = await visa_bot.poll_and_notify(app)

        self.assertTrue(first.succeeded)
        self.assertTrue(second.succeeded)
        self.assertEqual(app.bot_data["last_dates"], set())
        self.assertEqual(app.bot.send_message.await_count, 1)
        first_message = app.bot.send_message.await_args.kwargs["text"]
        self.assertIn("No dates currently available", first_message)

    async def test_empty_to_nonempty_sends_change(self):
        payloads = [valid_payload(), valid_payload(["2026-09-30"])]

        def responder(request):
            return httpx.Response(200, request=request, json=payloads.pop(0))

        async with make_client(responder) as client:
            app = SimpleNamespace(
                bot_data=bot_data(client),
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            await visa_bot.poll_and_notify(app)
            await visa_bot.poll_and_notify(app)

        self.assertEqual(app.bot.send_message.await_count, 2)
        change_message = app.bot.send_message.await_args.kwargs["text"]
        self.assertIn("New slots", change_message)
        self.assertIn("2026-09-30", change_message)

    async def test_failed_notification_is_retried(self):
        def responder(request):
            return httpx.Response(200, request=request, json=valid_payload())

        send_message = AsyncMock(side_effect=[RuntimeError("Telegram down"), None])
        async with make_client(responder) as client:
            app = SimpleNamespace(
                bot_data=bot_data(client),
                bot=SimpleNamespace(send_message=send_message),
            )
            await visa_bot.poll_and_notify(app)
            self.assertEqual(len(app.bot_data["pending_notifications"]), 1)
            await visa_bot.poll_and_notify(app)

        self.assertEqual(send_message.await_count, 2)
        self.assertEqual(app.bot_data["pending_notifications"], [])

    async def test_pending_notification_retries_even_when_source_then_fails(self):
        responses = [
            lambda request: httpx.Response(200, request=request, json=valid_payload()),
            lambda request: httpx.Response(
                429,
                request=request,
                headers={"x-vercel-mitigated": "challenge"},
                text="Vercel Security Checkpoint",
            ),
        ]

        def responder(request):
            return responses.pop(0)(request)

        send_message = AsyncMock(side_effect=[RuntimeError("Telegram down"), None])
        async with make_client(responder) as client:
            app = SimpleNamespace(
                bot_data=bot_data(client),
                bot=SimpleNamespace(send_message=send_message),
            )
            await visa_bot.poll_and_notify(app)
            self.assertEqual(len(app.bot_data["pending_notifications"]), 1)
            outcome = await visa_bot.poll_and_notify(app)

        self.assertFalse(outcome.succeeded)
        self.assertEqual(send_message.await_count, 2)
        self.assertEqual(app.bot_data["pending_notifications"], [])

    async def test_delivery_failures_remain_queued_and_visible(self):
        for error in (Forbidden("bot was blocked"), TimedOut("network timeout")):
            with self.subTest(error=type(error).__name__):
                data = bot_data(None)
                data["pending_notifications"].append("appointment alert")
                bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))

                await visa_bot.flush_pending_notifications(bot, data)

                self.assertEqual(data["pending_notifications"], ["appointment alert"])
                self.assertEqual(data["notification_error"], type(error).__name__)
                self.assertIsNotNone(data["notification_error_at"])

    async def test_invalid_token_during_delivery_is_fatal(self):
        data = bot_data(None)
        data["pending_notifications"].append("appointment alert")
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=InvalidToken("secret token rejected"))
        )

        with self.assertRaises(InvalidToken):
            await visa_bot.flush_pending_notifications(bot, data)

    async def test_enqueue_cannot_drop_an_in_flight_notification(self):
        data = bot_data(None)
        original_messages = [f"alert-{index}" for index in range(20)]
        data["pending_notifications"].extend(original_messages)
        data["last_enqueued_dates"] = set()
        first_send_started = asyncio.Event()
        release_first_send = asyncio.Event()
        sent_messages = []

        async def send_message(**kwargs):
            sent_messages.append(kwargs["text"])
            if len(sent_messages) == 1:
                first_send_started.set()
                await release_first_send.wait()

        bot = SimpleNamespace(send_message=send_message)
        flush_task = asyncio.create_task(
            visa_bot.flush_pending_notifications(bot, data)
        )
        await first_send_started.wait()
        enqueue_task = asyncio.create_task(
            visa_bot.enqueue_payload_notification(data, valid_payload(["2026-09-30"]))
        )
        await asyncio.sleep(0)
        self.assertFalse(enqueue_task.done())

        release_first_send.set()
        await flush_task
        await enqueue_task
        await visa_bot.flush_pending_notifications(bot, data)

        self.assertEqual(sent_messages[:20], original_messages)
        self.assertEqual(len(sent_messages), 21)
        self.assertEqual(data["pending_notifications"], [])


class ConfigurationAndCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_bypass_header_is_opt_in_and_host_scoped(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("x-vercel-protection-bypass", visa_bot.build_api_headers())

        with patch.dict(
            os.environ,
            {"MIGRATEMATE_VERCEL_BYPASS_SECRET": "secret-value"},
            clear=True,
        ):
            with patch.object(visa_bot, "API_URL", visa_bot.DEFAULT_API_URL):
                self.assertEqual(
                    visa_bot.build_api_headers()["x-vercel-protection-bypass"],
                    "secret-value",
                )
            with patch.object(visa_bot, "API_URL", "https://example.com/data"):
                with self.assertRaisesRegex(RuntimeError, "only be sent"):
                    visa_bot.build_api_headers()
            with patch.object(
                visa_bot,
                "API_URL",
                "http://migratemate.co/api/visa-processing/interview-slots",
            ):
                with self.assertRaisesRegex(RuntimeError, "over HTTPS"):
                    visa_bot.build_api_headers()

    def test_missing_required_environment_has_clear_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_TOKEN, CHAT_ID"):
                visa_bot.required_environment()

    async def test_ping_cooldown_avoids_another_request(self):
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=SimpleNamespace(reply_text=reply_text),
        )
        data = {
            "notification_chat_id": "123",
            "command_chat_id": "123",
            "last_manual_fetch_at": time.monotonic(),
        }
        context = SimpleNamespace(bot_data=data)

        await visa_bot.cmd_ping(update, context)

        reply_text.assert_awaited_once()
        self.assertIn("Please wait", reply_text.await_args.args[0])

    async def test_ping_enqueues_channel_alert_without_suppressing_next_poll(self):
        def responder(request):
            return httpx.Response(200, request=request, json=valid_payload())

        sent = SimpleNamespace(edit_text=AsyncMock())
        reply_text = AsyncMock(return_value=sent)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=456),
            message=SimpleNamespace(reply_text=reply_text),
        )

        async with make_client(responder) as client:
            data = bot_data(client)
            data["command_chat_id"] = "456"
            send_message = AsyncMock()
            bot = SimpleNamespace(send_message=send_message)
            context = SimpleNamespace(bot_data=data, bot=bot)

            await visa_bot.cmd_ping(update, context)
            await visa_bot.poll_and_notify(SimpleNamespace(bot_data=data, bot=bot))

        send_message.assert_awaited_once()
        self.assertEqual(send_message.await_args.kwargs["chat_id"], "123")
        self.assertEqual(data["last_enqueued_dates"], set())

    async def test_ping_reply_failure_does_not_change_upstream_health(self):
        def responder(request):
            return httpx.Response(200, request=request, json=valid_payload())

        sent = SimpleNamespace(
            edit_text=AsyncMock(side_effect=RuntimeError("Telegram edit failed"))
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=sent)),
        )

        async with make_client(responder) as client:
            data = bot_data(client)
            context = SimpleNamespace(
                bot_data=data,
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            await visa_bot.cmd_ping(update, context)

        self.assertIsNone(data["last_error"])
        self.assertIsNotNone(data["last_success_at"])
        sent.edit_text.assert_awaited_once()

    async def test_ping_honors_active_retry_backoff(self):
        client = SimpleNamespace(get=AsyncMock())
        data = bot_data(client)
        data["retry_not_before_monotonic"] = time.monotonic() + 120
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(bot_data=data)

        await visa_bot.cmd_ping(update, context)

        client.get.assert_not_awaited()
        self.assertIn("Please wait", reply_text.await_args.args[0])

    async def test_status_surfaces_degraded_telegram_delivery(self):
        data = bot_data(None)
        data["notification_error"] = "Forbidden"
        data["notification_error_at"] = "2026-09-04 20:00:00 UTC"
        data["pending_notifications"].append("appointment alert")
        reply_text = AsyncMock()
        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))

        await visa_bot.cmd_status(update, SimpleNamespace(bot_data=data))

        status = reply_text.await_args.args[0]
        self.assertIn("Telegram delivery: degraded", status)
        self.assertIn("Last delivery error: Forbidden", status)
        self.assertIn("Pending alerts: 1", status)

    async def test_manual_retry_after_also_defers_automatic_poll(self):
        request_count = 0

        def responder(request):
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                429,
                request=request,
                headers={
                    "x-vercel-mitigated": "challenge",
                    "retry-after": "3600",
                },
                text="Vercel Security Checkpoint",
            )

        sent = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=sent)),
        )

        async with make_client(responder) as client:
            data = bot_data(client)
            await visa_bot.cmd_ping(update, SimpleNamespace(bot_data=data))
            outcome = await visa_bot.poll_and_notify(
                SimpleNamespace(
                    bot_data=data,
                    bot=SimpleNamespace(send_message=AsyncMock()),
                )
            )

        self.assertFalse(outcome.succeeded)
        self.assertEqual(request_count, 1)
        self.assertGreaterEqual(visa_bot.retry_wait_seconds(data), 3599)


class ProcessBoundaryTests(unittest.TestCase):
    def test_invalid_telegram_token_is_never_printed(self):
        rejected_token = "123456:super-secret-token"

        async def rejected_main():
            raise InvalidToken(f"The token `{rejected_token}` was rejected")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(visa_bot, "main", rejected_main),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = visa_bot.run()

        self.assertEqual(exit_code, 1)
        self.assertNotIn(rejected_token, stdout.getvalue())
        self.assertNotIn(rejected_token, stderr.getvalue())
        self.assertIn("Telegram rejected TELEGRAM_TOKEN", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
