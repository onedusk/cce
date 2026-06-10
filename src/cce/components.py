"""Single wiring authority for the CCE component graph (ADR-001, finding 1.1).

Both ``CurationEngine.embedded()`` and the API lifespan build their
``Pipeline`` through :func:`build_pipeline`; neither wires components
directly. Adding a pipeline component means editing exactly this file — the
parity test in ``tests/test_components.py`` pins the contract.

Configuration loading lives in ``cce.config.registry`` (ADR-002, M06): this
module consumes a ``ConfigRegistry`` and constructs live runtime objects
from it — it never reads YAML or selects paths itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cce.config.registry import ConfigRegistry
from cce.config.types import EngineConfig
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
    crawl_adapter: CrawlAdapter
    embedding: EmbeddingProvider | None
    taxonomy: TaxonomyPlugin | None
    path_configs: dict[str, PathConfig]
    scorer: Scorer | None
    editor: Editor | None
    implied_claims: ImpliedClaimChecker | None


def build_components(
    config: EngineConfig,
    registry: ConfigRegistry,
    evidence_store: EvidenceStore,
) -> ComponentSet:
    """Construct the component graph from config + loaded registry.

    Preserves the warn-and-continue semantics for optional providers
    (embedding, taxonomy, path configs): construction or load failure logs a
    warning and yields ``None`` / empty, exactly as the old wiring site did.

    ``evidence_store`` is required because the implied-claim checker is
    constructor-injected with the live store (counter-evidence search) — the
    registry holds config-time data only.
    """
    # Concrete adapters are imported lazily so importing cce.components
    # (engine.py does, at module level) doesn't pull the anthropic/firecrawl
    # SDKs into keyless CLI commands (emit-mdx, api key generate).
    from cce.discovery.adapters.firecrawl import FirecrawlAdapter
    from cce.discovery.ollama import OllamaEmbeddingProvider
    from cce.llm.anthropic import AnthropicProvider
    from cce.tagging.loader import load_taxonomy
    from cce.tagging.wellbeing import WellBeingTaxonomy

    crawl_adapter = FirecrawlAdapter(config.crawl)
    llm = AnthropicProvider(config.llm)

    # Embedding provider (optional)
    embedding_provider = None
    if config.embedding.enabled:
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
            editor = Editor(llm=llm, config=config.humanization.editor)
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
                    evidence_store=evidence_store,
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
) -> Pipeline:
    """Assemble a ``Pipeline`` from a ``ComponentSet`` (built if not given).

    The single Pipeline-construction point shared by
    ``CurationEngine.embedded()`` and the API lifespan (via the
    ``api/app.py:_build_pipeline`` shim).
    """
    if components is None:
        components = build_components(config, registry, evidence_store)
    return Pipeline(
        config=config,
        crawl_adapter=components.crawl_adapter,
        evidence_store=evidence_store,
        llm=components.llm,
        embedding_provider=components.embedding,
        taxonomy_plugin=components.taxonomy,
        path_configs=components.path_configs,
        scorer=components.scorer,
        editor=components.editor,
        implied_claim_checker=components.implied_claims,
    )
