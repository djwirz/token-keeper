# 13. Token Keeper's table layout

Date: 2026-11-19
Status: Accepted

## Context
Spec §4 sets out one DynamoDB table with five item types, but only the Account and Snapshot rows show a sort key, and it doesn't say how attributes are typed. Slice 2 is the first code to write the table. Once it's deployed (slice 4), the key schema can't change without replacing the table. The table is `RETAIN` because it holds every token, so a replacement means a data migration. Attribute types matter too: the slice 3 `due` index sorts on `next_due_at`, and a key attribute's type is fixed when the index is created.

The OAuth state item had one more open question. Spec §4 keys it as `STATE#<random>`, which puts a live bearer value (anyone holding it can finish the flow within 10 minutes) into the table key. ADR 0003 already keeps that value out of CloudTrail by using its hash as the encryption context.

## Options considered
1. **`PK` + `SK` strings on every item.** Items with no natural second key use `SK=META`. The State item is keyed by the SHA-256 of the state.
2. **Partition key only.** Account and Snapshot would become `ACCOUNT#<id>` and `SNAPSHOT#<id>`. That works, but the deliverer would need two reads instead of one query.
3. **One table per item type.** Simpler keys, but five tables to provision, grant and retain, against the spec's single-table design.

## Decision
Option 1.

| Item | PK | SK |
|---|---|---|
| Account | `ACCOUNT#<account_id>` | `META` |
| Snapshot | `ACCOUNT#<account_id>` | `SNAPSHOT` |
| OAuth state | `STATE#<sha256(state), hex>` | `META` |
| User link | `LINK#<app_id>#<external_user_id>` | `META` |
| GitHub link | `GHUSER#<github_user_id>` | `META` |

- Times (`next_due_at`, `access_expires_at`, `refresh_expires_at`, `lease_until`, `ttl`) are Numbers in epoch seconds, so the slice 3 `due` index (partition `state`, sort `next_due_at`) sorts correctly.
- Ciphertext (`*_ct`) is Binary, as KMS returns it.
- State items carry `ttl` (DynamoDB TTL). The callback also checks expiry when it reads, because TTL deletion can lag by hours.
- The snapshot is one JSON string attribute (`snapshot`). It's only ever read and written whole.
- Account ids are `acc_` plus a ULID.

## Consequences
- The deliverer reads an account and its snapshot with one `Query` on `PK`. Its role gets `Query` and nothing that can write.
- Someone who can read the table can't replay a live OAuth state, because only its hash is stored.
- The `due` index is added in slice 3. Adding a GSI doesn't replace the table.
- Reserved words: `state` and `snapshot` are DynamoDB reserved words, so every expression that names them needs an alias. Slice 2's first deliverer query missed one, and the moto test caught it.
