"""SQLAlchemy ORM models for the Treadmill API.

All models inherit from ``treadmill_api.database.Base``. Importing this
package registers every model on ``Base.metadata`` so alembic
``--autogenerate`` and the ``run_migrations_online`` path see the full
schema.
"""

from treadmill_api.models.event import Event
from treadmill_api.models.plan import Plan
from treadmill_api.models.run import WorkflowRun, WorkflowRunStep
from treadmill_api.models.task import Task, TaskDependency, TaskPR, TaskValidation
from treadmill_api.models.workflow import (
    EventTrigger,
    Hook,
    Role,
    RoleHook,
    RoleSkill,
    Skill,
    Workflow,
    WorkflowVersion,
    WorkflowVersionStep,
)

__all__ = [
    "Event",
    "EventTrigger",
    "Hook",
    "Plan",
    "Role",
    "RoleHook",
    "RoleSkill",
    "Skill",
    "Task",
    "TaskDependency",
    "TaskPR",
    "TaskValidation",
    "Workflow",
    "WorkflowRun",
    "WorkflowRunStep",
    "WorkflowVersion",
    "WorkflowVersionStep",
]
