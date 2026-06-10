"""One load authority for all configuration surfaces (ADR-002, finding 1.3).

Composes the existing loaders — it does not replace them. Owns path
selection (previously hardcoded in the wiring: ``taxonomies/wellbeing-8d.yaml``
and the ``path_configs/{thnklabs,default}.yaml`` preference) and documents
precedence: env > YAML file > types.py defaults.

Policy loading is forgiving per PDR-003: a missing ``policies/`` directory
yields an empty dict, and a loader exception is logged and tolerated —
per-file catch-log-continue already lives inside ``load_policies``. Pre-deploy
strictness is ``cce validate``'s job, not the boot path's.

Note the deliberate dependency edge: config.registry → policy.loader,
tagging.loader (no cycle — neither package imports config.registry; verified
2026-06-09). CLAUDE.md dependency flow is updated in the same commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from cce.config.loader import ConfigError, load_config
from cce.config.markers import HumanizationMarkers, load_markers
from cce.config.types import EngineConfig
from cce.models.paths import PathConfig
from cce.policy.loader import load_policies
from cce.policy.types import SourcePolicy
from cce.tagging.loader import load_path_configs

logger = logging.getLogger(__name__)

# The taxonomy file the wiring selects when present (Phase 2 default).
_TAXONOMY_FILENAME = "wellbeing-8d.yaml"

# Operator file first (untracked), then the committed Tier B template.
_PATH_CONFIG_CANDIDATES = ("thnklabs.yaml", "default.yaml")


@dataclass
class ConfigRegistry:
    """Everything loaded from disk/env at startup, in one place.

    Config-time data only — live runtime objects (stores, providers) are
    wired by ``cce.components.build_components``, which consumes this.
    """

    engine: EngineConfig
    policies: dict[str, SourcePolicy] = field(default_factory=dict)
    path_configs: dict[str, PathConfig] = field(default_factory=dict)
    taxonomy_path: Path | None = None
    markers: HumanizationMarkers | None = None

    @classmethod
    def load(
        cls,
        root: Path,
        config_path: Path | None = None,
        *,
        engine: EngineConfig | None = None,
        policies_dir: Path | None = None,
        taxonomies_dir: Path | None = None,
        path_configs_path: Path | None = None,
    ) -> ConfigRegistry:
        """Load every configuration surface, in order:

        1. Engine config — ``load_config(config_path)`` (env > YAML >
           ``types.py`` defaults), unless an already-built ``engine`` is
           passed (the API lifespan receives its config from ``create_app``).
        2. Policies — ``root / "policies"`` (or ``policies_dir``), forgiving:
           missing directory → empty dict; loader exception → warn + empty
           dict (PDR-003).
        3. Path configs — ``path_configs_path`` when given; otherwise the
           first of ``root / "path_configs" / {thnklabs,default}.yaml`` that
           exists and yields a non-empty dict.
        4. Taxonomy — record ``root / "taxonomies" / "wellbeing-8d.yaml"``
           (or under ``taxonomies_dir``) when the file exists; parsing stays
           in ``build_components``, which constructs the plugin.
        5. Markers — loaded iff ``engine.humanization.enabled``; a missing
           markers file raises ``ConfigError`` (intentional fail-fast —
           an operator who enabled humanization must not silently ship
           unscored drafts; ConfigError so every CLI/app entry point renders
           it as one actionable line, same as missing API keys).

        Relative directory arguments resolve against ``root``; absolute
        arguments are used as-is (``Path.__truediv__`` semantics).
        """
        if engine is None:
            engine = load_config(config_path)

        policies = _load_policies_forgiving(
            root / (policies_dir if policies_dir is not None else "policies")
        )

        if path_configs_path is not None:
            candidates = [root / path_configs_path]
        else:
            candidates = [
                root / "path_configs" / name for name in _PATH_CONFIG_CANDIDATES
            ]
        path_configs: dict[str, PathConfig] = {}
        for candidate in candidates:
            if candidate.exists():
                loaded = load_path_configs(candidate)
                if loaded:
                    path_configs = loaded
                    logger.info(
                        "Path configs loaded from %s: %s",
                        candidate,
                        list(path_configs.keys()),
                    )
                    break

        taxonomy_path: Path | None = (
            root
            / (taxonomies_dir if taxonomies_dir is not None else "taxonomies")
            / _TAXONOMY_FILENAME
        )
        if not taxonomy_path.exists():
            taxonomy_path = None

        markers: HumanizationMarkers | None = None
        if engine.humanization.enabled:
            try:
                markers = load_markers(root / engine.humanization.markers_path)
            except FileNotFoundError as e:
                raise ConfigError(str(e)) from e

        return cls(
            engine=engine,
            policies=policies,
            path_configs=path_configs,
            taxonomy_path=taxonomy_path,
            markers=markers,
        )

    def get_policy(self, policy_id: str) -> SourcePolicy:
        """Raise KeyError listing known IDs — mirrors the API 400 payload."""
        try:
            return self.policies[policy_id]
        except KeyError:
            known = ", ".join(sorted(self.policies)) or "(none loaded)"
            raise KeyError(
                f"Unknown policy {policy_id!r}. Known policies: {known}"
            ) from None

    def get_path_config(self, path_id: str) -> PathConfig:
        try:
            return self.path_configs[path_id]
        except KeyError:
            known = ", ".join(sorted(self.path_configs)) or "(none loaded)"
            raise KeyError(f"Unknown path {path_id!r}. Known paths: {known}") from None


def _load_policies_forgiving(directory: Path) -> dict[str, SourcePolicy]:
    """Directory-missing tolerance on top of load_policies' own per-file
    catch-log-continue (PDR-003). One behavior for both wiring modes."""
    if not directory.exists():
        return {}
    try:
        return load_policies(directory)
    except Exception as e:
        logger.warning("Failed to load policies from %s: %s", directory, e)
        return {}
