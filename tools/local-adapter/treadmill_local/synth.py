"""Run `cdk synth` and parse the resulting CloudFormation templates.

The synth step is a subprocess shell-out to `cdk synth`. We do not embed CDK
in-process — the CLI is the canonical synthesizer, and shelling out keeps the
adapter language-agnostic about how the CDK app is authored.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CFNResource:
    """One CloudFormation resource extracted from the template."""

    logical_id: str
    type: str
    properties: dict[str, Any]


@dataclass
class SynthResult:
    """A synthesized CDK stack, parsed into a typed shape."""

    stack_name: str
    template_path: Path
    template: dict[str, Any]
    resources: list[CFNResource] = field(default_factory=list)

    def by_type(self, type_: str) -> list[CFNResource]:
        return [r for r in self.resources if r.type == type_]


def parse_template(template: dict[str, Any]) -> list[CFNResource]:
    """Pure parser: a CFN template dict → a list of CFNResource objects."""
    return [
        CFNResource(logical_id=lid, type=r["Type"], properties=r.get("Properties", {}))
        for lid, r in template.get("Resources", {}).items()
    ]


# Default values used when resolving Fn::GetAtt for SQS Arn computations.
# These mirror moto's default account so generated ARNs are usable.
DEFAULT_REGION = "us-east-1"
DEFAULT_ACCOUNT = "123456789012"


def resolve_value(value: Any, refs: dict[str, str]) -> str | None:
    """Resolve a CFN value: a literal, a Ref, or a Fn::GetAtt / Fn::Join.

    *refs* maps logical IDs to their provisioned URL/ARN/name (whatever the
    provisioner chose to store).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "Ref" in value:
            return refs.get(value["Ref"])
        if "Fn::GetAtt" in value:
            logical_id, attr = value["Fn::GetAtt"]
            if attr == "Arn" and logical_id in refs:
                stored = refs[logical_id]
                # If stored value is an SQS URL, derive the ARN.
                if stored.startswith("http"):
                    name = stored.rstrip("/").rsplit("/", 1)[-1]
                    return f"arn:aws:sqs:{DEFAULT_REGION}:{DEFAULT_ACCOUNT}:{name}"
                return stored
            return refs.get(logical_id)
        if "Fn::Join" in value:
            sep, parts = value["Fn::Join"]
            resolved = [resolve_value(p, refs) or "" for p in parts]
            return sep.join(resolved)
    return None


def synth(infra_dir: Path) -> SynthResult:
    """Run `cdk synth` in *infra_dir* and return the parsed result.

    Assumes a single-stack app for the spike. Multi-stack support is a
    follow-up.
    """
    out_dir = infra_dir / "cdk.out"
    env = {
        **os.environ,
        # Silence the node-version warning from jsii.
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    }
    subprocess.run(
        ["cdk", "synth", "--quiet", "-o", str(out_dir)],
        cwd=infra_dir,
        env=env,
        check=True,
    )

    template_files = sorted(out_dir.glob("*.template.json"))
    if not template_files:
        raise FileNotFoundError(f"No template.json found in {out_dir}")
    if len(template_files) > 1:
        raise NotImplementedError(
            f"Multi-stack apps not supported in spike (found {len(template_files)} stacks)."
        )

    template_path = template_files[0]
    stack_name = template_path.name.removesuffix(".template.json")
    template = json.loads(template_path.read_text())

    return SynthResult(
        stack_name=stack_name,
        template_path=template_path,
        template=template,
        resources=parse_template(template),
    )
