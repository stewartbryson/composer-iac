# composer-iac

Lightweight, code-driven provisioning for a **Cloud Composer 3** environment, plus a release-driven CI/CD pipeline. No Terraform — the deployer is a small Python script using the official [`google-cloud-orchestration-airflow`](https://pypi.org/project/google-cloud-orchestration-airflow/) SDK.

> Status: prototype. Authentication uses a service-account JSON stored as a GitHub Actions secret. Move to Workload Identity Federation before any production use.

## How it works

```
conventional commit on main
        |
        v
  release-please.yml  --opens-->  Release PR
        ^                              |
        |                              | merge
        |                              v
  bumps version.txt           GitHub Release published
                                       |
                                       v
                              deploy.yml (release:published)
                                       |
                                       v
                       python scripts/deploy_composer.py
                                       |
                              EnvironmentsClient
                            (create or update + LRO)
```

- `release-please` watches `main`, opens/maintains a Release PR from your conventional commits, bumps `version.txt`, and creates a GitHub Release when that PR is merged.
- The `deploy-composer` workflow fires on `release: published` (and on manual `workflow_dispatch`), authenticates with the deployer SA JSON, and runs `scripts/deploy_composer.py`.
- The deployer is idempotent: it calls `get_environment`, then either `create_environment` or `update_environment` with a tight `update_mask`. Either path waits on the long-running operation.

## Repo layout

```
.
|-- .github/
|   |-- workflows/
|   |   |-- release-please.yml      # cuts releases on main
|   |   `-- deploy.yml              # deploys on release:published
|   |-- release-please-config.json
|   `-- .release-please-manifest.json
|-- config/
|   `-- composer.yaml               # non-secret env shape
|-- scripts/
|   |-- deploy_composer.py          # idempotent SDK deployer
|   `-- requirements.txt
|-- version.txt                     # managed by release-please
`-- README.md
```

## One-time GCP prerequisites (manual)

Done outside of this repo, before the first run.

1. Pick / create a GCP project; note the project ID.
2. Enable APIs: `composer.googleapis.com`, `compute.googleapis.com`, `iam.googleapis.com`, `cloudresourcemanager.googleapis.com`, `artifactregistry.googleapis.com`.
3. Create the **deployer** service account (used by GitHub Actions). Grant it on the project:
   - `roles/composer.admin`
   - `roles/iam.serviceAccountUser` (so it can act as the runtime SA)
   - `roles/storage.admin` (Composer creates a GCS bucket per environment)
4. Create the **runtime** service account (used by the Composer environment itself). Grant it on the project:
   - `roles/composer.worker`
5. Generate a JSON key for the deployer SA. You will paste this into the `GCP_SA_KEY` secret below.
6. (Optional) If you plan to use a custom VPC, create the network/subnet ahead of time and capture their self-links.

## GitHub Actions configuration

Settings -> Secrets and variables -> Actions.

### Secrets (sensitive)

| Name | Value |
| --- | --- |
| `GCP_SA_KEY` | Full JSON content of the deployer SA key. |
| `GCP_PROJECT_ID` | Target GCP project ID. |
| `COMPOSER_SERVICE_ACCOUNT` | Email of the runtime SA used by the Composer env. |

### Variables (non-sensitive)

| Name | Example | Notes |
| --- | --- | --- |
| `GCP_REGION` | `us-central1` | Composer 3 region. |
| `COMPOSER_ENV_NAME` | `composer-dev` | Environment ID. |
| `COMPOSER_IMAGE_VERSION` | `composer-3-airflow-2.10.5` | List options with `gcloud composer images list --location=<REGION> --filter="imageVersionId ~ composer-3"`. |
| `COMPOSER_ENV_SIZE` | `SMALL` | `SMALL`, `MEDIUM`, or `LARGE`. |
| `COMPOSER_NETWORK` | _(empty)_ | Optional VPC self-link or short name. |
| `COMPOSER_SUBNETWORK` | _(empty)_ | Optional subnet self-link or short name. |

If a variable is unset, the deployer falls back to the value in [`config/composer.yaml`](config/composer.yaml).

## Local dry run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt
gcloud auth application-default login

export GCP_PROJECT_ID=my-proj
export GCP_REGION=us-central1
export COMPOSER_ENV_NAME=composer-dev
export COMPOSER_SERVICE_ACCOUNT=runtime-sa@my-proj.iam.gserviceaccount.com
export RELEASE_TAG=local-test

python scripts/deploy_composer.py
```

The first create can take 20-30 minutes; updates are usually much faster.

## Cutting a release

1. Land conventional commits (`feat:`, `fix:`, etc.) on `main`.
2. Merge the Release PR opened by `release-please`. This bumps `version.txt` and publishes a GitHub Release.
3. The `deploy-composer` workflow fires automatically and reapplies the environment with the new release tag stamped as a `release` label on the Composer env.

You can also trigger an out-of-band deploy via the Actions UI ("Run workflow" on `deploy-composer`).

## Out of scope

- Provisioning the deployer SA, runtime SA, or enabling APIs (do these manually before first run).
- Syncing DAGs into the environment's GCS bucket.
- Workload Identity Federation (intentional shortcut for this prototype).
