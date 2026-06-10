"""Coordination — background loops that keep API state honest.

Post-ADR-0087 Phase 4, the step-lifecycle consumer is gone (the
``CoordinationConsumer`` + ``EventProjector`` + ``PlanRouter`` pipeline
projected worker step events onto ``workflow_run_steps``; both the
events and the table no longer exist — coordinators write
``task_executions`` over HTTP instead). What remains:

* ``ReplayLoop`` — replays failed ``persist_and_publish`` SNS
  publishes from their durable Event markers (A.8). Independent of the
  old step pipeline; the dispatcher's event-log surface still uses it.
* ``WebhookInboxPoller`` — drains the GitHub-webhook SQS inbox in
  dev_local mode (ADR-0017) through the shared
  ``persist_and_resolve_webhook_event`` helper.
* ``NotificationFanout`` — pushes operator notifications (Slack /
  Telegram) off the event bus.
* Health sweeps (stuck-task, escalation-close, terminal-gate,
  step-starvation, fleet-wedge, conflict, auto-merge) — scheduler-direct
  callbacks per ADR-0087 §Health bots.
"""

from treadmill_api.coordination.notification_fanout import (
    NotificationFanout,
    make_notification_fanout,
)
from treadmill_api.coordination.replay import ReplayLoop
from treadmill_api.coordination.webhook_inbox import WebhookInboxPoller

__all__ = [
    "NotificationFanout",
    "ReplayLoop",
    "WebhookInboxPoller",
    "make_notification_fanout",
]
