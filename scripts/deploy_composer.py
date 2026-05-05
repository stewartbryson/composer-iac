"""Idempotently create or update a Cloud Composer 3 environment.

Configuration precedence (highest wins):
  1. Environment variables
  2. config/composer.yaml in the repo
  3. Built-in fallback defaults

Required env vars:
  GCP_PROJECT_ID, GCP_REGION, COMPOSER_ENV_NAME

Optional env vars:
  COMPOSER_IMAGE_VERSION         Composer 3 image (e.g. composer-3-airflow-2.10.5)
  COMPOSER_ENV_SIZE              SMALL | MEDIUM | LARGE
  COMPOSER_SERVICE_ACCOUNT       Runtime SA email used by the environment
  COMPOSER_NETWORK               Full VPC self-link or short name (optional)
  COMPOSER_SUBNETWORK            Full subnet self-link or short name (optional)
  COMPOSER_LABELS_JSON           Extra labels merged onto the environment (JSON object)
  RELEASE_TAG                    Stamped onto the environment as label "release"

Authentication is provided by google-github-actions/auth in CI, which exports
GOOGLE_APPLICATION_CREDENTIALS pointing at the deployer SA JSON. Locally, run
`gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from google.api_core.exceptions import NotFound
from google.cloud.orchestration.airflow import service_v1
from google.protobuf import field_mask_pb2

LOG = logging.getLogger("deploy_composer")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = REPO_ROOT / "config" / "composer.yaml"

LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
LABEL_VAL_RE = re.compile(r"^[a-z0-9_-]{0,63}$")

ENV_SIZE_MAP = {
    "SMALL": service_v1.EnvironmentConfig.EnvironmentSize.ENVIRONMENT_SIZE_SMALL,
    "MEDIUM": service_v1.EnvironmentConfig.EnvironmentSize.ENVIRONMENT_SIZE_MEDIUM,
    "LARGE": service_v1.EnvironmentConfig.EnvironmentSize.ENVIRONMENT_SIZE_LARGE,
}


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        LOG.error("Missing required environment variable: %s", name)
        sys.exit(2)
    return val


def _load_defaults() -> dict[str, Any]:
    if not DEFAULTS_PATH.exists():
        return {}
    with DEFAULTS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{DEFAULTS_PATH} must contain a YAML mapping at the top level")
    return data


def _merge_overrides(defaults: dict[str, Any]) -> dict[str, Any]:
    """Build the resolved config dict by overlaying env vars on YAML defaults."""
    cfg: dict[str, Any] = {
        "image_version": defaults.get("image_version", "composer-3-airflow-2.10.5"),
        "environment_size": defaults.get("environment_size", "SMALL"),
        "service_account": defaults.get("service_account"),
        "network": defaults.get("network"),
        "subnetwork": defaults.get("subnetwork"),
        "labels": dict(defaults.get("labels") or {}),
        "workloads": dict(defaults.get("workloads") or {}),
    }

    overrides = {
        "image_version": os.environ.get("COMPOSER_IMAGE_VERSION"),
        "environment_size": os.environ.get("COMPOSER_ENV_SIZE"),
        "service_account": os.environ.get("COMPOSER_SERVICE_ACCOUNT"),
        "network": os.environ.get("COMPOSER_NETWORK"),
        "subnetwork": os.environ.get("COMPOSER_SUBNETWORK"),
    }
    for k, v in overrides.items():
        if v:
            cfg[k] = v.strip()

    extra_labels = os.environ.get("COMPOSER_LABELS_JSON")
    if extra_labels:
        try:
            parsed = json.loads(extra_labels)
            if not isinstance(parsed, dict):
                raise ValueError("COMPOSER_LABELS_JSON must be a JSON object")
            cfg["labels"].update({str(k): str(v) for k, v in parsed.items()})
        except (json.JSONDecodeError, ValueError) as exc:
            LOG.error("Invalid COMPOSER_LABELS_JSON: %s", exc)
            sys.exit(2)

    release_tag = os.environ.get("RELEASE_TAG")
    if release_tag:
        cfg["labels"]["release"] = _normalize_label_value(release_tag)

    return cfg


def _normalize_label_value(value: str) -> str:
    """GCP labels allow only [a-z0-9_-], <=63 chars, must start with letter/digit."""
    cleaned = re.sub(r"[^a-z0-9_-]", "-", value.lower())[:63]
    return cleaned or "unspecified"


def _validate_labels(labels: dict[str, str]) -> None:
    for k, v in labels.items():
        if not LABEL_KEY_RE.match(k):
            raise ValueError(f"Invalid label key: {k!r}")
        if not LABEL_VAL_RE.match(v):
            raise ValueError(f"Invalid label value for {k!r}: {v!r}")


def _build_workloads_config(spec: dict[str, Any]) -> service_v1.WorkloadsConfig | None:
    """Construct WorkloadsConfig from a YAML mapping; return None if empty."""
    if not spec:
        return None

    workloads = service_v1.WorkloadsConfig()

    if (sched := spec.get("scheduler")):
        workloads.scheduler = service_v1.WorkloadsConfig.SchedulerResource(
            cpu=float(sched["cpu"]),
            memory_gb=float(sched["memory_gb"]),
            storage_gb=float(sched.get("storage_gb", 1)),
            count=int(sched.get("count", 1)),
        )
    if (web := spec.get("web_server")):
        workloads.web_server = service_v1.WorkloadsConfig.WebServerResource(
            cpu=float(web["cpu"]),
            memory_gb=float(web["memory_gb"]),
            storage_gb=float(web.get("storage_gb", 1)),
        )
    if (worker := spec.get("worker")):
        workloads.worker = service_v1.WorkloadsConfig.WorkerResource(
            cpu=float(worker["cpu"]),
            memory_gb=float(worker["memory_gb"]),
            storage_gb=float(worker.get("storage_gb", 1)),
            min_count=int(worker.get("min_count", 1)),
            max_count=int(worker.get("max_count", 3)),
        )
    if (trig := spec.get("triggerer")):
        workloads.triggerer = service_v1.WorkloadsConfig.TriggererResource(
            cpu=float(trig["cpu"]),
            memory_gb=float(trig["memory_gb"]),
            count=int(trig.get("count", 1)),
        )
    if (dagp := spec.get("dag_processor")):
        workloads.dag_processor = service_v1.WorkloadsConfig.DagProcessorResource(
            cpu=float(dagp["cpu"]),
            memory_gb=float(dagp["memory_gb"]),
            storage_gb=float(dagp.get("storage_gb", 1)),
            count=int(dagp.get("count", 1)),
        )
    return workloads


def _build_environment(parent: str, env_name: str, cfg: dict[str, Any]) -> service_v1.Environment:
    name = f"{parent}/environments/{env_name}"
    env_size_key = (cfg["environment_size"] or "SMALL").upper()
    if env_size_key not in ENV_SIZE_MAP:
        raise ValueError(f"environment_size must be one of {list(ENV_SIZE_MAP)}; got {env_size_key!r}")

    software_config = service_v1.SoftwareConfig(image_version=cfg["image_version"])

    node_config = service_v1.NodeConfig()
    if cfg.get("service_account"):
        node_config.service_account = cfg["service_account"]
    if cfg.get("network"):
        node_config.network = cfg["network"]
    if cfg.get("subnetwork"):
        node_config.subnetwork = cfg["subnetwork"]

    environment_config = service_v1.EnvironmentConfig(
        software_config=software_config,
        node_config=node_config,
        environment_size=ENV_SIZE_MAP[env_size_key],
    )
    workloads = _build_workloads_config(cfg.get("workloads") or {})
    if workloads is not None:
        environment_config.workloads_config = workloads

    _validate_labels(cfg["labels"])

    return service_v1.Environment(
        name=name,
        config=environment_config,
        labels=cfg["labels"],
    )


def _update_mask_for(cfg: dict[str, Any]) -> field_mask_pb2.FieldMask:
    """Mutable fields safe to update on an existing Composer 3 env."""
    paths = [
        "config.software_config.image_version",
        "config.environment_size",
        "labels",
    ]
    workloads = cfg.get("workloads") or {}
    if workloads.get("scheduler"):
        paths.append("config.workloads_config.scheduler")
    if workloads.get("web_server"):
        paths.append("config.workloads_config.web_server")
    if workloads.get("worker"):
        paths.append("config.workloads_config.worker")
    if workloads.get("triggerer"):
        paths.append("config.workloads_config.triggerer")
    if workloads.get("dag_processor"):
        paths.append("config.workloads_config.dag_processor")
    return field_mask_pb2.FieldMask(paths=paths)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    project = _require("GCP_PROJECT_ID")
    region = _require("GCP_REGION")
    env_name = _require("COMPOSER_ENV_NAME")

    cfg = _merge_overrides(_load_defaults())
    parent = f"projects/{project}/locations/{region}"
    full_name = f"{parent}/environments/{env_name}"

    LOG.info("Resolved configuration:")
    LOG.info("  project=%s region=%s env=%s", project, region, env_name)
    LOG.info("  image_version=%s size=%s", cfg["image_version"], cfg["environment_size"])
    LOG.info("  service_account=%s", cfg.get("service_account") or "(default compute SA)")
    LOG.info("  network=%s subnetwork=%s", cfg.get("network") or "(default)", cfg.get("subnetwork") or "(default)")
    LOG.info("  labels=%s", cfg["labels"])

    client = service_v1.EnvironmentsClient()
    environment = _build_environment(parent, env_name, cfg)

    try:
        existing = client.get_environment(name=full_name)
        LOG.info("Environment %s exists (state=%s); updating.", full_name, existing.state.name)
        op = client.update_environment(
            name=full_name,
            environment=environment,
            update_mask=_update_mask_for(cfg),
        )
    except NotFound:
        LOG.info("Environment %s not found; creating.", full_name)
        op = client.create_environment(parent=parent, environment=environment)

    LOG.info("Operation started: %s (this can take 20-30 minutes for create)", op.operation.name)
    result = op.result()
    LOG.info("Done. Environment URI: %s", result.name)
    LOG.info("Airflow UI: %s", getattr(result.config, "airflow_uri", "(not yet provisioned)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
