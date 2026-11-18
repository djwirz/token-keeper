# 3. Field-level KMS encryption for tokens

Date: 2026-11-18
Status: Accepted

## Context
Tokens must be encrypted at rest (CLAUDE.md). DynamoDB encrypts tables by default, but anyone or anything with `dynamodb:GetItem` on the table still reads plaintext tokens. GitHub tokens are well under KMS's 4 KB direct-encrypt limit.

## Options considered
1. **DynamoDB encryption only.** Free and simple, but meets the requirement in letter only.
2. **Field-level `kms:Encrypt` with a customer-managed key (CMK)**, with `account_id` as encryption context. About a dollar a month for the key. Decryption is limited by IAM to specific roles and audited in CloudTrail.
3. **Envelope encryption (AWS Encryption SDK) with data keys.** The standard pattern for large payloads or high volume. Extra dependency and complexity we don't need.
4. **A Secrets Manager secret per account.** Per-secret monthly cost grows with users, and it isn't designed for this.

## Decision
Option 2. Access and refresh tokens (and the PKCE verifier in OAuth state records) are stored as KMS ciphertext. The encryption context binds ciphertext to its row so it can't be swapped between rows: `{"account_id": …}` for accounts, and `{"state_hash": <SHA-256 of the state value>}` for state records. The state value itself is a bearer secret, and encryption context is written to CloudTrail in plain text, so only its hash goes there.

Grants: `connect` can encrypt; `callback` and `worker` can encrypt and decrypt; no other role has either. CDK assertion tests enforce this.

## Consequences
- A table export or a mis-granted read role leaks nothing usable.
- One KMS call per encrypt or decrypt. At demo volume that's well inside the free request allowance.
- Fixed cost of about a dollar a month, accepted within the $5 budget.
- If volume ever grew a lot, option 3 (or data-key caching) would be the migration path.
