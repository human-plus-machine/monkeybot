"""deepeval scoring + optional Langfuse score push."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from models import Scenario, TurnResult

_log = logging.getLogger(__name__)

EVAL_ROOT = Path(__file__).resolve().parent

# Scenario YAML metric names → deepeval.metrics class names (ConversationalTestCase).
_METRIC_CLASS_ALIASES: dict[str, str] = {
    "response_relevancy": "TurnRelevancyMetric",
    "conversation_relevancy": "TurnRelevancyMetric",
    "turn_relevancy": "TurnRelevancyMetric",
    "faithfulness": "TurnFaithfulnessMetric",
    "conversational_coherence": "KnowledgeRetentionMetric",
    "turn_coherence": "KnowledgeRetentionMetric",
    "knowledge_retention": "KnowledgeRetentionMetric",
}


def _read_agent_model_config() -> tuple[str, str]:
    """Read ``model.provider`` + ``model.name`` from mounted monkeybot.yaml."""
    config_path = os.environ.get("MONKEYBOT_AGENT_CONFIG", "/config/monkeybot.yaml")
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        model_section = data.get("model", {}) or {}
        provider = str(model_section.get("provider", "gemini")).strip().lower()
        name = str(model_section.get("name", "gemini-2.5-flash")).strip()
        return provider, name
    except FileNotFoundError:
        _log.warning("Agent config not found at %s; using defaults", config_path)
        return "gemini", "gemini-2.5-flash"


def _load_eval_config() -> dict[str, Any]:
    path = EVAL_ROOT / "eval_config.yaml"
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        _log.warning("Could not read eval_config.yaml: %s", exc)
        return {}


def _build_judge_model() -> Any | None:
    """Return a deepeval judge model, or ``None`` to use deepeval defaults.

    This runs inside the **evals** service (separate image from the MonkeyBot gateway). The
    gateway uses MonkeyBot's provider stack; scoring uses deepeval's ``GeminiModel`` / etc.,
    which require their own deps (e.g. ``google-genai`` for Gemini judges).
    """
    cfg = _load_eval_config()
    if cfg.get("judge_provider"):
        provider = str(cfg["judge_provider"]).strip().lower()
        model = str(cfg.get("judge_model", "")).strip()
    else:
        provider, model = _read_agent_model_config()

    if not model:
        return None

    if provider in ("fake", "mock", "none", "disabled"):
        return None

    try:
        from deepeval.models import AnthropicModel, GeminiModel, GPTModel
    except ImportError as exc:
        _log.warning("deepeval models unavailable: %s", exc)
        return None

    # MonkeyBot YAML may use ``vertex`` / ``gemini`` aliases or canonical ``google_vertexai``.
    if provider in ("vertex", "gemini", "google", "google_vertexai", "google-vertexai"):
        project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("VERTEX_AI_PROJECT_ID")
            or os.environ.get("GCP_PROJECT_ID")
        ) or None
        location = os.environ.get("VERTEX_AI_LOCATION") or "us-central1"
        # deepeval GeminiModel only knows a fixed set of model names; preview / custom names
        # fall back to the default-capability bucket which is fine for scoring.  Force a known
        # flash model for the judge — the agent model and scoring model are independent.
        judge_model = model if model in (
            "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash",
            "gemini-2.0-flash-lite", "gemini-1.5-pro", "gemini-1.5-flash",
        ) else "gemini-2.5-flash"
        _log.info(
            "Building GeminiModel judge: model=%s project=%s location=%s vertexai=%s",
            judge_model, project, location, bool(project),
        )
        return GeminiModel(
            model=judge_model,
            project=project,
            location=location if project else None,
        )
    if provider in ("openai", "azure", "azure_openai"):
        return GPTModel(model=model)
    if provider in ("claude", "anthropic", "vertex-claude"):
        return AnthropicModel(model=model)
    if provider in ("bedrock", "aws"):
        try:
            from deepeval.models import AmazonBedrockModel

            region = os.environ.get("AWS_BEDROCK_REGION") or os.environ.get("AWS_REGION")
            if region:
                return AmazonBedrockModel(model=model, region=region)
            return AmazonBedrockModel(model=model)
        except Exception as exc:
            _log.warning("Could not init AmazonBedrockModel judge: %s", exc)

    _log.info("Unknown judge provider %r; deepeval default judge will be used", provider)
    return None


def _import_metric_class(class_name: str) -> type | None:
    try:
        import deepeval.metrics as metrics_mod
    except ImportError:
        return None
    return getattr(metrics_mod, class_name, None)


def _resolve_metrics(metric_names: list[str], judge: Any | None) -> list[Any]:
    instances: list[Any] = []
    for raw in metric_names:
        key = str(raw).strip()
        lower = key.lower()
        cls_name = _METRIC_CLASS_ALIASES.get(lower)
        if cls_name is None:
            if key.endswith("Metric"):
                cls_name = key
            else:
                cls_name = "".join(part.title() for part in lower.split("_")) + "Metric"
        cls = _import_metric_class(cls_name)
        if cls is None:
            _log.warning("Unknown or unavailable metric %r (resolved %r)", raw, cls_name)
            continue
        inst = _instantiate_metric(cls, judge)
        if inst is not None:
            instances.append(inst)
    return instances


def _instantiate_metric(cls: type, judge: Any | None) -> Any | None:
    attempts: list[dict[str, Any]] = []
    if judge is not None:
        attempts.extend(
            [
                {"model": judge, "include_reason": True},
                {"model": judge},
                {"llm": judge, "include_reason": True},
                {"llm": judge},
            ]
        )
    attempts.extend([{"include_reason": True}, {}])
    for kwargs in attempts:
        try:
            return cls(**kwargs)  # type: ignore[misc]
        except TypeError:
            continue
    _log.warning("Could not construct metric %s", cls.__name__)
    return None


def _turns_to_conversational_test_case(scenario: Scenario, turns: list[TurnResult]) -> Any:
    from deepeval.test_case import ConversationalTestCase, Turn

    conv: list[Turn] = []
    for tr in turns:
        conv.append(Turn(role="user", content=tr.input))
        conv.append(Turn(role="assistant", content=tr.output))
    kwargs: dict[str, Any] = {
        "scenario": scenario.description or scenario.id,
        "turns": conv,
        "tags": scenario.tags,
    }
    try:
        return ConversationalTestCase(name=scenario.id, **kwargs)
    except TypeError:
        return ConversationalTestCase(**kwargs)


def _extract_scores_and_details(eval_result: Any) -> tuple[dict[str, float], list[dict[str, Any]]]:
    scores: dict[str, float] = {}
    details: list[dict[str, Any]] = []

    test_results = getattr(eval_result, "test_results", None) or []
    for ti, tr in enumerate(test_results):
        for md in getattr(tr, "metrics_data", None) or []:
            name = str(getattr(md, "name", None) or "metric")
            sc = getattr(md, "score", None)
            if sc is not None:
                scores[name] = float(sc)
            details.append(
                {
                    "metric": name,
                    "score": float(sc) if sc is not None else None,
                    "reason": getattr(md, "reason", None),
                    "success": getattr(md, "success", None),
                    "error": getattr(md, "error", None),
                    "threshold": getattr(md, "threshold", None),
                    "test_index": ti,
                }
            )

    return scores, details


def score_scenario(
    scenario: Scenario,
    turns: list[TurnResult],
) -> tuple[dict[str, float], list[dict[str, Any]], float | None]:
    """Run deepeval metrics; returns ``(aggregate_scores, score_details, pass_rate)``."""
    assertions = scenario.assertions or {}
    metric_names = assertions.get("metrics") or ["turn_relevancy"]
    if isinstance(metric_names, str):
        metric_names = [metric_names]

    judge = _build_judge_model()
    metrics = _resolve_metrics([str(m) for m in metric_names], judge)
    if not metrics:
        _log.warning("No metrics resolved for scenario %s", scenario.id)
        return {}, [], None

    test_case = _turns_to_conversational_test_case(scenario, turns)

    try:
        from deepeval import evaluate
    except ImportError as exc:
        _log.error("deepeval not installed: %s", exc)
        return {}, [], None

    try:
        from deepeval.evaluate.configs import DisplayConfig, ErrorConfig

        eval_result = evaluate(
            test_cases=[test_case],
            metrics=metrics,
            display_config=DisplayConfig(
                verbose_mode=False,
                show_indicator=False,
                print_results=False,
                inspect_after_run=False,
            ),
            error_config=ErrorConfig(ignore_errors=True, skip_on_missing_params=True),
        )
    except Exception as exc:
        _log.exception("deepeval.evaluate failed: %s", exc)
        return {}, [], None

    scores, details = _extract_scores_and_details(eval_result)
    pass_rate = None
    if scores:
        pass_rate = sum(scores.values()) / max(len(scores), 1)
    return scores, details, pass_rate


def push_langfuse_scores(
    turns: list[TurnResult],
    scores: dict[str, float],
    pass_rate: float | None,
) -> None:
    """Push aggregate scores to Langfuse using the last turn's ``trace_id`` if present."""
    host = os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not host or not public_key or not secret_key:
        return

    trace_id = next((t.trace_id for t in reversed(turns) if t.trace_id), None)
    if not trace_id:
        _log.info("Langfuse keys set but no trace_id on turns; skipping score push")
        return

    try:
        from langfuse import Langfuse
    except ImportError:
        return

    try:
        client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)
    except TypeError:
        client = Langfuse(base_url=host, public_key=public_key, secret_key=secret_key)

    for name, val in scores.items():
        try:
            client.create_score(
                name=name,
                value=float(val),
                trace_id=trace_id,
                data_type="NUMERIC",
            )
        except Exception as exc:
            _log.warning("Langfuse create_score(%s): %s", name, exc)

    if pass_rate is not None:
        try:
            client.create_score(
                name="eval_pass_rate",
                value=float(pass_rate),
                trace_id=trace_id,
                data_type="NUMERIC",
            )
        except Exception as exc:
            _log.warning("Langfuse create_score(pass_rate): %s", exc)

    try:
        client.flush()
    except Exception:
        pass
