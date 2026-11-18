# 8. Token Keeper syncs snapshots; it does not vend tokens

Date: 2026-11-18
Status: Proposed

## Context
A connector that only reports "connected" has nothing to show, and the only use of each token would be the check itself. Dan pushed on this in the interview: there should be something visible from GitHub. Meanwhile, letting the main app use the token directly would mean a token-vending or proxy API, which is a second interface and a much larger security surface.

## Options considered
1. **State only.** The smallest scope, and nothing to demo.
2. **State plus a synced snapshot.** Token Keeper uses the token on schedule to fetch the public profile and the last ~10 public events, and ships them in the signed event.
3. **Token vending.** The main app pulls a current access token.
4. **Proxy.** The main app asks Token Keeper to make specific GitHub calls.

## Decision
Option 2. Tokens never leave Token Keeper. Public endpoints only (`/user` and `/users/{login}/events/public`), so nothing private lands on a public receiver page.

## Consequences
- A visible demo: activity changes between syncs, which shows the schedule working.
- Each sync is a genuine token use, which is how real connectors (e.g. Instagram media sync) justify keeping tokens alive.
- Any feature that needs the main app to act as the user means revisiting this ADR.
