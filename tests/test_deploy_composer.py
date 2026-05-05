"""Unit tests for scripts/deploy_composer.py.

These exercise the pure-Python helpers (config merging, label normalization,
proto building, update-mask construction) without touching GCP.
"""

from __future__ import annotations

from typing import Any

import pytest

import deploy_composer as dc


# ---- _normalize_label_value ------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("v0.1.0", "v0-1-0"),
        ("V0.1.0", "v0-1-0"),
        ("RELEASE_TAG/123", "release_tag-123"),
        ("feat/foo bar", "feat-foo-bar"),
        ("a" * 80, "a" * 63),
        ("!!!", "---"),
        ("", "unspecified"),
    ],
)
def test_normalize_label_value(raw: str, expected: str) -> None:
    assert dc._normalize_label_value(raw) == expected


# ---- _validate_labels ------------------------------------------------------


def test_validate_labels_accepts_valid() -> None:
    dc._validate_labels({"managed-by": "github-actions", "release": "v0-1-0"})


@pytest.mark.parametrize(
    "labels",
    [
        {"Bad-Key": "v"},
        {"1bad": "v"},
        {"good": "Bad Value"},
        {"good": "x" * 64},
    ],
)
def test_validate_labels_rejects_invalid(labels: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        dc._validate_labels(labels)


# ---- _merge_overrides ------------------------------------------------------


def test_merge_overrides_uses_defaults_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "COMPOSER_IMAGE_VERSION",
        "COMPOSER_ENV_SIZE",
        "COMPOSER_SERVICE_ACCOUNT",
        "COMPOSER_NETWORK",
        "COMPOSER_SUBNETWORK",
        "COMPOSER_LABELS_JSON",
        "RELEASE_TAG",
    ):
        monkeypatch.delenv(var, raising=False)

    defaults: dict[str, Any] = {
        "image_version": "composer-3-airflow-2.10.5",
        "environment_size": "SMALL",
        "labels": {"managed-by": "github-actions"},
        "workloads": {"scheduler": {"cpu": 0.5, "memory_gb": 2.0}},
    }
    cfg = dc._merge_overrides(defaults)
    assert cfg["image_version"] == "composer-3-airflow-2.10.5"
    assert cfg["environment_size"] == "SMALL"
    assert cfg["labels"] == {"managed-by": "github-actions"}
    assert cfg["workloads"]["scheduler"]["cpu"] == 0.5


def test_merge_overrides_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSER_IMAGE_VERSION", "composer-3-airflow-2.10.5-build.99")
    monkeypatch.setenv("COMPOSER_ENV_SIZE", "MEDIUM")
    monkeypatch.setenv("COMPOSER_SERVICE_ACCOUNT", "sa@p.iam.gserviceaccount.com")
    monkeypatch.setenv("RELEASE_TAG", "v1.2.3")
    monkeypatch.delenv("COMPOSER_LABELS_JSON", raising=False)

    cfg = dc._merge_overrides({"image_version": "old", "environment_size": "SMALL"})
    assert cfg["image_version"] == "composer-3-airflow-2.10.5-build.99"
    assert cfg["environment_size"] == "MEDIUM"
    assert cfg["service_account"] == "sa@p.iam.gserviceaccount.com"
    assert cfg["labels"]["release"] == "v1-2-3"


def test_merge_overrides_extra_labels_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSER_LABELS_JSON", '{"team": "data-platform", "env": "dev"}')
    monkeypatch.delenv("RELEASE_TAG", raising=False)
    cfg = dc._merge_overrides({"labels": {"managed-by": "github-actions"}})
    assert cfg["labels"] == {
        "managed-by": "github-actions",
        "team": "data-platform",
        "env": "dev",
    }


def test_merge_overrides_invalid_labels_json_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSER_LABELS_JSON", "not-valid-json")
    with pytest.raises(SystemExit) as exc:
        dc._merge_overrides({})
    assert exc.value.code == 2


# ---- _build_workloads_config ----------------------------------------------


def test_build_workloads_returns_none_when_empty() -> None:
    assert dc._build_workloads_config({}) is None


def test_build_workloads_full_spec() -> None:
    spec = {
        "scheduler": {"cpu": 0.5, "memory_gb": 2.0, "storage_gb": 1.0, "count": 1},
        "web_server": {"cpu": 0.5, "memory_gb": 2.0, "storage_gb": 1.0},
        "worker": {"cpu": 0.5, "memory_gb": 2.0, "storage_gb": 1.0, "min_count": 1, "max_count": 3},
        "triggerer": {"cpu": 0.5, "memory_gb": 0.5, "count": 1},
        "dag_processor": {"cpu": 0.5, "memory_gb": 1.0, "storage_gb": 1.0, "count": 1},
    }
    wc = dc._build_workloads_config(spec)
    assert wc is not None
    assert wc.scheduler.cpu == 0.5
    assert wc.web_server.memory_gb == 2.0
    assert wc.worker.max_count == 3
    assert wc.triggerer.count == 1
    assert wc.dag_processor.cpu == 0.5


