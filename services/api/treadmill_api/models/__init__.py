"""SQLAlchemy ORM models for the Treadmill API.

All models inherit from ``treadmill_api.database.Base``. Importing this
package registers every model on ``Base.metadata`` so alembic
``--autogenerate`` and the ``run_migrations_online`` path see the full
schema.
"""

from treadmill_api.models.event import Event
from treadmill_api.models.onboarding import (
    RepoConfigRow,
    RepoContextDocRow,
    RepoProfileRow,
)
from treadmill_api.models.plan import Plan
from treadmill_api.models.run import WorkflowRun, WorkflowRunStep
from treadmill_api.models.task import Task, TaskDependency, TaskPR, TaskValidation
from treadmill_api.models.task_board import TASK_BOARD_STATUSES, TaskBoard
from treadmill_api.models.task_execution import TaskExecution
from treadmill_api.models.llm_call import LLMCall
from treadmill_api.models.team_config import TeamConfig
from treadmill_api.models.workflow import (
    EventTrigger,
    Hook,
    OutputKind,
    Role,
    RoleHook,
    RoleSkill,
    RoleVersion,
    Skill,
    Workflow,
    WorkflowVersion,
    WorkflowVersionStep,
)
from treadmill_api.models.schedule import Schedule
from treadmill_api.models.system_status import SystemStatus
from treadmill_api.models.workflow_dispatch_dedup import WorkflowDispatchDedup

__all__ = [
    "Event",
    "EventTrigger",
    "Hook",
    "OutputKind",
    "Plan",
    "RepoConfigRow",
    "RepoContextDocRow",
    "RepoProfileRow",
    "Role",
    "RoleHook",
    "RoleSkill",
    "RoleVersion",
    "Schedule",
    "Skill",
    "SystemStatus",
    "TASK_BOARD_STATUSES",
    "LLMCall",
    "Task",
    "TaskBoard",
    "TaskExecution",
    "TaskDependency",
    "TaskPR",
    "TaskValidation",
    "TeamConfig",
    "Workflow",
    "WorkflowDispatchDedup",
    "WorkflowRun",
    "WorkflowRunStep",
    "WorkflowVersion",
    "WorkflowVersionStep",
]
