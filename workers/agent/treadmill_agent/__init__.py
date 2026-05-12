"""Treadmill agent worker.

Polls the work queue, fetches a step's full context from the API,
drives Claude Code in a workspace, opens a PR, and publishes step
lifecycle events on the events SNS topic.
"""

__version__ = "0.0.0"
