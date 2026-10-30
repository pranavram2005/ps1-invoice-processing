"""The live rule configuration.

One process-wide RuleConfig, persisted to `app_config` so a change survives a
restart. It lives in its own module so the API, the pipeline and the rule engine
all read the same object and there is exactly one answer to "what thresholds are
in force right now".

This is what makes the rules demonstrably real: change the PO tolerance from 2%
to 5% in the Admin tab, re-run the same invoice, and the decision flips. A
hardcoded threshold could not do that.
"""
from __future__ import annotations

import logging

from . import db
from .rules import RuleConfig

log = logging.getLogger("state")

_config = RuleConfig()


def load_from_db() -> RuleConfig:
    """Called at startup. Unknown or malformed keys are ignored, so a stale
    override row can never stop the service from booting."""
    try:
        overrides = db.load_config_overrides()
    except Exception as exc:
        log.warning("could not load rule config overrides: %s", exc)
        return _config
    if overrides:
        changed = _config.apply(overrides)
        if changed:
            log.info("rule config overrides applied: %s", ", ".join(changed))
    return _config


def current() -> RuleConfig:
    return _config


def update(patch: dict) -> tuple[RuleConfig, list[str]]:
    before = _config.to_dict()
    changed = _config.apply(patch)
    if changed:
        db.save_config_overrides(_config.to_dict())
        log.info(
            "rule config changed: %s",
            ", ".join(f"{k} {before[k]} -> {getattr(_config, k)}" for k in changed),
        )
    return _config, changed


def reset() -> RuleConfig:
    global _config
    _config = RuleConfig()
    db.save_config_overrides(_config.to_dict())
    return _config
