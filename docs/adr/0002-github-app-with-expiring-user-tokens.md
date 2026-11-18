# 2. GitHub App with expiring user tokens

Date: 2026-11-18
Status: Proposed

## Context
Token Keeper needs a GitHub identity for each connected user, plus a token it can use on a schedule. GitHub offers two app types that can act as a user.

Facts, checked against GitHub's docs and changelog on 2026-11-18:
- **GitHub App user tokens:** the access token lasts 8 hours and the refresh token 6 months. Refresh tokens are single-use: a refresh returns a new pair and invalidates both old tokens. Expiry is an app setting, recommended on.
- **OAuth App tokens:** historically non-expiring, revoked by GitHub after a year of non-use. Since August 2026, OAuth Apps can opt into the same 8-hour / 6-month expiring model (`offline_access`, or an app setting that is on by default for new apps).
- **Permissions:** GitHub Apps use fine-grained permissions and can request **none**, which is enough to read a user's public profile and public events. OAuth Apps use coarse scopes.
- Both support PKCE (S256) on the authorization-code flow.

### The Instagram parallel (public API knowledge only)
Instagram's platform issues long-lived user tokens that last about 60 days. They can be refreshed once they are at least 24 hours old and still unexpired, and each refresh gives another 60 days. If a token lapses, the only recovery is sending the user back through login. A connector for that platform therefore has to:
1. track each token's expiry,
2. refresh on a schedule well before the cliff,
3. tell "the platform is having a bad day" apart from "this user is gone", and
4. report re-auth needs to the product.

A GitHub App with expiring tokens has the same shape on a shorter clock: a short credential (8 hours), a renewal credential with a hard cliff (6 months), and re-auth as the only recovery. GitHub adds one twist Instagram's refresh does not have, **single-use rotation**, so concurrent refreshes must be serialised. Building this for GitHub exercises the same design muscles as the Instagram pattern, plus a concurrency problem.

## Options considered
1. **GitHub App, expiring user tokens, no permissions.** The narrowest access, a real refresh lifecycle, and the rotation hazard to design for.
2. **OAuth App with the new expiring tokens.** Same lifecycle now, but coarse scopes, and it's the older model GitHub steers new integrations away from.
3. **OAuth App, non-expiring tokens.** Simplest. "Keeping tokens alive" shrinks to re-verification, and a leaked token stays valid indefinitely.
4. **Both app types behind the provider interface.** Shows abstraction, but doubles the auth slice. Doesn't fit a one-to-two-week appetite.

## Decision
Option 1: a GitHub App owned by Dan's personal account, with user-token expiry on and no repository or account permissions. Token refresh happens under a DynamoDB lease (see ADR 0004 and spec §5).

## Consequences
- The refresh lifecycle is real and demonstrable: roughly every other 6-hourly sync rotates the pair.
- A lease is mandatory. A crash between GitHub rotating the pair and our write loses the account to re-auth. That's accepted and documented as a residual risk.
- Reading private data later would mean adding permissions, which forces existing users to re-approve. That's acceptable for v1.
- Mistake noted for the case study: during shaping, Claude first claimed an OAuth App would leave nothing to keep alive. GitHub's August 2026 change made that untrue, so the decision rests on permissions, not lifecycle.
