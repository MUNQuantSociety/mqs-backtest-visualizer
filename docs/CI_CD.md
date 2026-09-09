# CI/CD: dev checks, main releases

`dev` is CI-only. Pull requests into `dev` or `main` are tested; a push to
`main` runs the same CI workflow and can deploy the existing production ECS
service. There is no separate development deployment.

These files define the pipeline; they do not create GitHub protection rules,
IAM roles, buckets, ECS services, or database credentials.

## Activation status — 2026-09-09

The following activation state is confirmed by the operations handoff:

- Main ruleset **22692640** is active: PR-only, required status check `test`,
  strict status checks (the PR must be up to date), and no bypass actors.
- The **`production`** environment permits only the `main` branch and requires
  review by **`kmote02`**. Deployment variables and the OIDC role ARN secret
  are configured.
- The separate `Backtest_Visualizer_Integrations` Terraform root has
  provisioned the dedicated S3 bucket, task role, and GitHub deploy role.
  The narrowly scoped `ecs:TagResource` policy correction is applied.
- **`PRODUCTION_DEPLOY_ENABLED=false`** keeps production release disabled.
  The existing ECS task lacks usable database configuration, and CAIR
  PostgreSQL TLS remains off. Application authentication and worker
  sandboxing are separate unfinished release prerequisites.
- Backend and S3 checks have been verified in the finish-work. Hosted GitHub
  CI and the image build for the final commit remain unconfirmed until the
  commit is pushed and the final Actions run is inspected.

Keep the release flag false until all release prerequisites below are met.
The existing ECS service remains outside the integrations Terraform state.

## Branches and checks

| Event | Checks | Deployment |
| --- | --- | --- |
| PR targeting `main` or `dev` | Unit suite, disposable PostgreSQL fixture, image smoke | None |
| Push to `dev` | Same checks | None |
| Push to `main` | Reusable unit and PostgreSQL checks, then build/push | Existing ECS service, gated by `production` |
| Manual `deploy` on `main` | Same release path and exact selected commit | Same production gates |
| Manual `deploy` on any other branch/tag | Branch guard fails | None |

