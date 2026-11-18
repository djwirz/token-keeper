# 1. Record architecture decisions

Date: 2026-11-18
Status: Accepted

## Context
Token Keeper is built in the open with Claude Code. Decisions made in chat are lost; decisions made silently in code are hard to review or explain later.

## Decision
Every decision that would be expensive to reverse gets a short ADR in `docs/adr/`, numbered in order, with the sections: Context, Options considered, Decision, Consequences. ADRs arrive through pull requests like code. A superseded ADR stays, marked "Superseded by N".

## Consequences
- Reviewers (and later Claude sessions) can see why the system looks the way it does.
- Small cost per decision; trivial choices don't need one.
