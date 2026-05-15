"""Parsers for plan documents and other markdown-with-embedded-YAML sources.

The plan-doc parser is the entrypoint Scenario 1 uses (per ADR-0010): a
human + orchestrator pre-author a plan doc, the doc is committed, and the
API service parses the ``## sequence_of_work`` block to spawn Task rows.
"""

from treadmill_api.parsers.plan_doc import (
    PlanDocFormatError,
    PlanFrontmatter,
    TaskScope,
    TaskSpec,
    TaskValidationCheck,
    extract_sequence_yaml,
    parse_plan_doc,
    parse_plan_doc_frontmatter,
)


__all__ = [
    "PlanDocFormatError",
    "PlanFrontmatter",
    "TaskScope",
    "TaskSpec",
    "TaskValidationCheck",
    "extract_sequence_yaml",
    "parse_plan_doc",
    "parse_plan_doc_frontmatter",
]
