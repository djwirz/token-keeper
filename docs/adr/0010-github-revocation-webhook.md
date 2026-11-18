# 10. Instant revocation through GitHub's App webhook

Date: 2026-11-18
Status: Accepted

## Context
When a user revokes the GitHub App in their settings, their tokens stop working at once. Without help from GitHub, Token Keeper only notices at the next sync, up to 6 hours later. The README GIF is meant to show the whole lifecycle, and "revoke, then wait six hours" doesn't show anything.

GitHub sends every GitHub App a `github_app_authorization` webhook when a user revokes their authorization. The app gets it by default and needs no permission for it. The payload identifies the user through `sender`. GitHub signs deliveries with `X-Hub-Signature-256` (hex HMAC-SHA256 of the body) when the app has a webhook secret.

## Options considered
1. **Handle `github_app_authorization` in a small `github-webhook` λ.** The card flips within seconds of revoking. Costs one inbound route, one secret and a lookup item from GitHub user id to account.
2. **Detect revocation at the next sync only.** No new parts, but the flip is hours later, and the GIF needs the demo cadence knob plus a wait.
3. **A "verify now" button on the receiver.** Shows the 401 path on demand, but it's a second way into Token Keeper and a manual step in the demo.

## Decision
Option 1. Signature checked before parsing; only `github_app_authorization` is acted on; the account moves to `needs_reauth` with reason `revoked`, its tokens are deleted, and `account.updated` is sent. The write takes over any in-flight lease (ADR 0004). Unknown users and other events get a 2xx and a log line. Handling is idempotent, so redeliveries are harmless.

Detection at the next sync stays as a backstop (a 401 that a refresh can't fix), for deliveries GitHub drops.

## Consequences
- The demo shows the full loop in one take: connect, sync, revoke, flip, reconnect.
- A second inbound surface. Its worst case, even if a forged request passed the signature check, is forcing a re-auth: it can't read or write tokens.
- Token Keeper still reports to the main app through one endpoint. This is an inbound route from GitHub, not a second reporting channel.