def test_build_workloads_triggerer_has_no_storage_gb_field() -> None:
    """Regression: TriggererResource lacks storage_gb. Passing it would raise."""
    spec = {"triggerer": {"cpu": 0.5, "memory_gb": 0.5, "storage_gb": 1.0}}
    wc = dc._build_workloads_config(spec)
    assert wc is not None
    assert wc.triggerer.cpu == 0.5
    assert wc.triggerer.memory_gb == 0.5


# ---- _build_environment ---------------------------------------------------


def _minimal_cfg() -> dict[str, Any]:
    return {
        "image_version": "composer-3-airflow-2.10.5",
        "environment_size": "SMALL",
        "service_account": "sa@p.iam.gserviceaccount.com",
        "network": None,
        "subnetwork": None,
        "labels": {"managed-by": "github-actions"},
        "workloads": {},
    }


def test_build_environment_minimal() -> None:
    env = dc._build_environment("projects/p/locations/us-central1", "composer-dev", _minimal_cfg())
    assert env.name == "projects/p/locations/us-central1/environments/composer-dev"
    assert env.config.environment_size.name == "ENVIRONMENT_SIZE_SMALL"
    assert env.config.software_config.image_version == "composer-3-airflow-2.10.5"
    assert env.config.node_config.service_account == "sa@p.iam.gserviceaccount.com"
    assert env.config.node_config.network == ""
    assert dict(env.labels) == {"managed-by": "github-actions"}


def test_build_environment_size_case_insensitive() -> None:
    cfg = _minimal_cfg()
    cfg["environment_size"] = "medium"
    env = dc._build_environment("projects/p/locations/us-central1", "e", cfg)
    assert env.config.environment_size.name == "ENVIRONMENT_SIZE_MEDIUM"


def test_build_environment_invalid_size() -> None:
    cfg = _minimal_cfg()
    cfg["environment_size"] = "HUGE"
    with pytest.raises(ValueError, match="environment_size must be one of"):
        dc._build_environment("projects/p/locations/us-central1", "e", cfg)


def test_build_environment_with_network() -> None:
    cfg = _minimal_cfg()
    cfg["network"] = "projects/p/global/networks/default"
    cfg["subnetwork"] = "projects/p/regions/us-central1/subnetworks/default"
    env = dc._build_environment("projects/p/locations/us-central1", "e", cfg)
    assert env.config.node_config.network == cfg["network"]
    assert env.config.node_config.subnetwork == cfg["subnetwork"]


# ---- _update_mask_for ------------------------------------------------------


def test_update_mask_baseline_paths() -> None:
    paths = list(dc._update_mask_for({"workloads": {}}).paths)
    assert paths == [
        "config.software_config.image_version",
        "config.environment_size",
        "labels",
    ]


def test_update_mask_includes_workload_paths() -> None:
    cfg = {
        "workloads": {
            "scheduler": {"cpu": 0.5, "memory_gb": 2.0},
            "worker": {"cpu": 0.5, "memory_gb": 2.0},
        }
    }
    paths = list(dc._update_mask_for(cfg).paths)
    assert "config.workloads_config.scheduler" in paths
    assert "config.workloads_config.worker" in paths
    assert "config.workloads_config.web_server" not in paths


# ---- main() / --dry-run ---------------------------------------------------


def test_main_dry_run_succeeds_without_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "fake-proj")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    monkeypatch.setenv("COMPOSER_ENV_NAME", "composer-dev")
    monkeypatch.setenv("COMPOSER_SERVICE_ACCOUNT", "sa@fake-proj.iam.gserviceaccount.com")
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    caplog.set_level("INFO", logger="deploy_composer")
    rc = dc.main(["--dry-run"])
    assert rc == 0
    text = caplog.text
    assert "DRY RUN" in text
    assert "fake-proj" in text
    assert "composer-dev" in text


def test_main_dry_run_via_env_var(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "fake-proj")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    monkeypatch.setenv("COMPOSER_ENV_NAME", "composer-dev")
    monkeypatch.setenv("DRY_RUN", "1")
    caplog.set_level("INFO", logger="deploy_composer")
    rc = dc.main([])
    assert rc == 0
    assert "DRY RUN" in caplog.text


def test_main_missing_required_env_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GCP_PROJECT_ID", "GCP_REGION", "COMPOSER_ENV_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DRY_RUN", "1")
    with pytest.raises(SystemExit) as exc:
        dc.main([])
    assert exc.value.code == 2


def test_load_defaults_returns_dict() -> None:
    """The bundled config/composer.yaml must parse cleanly and be a mapping."""
    data = dc._load_defaults()
    assert isinstance(data, dict)
    assert "image_version" in data
