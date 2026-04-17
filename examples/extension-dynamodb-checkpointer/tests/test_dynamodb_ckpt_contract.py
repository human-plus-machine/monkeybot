"""Consumer-side contract test for :class:`DynamoDBCheckpointer`.

The whole point of this test file is to demonstrate the "one-liner" that a
third-party backend author runs to verify their extension against the seven
``CKPT-C-*`` invariants shipped by monkey-bot — *without* ever importing
anything from the framework's ``tests/`` tree.

If ``moto`` (with the ``server`` extra) or ``aioboto3`` are not installed, the
suite skips rather than fails, so this file is safe to include in repos that
only wire AWS deps on demand.

Implementation notes:

1. Earlier revisions drove the suite through ``moto.mock_aws()``, which
   monkey-patches ``botocore``'s synchronous HTTP transport in place. That
   strategy is incompatible with ``aiobotocore`` (used by ``aioboto3``) — the
   async endpoint expects ``await response.content`` while the monkey-patched
   synchronous transport returns raw ``bytes``. We therefore stand up an
   in-process :class:`moto.server.ThreadedMotoServer` and point the
   Checkpointer at its URL via the existing ``endpoint_url`` kwarg. This
   matches how real consumers drive DynamoDB Local or LocalStack during
   integration tests.

2. The contract suite runs every invariant against a *fresh* Checkpointer
   instance (via ``factory()``) but several invariants reuse the same
   ``session_id`` (``"s"``). With a single shared DynamoDB table, state from
   earlier invariants leaks into later ones (most visibly in CKPT-C-04's
   ``list(limit=3)`` assertion). To honour the "fresh backend per invariant"
   contract we create a *new* uniquely-named table per ``factory()`` call.
   The moto server is cheap and purely in-memory, so this is fast.

3. CKPT-C-07 writes a 1 MB payload. DynamoDB's per-item hard limit is 400 KB
   (see the README's "Operational notes" section — large payloads are
   expected to be offloaded through ``ContextPolicyMW`` rather than written
   directly). We express "this backend cannot satisfy this invariant" using
   the contract's documented :class:`ContractSkipped` escape hatch in a
   thin test-only subclass of the production :class:`DynamoDBCheckpointer`.
"""

from __future__ import annotations

import itertools
import pickle
import uuid
from collections.abc import Iterator, Mapping
from typing import Any, Literal

import pytest

pytest.importorskip("moto")
pytest.importorskip("aioboto3")
# ``ThreadedMotoServer`` requires the ``moto[server]`` extra (flask, werkzeug).
# If only core moto is installed, skip cleanly rather than fail the suite.
pytest.importorskip("moto.server")

# isort: off
from emonk.core.harness.extensions import CheckpointRef  # noqa: E402
from emonk.core.harness.extensions.testing import (  # noqa: E402
    ContractSkipped,
    checkpointer_contract_suite,
)

from dynamodb_ckpt import DynamoDBCheckpointer  # noqa: E402

# isort: on

_TABLE_PREFIX = "emonk-checkpoints-contract"
_REGION = "us-east-1"
# DynamoDB's per-item hard limit is 400 KB; we leave headroom for the
# metadata attributes (session_id, reason, created_at, uri, ...).
_MAX_ITEM_BYTES = 380_000


class _ContractTestCheckpointer(DynamoDBCheckpointer):
    """Test-only subclass that honours DynamoDB's 400 KB item limit.

    CKPT-C-07 exercises a 1 MB payload which fundamentally cannot fit in a
    single DynamoDB item. The README documents this limit and recommends
    offloading large payloads via ``ContextPolicyMW`` rather than trying to
    smuggle them through the Checkpointer. We translate the incompatibility
    into the contract's :class:`ContractSkipped` escape hatch so the suite
    skips (not fails) that single invariant while exercising the other six
    end-to-end against the production ``write`` code path.
    """

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        if len(pickle.dumps(dict(state))) > _MAX_ITEM_BYTES:
            raise ContractSkipped(
                "DynamoDB items are capped at 400KB; the README recommends "
                "offloading large payloads via ContextPolicyMW rather than "
                "writing them directly."
            )
        return await super().write(session_id, state, reason=reason)


@pytest.fixture
def dynamodb_endpoint() -> Iterator[str]:
    """Stand up an in-process moto DynamoDB server and yield its URL.

    We use :class:`moto.server.ThreadedMotoServer` (rather than
    ``moto.mock_aws()``) because the latter patches ``botocore``'s synchronous
    HTTP transport, which is incompatible with the ``aiobotocore`` transport
    used by :class:`aioboto3.Session`. The server variant speaks real HTTP,
    so the Checkpointer's async client talks to it over the loopback network
    exactly the same way it would talk to a production DynamoDB endpoint.
    """
    from moto.server import ThreadedMotoServer

    # Bind explicitly to 127.0.0.1 (rather than moto's 0.0.0.0 default) so the
    # advertised URL is always loopback — required under sandboxed CI runners
    # that allow only loopback traffic, and avoids a stray DNS lookup path.
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    try:
        _host, port = server.get_host_and_port()
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def _create_table(endpoint_url: str, table_name: str) -> None:
    """Create the checkpoint table synchronously via boto3.

    Fixture setup uses the synchronous boto3 client (not ``aioboto3``) so
    table creation runs in the pytest thread, not inside the invariant's
    event loop.
    """
    import boto3

    client = boto3.client(
        "dynamodb",
        region_name=_REGION,
        endpoint_url=endpoint_url,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )
    client.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "checkpoint_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "session_id", "KeyType": "HASH"},
            {"AttributeName": "checkpoint_id", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=table_name)


def test_dynamodb_checkpointer_matches_contract(dynamodb_endpoint: str) -> None:
    """Run CKPT-C-01 … CKPT-C-07 against the DynamoDB backend.

    The factory creates a *fresh, uniquely-named table* per invocation. The
    contract guarantees the factory is invoked once per invariant, so each
    invariant runs against a clean table — matching the "fresh backend per
    invariant" contract shipped backends (in-memory, postgres) enforce via
    in-process state reset or per-run schemas. Each instance is wired to the
    moto server via the production ``endpoint_url`` kwarg, exercising the
    same code path consumers use against DynamoDB Local or LocalStack.
    """
    # The checkpointer's aioboto3 session discovers credentials through the
    # usual provider chain. Moto accepts any non-empty credentials, so we
    # export dummies into the process env just for the duration of this test.
    import os

    prior = {
        key: os.environ.get(key)
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    }
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    try:
        table_counter = itertools.count()

        def factory() -> _ContractTestCheckpointer:
            table_name = f"{_TABLE_PREFIX}-{next(table_counter)}-{uuid.uuid4().hex[:8]}"
            _create_table(dynamodb_endpoint, table_name)
            return _ContractTestCheckpointer(
                table_name=table_name,
                region=_REGION,
                endpoint_url=dynamodb_endpoint,
            )

        checkpointer_contract_suite(factory)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
