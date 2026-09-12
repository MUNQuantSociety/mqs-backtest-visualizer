"""Workflow contracts and rollout regression tests. No Docker, database or AWS."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.check_ci_test_report import check_report

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
REPOSITORY = "MUNQuantSociety/mqs-backtest-visualizer"
PREVIOUS = "arn:aws:ecs:us-east-2:123456789012:task-definition/api:1"
REVISION = "arn:aws:ecs:us-east-2:123456789012:task-definition/api:2"
DIGEST = "sha256:" + "a" * 64


class UniqueKeyLoader(yaml.SafeLoader):
    """A duplicated key must not silently disable a guard."""


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _load(name):
    document = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    assert isinstance(document, dict)
    return document


def _triggers(document):
    # PyYAML's YAML 1.1 reader treats the unquoted Actions key 'on' as True.
    return document.get("on", document.get(True))


def _step(name, job="deploy", workflow="deploy.yml"):
    return next(s for s in _load(workflow)["jobs"][job]["steps"] if s.get("name") == name)


def _execute(step):
    assert step["shell"] == "python {0}"
    exec(compile(step["run"], "<workflow step>", "exec"), {"__name__": "__main__"})


def test_triggers_reusable_ci_and_required_check():
    ci, deploy = _load("ci.yml"), _load("deploy.yml")
    assert set(_triggers(ci)) == {"pull_request", "push", "workflow_call"}
    assert _triggers(ci)["pull_request"]["branches"] == ["main", "dev"]
    assert _triggers(ci)["push"]["branches"] == ["dev"]
    assert set(_triggers(deploy)) == {"push", "workflow_dispatch"}
    assert _triggers(deploy)["push"]["branches"] == ["main"]
    assert ci["permissions"] == deploy["permissions"] == {"contents": "read"}
    assert not (WORKFLOWS / ".gitkeep").exists()
    assert "secrets." not in (WORKFLOWS / "ci.yml").read_text()
    assert "id-token" not in (WORKFLOWS / "ci.yml").read_text()
    gate = ci["jobs"]["test"]
    assert gate.get("name", "test") == "test" and gate["if"] == "always()"
    assert set(gate["needs"]) == {"unit", "postgres", "image"}
    call = deploy["jobs"]["test"]
    assert call["uses"] == "./.github/workflows/ci.yml" and call["needs"] == "branch"
    assert "secrets" not in call and "steps" not in call
    release = deploy["jobs"]["deploy"]
    assert release["needs"] == "test" and release["environment"] == "production"
    assert "github.ref == 'refs/heads/main'" in release["if"] and REPOSITORY in release["if"]
    assert release["permissions"] == {"contents": "read", "id-token": "write"}
    assert release["steps"][0]["id"] == "guard"
    assert deploy["concurrency"] == {"group": "deploy-production", "cancel-in-progress": False}
    assert ci["concurrency"]["group"].startswith("ci-")
    assert "pull_request" in ci["concurrency"]["cancel-in-progress"]
    assert "refs/heads/dev" in ci["concurrency"]["cancel-in-progress"]


def test_ci_uses_disposable_postgres_and_scoped_tests():
    jobs = _load("ci.yml")["jobs"]
    unit = "\n".join(s.get("run", "") for s in jobs["unit"]["steps"])
    assert "import server" in unit and "pytest -q -m 'not db'" in unit
    pg = jobs["postgres"]
    expected = {
        "CI_DATABASE_TESTS": "1", "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "mqs_test", "POSTGRES_USER": "mqs_test",
        "POSTGRES_PASSWORD": "ci-only-password", "POSTGRES_SSLMODE": "disable",
    }
    assert {k: pg["env"][k] for k in expected} == expected
    service = pg["services"]["postgres"]
    assert service["image"] == "postgres:16" and service["ports"] == ["5432:5432"]
    assert service["env"] == {k: expected[k] for k in
                              ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")}
    assert "pg_isready" in service["options"]
    commands = "\n".join(s.get("run", "") for s in pg["steps"])
    assert "pytest tests/integration/test_ci_pipeline.py" in commands
    assert "--junitxml=" in commands and "scripts/check_ci_test_report.py" in commands
    for job in ("unit", "postgres"):
        for key in ("STRATEGY_STORE_ROOT", "MARKET_CACHE_DIR", "ARTIFACT_DIR"):
            if job == "postgres":
                integration = _step("Deterministic PostgreSQL integration tests", job, "ci.yml")
                assert "runner.temp" in integration["env"][key]
            else:
                storage = _step("Set transient test storage", job, "ci.yml")["run"]
                assert "RUNNER_TEMP" in storage and key in storage and "GITHUB_ENV" in storage
        setup = next(s for s in jobs[job]["steps"]
                     if s.get("uses", "").startswith("actions/setup-python@"))
        assert setup["with"]["python-version"] == "3.12"
        assert setup["with"]["cache-dependency-path"] == "requirements.txt"
    assert jobs["image"]["if"] == (
        "github.event_name == 'pull_request' || github.ref == 'refs/heads/dev'"
    )


def test_container_and_digest_contract():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG PYTHON_VERSION=3.12" in dockerfile and "USER appuser" in dockerfile
    assert "COPY --chown=appuser:appuser . ." not in dockerfile
    for source in ("server.py", "src/", "engine/"):
        assert f"COPY --chown=appuser:appuser {source} " in dockerfile
    assert "/app/.artifacts /app/.strategy_store /app/data/backfill_cache" in dockerfile
    assert "/api/v1/health" in dockerfile
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    for pattern in ("**/.env", "**/.env.*", "**/.git", "**/venv", "data/",
                    ".strategy_store/", ".artifacts/", "frontend/", "scripts/"):
        assert pattern in patterns
    assert "**/data" not in patterns and "engine/" not in patterns
    build = next(s for s in _load("deploy.yml")["jobs"]["deploy"]["steps"] if s.get("id") == "build")
    assert "github.sha" in build["with"]["tags"]
    assert "\n" not in build["with"]["tags"]
    assert build["with"]["platforms"] == "linux/amd64" and build["with"]["push"] is True
    assert build["with"]["provenance"] is False and build["with"]["sbom"] is False
    text = (WORKFLOWS / "deploy.yml").read_text()
    assert ":latest" not in text and "--force-new-deployment" not in text
    assert "ready=false" not in text and "continue-on-error" not in text


@pytest.mark.parametrize("workflow", ["ci.yml", "deploy.yml"])
def test_all_embedded_scripts_parse(workflow):
    bash = shutil.which("bash")
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if bash is None and git_bash.exists():
        bash = str(git_bash)
    for job in _load(workflow)["jobs"].values():
        for step in job.get("steps", []):
            if "run" not in step:
                continue
            if step.get("shell") == "python {0}":
                compile(step["run"], f"{workflow}:{step['name']}", "exec")
            elif bash:
                subprocess.run([bash, "--noprofile", "--norc", "-n"], input=step["run"],
                               text=True, check=True, capture_output=True)


def test_yaml_duplicate_keys_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        yaml.load("jobs:\n  deploy: {}\n  deploy: {}\n", Loader=UniqueKeyLoader)


@pytest.fixture
def workflow_env(monkeypatch, tmp_path):
    # These scripts cannot inherit a local AWS or DB credential.
    env = {
        "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": "b" * 40,
        "GITHUB_OUTPUT": str(tmp_path / "outputs"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "RUNNER_TEMP": str(tmp_path), "ROLE_ARN": "role-placeholder",
        "AWS_REGION": "us-east-2", "ECR_REPOSITORY": "image-repo",
        "REGISTRY": "123456789012.dkr.ecr.us-east-2.amazonaws.com",
        "ECS_CLUSTER": "cluster", "ECS_SERVICE": "api", "ECS_CONTAINER_NAME": "api",
        "PRODUCTION_DEPLOY_ENABLED": "true",
        "IMAGE_DIGEST": DIGEST, "PREVIOUS_TASK_DEFINITION": PREVIOUS,
        "BASELINE_DEPLOYMENT": "ecs-svc/previous", "TASK_DEFINITION": REVISION,
        "DEPLOYMENT_ID": "ecs-svc/new", "BASELINE_DESIRED": "1", "BOOTSTRAP": "false",
    }
    monkeypatch.setattr(os, "environ", env)
    (tmp_path / "deployment-configuration.json").write_text(json.dumps(_service()["deploymentConfiguration"]))
    return env


@pytest.mark.parametrize("event,ref,repo,ok", [
    ("push", "refs/heads/main", REPOSITORY, True),
    ("workflow_dispatch", "refs/heads/main", REPOSITORY, True),
    ("workflow_dispatch", "refs/heads/dev", REPOSITORY, False),
    ("workflow_dispatch", "refs/tags/main", REPOSITORY, False),
    ("pull_request", "refs/heads/main", REPOSITORY, False),
    ("push", "refs/heads/main", "someone/fork", False),
])
def test_deployment_branch_guard(workflow_env, event, ref, repo, ok):
    workflow_env.update(GITHUB_EVENT_NAME=event, GITHUB_REF=ref, GITHUB_REPOSITORY=repo)
    step = _step("Require this repository and main", job="branch")
    if ok:
        _execute(step)
    else:
        with pytest.raises(SystemExit, match="restricted"):
            _execute(step)


@pytest.mark.parametrize("missing", [
    None, "ROLE_ARN", "AWS_REGION", "ECR_REPOSITORY", "ECS_CLUSTER",
    "ECS_SERVICE", "ECS_CONTAINER_NAME", "PRODUCTION_DEPLOY_ENABLED",
])
def test_missing_config_or_closed_release_gate_fails(workflow_env, missing, capsys):
    if missing:
        workflow_env[missing] = "  "
        with pytest.raises(SystemExit) as error:
            _execute(_step("Check deploy configuration"))
        assert "role-placeholder" not in str(error.value)
    else:
        _execute(_step("Check deploy configuration"))
    assert not capsys.readouterr().out


@pytest.mark.parametrize("unit,postgres,image,ref,ok", [
    ("success", "success", "success", "dev", True),
    ("success", "success", "skipped", "main", True),
    ("success", "skipped", "success", "dev", False),
    ("failure", "success", "success", "dev", False),
    ("success", "success", "failure", "dev", False),
    ("success", "success", "skipped", "dev", False),
    ("cancelled", "success", "skipped", "main", False),
])
def test_required_gate_cannot_hide_failed_or_skipped_tests(workflow_env, unit, postgres, image, ref, ok):
    workflow_env.update(UNIT_RESULT=unit, POSTGRES_RESULT=postgres, IMAGE_RESULT=image,
                        GITHUB_REF=f"refs/heads/{ref}")
    step = _step("Require all checks", job="test", workflow="ci.yml")
    if ok:
        _execute(step)
    else:
        with pytest.raises(SystemExit, match="Required CI"):
            _execute(step)


def _service(revision=REVISION, deployment="ecs-svc/new"):
    return {
        "status": "ACTIVE", "taskDefinition": revision,
        "desiredCount": 1, "runningCount": 1, "pendingCount": 0,
        "deploymentController": {"type": "ECS"},
        "deploymentConfiguration": {"deploymentCircuitBreaker": {"enable": True, "rollback": True}},
        "deployments": [{"id": deployment, "taskDefinition": revision,
                         "status": "PRIMARY", "rolloutState": "COMPLETED",
                         "desiredCount": 1, "runningCount": 1, "pendingCount": 0}],
    }


def _definition():
    return {
        "taskDefinitionArn": PREVIOUS, "revision": 1, "status": "ACTIVE",
        "registeredAt": "yesterday", "registeredBy": "role", "compatibilities": ["FARGATE"],
        "requiresAttributes": [], "family": "api", "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"], "cpu": "512", "memory": "1024",
        "executionRoleArn": "execution-role", "taskRoleArn": "task-role",
        "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
        "containerDefinitions": [
            {"name": "api", "image": "old-image", "essential": True,
             "portMappings": [{"containerPort": 8000}],
             "environment": [{"name": "POSTGRES_SSLMODE", "value": "require"}],
             "secrets": [{"name": "POSTGRES_" + key, "valueFrom": "ssm-parameter-arn"}
                         for key in ("HOST", "DB", "USER", "PASSWORD")],
             "healthCheck": {"command": ["CMD", "python", "-V"]}},
            {"name": "sidecar", "image": "keep-this-digest", "essential": False},
        ], "volumes": [{"name": "retain-volume"}],
    }


def _mock_aws(monkeypatch, responses):
    calls = []

    def check_output(args, **kwargs):
        assert args[0] == "aws" and args[2] in responses
        calls.append(args)
        response = responses[args[2]]
        return json.dumps(response(args) if callable(response) else response)

    def run(args, **kwargs):
        assert args[:4] == ["aws", "ecs", "wait", "services-stable"]
        assert kwargs["check"] is True
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_registers_digest_revision_preserving_runtime_configuration(workflow_env, monkeypatch):
    original = _definition()
    calls = _mock_aws(monkeypatch, {
        "describe-services": {"services": [_service(PREVIOUS, "ecs-svc/previous")]},
        "describe-task-definition": {"taskDefinition": original, "tags": [{"key": "team", "value": "mqs"}]},
        "register-task-definition": {"taskDefinition": {"taskDefinitionArn": REVISION}},
        "update-service": {"service": _service()},
    })
    _execute(_step("Validate service and preserve task configuration"))
    _execute(_step("Register explicit revision and deploy"))
    registered = json.loads(Path(workflow_env["RUNNER_TEMP"], "task-definition.json").read_text())
    expected = copy.deepcopy(original)
    for key in ("taskDefinitionArn", "revision", "status", "registeredAt", "registeredBy",
                "compatibilities", "requiresAttributes"):
        expected.pop(key)
    expected["containerDefinitions"][0]["image"] = (
        f"{workflow_env['REGISTRY']}/{workflow_env['ECR_REPOSITORY']}@{DIGEST}"
    )
    expected["tags"] = [{"key": "team", "value": "mqs"}]
    assert registered == expected
    update = next(c for c in calls if c[2] == "update-service")
    assert update[update.index("--task-definition") + 1] == REVISION
    assert "--desired-count" not in update and "--deployment-configuration" not in update
    assert calls.index(next(c for c in calls if c[2] == "register-task-definition")) < calls.index(update)
    output = Path(workflow_env["GITHUB_OUTPUT"]).read_text()
    assert f"previous={PREVIOUS}" in output and f"task-definition={REVISION}" in output


@pytest.mark.parametrize("change", [
    "zero", "rollback-disabled", "in-progress", "arm", "wrong-container",
    "missing-db", "missing-password", "insecure-ssl", "s3-without-role", "s3-without-bucket",
])
def test_preflight_rejects_broken_configuration_before_mutation(workflow_env, monkeypatch, change):
    service, definition = _service(PREVIOUS, "ecs-svc/previous"), _definition()
    container = definition["containerDefinitions"][0]
    if change == "zero":
        service.update(desiredCount=0, runningCount=0)
    elif change == "rollback-disabled":
        service["deploymentConfiguration"]["deploymentCircuitBreaker"]["rollback"] = False
    elif change == "in-progress":
        service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
    elif change == "arm":
        definition["runtimePlatform"]["cpuArchitecture"] = "ARM64"
    elif change == "wrong-container":
        workflow_env["ECS_CONTAINER_NAME"] = "missing"
    elif change == "missing-db":
        container.update(environment=[], secrets=None)
    elif change == "missing-password":
        container["secrets"] = container["secrets"][:-1]
    elif change == "insecure-ssl":
        container["environment"][0]["value"] = "prefer"
    else:
        container["environment"].append({"name": "STRATEGY_STORE_BACKEND", "value": "s3"})
        if change == "s3-without-role":
            definition["taskRoleArn"] = None
    calls = _mock_aws(monkeypatch, {
        "describe-services": {"services": [service]},
        "describe-task-definition": {"taskDefinition": definition},
    })
    with pytest.raises(SystemExit):
        _execute(_step("Validate service and preserve task configuration"))
    assert not any(c[2] in {"register-task-definition", "update-service"} for c in calls)


def test_s3_resources_can_be_rendered_from_parent_variables(workflow_env, monkeypatch):
    definition = _definition()
    definition["taskRoleArn"] = None
    workflow_env.update(ECS_TASK_ROLE_ARN="arn:aws:iam::123456789012:role/btv-task",
                        STRATEGY_STORE_S3_BUCKET="btv-strategies", STRATEGY_STORE_S3_PREFIX="strategies")
    _mock_aws(monkeypatch, {
        "describe-services": {"services": [_service(PREVIOUS, "ecs-svc/previous")]},
        "describe-task-definition": {"taskDefinition": definition},
    })
    _execute(_step("Validate service and preserve task configuration"))
    rendered = json.loads(Path(workflow_env["RUNNER_TEMP"], "task-definition.json").read_text())
    assert rendered["taskRoleArn"] == workflow_env["ECS_TASK_ROLE_ARN"]
    env = {e["name"]: e["value"] for e in rendered["containerDefinitions"][0]["environment"]}
    assert env["STRATEGY_STORE_BACKEND"] == "s3" and env["STRATEGY_STORE_S3_BUCKET"] == "btv-strategies"
    assert env["STRATEGY_STORE_S3_PREFIX"] == "strategies"


def test_service_changed_during_build_is_not_overwritten(workflow_env, monkeypatch):
    Path(workflow_env["RUNNER_TEMP"], "task-definition.json").write_text(json.dumps(_definition()))
    calls = _mock_aws(monkeypatch, {"describe-services": {"services": [_service()]}})
    with pytest.raises(SystemExit, match="changed during build"):
        _execute(_step("Register explicit revision and deploy"))
    assert len(calls) == 1


@pytest.mark.parametrize("scenario", [
    "correct", "rollback", "failed", "zero", "wrong-deployment", "old-task-revision",
    "wrong-digest", "missing-digest", "missing-container", "stopped-container",
    "unhealthy", "missing-tasks", "describe-failure", "missing-service",
])
def test_rollout_verification_rejects_rollback_and_wrong_image(workflow_env, monkeypatch, scenario):
    service = _service()
    task = {
        "taskArn": "task-1", "taskDefinitionArn": REVISION,
        "lastStatus": "RUNNING", "desiredStatus": "RUNNING",
        "group": "service:api", "startedBy": "ecs-svc/new",
        "containers": [{"name": "api", "lastStatus": "RUNNING", "imageDigest": DIGEST}],
    }
    responses = {
        "describe-services": {"services": [service]}, "list-tasks": {"taskArns": ["task-1"]},
        "describe-tasks": {"tasks": [task]},
    }
    if scenario == "rollback":
        responses["describe-services"]["services"] = [_service(PREVIOUS, "ecs-svc/previous")]
    elif scenario == "failed":
        service["deployments"][0]["rolloutState"] = "FAILED"
    elif scenario == "zero":
        service.update(desiredCount=0, runningCount=0)
    elif scenario == "wrong-deployment":
        service["deployments"][0]["id"] = "ecs-svc/other"
    elif scenario == "old-task-revision":
        task["taskDefinitionArn"] = PREVIOUS
    elif scenario == "wrong-digest":
        task["containers"][0]["imageDigest"] = "sha256:" + "c" * 64
    elif scenario == "missing-digest":
        task["containers"][0].pop("imageDigest")
    elif scenario == "missing-container":
        task["containers"] = []
    elif scenario == "stopped-container":
        task["containers"][0]["lastStatus"] = "STOPPED"
    elif scenario == "unhealthy":
        task["healthStatus"] = "UNHEALTHY"
    elif scenario == "missing-tasks":
        responses["list-tasks"]["taskArns"] = []
    elif scenario == "describe-failure":
        responses["describe-tasks"]["failures"] = [{"reason": "MISSING"}]
    elif scenario == "missing-service":
        responses["describe-services"] = {"services": [], "failures": [{"reason": "MISSING"}]}
    calls = _mock_aws(monkeypatch, responses)
    if scenario == "correct":
        _execute(_step("Verify intended revision and running digest"))
        assert "Verified deployment" in Path(workflow_env["GITHUB_STEP_SUMMARY"]).read_text()
    else:
        with pytest.raises(SystemExit):
            _execute(_step("Verify intended revision and running digest"))
        assert not Path(workflow_env["GITHUB_STEP_SUMMARY"]).exists()
    assert calls[0][:4] == ["aws", "ecs", "wait", "services-stable"]


@pytest.mark.parametrize("xml,ok", [
    ('<testsuites><testsuite><testcase name="pipeline"/></testsuite></testsuites>', True),
    ('<testsuites><testsuite tests="0"/></testsuites>', False),
    ('<testsuite><testcase><skipped/></testcase></testsuite>', False),
    ('<testsuite><testcase><failure/></testcase></testsuite>', False),
    ('<testsuite><testcase><error/></testcase></testsuite>', False),
])
def test_postgres_report_requires_executed_passing_tests(tmp_path, xml, ok):
    report = tmp_path / "report.xml"
    report.write_text(xml)
    if ok:
        assert check_report(report) == 1
    else:
        with pytest.raises(ValueError):
            check_report(report)


def _bootstrap(env):
    env.update(BOOTSTRAP="true", GITHUB_EVENT_NAME="workflow_dispatch", BASELINE_DESIRED="0")
    service = _service(PREVIOUS, "ecs-svc/previous")
    service.update(desiredCount=0, runningCount=0)
    service["deployments"][0].update(desiredCount=0, runningCount=0)
    configuration = service["deploymentConfiguration"]
    configuration.update(maximumPercent=200, minimumHealthyPercent=100, strategy="ROLLING", bakeTimeInMinutes=0)
    configuration["deploymentCircuitBreaker"].update(resetOnHealthyTask=False, thresholdConfiguration={"type": "BOUNDED_PERCENT", "value": 50})
    Path(env["RUNNER_TEMP"], "deployment-configuration.json").write_text(json.dumps(configuration))
    temporary = copy.deepcopy(configuration)
    temporary["deploymentCircuitBreaker"]["rollback"] = False
    Path(env["RUNNER_TEMP"], "bootstrap-deployment-configuration.json").write_text(json.dumps(temporary))
    return service, configuration, temporary


def _healthy_task():
    return {"taskArn": "task-1", "taskDefinitionArn": REVISION, "lastStatus": "RUNNING",
            "desiredStatus": "RUNNING", "group": "service:api", "startedBy": "ecs-svc/new",
            "healthStatus": "HEALTHY", "containers": [{"name": "api", "lastStatus": "RUNNING",
            "healthStatus": "HEALTHY", "imageDigest": DIGEST}]}


def test_bootstrap_is_explicit_manual_and_keeps_the_same_ci():
    workflow = _load("deploy.yml")
    bootstrap = _triggers(workflow)["workflow_dispatch"]["inputs"]["bootstrap"]
    assert bootstrap["type"] == "boolean" and bootstrap["default"] is False
    assert workflow["jobs"]["deploy"]["needs"] == "test"
    assert "github.event_name == 'workflow_dispatch'" in workflow["jobs"]["deploy"]["env"]["BOOTSTRAP"]
    cleanup = _step("Restore stopped service after failed bootstrap")["if"]
    assert "failure()" in cleanup and "inputs.bootstrap" in cleanup
    assert "steps.revision.outputs.task-definition" in cleanup  # Also handles a lost update response.


@pytest.mark.parametrize("bad", [None, "push", "dev", "fork", "positive", "pending", "deployment-count", "no-healthcheck", "alarm-rollback", "insecure-tls", "missing-password"])
def test_bootstrap_preflight_only_admits_prepared_stopped_service(workflow_env, monkeypatch, bad):
    service, _, _ = _bootstrap(workflow_env)
    definition = _definition()
    if bad in {"push", "dev", "fork"}:
        key, value = {"push": ("GITHUB_EVENT_NAME", "push"), "dev": ("GITHUB_REF", "refs/heads/dev"), "fork": ("GITHUB_REPOSITORY", "someone/fork")}[bad]
        workflow_env[key] = value
    elif bad == "positive":
        service.update(desiredCount=1, runningCount=1)
    elif bad == "pending":
        service["pendingCount"] = 1
    elif bad == "deployment-count":
        service["deployments"][0]["runningCount"] = 1
    elif bad == "no-healthcheck":
        definition["containerDefinitions"][0].pop("healthCheck")
    elif bad == "alarm-rollback":
        service["deploymentConfiguration"]["alarms"] = {"enable": True, "rollback": True, "alarmNames": ["alarm"]}
    elif bad == "insecure-tls":
        definition["containerDefinitions"][0]["environment"][0]["value"] = "prefer"
    elif bad == "missing-password":
        definition["containerDefinitions"][0]["secrets"] = definition["containerDefinitions"][0]["secrets"][:-1]
    calls = _mock_aws(monkeypatch, {"describe-services": {"services": [service]}, "describe-task-definition": {"taskDefinition": definition}})
    if bad:
        with pytest.raises(SystemExit):
            _execute(_step("Validate service and preserve task configuration"))
    else:
        _execute(_step("Validate service and preserve task configuration"))
        assert "desired=0" in Path(workflow_env["GITHUB_OUTPUT"]).read_text()
    assert not any(c[2] in {"register-task-definition", "update-service"} for c in calls)


@pytest.mark.parametrize("drift", [None, "desired", "running", "pending", "deployment", "deployment-count", "configuration"])
def test_bootstrap_registers_one_digest_task_without_scaffold_rollback(workflow_env, monkeypatch, drift):
    service, original, temporary = _bootstrap(workflow_env)
    responses = {"describe-services": {"services": [service]}, "describe-task-definition": {"taskDefinition": _definition()},
                 "register-task-definition": {"taskDefinition": {"taskDefinitionArn": REVISION}}}
    calls = _mock_aws(monkeypatch, responses)
    _execute(_step("Validate service and preserve task configuration"))
    if drift in {"desired", "running", "pending"}:
        service[drift + "Count"] = 1
    elif drift == "deployment":
        service["deployments"][0]["id"] = "ecs-svc/other"
    elif drift == "deployment-count":
        service["deployments"][0]["pendingCount"] = 1
    elif drift == "configuration":
        service["deploymentConfiguration"]["maximumPercent"] = 150
    updated = _service()
    updated["deploymentConfiguration"] = temporary
    responses["update-service"] = {"service": updated}
    if drift:
        with pytest.raises(SystemExit, match="changed during build"):
            _execute(_step("Register explicit revision and deploy"))
        assert not any(c[2] in {"register-task-definition", "update-service"} for c in calls)
    else:
        _execute(_step("Register explicit revision and deploy"))
        update = next(c for c in calls if c[2] == "update-service")
        assert update[update.index("--desired-count") + 1] == "1"
        sent = json.loads(Path(update[update.index("--deployment-configuration") + 1].removeprefix("file://")).read_text())
        assert sent == temporary and original["deploymentCircuitBreaker"]["rollback"] is True
        rendered = json.loads(Path(workflow_env["RUNNER_TEMP"], "task-definition.json").read_text())
        assert rendered["containerDefinitions"][0]["image"].endswith("@" + DIGEST)


@pytest.mark.parametrize("bad", [None, "task-health", "container-health", "digest", "deployment", "configuration"])
def test_bootstrap_restores_full_rollback_configuration_only_after_owned_health(workflow_env, monkeypatch, bad):
    _, original, temporary = _bootstrap(workflow_env)
    service, task = _service(), _healthy_task()
    service["deploymentConfiguration"] = copy.deepcopy(temporary)
    if bad == "task-health":
        task["healthStatus"] = "UNKNOWN"
    elif bad == "container-health":
        task["containers"][0]["healthStatus"] = "UNHEALTHY"
    elif bad == "digest":
        task["containers"][0]["imageDigest"] = "wrong"
    elif bad == "deployment":
        service["deployments"][0]["id"] = "ecs-svc/other"
    elif bad == "configuration":
        service["deploymentConfiguration"]["maximumPercent"] = 150
    responses = {"describe-services": {"services": [service]}, "list-tasks": {"taskArns": ["task-1"]}, "describe-tasks": {"tasks": [task]}}
    def update(args):
        assert "--task-definition" not in args and "--desired-count" not in args
        sent = json.loads(Path(args[args.index("--deployment-configuration") + 1].removeprefix("file://")).read_text())
        assert sent == original
        service["deploymentConfiguration"] = sent
        return {"service": service}
    responses["update-service"] = update
    calls = _mock_aws(monkeypatch, responses)
    if bad:
        with pytest.raises(SystemExit):
            _execute(_step("Restore rollback after healthy bootstrap"))
        assert not any(c[2] == "update-service" for c in calls)
    else:
        _execute(_step("Restore rollback after healthy bootstrap"))
        _execute(_step("Verify intended revision and running digest"))
        task["healthStatus"] = "UNKNOWN"
        with pytest.raises(SystemExit):
            _execute(_step("Verify intended revision and running digest"))


@pytest.mark.parametrize("scenario", ["owned", "lost-response", "restored-rollback", "unchanged", "other-revision", "other-deployment", "other-configuration", "other-count"])
def test_failed_bootstrap_only_restores_its_owned_stopped_baseline(workflow_env, monkeypatch, scenario):
    stopped, original, temporary = _bootstrap(workflow_env)
    service = _service()
    service["deployments"][0]["rolloutState"] = "FAILED"
    service["deploymentConfiguration"] = copy.deepcopy(temporary)
    if scenario == "lost-response":
        workflow_env["DEPLOYMENT_ID"] = ""
    elif scenario == "restored-rollback":
        service["deploymentConfiguration"] = original
    elif scenario == "unchanged":
        service = stopped
    elif scenario == "other-revision":
        service["taskDefinition"] = PREVIOUS
    elif scenario == "other-deployment":
        service["deployments"][0]["id"] = "ecs-svc/other"
    elif scenario == "other-configuration":
        service["deploymentConfiguration"]["maximumPercent"] = 150
    elif scenario == "other-count":
        service["desiredCount"] = 2
    responses = {"describe-services": {"services": [service]}}
    def update(args):
        assert args[args.index("--task-definition") + 1] == PREVIOUS
        assert args[args.index("--desired-count") + 1] == "0"
        sent = json.loads(Path(args[args.index("--deployment-configuration") + 1].removeprefix("file://")).read_text())
        assert sent == original
        responses["describe-services"] = {"services": [stopped]}
        return {"service": stopped}
    responses["update-service"] = update
    calls = _mock_aws(monkeypatch, responses)
    if scenario.startswith("other-") or scenario == "unchanged":
        with pytest.raises(SystemExit) as error:
            _execute(_step("Restore stopped service after failed bootstrap"))
        assert (error.value.code == 0) == (scenario == "unchanged")
        assert not any(c[2] == "update-service" for c in calls)
    else:
        _execute(_step("Restore stopped service after failed bootstrap"))
        assert sum(c[2] == "update-service" for c in calls) == 1
