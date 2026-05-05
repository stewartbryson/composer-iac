# composer-iac

Lightweight, code-driven provisioning for a **Cloud Composer 3** environment, plus a release-driven CI/CD pipeline. No Terraform — the deployer is a small Python script using the official [`google-cloud-orchestration-airflow`](https://pypi.org/project/google-cloud-orchestration-airflow/) SDK.

> Status: prototype. Authentication uses a service-account JSON stored as a GitHub Actions secret. Move to Workload Identity Federation before any production use.

## How it works

```
conventional commit on main
        |
        v
  release-please.yml
        |
        |--(no release yet)--> opens / updates Release PR  ----.
        |                                                      |
        |                                                      | merge Release PR
        |                                                      v
        |<-------------------------- push to main runs release-please.yml again
        |
        v
  release_created == true
        |
        v
  deploy job (calls deploy.yml via workflow_call)
        |
        v
  python scripts/deploy_composer.py
        |
        v
  EnvironmentsClient  (create or update + LRO)
```

- `release-please` watches `main`, opens/maintains a Release PR from your conventional commits, bumps `version.txt`, and creates a GitHub Release when that PR is merged.
- Inside the same `release-please.yml` run that creates the release, a follow-up `deploy` job calls the reusable `deploy.yml` workflow (`workflow_call`) and applies the environment. We do this in-process instead of listening to `release: published` because GitHub's default `GITHUB_TOKEN` cannot trigger downstream workflows.
- `deploy.yml` is also exposed via `workflow_dispatch` for ad-hoc redeploys.
- The deployer is idempotent: it calls `get_environment`, then either `create_environment` or `update_environment` with a tight `update_mask`. Either path waits on the long-running operation.

## Repo layout

```
.
|-- .github/
|   |-- workflows/
|   |   |-- ci.yml                  # PR validation: ruff, pytest, dry-run smoke
|   |   |-- release-please.yml      # cuts releases on main, then calls deploy
|   |   `-- deploy.yml              # reusable: workflow_call + workflow_dispatch
|   |-- release-please-config.json
|   `-- .release-please-manifest.json
|-- config/
|   `-- composer.yaml               # non-secret env shape
|-- scripts/
|   |-- deploy_composer.py          # idempotent SDK deployer
|   |-- requirements.txt
|   `-- requirements-dev.txt        # pytest + ruff
|-- tests/
|   |-- conftest.py
|   `-- test_deploy_composer.py     # unit tests for the deployer helpers
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

The deployer accepts a `--dry-run` flag (or `DRY_RUN=1`) that resolves the config, builds the `Environment` proto and `update_mask`, prints them, and exits without contacting GCP. Use it to iterate on `config/composer.yaml` without burning the 20-30 minute create cycle.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt

export GCP_PROJECT_ID=my-proj
export GCP_REGION=us-central1
export COMPOSER_ENV_NAME=composer-dev
export COMPOSER_SERVICE_ACCOUNT=runtime-sa@my-proj.iam.gserviceaccount.com
export RELEASE_TAG=local-test

python scripts/deploy_composer.py --dry-run
```

For a real deploy, drop the `--dry-run` flag and authenticate first with `gcloud auth application-default login` (or set `GOOGLE_APPLICATION_CREDENTIALS`).

## Running tests locally

```bash
pip install -r scripts/requirements-dev.txt
ruff check scripts/ tests/
pytest -v
```

The same checks run automatically on every pull request via [`.github/workflows/ci.yml`](.github/workflows/ci.yml): ruff, workflow-YAML parse, `py_compile` of the deployer, the pytest suite, and a `--dry-run` smoke invocation against fake env vars.

## Cutting a release

1. Land conventional commits (`feat:`, `fix:`, etc.) on `main`.
2. Merge the Release PR opened by `release-please`. This bumps `version.txt` and publishes a GitHub Release.
3. The same `release-please.yml` run that creates the release then invokes the `deploy` job (which calls `deploy.yml` via `workflow_call`) and reapplies the environment, stamping the new tag onto the Composer env's `release` label.

You can also trigger an out-of-band deploy via the Actions UI ("Run workflow" on `deploy-composer`), optionally specifying a tag or branch in the `ref` input.

### Commit message format

`release-please` only reacts to commits that conform to [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/). The grammar is:

```
<type>[optional scope]: <description>
```

Working examples:

```text
feat: add deploy_composer.py
fix(deploy): correct triggerer memory default
feat(workflows): wire release-please into deploy
feat!: drop Composer 2 image versions     # breaking change
docs: clarify SA setup
```

Common mistakes that cause `release-please` to silently ignore your commit:

```text
feat(gitignore)              # missing ': <description>'
feat(deployment created)     # scope must be a single noun, no spaces
Initial commit               # not a conventional commit at all
```

If you tend to squash-merge PRs, set the squash commit subject to a conventional message — the PR title is what `release-please` will see.

## Out of scope

- Provisioning the deployer SA, runtime SA, or enabling APIs (do these manually before first run).
- Syncing DAGs into the environment's GCS bucket.
- Workload Identity Federation (intentional shortcut for this prototype).
