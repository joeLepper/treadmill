"""Unit tests for the CFN parser in treadmill_local.synth."""

from __future__ import annotations

from treadmill_local.synth import CFNResource, SynthResult, parse_template


def test_parse_template_empty():
    assert parse_template({}) == []
    assert parse_template({"Resources": {}}) == []


def test_parse_template_single_resource():
    template = {
        "Resources": {
            "MyTopic": {
                "Type": "AWS::SNS::Topic",
                "Properties": {"TopicName": "my-topic"},
            },
        },
    }
    [resource] = parse_template(template)
    assert resource.logical_id == "MyTopic"
    assert resource.type == "AWS::SNS::Topic"
    assert resource.properties == {"TopicName": "my-topic"}


def test_parse_template_resource_without_properties():
    """CFN allows resources to omit Properties; parser must default to {}."""
    template = {
        "Resources": {
            "Bare": {"Type": "AWS::Some::Thing"},
        },
    }
    [resource] = parse_template(template)
    assert resource.properties == {}


def test_parse_template_preserves_order():
    template = {
        "Resources": {
            "First": {"Type": "AWS::A::A"},
            "Second": {"Type": "AWS::B::B"},
            "Third": {"Type": "AWS::A::A"},
        },
    }
    resources = parse_template(template)
    assert [r.logical_id for r in resources] == ["First", "Second", "Third"]


def test_synth_result_by_type_filters():
    resources = [
        CFNResource("A", "AWS::SNS::Topic", {}),
        CFNResource("B", "AWS::SQS::Queue", {}),
        CFNResource("C", "AWS::SNS::Topic", {}),
    ]
    result = SynthResult(
        stack_name="Test",
        template_path=None,  # type: ignore[arg-type]
        template={},
        resources=resources,
    )
    topics = result.by_type("AWS::SNS::Topic")
    assert [r.logical_id for r in topics] == ["A", "C"]
    assert result.by_type("AWS::S3::Bucket") == []
