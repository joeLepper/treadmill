"""task_validations: add script + prompt columns.

Adds nullable ``script`` (text) and ``prompt`` (text) columns to
``task_validations`` to hold the implementation details for each kind:

  * ``deterministic`` checks need a ``script`` (an executable, e.g.
    ``pytest tests/...``), ``prompt`` must be NULL.
  * ``llm-judge`` checks need a ``prompt`` (a natural-language criterion),
    ``script`` must be NULL.

The CHECK constraint is extended to enforce this pairing:

  (kind='deterministic' AND script IS NOT NULL AND prompt IS NULL)
  OR (kind='llm-judge' AND prompt IS NOT NULL AND script IS NULL)

Backfill: existing rows have neither column populated (the plan-doc
parser writes only kind + description). Rows are backfilled with
placeholders so the CHECK passes:

  * deterministic: script='echo "placeholder-no-content"'
  * llm-judge:     prompt='Placeholder; rule not authored.'

The validation handler treats placeholder values as error with a clear
message so the operator sees they need to author the actual check.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the new columns (nullable at first).
    op.add_column("task_validations", sa.Column("script", sa.Text(), nullable=True))
    op.add_column("task_validations", sa.Column("prompt", sa.Text(), nullable=True))

    # Backfill existing rows based on their kind.
    op.execute(
        sa.text(
            "UPDATE task_validations "
            "SET script = 'echo \"placeholder-no-content\"' "
            "WHERE kind = 'deterministic'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE task_validations "
            "SET prompt = 'Placeholder; rule not authored.' "
            "WHERE kind = 'llm-judge'"
        )
    )

    # Drop the old CHECK constraint and add the new one that enforces
    # the script/prompt pairing.
    op.drop_constraint("ck_task_validations_kind", "task_validations")
    op.create_check_constraint(
        "ck_task_validations_kind_script_prompt",
        "task_validations",
        "(kind='deterministic' AND script IS NOT NULL AND prompt IS NULL) "
        "OR (kind='llm-judge' AND prompt IS NOT NULL AND script IS NULL)",
    )


def downgrade() -> None:
    # Restore the original CHECK constraint.
    op.drop_constraint("ck_task_validations_kind_script_prompt", "task_validations")
    op.create_check_constraint(
        "ck_task_validations_kind",
        "task_validations",
        "kind IN ('deterministic', 'llm-judge')",
    )

    # Drop the new columns.
    op.drop_column("task_validations", "prompt")
    op.drop_column("task_validations", "script")
