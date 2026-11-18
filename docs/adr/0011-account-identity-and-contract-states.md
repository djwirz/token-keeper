# 11. Account identity and the states in the contract

Date: 2026-11-18
Status: Accepted

## Context
The webhook contract (ADR 0005) exposes `account_id`, `version`, `state` and `state_reason` to the main app. Once a main app depends on these, changing what they mean is expensive. Three questions had to be settled: what happens on reconnect, which states the main app sees, and whether there's a disconnect path.

The project's purpose shapes the answers: it exists to prove a careful refresh lifecycle and show it working in a GIF, with one demo user. Anything that adds states or interfaces without adding to that proof is out.

## Options considered
1. **Two states, stable identity, no disconnect API.** `active` and `needs_reauth`; one account per main-app user, kept across reconnects; users leave by revoking on GitHub.
2. **Expose transient trouble too** (a `refresh_failing` or `degraded` state). More honest about hiccups, but it needs thresholds, and an outage would flip every card.
3. **Add a `disconnected` state and a disconnect endpoint** for the main app. Complete, but a second inbound API for a case the demo never exercises.

## Decision
Option 1.

- **Identity:** `account_id` is created on a main-app user's first connect and kept for good. A reconnect updates the same account, `version` keeps counting, and `account.connected` is sent again. If the user reconnects with a different GitHub account, that account replaces the old one's GitHub fields and snapshot.
- **One to one:** a GitHub account can be linked to only one main-app user. A connect that would break this stores nothing and returns `result=already_linked`.
- **States:** `active` or `needs_reauth`. `state_reason` is `null`, `revoked`, `refresh_rejected`, `refresh_expired` or `unauthorized`. Transient errors (5xx, timeouts, rate limits) never change state and send no event; the receiver can see staleness from `snapshot.fetched_at`.
- **Leaving:** a user leaves by revoking the app on GitHub (ADR 0010). Tokens are deleted on `needs_reauth`. Deleting an account's data entirely is an operator step in the runbook, with no event.

## Consequences
- The main app handles two states and five reasons, all of which the demo can show.
- A long GitHub outage leaves accounts `active` with an old `fetched_at`, which is the honest picture.
- A real product would want a disconnect API and probably a degraded signal. Either is an additive change: a new event type or state the receiver can ignore until it understands it.
