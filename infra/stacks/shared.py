"""SSM parameters the two stacks share (ADR 0014).

Each stack publishes its own URL as a plain String parameter, and the other stack's
Lambdas read it by name at cold start. There is no CloudFormation export, so the
stacks deploy in any order and either can be replaced on its own.

``GITHUB_CLIENT_ID`` comes from the GitHub App, so it is created by hand (slice 4),
like the secrets. It isn't a secret.
"""

from __future__ import annotations

PREFIX = "/token-keeper/demo/shared"
CONNECT_URL_PARAM = f"{PREFIX}/connect-url"  # written by TokenKeeper
RECEIVER_URL_PARAM = f"{PREFIX}/receiver-url"  # written by DemoReceiver
GITHUB_CLIENT_ID_PARAM = f"{PREFIX}/github-client-id"  # created by hand

# The alarm topic (ADR 0019). The hand-deployed Alerts stack creates it with this name,
# and TokenKeeper's alarms build its ARN from the name: no export, and the email never
# reaches CI.
ALARM_TOPIC_NAME = "token-keeper-alarms"
