# 7. Signed connect links, OAuth state and PKCE

Date: 2026-11-18
Status: Proposed

## Context
Token Keeper owns the OAuth flow, so it has to know which main-app user is connecting. Otherwise anyone could hit `/connect` and attach a GitHub account to another user. The callback must also resist CSRF and code interception.

## Options considered
1. **A signed, short-lived connect link** built by the main app (HMAC over the canonical query, `exp` at most 10 minutes), plus server-side single-use `state` plus PKCE S256.
2. **A server-to-server "mint connect session" API** that returns a one-time URL. Slightly stronger, since nothing reusable sits in a URL, but it's a second inbound API.
3. **An open `/connect`, linked by GitHub login.** An account-linking hole, only acceptable for a single-user toy.

## Decision
Option 1. The connect-link secret is **separate** from the webhook signing secret: one key per purpose, so a leak of one doesn't compromise the other. During the interview Claude first suggested sharing a secret and corrected it before drafting. `return_to` must match a per-app allow-list.

## Consequences
- No new inbound API. The main app only needs an HMAC helper (provided in the receiver).
- A leaked link is usable once (the state is single-use) for up to 10 minutes, by someone who would still have to approve with their own GitHub account. Low impact.
- Option 2 stays available if multi-tenancy ever becomes real.
