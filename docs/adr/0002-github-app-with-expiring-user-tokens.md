# 2. GitHub App with expiring user tokens

Date: 2026-11-18
Status: Accepted

## Context
Token Keeper needs a GitHub identity for each connected user, plus a token it can use on a schedule. GitHub offers two app types that can act as a user.

Facts, checked against GitHub's docs and changelog on 2026-11-18:
- **GitHub App user tokens:** the access token lasts 8 hours and the refresh token 6 months. Refresh tokens are single-use: once used, that refresh token and the old access token stop working, and GitHub documents no grace period. Expiry is an app setting, recommended on.
- **OAuth App tokens:** historically non-expiring, revoked by GitHub after a year of non-use. Since August 2026, OAuth Apps can opt into the same 8-hour / 6-month expiring model (the `offline_access` scope, or an app setting that is on by default for new apps).
- **Permissions:** GitHub Apps use fine-grained permissions and can request **none**, which is enough to read a user's public profile and public events. OAuth Apps use coarse scopes.
- **Revocation:** a GitHub App is sent a `github_app_authorization` webhook when a user revokes it, by default and with no permission needed (see ADR 0010).
- Both app types support PKCE (S256) on the authorization-code flow (since July 2025).

### The Instagram parallel (public API knowledge only)
The Instagram API with Instagram Login (business and creator accounts) issues long-lived user tokens that last 60 days. They can be refreshed once they are at least 24 hours old and still unexpired, and each refresh gives another 60 days. If a token lapses, the only recovery is sending the user back through login. (Tokens obtained through Facebook Login follow different rules; this comparison is only about Instagram Login.) A connector for that platform therefore has to:
1. track each token's expiry,
2. refresh on a schedule well before the cliff,
3. tell a passing error apart from "this user is gone", and
4. report re-auth needs to the product.

A GitHub App with expiring tokens has the same shape on a shorter clock: a short credential (8 hours), a renewal credential with a hard cliff (6 months), and re-auth as the only recovery. GitHub adds one twist: **single-use rotation**, so concurrent refreshes must be serialised. Building this for GitHub exercises the same design muscles as the Instagram pattern, plus a concurrency problem.

## Options considered
1. **GitHub App, expiring user tokens, no permissions.** The narrowest access, a real refresh lifecycle, the rotation hazard to design for, and a revocation webhook for free.
2. **OAuth App with the new expiring tokens.** Same lifecycle now, but coarse scopes, no revocation webhook, and it's the older model GitHub steers new integrations away from.
3. **OAuth App, non-expiring tokens.** Simplest. "Keeping tokens alive" shrinks to re-verification, and a leaked token stays valid indefinitely.
4. **Both app types behind the provider interface.** Shows abstraction, but doubles the auth slice. Doesn't fit a one-to-two-week appetite.

## Decision
Option 1: a GitHub App owned by Dan's personal account, with user-token expiry on, webhooks on, and no repository or account permissions. Token refresh happens under a DynamoDB lease (see ADR 0004 and spec §4).

## Consequences
- The refresh lifecycle is real and demonstrable: about every other 6-hourly sync rotates the pair.
- A lease is mandatory. If GitHub rotates the pair and we never store the result (a lost response or a crash), the account is lost to re-auth. That's accepted and documented as a residual risk.
- Reading private data later would mean adding permissions, which forces existing users to re-approve. That's acceptable for v1.
- Mistake noted for the case study: during shaping, Claude first claimed an OAuth App would leave nothing to keep alive. GitHub's August 2026 change made that untrue, so the decision rests on permissions (and the revocation webhook), not lifecycle.
