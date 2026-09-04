# Sydney Visa Telegram Bot

This bot polls Sydney E-3 interview availability and reports changes to a
Telegram chat.

## Railway configuration

Railway builds the included `Dockerfile`. Configure these required variables:

- `TELEGRAM_TOKEN`: token from BotFather
- `CHAT_ID`: numeric Telegram chat or channel ID that should receive alerts

Optional variables:

- `POLL_INTERVAL`: successful polling interval in seconds (default `300`)
- `REQUEST_TIMEOUT`: HTTP timeout in seconds (default `30`)
- `MAX_RETRY_INTERVAL`: maximum client-generated failure backoff in seconds
  (default `3600`); a server `Retry-After` may take precedence, capped at 24 hours
- `MANUAL_FETCH_COOLDOWN`: minimum seconds between `/ping` calls (default `60`)
- `COMMAND_CHAT_ID`: numeric chat allowed to use `/ping` (defaults to `CHAT_ID`)
- `MIGRATEMATE_API_URL`: an authorized, schema-compatible data endpoint
- `MIGRATEMATE_VERCEL_BYPASS_SECRET`: an automation secret issued by the
  owner of the MigrateMate Vercel project

The bot expects the endpoint to return an object with an `interview_dates`
array of ISO dates and a timezone-aware `updated_at` timestamp.

## Upstream access

MigrateMate currently serves a Vercel Security Checkpoint to automated
requests from hosted infrastructure. [Vercel documents that automated tools
and scripts cannot establish challenge sessions](https://vercel.com/docs/vercel-firewall/firewall-concepts),
so the bot does not attempt to defeat that protection.

When the endpoint is unavailable, the Telegram worker remains online, records
the error, and retries with exponential backoff plus jitter. Client-generated
backoff is capped by `MAX_RETRY_INTERVAL`; an upstream `Retry-After` may take
precedence and is capped at 24 hours. Reliable appointment updates require an
authorized endpoint or an automation bypass secret supplied by the endpoint
owner. The optional secret is sent only over HTTPS to `migratemate.co`, is never
logged, and is not forwarded through redirects.
[Vercel documents the owner-managed automation bypass and its
limits](https://vercel.com/docs/deployment-protection/methods-to-bypass-deployment-protection/protection-bypass-automation).

## Commands

- `/dates`: show the last valid snapshot, including a valid zero-date snapshot
- `/ping`: request a refresh (restricted to `COMMAND_CHAT_ID`, which defaults to
  `CHAT_ID`, and rate-limited)
- `/status`: show worker and upstream health
- `/help`: show command help

## Tests

```bash
python -m unittest discover -s tests -v
```
