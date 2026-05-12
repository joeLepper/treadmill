"""Worker process entrypoint.

Runs as the ECS task command. Wires boto3 clients, the API client, and
the event publisher, then hands off to ``runner.run`` for the main loop.

When ``REPO_MODE=github`` the worker also runs the GitHub PAT bootstrap
sequence at startup — see ``startup_auth.py`` for the rationale and
the chicken-and-egg around bootstrap-vs-worker AWS credentials.
"""

from __future__ import annotations

import logging
import sys

from treadmill_agent import config, runner, startup_auth
from treadmill_agent.api_client import ApiClient
from treadmill_agent.eventbus import EventPublisher


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stderr,
    )
    settings = config.load()
    logging.getLogger("treadmill.agent").info(
        "starting agent: api=%s queue=%s mode=%s exit_after_step=%s",
        settings.api_url, settings.work_queue_url,
        settings.repo_mode, settings.exit_after_step,
    )

    # Resolve the AWS session the worker uses for every AWS call. When
    # ``worker_aws_credentials_secret_name`` is set, this fetches the
    # long-lived IAM-User keys from Secrets Manager (using a bootstrap
    # session built from the operator's default credential chain) and
    # returns a session bound to those keys. When unset, the default
    # chain is used directly.
    aws_session = startup_auth.resolve_worker_aws_session(settings)

    # GitHub-mode workers authenticate via ``gh``'s keyring; the PAT is
    # fetched from Secrets Manager and handed to ``gh`` here so that
    # subsequent ``git clone`` / ``gh pr create`` calls in the runner
    # need no token at all in their argv or env.
    if settings.repo_mode == "github":
        startup_auth.bootstrap_github_auth(
            settings=settings, aws_session=aws_session,
        )

    sqs = aws_session.client(
        "sqs",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
    )
    sns = aws_session.client(
        "sns",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
    )
    publisher = EventPublisher(sns_client=sns, topic_arn=settings.events_topic_arn)

    with ApiClient(settings.api_url) as api:
        runner.run(
            settings=settings, api=api,
            sqs_client=sqs, publisher=publisher,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