`ci.yml` exposes `workflow_call`, and `deploy.yml` calls it with a local
`./.github/workflows/ci.yml` reference. GitHub resolves this to the caller's
commit. Main has no additional standalone CI push trigger, so its expensive
tests run once. PR/dev image builds do not push; main builds and pushes once
in the deploy job. Testing a PR merge ref and later the actual main commit
is intentional. [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

The standalone PR check stays named **`test`**. It requires successful unit,
PostgreSQL, and image jobs and runs even if a dependency fails or is skipped.
On main, image checking is deferred to the release build; the gate permits
that specific skip. Main ruleset `22692640` requires `test`, enforces strict
status checks and PR-only changes, and has no bypass actors. Preserve the
check name when editing the workflow. This ruleset status applies to `main`;
the workflow also tests PRs targeting `dev`.

PR/dev runs cancel stale runs for the same ref. Production runs share
`deploy-production` with cancellation disabled; the reusable CI group is
distinct. GitHub concurrency limits simultaneous runs, but does not promise
FIFO ordering or retention of every pending run.

## CI and disposable data

Both Python test jobs use Python 3.12 and install the existing
`requirements.txt`, with pip caching. CI uses the same dependency file as the
image. PyYAML is supplied through `uvicorn[standard]`; the
workflow tests require it and do not silently fall back to text matching.

The unit job checks server imports, the existing architecture invariants,
and `python -m pytest -q -m 'not db'`. `CI_DATABASE_TESTS=0` prevents the
opt-in fixture proof from opening a database connection in this job.

The PostgreSQL job runs only
`tests/integration/test_ci_pipeline.py`. Its
synthetic market data and strategy exercise the real API/worker/engine
pipeline. It must not depend on production coverage, current-date windows,
production rows, or pre-existing caches. The fixture owns seeding and checks
that its target is disposable before connecting.

| Variable | Disposable value |
| --- | --- |
| `CI_DATABASE_TESTS` | `1` |
| `POSTGRES_HOST` | `127.0.0.1` |
| `POSTGRES_PORT` | `5432` |
| `POSTGRES_DB` | `mqs_test` |
| `POSTGRES_USER` | `mqs_test` |
| `POSTGRES_PASSWORD` | `ci-only-password` (public fixture credential) |
| `POSTGRES_SSLMODE` | `disable` (only the disposable local service) |
| `STRATEGY_STORE_BACKEND` | `local` |

GitHub starts a fresh `postgres:16` service, waits for `pg_isready`, maps
port 5432 to the runner, and removes the service at job completion.
Production `POSTGRES_*` or `MARKET_DATA_*` secrets must not be passed to CI.
CI has no deployment environment, cloud credentials, or OIDC permission.
[GitHub PostgreSQL services](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers).

`STRATEGY_STORE_ROOT`, `MARKET_CACHE_DIR`, and `ARTIFACT_DIR` are directed
under runner temp. Pytest's base temp is also under runner temp so fixture
overrides remain disposable. These directories are never cached or uploaded.
Only pip downloads and Docker build layers are cached.

`scripts/check_ci_test_report.py` reads the fixture's JUnit report and
requires at least one executed test, with no skipped, failed, or errored
cases. An absent fixture file, failed database connection, empty collection,
or all-skipped suite cannot produce a green PostgreSQL gate. The helper uses
only Python's standard library.

## Production configuration and release gate

The GitHub environment **`production`** is configured with a selected
**branch** rule for `main` and required reviewer **`kmote02`**. Preserve these
protections; the YAML environment declaration alone does not recreate them.
The workflow also rejects other repositories, non-main refs, and unsupported
events before CI or AWS authentication.
[GitHub environment protection](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).

| Setting | Location / purpose |
| --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | Configured Actions secret; this repository's dedicated GitHub OIDC deploy role |
| `AWS_REGION` | Configured Actions variable; region of the existing target |
| `ECR_REPOSITORY` | Configured Actions variable; existing repository name |
| `ECS_CLUSTER` | Configured Actions variable; existing cluster name/ARN |
| `ECS_SERVICE` | Configured Actions variable; existing service name |
| `ECS_CONTAINER_NAME` | Configured Actions variable; target API container, `api` |
| `PRODUCTION_DEPLOY_ENABLED` | Configured Actions variable; currently `false`; set exactly `true` only after clearing every release prerequisite |
| `ECS_TASK_ROLE_ARN` | Configured Actions variable; provisioned task role rendered into the new revision |
| `STRATEGY_STORE_S3_BUCKET` | Configured Actions variable; provisioned bucket, enables `s3` strategy storage |
| `STRATEGY_STORE_S3_PREFIX` | Configured Actions variable; prefix used by the S3 strategy store |

Missing required configuration fails the deploy job before authentication.
A missing/false release flag also fails; there is no green no-op.
Values are not echoed by the configuration guard. Optional S3 values only
render references to the provisioned integrations resources. A bucket override
sets `STRATEGY_STORE_BACKEND=s3`, bucket and prefix; a task-role override sets
`taskRoleArn`. Conflicting inherited S3 secret references are rejected.

Before any image build or task registration, the workflow requires an active,
stable ECS rolling service with positive desired count and circuit-breaker
rollback enabled. It checks an essential API container on port 8000,
Fargate/awsvpc, and Linux/X86_64 compatibility.

The inherited API container must supply `HOST`, `DB`, `USER`, and `PASSWORD`
through `POSTGRES_*` or `MARKET_DATA_*` environment entries or nonempty ECS
secret references. The effective SSL mode must be an explicit non-secret
`require`, `verify-ca`, or `verify-full`; absent mode or `prefer`/`disable`
fails. S3 requires a bucket and a task role. These are presence/configuration
checks, not proof of database TLS, credentials, grants, connectivity, or S3
permissions. CI never probes the production database.

The initial operations inspection on 2026-09-09 found the manually scaffolded
`mqs-backtest-visualizer:1` task with no task role,
empty API environment and no secret references; the inspected
Backtest_Visualizer state prefix was empty. The new integrations root owns
only its dedicated resources; it does not adopt the live ECS service or its
network. The existing service still needs usable database configuration.

Before enabling production release, the operator must:

1. Enable CAIR PostgreSQL TLS and verify the database credentials, access and
   network path needed by the ECS service. Configure the existing service's
   task definition with the database settings/secret references and a secure
   SSL mode required by the preflight.
2. Complete application authentication/authorization and sandboxing for
   uploaded strategy workers. These remain separate release blockers; the
   deployment preflight does not verify either control.
3. Inspect successful GitHub CI and image checks for the final pushed commit
   and confirm the existing service is stable with a recoverable revision.
4. Set `PRODUCTION_DEPLOY_ENABLED=true` only after those prerequisites are
   complete, then release through `main` and the production review gate.

## OIDC and deploy permissions

The dedicated GitHub deploy role is provisioned by
`Backtest_Visualizer_Integrations`. Maintain it through that separate root;
do not widen the `MQSMaster` role or apply the unrelated full infrastructure
stack. The configured role's trust conditions are:

```json
{
  "StringEquals": {
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub": "repo:MUNQuantSociety/mqs-backtest-visualizer:environment:production"
  }
}
```

The subject includes the environment. Main is enforced through the configured
production branch policy and the workflow guards. Do not
use repository-wide or organization-wide wildcards. Repositories using
GitHub's immutable subject format include owner/repository IDs; if this
repository migrates to that format, update the role trust to match.
[GitHub OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

The applied deploy policy covers the workflow's operations. ECR access and
ECS service updates are scoped to the existing target, task listing and
description are constrained to its cluster, and `PassRole` is restricted to
the provisioned task role and the existing execution role:

- ECR: `GetAuthorizationToken`, `BatchCheckLayerAvailability`,
  `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`,
  `BatchGetImage`, `GetDownloadUrlForLayer`.
- ECS: `DescribeServices`, `DescribeTaskDefinition`, `RegisterTaskDefinition`,
  `UpdateService`, `ListTasks`, `DescribeTasks`,
  `TagResource` (registration preserves user task-definition tags).
- IAM: `PassRole` for the existing execution role and provisioned task role,
  constrained by `iam:PassedToService=ecs-tasks.amazonaws.com`.

The applied tagging grant is restricted to this task-definition family and
`ecs:CreateAction=RegisterTaskDefinition`. It permits preserving tags during
registration without granting arbitrary edits to existing resource tags.
`ListTagsForResource` is not needed by the current calls: tags are returned
by `DescribeTaskDefinition --include TAGS`.
[AWS tagging authorization](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/supported-iam-actions-tagging.html),
[AWS API permission mapping](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecs.html).

The workflow does not need IAM creation, ECS service creation,
SSM/Secrets Manager value reads, or direct S3 data permissions. The task role
owns S3 access; the execution role owns resolving database secret references.
The configured existing execution role is `service-role/ecsTaskExecutionRole`.

## Image and rollout identity

The Dockerfile uses Python 3.12-slim, installs the existing requirements and
copies only `server.py`, `src/`, and `engine/`. It preserves engine strategy
JSON files and `engine/data/`. Non-root `appuser` has writable artifact,
strategy-store and market-cache directories. `.dockerignore` excludes local
environments, credentials, runtime data, tests and development trees.

PR/dev image checks build for `linux/amd64` and run an import/write smoke test
with networking disabled. They do not start the ASGI lifespan or a production
database connection. The release build pushes only the full Git commit SHA
tag and consumes Buildx's manifest digest. Provenance/SBOM index generation
is disabled so the recorded digest can be compared directly to the running
single-platform container manifest.

The deployment sequence is:

1. Read the configured service's active task definition and validate it.
2. Preserve roles, secret references, limits, volumes, sidecars, logging and
   health checks, with only the explicit optional S3 overrides above.
3. Build/push the SHA image and set the API image to `repository@sha256:...`.
4. Recheck the baseline service deployment so a change during the build
   aborts instead of being overwritten.
5. Register a new task definition and call `update-service --task-definition`
   with the returned revision ARN; record its deployment ID.
6. Wait for stability, then verify the exact revision/deployment is PRIMARY
   and COMPLETED, with positive desired count and no pending tasks.
7. Describe every running service task and require the intended revision,
   deployment ID and API image digest. Missing/unhealthy/stopped containers
   or incomplete task responses fail verification.

`services-stable` alone can succeed when ECS has rolled back to an older
revision. Its documented condition checks deployment count and running versus
desired tasks; it does not require the intended revision. The extra identity
checks deliberately make a stable rollback red.
[AWS stability waiter](https://docs.aws.amazon.com/cli/latest/reference/ecs/wait/services-stable.html),
[AWS circuit breaker](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html),
[AWS running task details](https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-tasks.html).

The Actions summary records source SHA, image digest, previous task revision
and intended task revision. Success is appended only after verification.
Full task definitions remain in runner temp and are not printed or uploaded.

## Rollback and operational limits

Let the configured ECS circuit breaker handle a failed rollout; the workflow
will still fail if the intended revision did not complete. For a separately
authorized manual rollback, select a known-good task revision ARN from a
successful release, verify its digest remains in ECR, and update the existing
service to that ARN. Reapply the same revision/digest checks afterward:

```bash
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --task-definition "$KNOWN_GOOD_TASK_DEFINITION_ARN"
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
```

The waiter is only the first check. Do not retag `latest` or depend on a
floating tag to identify rollback code. Keep rollback revisions and their
ECR digests retained. Rebuilding a SHA can produce different bytes because
the base image and some dependency ranges are not locked; task revisions pin
the actual digest. Operators maintain the image retention and mutability
policy in the infrastructure configuration.

The Dockerfile health probe is `/api/v1/health`. ECS only monitors container
health checks declared in the task definition, so the Dockerfile instruction
does not configure an ECS health check. The workflow preserves inherited
checks; operations owns verifying the ALB probe and any ECS container check.
An equivalent container command uses the image's Python interpreter:

```json
["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"]
```

Tasks without an ECS container health check can report `UNKNOWN`; identity
verification permits that and rejects `UNHEALTHY`. The configured ALB check
and circuit breaker must provide the corresponding service health signal.
Health responses do not prove database or S3 functionality.
[AWS container health checks](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/healthcheck.html).

Operations must coordinate deployments outside GitHub; its concurrency lock
does not lock the ECS API. A race after the baseline recheck cannot be made
atomic by these CLI calls. Waiter timeout is a failed release even if ECS
converges later; inspect the actual revision before retrying. A scaled-to-zero
or already unhealthy service requires operator recovery before deploying.

## Validation

Local validation uses the project venv, never a live database:

```powershell
& venv/Scripts/python.exe -m pytest tests/unit/test_workflows_are_valid_yaml.py tests/unit/test_engine_imports.py tests/unit/test_health.py -q -m 'not db' -p no:cacheprovider
```

The workflow tests parse YAML with duplicate-key rejection, compile embedded
Python, parse shell syntax where Bash is available, and exercise guards,
rendering and rollout checks against mocked AWS responses. They do not run
Docker or AWS. Actionlint checks GitHub-specific schema/expression validity.
Actual image build and PostgreSQL fixture execution must be verified on
GitHub runners for the final pushed commit. The environment, deploy IAM
policy and S3 integrations are configured. Production release remains
disabled pending usable ECS database configuration, CAIR TLS, application
authentication and worker sandboxing. Local test results and backend/S3
verification do not establish a green hosted CI run.
