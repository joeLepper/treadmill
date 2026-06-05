"""Merge architect-gold and dspy-variant-pr migration heads.

ADR-0070 substep 3.1 (20260604_0200 architect_gold_rows) and substep 4.1
(20260604_1200 review_dspy_variant_pr) were authored independently on
parallel branches and both declared down_revision="20260604_0100". After
both landed on main the Alembic graph had two heads, which crashes API
container startup with "Multiple head revisions are present for given
argument 'head'". This merge revision joins them.

Revision ID: 20260605_1615
Revises: 20260604_0200, 20260604_1200
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union


revision: str = "20260605_1615"
down_revision: Union[str, Sequence[str], None] = ("20260604_0200", "20260604_1200")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
