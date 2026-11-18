# 7. Signed connect links, OAuth state and PKCE

Date: 2026-11-18
Status: Accepted

## Context
Token Keeper owns the OAuth flow, so it has to know which main-app user is connecting. Otherwise anyone could hit `/connect` and attach a GitHub account to another user. The callback must also resist CSRF and code interception.

## Options considered
1. **A signed, short-lived connect link** built by the main app (HMAC over a canonical query string, `exp` at most 10 minutes), plus server-side single-use `state` plus PKCE S256.
2. **A server-to-server "mint connect session" API** that returns a one-time URL. Slightly stronger, since nothing reusable sits in a URL, but it's a second inbound API.
3. **An open `/connect`, linked by GitHub login.** An account-linking hole, only acceptable for a single-user toy.

## Decision
Option 1.

- **Canonical string:** parameters except `sig`, sorted by name, RFC 3986 percent-encoded, joined with `&` (exact rules in spec §6.1). The repo carries test vectors that both the signer and the verifier must pass, so the two sides can't drift. Signatures are compared in constant time.
- **Secrets:** the connect-link secret is **separate** from the webhook signing secret: one key per purpose, so a leak of one doesn't compromise the other. During the interview Claude first suggested sharing a secret and corrected it before drafting.
- **`return_to`:** must exactly match an entry on the app's allow-list (scheme, host, port, path).
- **Leakage:** connect pages send `Referrer-Policy: no-referrer`.
- **Demo receiver:** it only signs links after the demo passcode is entered.

## Consequences
- No new inbound API. The main app only needs an HMAC helper (provided in the receiver).
- A leaked link can be replayed until it expires (up to 10 minutes): each use starts a fresh `state`. Whoever holds it could attach **their own** GitHub account to the link's main-app user. With one demo user behind a passcode, that impact is low and accepted. A single-use nonce would close it if multi-tenancy ever became real.
- Option 2 stays available for the same reason.
