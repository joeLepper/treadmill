"""SQLAlchemy ORM models for the Treadmill API.

All models inherit from ``treadmill_api.database.Base``. Importing this
package registers every model on ``Base.metadata`` so alembic
``--autogenerate`` and the ``run_migrations_online`` path see the full
schema.

Post-ADR-0087 Phase 5 the workflow-definition layer (Workflow,
WorkflowVersion, WorkflowVersionStep, EventTrigger), the run layer
(WorkflowRun, WorkflowRunStep), the role/skill/hook prompt machinery,
and the dispatch-dedup guard are gone — coordinators drive execution
via ``task_executions``; worker/evaluator CLAUDE.md templates replace
role prompts.
"""

from treadmill_api.models.event import Event
from treadmill_api.models.onboarding import (
    RepoConfigRow,
    RepoContextDocRow,
    RepoProfileRow,
)
from treadmill_api.models.plan import Plan
from treadmill_api.models.prod_promotion import PROD_PROMOTION_STATUSES, ProdPromotion
from treadmill_api.models.task import Task, TaskDependency, TaskPR
from treadmill_api.models.task_board import TASK_BOARD_STATUSES, TaskBoard
from treadmill_api.models.task_execution import TaskExecution
from treadmill_api.models.llm_call import LLMCall
from treadmill_api.models.team_config import TeamConfig
from treadmill_api.models.schedule import Schedule
from treadmill_api.models.system_status import SystemStatus

__all__ = [
    "ProdPromotion",
    "PROD_PROMOTION_STATUSES",
    "Event",
    "Plan",
    "RepoConfigRow",
    "RepoContextDocRow",
    "RepoProfileRow",
    "Schedule",
    "SystemStatus",
    "TASK_BOARD_STATUSES",
    "LLMCall",
    "Task",
    "TaskBoard",
    "TaskExecution",
    "TaskDependency",
    "TaskPR",
    "TeamConfig",
]
