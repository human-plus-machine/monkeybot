from monkeybot.providers.pricing import estimate_cost, pricing_for


def test_bedrock_and_vertex_ids_resolve_to_the_same_price_as_the_bare_id() -> None:
    bare = pricing_for("claude-sonnet-4-6")
    assert bare[0] > 0
    for hosted in (
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-sonnet-4-6-v1:0",
        "aws_bedrock/us.anthropic.claude-sonnet-4-6-v1:0",
        "eu.anthropic.claude-sonnet-4-6",
    ):
        assert pricing_for(hosted) == bare, hosted


def test_unknown_model_still_costs_zero_instead_of_raising() -> None:
    assert pricing_for("not-a-model") == (0.0, 0.0, 0.0, 0.0)
    assert estimate_cost("not-a-model", 1_000, 1_000) == 0.0


def test_bedrock_run_reports_nonzero_cost() -> None:
    # The PRT-5022 symptom: 8.4M in / 81.9K out displayed as $0.0000.
    assert estimate_cost("us.anthropic.claude-sonnet-4-6", 8_433_563, 81_922) > 0
