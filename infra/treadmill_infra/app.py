"""CDK app entrypoint for Treadmill.

Dispatch on two CDK context flags — ``mode`` and ``deployment_id`` — to
synthesize the right stack(s) for the requested deployment topology.

Per ADR-0016 §"Canonical spellings", ``mode`` is one of the snake_case
literals ``fully_local``, ``dev_local``, or ``fully_remote``. Each maps
to a distinct synth target:

- ``dev_local`` → :class:`TreadmillCloudLite` (requires ``deployment_id``)
- ``fully_local`` → no stack synthesized (moto-only; the local-adapter
  handles substrate provisioning)
- ``fully_remote`` → reserved for :class:`TreadmillCloudFull` (future
  ADR); no stack synthesized today
- unset (``None``) → behaves like ``fully_local``: no-op

Optional context flags:

- ``include_observability=true`` (``dev_local`` only) → also synthesizes
  :class:`TreadmillObservabilityStack` alongside ``TreadmillCloudLite``.
  Example::

      cdk synth \\
          --context mode=dev_local \\
          --context deployment_id=personal \\
          --context include_observability=true

Invocation:

    cdk synth --context mode=dev_local --context deployment_id=personal

Phase A.3 (per ``docs/plans/2026-05-13-week-4-dev-local-deployment.md``):
the dispatch logic is factored into :func:`synthesize` so it's unit-testable
without spinning up an actual ``cdk.App`` from the command line.
"""

from __future__ import annotations

import sys
from typing import Optional

import aws_cdk as cdk

from treadmill_infra.stacks import TreadmillCloudLite, TreadmillObservabilityStack
from treadmill_infra.stacks.cloud_lite import _stack_name_for
from treadmill_infra.stacks.observability import _obs_stack_name_for


# Canonical mode literals per ADR-0016 §"Canonical spellings". The set is
# the source of truth for "is this mode known?" — drift here breaks
# dispatch loud.
ALLOWED_MODES: frozenset[str] = frozenset({"fully_local", "dev_local", "fully_remote"})


def synthesize(app: cdk.App, context: dict) -> list[cdk.Stack]:
    """Dispatch the CDK app on ``mode`` + ``deployment_id`` context.

    Args:
        app: The CDK app to attach stacks to.
        context: Dict with optional keys:

            - ``mode``: one of ``fully_local``, ``dev_local``,
              ``fully_remote`` (snake_case literals per ADR-0016), or
              ``None``. Unknown modes raise ``ValueError``.
            - ``deployment_id``: required when ``mode == "dev_local"``.
            - ``include_observability``: optional; when ``"true"`` and
              ``mode == "dev_local"``, also synthesizes
              :class:`TreadmillObservabilityStack` alongside
              ``TreadmillCloudLite``.

    Returns:
        The list of stacks instantiated. Empty for no-op modes
        (``fully_local``, ``fully_remote``, or unset ``mode``).

    Raises:
        ValueError: ``mode`` is non-empty and not in
            :data:`ALLOWED_MODES`; or ``mode == "dev_local"`` and
            ``deployment_id`` is missing.
    """
    mode: Optional[str] = context.get("mode")
    deployment_id: Optional[str] = context.get("deployment_id")
    include_observability: bool = context.get("include_observability") == "true"

    if mode is not None and mode not in ALLOWED_MODES:
        allowed = ", ".join(sorted(ALLOWED_MODES))
        raise ValueError(
            f"unknown mode {mode!r}: must be one of {{{allowed}}} (or unset)"
        )

    if mode == "dev_local":
        if not deployment_id:
            raise ValueError(
                "mode=dev_local requires a deployment_id context flag "
                "(--context deployment_id=<slug>)"
            )
        stack = TreadmillCloudLite(
            app,
            _stack_name_for(deployment_id),
            deployment_id=deployment_id,
        )
        stacks: list[cdk.Stack] = [stack]
        if include_observability:
            obs_stack = TreadmillObservabilityStack(
                app,
                _obs_stack_name_for(deployment_id),
                deployment_id=deployment_id,
            )
            stacks.append(obs_stack)
        return stacks

    if mode == "fully_remote":
        # TreadmillCloudFull is out of scope for Week 4 (future ADR per
        # ADR-0016 §"Three deployment modes; one CLI; one CDK app").
        # No-op for now so `cdk deploy --context mode=fully_remote` exits
        # cleanly with a clear message rather than crashing.
        print(
            "mode=fully_remote: TreadmillCloudFull is out of scope for now "
            "(future ADR). No stacks synthesized.",
            file=sys.stderr,
        )
        return []

    # mode is "fully_local" or unset (None).
    print(
        "mode=fully_local (or unset): fully-local deployments do not use "
        "CDK against real AWS — substrate is moto via the local-adapter. "
        "No stacks synthesized.",
        file=sys.stderr,
    )
    return []


def main() -> None:
    app = cdk.App()
    context = {
        "mode": app.node.try_get_context("mode"),
        "deployment_id": app.node.try_get_context("deployment_id"),
        "include_observability": app.node.try_get_context("include_observability"),
    }
    synthesize(app, context)
    app.synth()


if __name__ == "__main__":
    main()
