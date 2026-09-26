"""Single wiring authority for the CCE component graph (ADR-001, finding 1.1).

Both ``CurationEngine.embedded()`` and the API lifespan build their
``Pipeline`` through :func:`build_pipeline`; neither wires components
directly. Adding a pipeline component means editing exactly this file — the
parity test in ``tests/test_components.py`` pins the contract.

Configuration loading lives in ``cce.config.registry`` (ADR-002, M06): this
module consumes a ``ConfigRegistry`` and constructs live runtime objects
from it — it never reads YAML or selects paths itself.

A consumer that must route outbound calls through its own gateway injects
its providers with :class:`ComponentOverrides` (B5) instead of building a
``Pipeline`` by hand; configuration still loads only through the registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cce.config.registry import ConfigRegistry
from cce.config.types import EngineConfig, LLMConfig, RoleLLMSettings
from cce.discovery.adapters.base import CrawlAdapter
from cce.discovery.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from cce.evidence.store import EvidenceStore
from cce.llm.base import LLMProvider
from cce.models.paths import PathConfig
from cce.orchestrator.pipeline import Pipeline
from cce.synthesis.editor import Editor
from cce.synthesis.implied_claims import ImpliedClaimChecker
from cce.synthesis.scoring import Scorer
from cce.tagging.base import TaxonomyPlugin

logger = logging.getLogger(__name__)


@dataclass
class ComponentSet:
    """Live pipeline components. Runtime objects, never persisted —
    hence a dataclass at package root, not a frozen model in models/."""

    llm: LLMProvider
    verifier_llm: LLMProvider
    crawl_adapter: CrawlAdapter
    embedding: EmbeddingProvider | None
    taxonomy: TaxonomyPlugin | None
    path_configs: dict[str, PathConfig]
    scorer: Scorer | None
    editor: Editor | None
    implied_claims: ImpliedClaimChecker | None


@dataclass(frozen=True)
class ComponentOverrides:
    """Caller-supplied providers used instead of the config-built ones (B5).

    Every field left ``None`` is built from config as usual. An injected
    ``llm`` reaches the writer, editor and implied-claim checker, and the
    verifier too unless ``verifier_llm`` is given. Injected providers must
    honour the ``LLMProvider`` contract: accept ``output_schema``, set
    ``stop_reason``, and report the usage keys the token budget reads
    (``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens``,
    ``cache_read_input_tokens``).
    """

    llm: LLMProvider | None = None
    verifier_llm: LLMProvider | None = None
    crawl_adapter: CrawlAdapter | None = None
    embedding: EmbeddingProvider | None = None


def build_components(
    config: EngineConfig,
    registry: ConfigRegistry,
    *,
    overrides: ComponentOverrides | None = None,
) -> ComponentSet:
    """Construct the component graph from config + loaded registry.

    Preserves the warn-and-continue semantics for optional providers
    (embedding, taxonomy, path configs): construction or load failure logs a
    warning and yields ``None`` / empty, exactly as the old wiring site did.

    The set holds no evidence store (B6): the implied-claim checker gets its
    Pipeline's store per call, so one set can back several Pipelines — one
    per tenant, each with its own store — without pooling their evidence.

    ``overrides`` (B5) replaces the config-built LLM providers, crawl adapter
    or embedding provider with the caller's own. Raises ``ValueError`` when
    ``verifier.model`` is set and only ``llm`` is injected: building the
    verifier from config would bypass the injected provider.
    """
    o = overrides or ComponentOverrides()
    if o.llm is not None and o.verifier_llm is None and config.verifier.model:
        raise ValueError(
            "verifier.model is set but only an llm override was given; inject "
            "verifier_llm as well — building the verifier from config would "
            "bypass the injected provider."
        )
    if o.llm is not None:
        # Per-role settings configure providers built from config; with an
        # injected llm they would be silently ignored.
        roles = [("writer", config.writer), ("editor", config.humanization.editor)]
        if o.verifier_llm is None:
            roles.append(("verifier", config.verifier))
        for name, role in roles:
            if _role_overrides(role):
                raise ValueError(
                    f"{name} model/max_tokens/thinking/effort are set but the llm "
                    "is injected; configure them on the injected provider instead."
                )

    # Concrete adapters are imported lazily so importing cce.components
    # (engine.py does, at module level) doesn't pull the anthropic/firecrawl
    # SDKs into keyless CLI commands (emit-mdx, api key generate).
    from cce.discovery.adapters.firecrawl import FirecrawlAdapter
    from cce.discovery.ollama import OllamaEmbeddingProvider
    from cce.llm.anthropic import AnthropicProvider
    from cce.tagging.loader import load_taxonomy
    from cce.tagging.wellbeing import WellBeingTaxonomy

    crawl_adapter: CrawlAdapter = (
        o.crawl_adapter
        if o.crawl_adapter is not None
        else FirecrawlAdapter(config.crawl)
    )
    # One provider per role with its own settings (model, max_tokens,
    # thinking, effort over `llm`); a role without any shares the writer's.
    # The writer's provider also serves the implied-claim checker.
    llm: LLMProvider = (
        o.llm
        if o.llm is not None
        else AnthropicProvider(_role_llm_config(config.llm, config.writer))
    )

    def _role_provider(name: str, role: RoleLLMSettings) -> LLMProvider:
        if o.llm is not None or not _role_overrides(role):
            return llm
        role_config = _role_llm_config(config.llm, role)
        logger.info(
            "%s model: %s (max_tokens=%s, thinking=%s, effort=%s)",
            name.capitalize(),
            role_config.model,
            role_config.max_tokens or "model maximum",
            role_config.thinking,
            role_config.effort,
        )
        return AnthropicProvider(role_config)

    verifier_llm: LLMProvider = (
        o.verifier_llm
        if o.verifier_llm is not None
        else _role_provider("verifier", config.verifier)
    )
    editor_llm = _role_provider("editor", config.humanization.editor)

    # Embedding provider (optional)
    embedding_provider = o.embedding
    if embedding_provider is None and config.embedding.enabled:
        try:
            provider = OllamaEmbeddingProvider(config.embedding)
            embedding_provider = provider
            logger.info("Embedding provider ready: %s", config.embedding.model)
        except (EmbeddingUnavailableError, Exception) as e:
            logger.warning("Embedding provider unavailable: %s", e)

    # Taxonomy plugin (optional). The registry owns path selection; the
    # YAML -> plugin step stays here because WellBeingTaxonomy is a live
    # component. load_taxonomy catches parse errors and returns None
    # (audit A4 / ADR-006), so no outer try/except here.
    taxonomy_plugin = None
    if registry.taxonomy_path is not None:
        taxonomy_config = load_taxonomy(registry.taxonomy_path)
        if taxonomy_config is not None:
            taxonomy_plugin = WellBeingTaxonomy(taxonomy_config)
            logger.info("Taxonomy loaded: %s", taxonomy_config.name)

    # Humanization scorer (M02, optional). Constructed only when the master
    # switch is on. Markers are loaded fail-fast by ConfigRegistry.load (not
    # graceful like taxonomy/embedding/path_configs above): silent
    # humanization failure would be worse than a dead server. An operator
    # who set `humanization.enabled = true` has explicitly opted in; booting
    # with scoring silently disabled would leave them shipping unscored
    # drafts under the impression the gate was measuring them.
    scorer = None
    editor = None
    implied_claim_checker = None
    if config.humanization.enabled:
        markers = registry.markers
        if markers is None:
            raise ValueError(
                "humanization.enabled=True but the ConfigRegistry holds no "
                "markers — build the registry from the same EngineConfig "
                "(ConfigRegistry.load loads markers when humanization is "
                "enabled)."
            )
        scorer = Scorer(thresholds=config.humanization.thresholds, markers=markers)
        logger.info(
            "Humanization scorer ready (markers: %s)", config.humanization.markers_path
        )

        # Editor (M03, optional). Double-gate: master + per-stage switch.
        if config.humanization.editor.enabled:
            editor = Editor(llm=editor_llm, config=config.humanization.editor)
            logger.info(
                "Humanization editor ready (temp=%s)",
                config.humanization.editor.temperature,
            )

        # Implied-claim checker (M04, optional). Requires the editor — the
        # editor is the only consumer of checker annotations. If an operator
        # enables implied_claims without the editor, log a warning and skip
        # construction rather than build a checker whose output has nowhere
        # to land. (If a future audit-only mode ever needs the checker without
        # the editor, add an explicit config flag for it.)
        if config.humanization.implied_claims.enabled:
            if editor is None:
                logger.warning(
                    "humanization.implied_claims.enabled=True but editor "
                    "is not enabled; skipping checker construction. "
                    "Enable humanization.editor to use implied-claim annotations."
                )
            else:
                implied_claim_checker = ImpliedClaimChecker(
                    llm=llm,
                    config=config.humanization.implied_claims,
                    markers=markers,
                )
                logger.info(
                    "Implied-claim checker ready (strategy=%s, release_valve=%.2f)",
                    config.humanization.implied_claims.search_strategy,
                    config.humanization.implied_claims.dismissal_release_valve_ratio,
                )

    return ComponentSet(
        llm=llm,
        verifier_llm=verifier_llm,
        crawl_adapter=crawl_adapter,
        embedding=embedding_provider,
        taxonomy=taxonomy_plugin,
        path_configs=registry.path_configs,
        scorer=scorer,
        editor=editor,
        implied_claims=implied_claim_checker,
    )


def build_pipeline(
    config: EngineConfig,
    registry: ConfigRegistry,
    evidence_store: EvidenceStore,
    components: ComponentSet | None = None,
    *,
    overrides: ComponentOverrides | None = None,
) -> Pipeline:
    """Assemble a ``Pipeline`` from a ``ComponentSet`` (built if not given).

    The single Pipeline-construction point shared by
    ``CurationEngine.embedded()`` and the API lifespan (via the
    ``api/app.py:_build_pipeline`` shim). ``overrides`` is forwarded to
    :func:`build_components`; passing it with prebuilt ``components`` raises,
    so an override is never silently ignored.

    Multi-tenant use (B6): build one Pipeline per tenant, each with its own
    ``evidence_store`` (and, through the engine, its own job store). They
    may share one ``ComponentSet``; nothing in it holds tenant data.
    """
    if components is not None and overrides is not None:
        raise ValueError(
            "pass either prebuilt components or overrides, not both — the "
            "overrides would be ignored"
        )
    if components is None:
        components = build_components(config, registry, overrides=overrides)
    return Pipeline(
        config=config,
        crawl_adapter=components.crawl_adapter,
        evidence_store=evidence_store,
        llm=components.llm,
        verifier_llm=components.verifier_llm,
        embedding_provider=components.embedding,
        taxonomy_plugin=components.taxonomy,
        path_configs=components.path_configs,
        scorer=components.scorer,
        editor=components.editor,
        implied_claim_checker=components.implied_claims,
    )


_ROLE_FIELDS = ("model", "max_tokens", "thinking", "effort")


def _role_overrides(role: RoleLLMSettings) -> dict:
    """The role's settings that differ from `llm` (the ones it set)."""
    return {k: v for k in _ROLE_FIELDS if (v := getattr(role, k)) is not None}


def _role_llm_config(base: LLMConfig, role: RoleLLMSettings) -> LLMConfig:
    """`llm` with the role's own model / max_tokens / thinking / effort."""
    return base.model_copy(update=_role_overrides(role))
