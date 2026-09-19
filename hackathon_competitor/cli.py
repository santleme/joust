from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from .agent_index import (
    AgentIndexService,
    PinnedCliAgentIndexClient,
    observe_license_spdx,
)
from .ai import ClaudeCliReasoner, UnavailableReasoner
from .ai_mission import MissionBlocked, ask, joust_it, redirect
from .build_loop import (
    ClaudeCodeImplementer,
    CommandImplementer,
    HermesImplementer,
    project_environment,
)
from .capabilities.repository_context import detect_default_branch
from .competition_actions import (
    CompetitionActionDispatcher,
    RealBuildActionExecutor,
    RealResearchActionExecutor,
)
from .competition_intelligence import CompetitionIntelligence
from .competition_runner import CompetitionIterationRunner
from .credentials import DEFAULT_RELATIVE_PATH, resolve_credential_path
from .distribution import build_public_bundle
from .exporter import export_mission_bundle
from .github import GitHubCliAdapter, GitHubConnectionObserver
from .hermes_planner import (
    HermesCompetitionPlanner,
    HermesOneShotReasoner,
    ObservationOnlyExecutor,
    ReadinessMeasurer,
)
from .identity import identity_from_environment
from .metrics import MetricsUnavailable, PlowMetricsIngestor, PlowMetricsReader
from .models import (
    CompetitionActionType,
    EntrantProfile,
    MissionState,
    MonitorOutcome,
    ProjectMode,
    ProjectTarget,
)
from .monitoring import MonitoredCompetitionRunner
from .observation import CompetitionObserver
from .orchestrator import MissionOrchestrator
from .pipeline import (
    build_project_for_mission,
    mission_status,
    prepare_project_submission,
)
from .rule_updates import refresh_official_rules
from .storage import MIGRATIONS, Database


def default_home() -> Path:
    configured = os.environ.get("HACKATHON_COMPETITOR_HOME")
    if configured:
        return Path(configured)
    runtime_home = Path("/var/lib/hermes")
    if runtime_home.is_dir():
        return runtime_home / "hackathon_competitor"
    return Path.home() / ".joust"


def runtime(home: Path | None = None) -> MissionOrchestrator:
    root = (home or default_home()).resolve()
    database = Database(root / "state.db")
    database.migrate()
    identity = identity_from_environment()
    if identity is not None:
        database.bind_agent_identity(identity)
    return MissionOrchestrator(
        database,
        root / "missions",
    )


def installation_home(home: Path | None = None) -> Path:
    """Return the persistent home that owns this installation's GitHub login."""

    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if home is not None:
        return home.expanduser().resolve()
    configured_competitor_home = os.environ.get("HACKATHON_COMPETITOR_HOME")
    if configured_competitor_home:
        return Path(configured_competitor_home).expanduser().resolve()
    runtime_home = Path("/var/lib/hermes")
    if runtime_home.is_dir():
        return runtime_home.resolve()
    return default_home().resolve()


def github_adapter(home: Path | None, project_path: str | Path) -> GitHubCliAdapter:
    """Build a GitHub adapter pinned to this installation's config directory."""

    return GitHubCliAdapter(
        str(Path(project_path).resolve()),
        installation_home=installation_home(home),
    )


def target_repository(target: ProjectTarget | None) -> str | None:
    if target is None:
        return None
    if target.repository_owner and target.repository_name:
        return f"{target.repository_owner}/{target.repository_name}"
    if target.repository_url:
        value = target.repository_url.strip().rstrip("/").removesuffix(".git")
        for prefix in ("https://github.com/", "http://github.com/"):
            if value.startswith(prefix):
                value = value.removeprefix(prefix)
        return value if "/" in value else None
    return None


def _runtime_marker_present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _parse_command_vectors(values: list[str]) -> list[list[str]]:
    commands: list[list[str]] = []
    for value in values:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"command must be a JSON argv list: {value!r}") from exc
        if (
            not isinstance(parsed, list)
            or not parsed
            or any(not isinstance(argument, str) or not argument for argument in parsed)
        ):
            raise ValueError("command must be a non-empty JSON list of non-empty strings")
        commands.append(parsed)
    return commands


def credential_candidates(
    repository_root: Path,
    environment: Mapping[str, str] | None = None,
) -> list[Path]:
    """Where the Plow token may live, most specific first.

    The first candidate is resolved the same way Compose itself would read
    it — `PLOW_CREDENTIALS`, then `PLOW_CREDENTIALS_PATH`, then those keys in
    a `.env` next to the checkout, then the documented default — so the doctor
    never has to ask an operator where a credential they already configured
    actually is.
    Without it, a checkout on a filesystem that cannot hold POSIX modes
    would keep failing this check while the token the container actually
    reads is correctly protected somewhere else.
    """

    configured = resolve_credential_path(repository_root=repository_root, environment=environment)
    default = repository_root / DEFAULT_RELATIVE_PATH
    candidates = [configured]
    if configured != default:
        candidates.append(default)
    candidates.append(Path("/var/lib/plow/credentials"))
    return candidates


def credential_check(candidates: list[Path]) -> dict[str, object]:
    """The Plow token must not be readable by anyone else on the machine."""

    credential = next((path for path in candidates if path.exists()), None)
    if credential is None:
        return {"ok": True, "present": False}
    mode = stat.S_IMODE(credential.stat().st_mode)
    return {
        "ok": os.name == "nt" or mode in {0o400, 0o600},
        "present": True,
        "mode": oct(mode),
    }


def doctor(home: Path | None = None) -> tuple[dict[str, dict[str, object]], bool]:
    root = (home or default_home()).resolve()
    checks: dict[str, dict[str, object]] = {}
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, delete=True):
            pass
        checks["state_directory"] = {"ok": True, "path": str(root)}
    except OSError as exc:
        checks["state_directory"] = {"ok": False, "error": str(exc)}

    workspace_root = root / "missions"
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=workspace_root, delete=True):
            pass
        checks["workspace"] = {"ok": True, "path": str(workspace_root)}
    except OSError as exc:
        checks["workspace"] = {"ok": False, "error": str(exc)}

    database = Database(root / "state.db")
    try:
        current = database.migrate()
        checks["database"] = {"ok": current == len(MIGRATIONS), "version": current}
    except Exception as exc:  # noqa: BLE001 - doctor must report every failed check
        checks["database"] = {"ok": False, "error": str(exc)}

    checks["git"] = {"ok": shutil.which("git") is not None}
    repo_root = Path(__file__).resolve().parents[1]
    expected_skills = [
        "hackathon-intake",
        "hackathon-research",
        "hackathon-strategy",
        "hackathon-build",
        "hackathon-red-team",
        "hackathon-submit",
        "hackathon-safety",
    ]
    skill_roots = [repo_root / "skills", Path("/opt/hermes/skills")]
    installed = {
        name: any((skill_root / name / "SKILL.md").is_file() for skill_root in skill_roots)
        for name in expected_skills
    }
    checks["skills"] = {"ok": all(installed.values()), "installed": installed}
    plow_tools_available = (
        bool(os.environ.get("PLOW_MCP_URL"))
        or shutil.which("plow-gog") is not None
        or _runtime_marker_present(Path("/run/s6/container_environment/PLOW_MCP_URL"))
    )
    checks["plow_tools"] = {
        "ok": plow_tools_available,
        "available": plow_tools_available,
    }
    configured_identity = identity_from_environment()
    bound_identity = database.get_agent_identity()
    identity_matches = configured_identity is not None and (
        bound_identity is None or bound_identity.agent_id == configured_identity.agent_id
    )
    checks["agent_id"] = {
        "ok": identity_matches,
        "present": configured_identity is not None,
        "bound": bound_identity is not None,
        "matches_bound_identity": identity_matches,
    }
    # The base image ships this service at this path, so it is required only
    # inside the image; a checkout on the host has no s6 tree to hold it.
    s6_services = Path("/etc/s6-overlay/s6-rc.d")
    if s6_services.is_dir():
        checks["agent_index_service"] = {
            "ok": (s6_services / "agent-index" / "run").is_file(),
            "available": True,
        }
    else:
        checks["agent_index_service"] = {"ok": True, "available": False}
    client = Path("/opt/plow/agent-index-client.py")
    if client.is_file():
        smoke_environment = os.environ.copy()
        hermes_home = Path(os.environ.get("HERMES_HOME") or root).resolve()
        smoke_environment["HOME"] = str(hermes_home)
        smoke_environment["HERMES_HOME"] = str(hermes_home)
        try:
            result = subprocess.run(
                [sys.executable, str(client), "status"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=smoke_environment,
            )
            checks["agent_index_client_smoke"] = {
                "ok": result.returncode in {0, 3},
                "available": True,
                "status": "registered" if result.returncode == 0 else "not_registered",
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks["agent_index_client_smoke"] = {
                "ok": False,
                "available": True,
                "error_type": type(exc).__name__,
            }
    else:
        checks["agent_index_client_smoke"] = {"ok": True, "available": False}

    checks["credentials"] = credential_check(credential_candidates(repo_root))
    healthy = all(check["ok"] for check in checks.values())
    return checks, healthy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="joust")
    parser.add_argument("--home", type=Path, help="state directory override")
    commands = parser.add_subparsers(dest="command", required=True)

    mission = commands.add_parser("mission")
    mission_commands = mission.add_subparsers(dest="mission_command", required=True)
    for name in ("show", "resume", "pause", "cancel", "tasks", "github-status"):
        sub = mission_commands.add_parser(name)
        sub.add_argument("mission_id", type=UUID)
    joust = mission_commands.add_parser("joust-it", aliases=["create"])
    joust.add_argument("--url", required=True)
    joust.add_argument(
        "--workspace",
        default=None,
        help="optional workspace override; defaults to the persistent tenant home",
    )
    joust.add_argument("--projects-root")
    joust.add_argument(
        "--existing-project-path",
        help="a real local repository this mission must evolve rather than replace",
    )
    joust.add_argument(
        "--claude",
        action="store_true",
        help="use the locally authenticated Claude Code CLI instead of Hermes",
    )
    joust.add_argument("--claude-model", default="default")
    joust.add_argument("--hermes-model")
    joust.add_argument("--hermes-reasoning")
    joust.add_argument(
        "--no-model",
        action="store_true",
        help="run the same mission with no reasoning provider, to see where it stops",
    )
    ask_parser = mission_commands.add_parser("ask")
    ask_parser.add_argument("mission_id", type=UUID)
    ask_parser.add_argument("question")
    ask_parser.add_argument("--claude-model", default="default")
    redirect_parser = mission_commands.add_parser("redirect")
    redirect_parser.add_argument("mission_id", type=UUID)
    redirect_parser.add_argument("instruction")
    redirect_parser.add_argument("--claude-model", default="default")
    redirect_parser.add_argument("--projects-root")
    export = mission_commands.add_parser("export")
    export.add_argument("mission_id", type=UUID)
    export.add_argument("--bundle", type=Path)
    refresh = mission_commands.add_parser("refresh-rules")
    refresh.add_argument("mission_id", type=UUID)
    refresh.add_argument("--url", required=True)
    entrant = mission_commands.add_parser("attach-entrant")
    entrant.add_argument("mission_id", type=UUID)
    entrant.add_argument("--display-name", required=True)
    entrant.add_argument("--attribution-name")
    entrant.add_argument("--github-identity")
    entrant.add_argument("--discord-identity")
    entrant.add_argument("--team-member", action="append", default=[])
    entrant.add_argument("--default-public-attribution")
    attach = mission_commands.add_parser("attach-project")
    attach.add_argument("mission_id", type=UUID)
    attach.add_argument("--path", required=True)
    attach.add_argument(
        "--mode",
        choices=[mode.value for mode in ProjectMode],
        default=ProjectMode.EXISTING_REPO.value,
    )
    attach.add_argument("--repo")
    attach.add_argument("--repo-owner")
    attach.add_argument("--repo-name")
    attach.add_argument(
        "--default-branch",
        default=None,
        help="defaults to the path's actual current branch when it is already a Git "
        "checkout; falls back to 'main' only when there is no existing repository to read",
    )
    attach.add_argument(
        "--install-command",
        action="append",
        default=[],
        metavar="JSON_ARGV",
        help='repeatable JSON argv, e.g. ["python","-m","pip","install","-e", "."]',
    )
    attach.add_argument("--build-command", action="append", default=[], metavar="JSON_ARGV")
    attach.add_argument("--dev-command", action="append", default=[], metavar="JSON_ARGV")
    attach.add_argument("--test-command", action="append", default=[], metavar="JSON_ARGV")
    attach.add_argument("--lint-command", action="append", default=[], metavar="JSON_ARGV")
    attach.add_argument("--run-command", action="append", default=[], metavar="JSON_ARGV")
    attach.add_argument("--deployment-requirement")
    attach.add_argument("--deploy-target")
    attach.add_argument(
        "--environment-name",
        action="append",
        default=[],
        metavar="NAME",
        help="repeatable non-sensitive host environment name approved for project commands",
    )
    build = mission_commands.add_parser("build-project")
    build.add_argument("mission_id", type=UUID)
    implementer = build.add_mutually_exclusive_group(required=True)
    implementer.add_argument("--implementation-command", metavar="JSON_ARGV")
    implementer.add_argument("--hermes", action="store_true")
    implementer.add_argument("--claude", action="store_true")
    build.add_argument("--claude-model", default="default")
    build.add_argument("--hermes-model")
    build.add_argument("--hermes-reasoning")
    build.add_argument("--spec")
    build.add_argument("--max-repairs", type=int, default=0)
    prepare = mission_commands.add_parser("prepare-project-submission")
    prepare.add_argument("mission_id", type=UUID)
    compete = mission_commands.add_parser("compete-run")
    compete.add_argument("mission_id", type=UUID)
    compete.add_argument("--hermes-model")
    compete.add_argument("--hermes-reasoning")
    compete.add_argument(
        "--claude",
        action="store_true",
        help="plan and implement with the Claude Code CLI instead of the hosted Hermes runtime",
    )
    compete.add_argument("--claude-model", default="default")
    compete.add_argument("--max-repairs", type=int, default=1)

    eligibility = mission_commands.add_parser("index-eligibility")
    eligibility.add_argument("mission_id", type=UUID)
    eligibility.add_argument("--agent", required=True)
    verification = mission_commands.add_parser("request-verification")
    verification.add_argument("mission_id", type=UUID)
    verification.add_argument("--agent", required=True)
    verification.add_argument("--contact", required=True)
    verification.add_argument("--repo-url", required=True)
    verification.add_argument("--commit", required=True)

    db = commands.add_parser("db")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate")
    commands.add_parser("doctor")
    bundle = commands.add_parser("bundle")
    bundle.add_argument("--output", type=Path, default=Path("dist/joust-public.zip"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = args.home
    if args.command == "doctor":
        checks, healthy = doctor(home)
        print(json.dumps({"healthy": healthy, "checks": checks}, indent=2))
        return 0 if healthy else 1
    if args.command == "bundle":
        print(json.dumps(build_public_bundle(Path.cwd(), args.output), indent=2))
        return 0
    app = runtime(home)
    if args.command == "db":
        print(json.dumps({"migration_version": app.database.migration_version()}))
        return 0
    if args.mission_command == "attach-project":
        # Validate the names at attachment time so a credential-shaped name
        # cannot be persisted as a future build permission.
        project_environment(args.environment_name)
        resolved_path = Path(args.path).resolve()
        default_branch = args.default_branch or detect_default_branch(resolved_path) or "main"
        target = ProjectTarget(
            mission_id=args.mission_id,
            mode=ProjectMode(args.mode),
            local_path=str(resolved_path),
            repository_url=args.repo,
            repository_owner=args.repo_owner,
            repository_name=args.repo_name,
            default_branch=default_branch,
            install_commands=_parse_command_vectors(args.install_command),
            dev_commands=_parse_command_vectors(args.dev_command),
            build_commands=_parse_command_vectors(args.build_command),
            test_commands=_parse_command_vectors(args.test_command),
            lint_commands=_parse_command_vectors(args.lint_command),
            run_commands=_parse_command_vectors(args.run_command),
            deployment_requirement=args.deployment_requirement,
            deploy_target=args.deploy_target,
            environment_allowlist=list(args.environment_name),
        )
        app.attach_project_target(target)
        print(json.dumps(target.model_dump(mode="json"), indent=2))
        return 0
    if args.mission_command in {"joust-it", "create"}:
        reasoner = (
            UnavailableReasoner()
            if args.no_model
            else (
                ClaudeCliReasoner(model=args.claude_model, workdir=Path.cwd())
                if args.claude
                else HermesOneShotReasoner(
                    args.workspace or Path.cwd(),
                    model=args.hermes_model,
                    reasoning=args.hermes_reasoning,
                )
            )
        )
        try:
            mission, selected, decision, target = joust_it(
                app,
                args.url,
                reasoner,
                workspace_path=args.workspace,
                projects_root=args.projects_root,
                existing_project_path=args.existing_project_path,
            )
        except MissionBlocked as blocked:
            print(
                json.dumps(
                    {
                        "mission_id": str(blocked.mission_id),
                        "state": "BLOCKED",
                        "boundary": blocked.code,
                        "detail": blocked.detail,
                    },
                    indent=2,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "mission_id": str(mission.id),
                    "competition": mission.title,
                    "deadline_at": (
                        mission.deadline_at.isoformat() if mission.deadline_at else None
                    ),
                    "strategies_considered": len(decision.options),
                    "selected": selected.product_thesis,
                    "target_user": selected.target_user,
                    "winning_mechanism": selected.winning_mechanism,
                    "rationale": decision.rationale,
                    "project_target_id": str(target.id),
                    "project_path": target.local_path,
                    "state": mission.state.value,
                },
                indent=2,
            )
        )
        return 0
    if args.mission_command == "ask":
        try:
            target = app.database.get_project_target_for_mission(args.mission_id)
        except KeyError:
            target = None
        adapter = github_adapter(
            home,
            target.local_path if target is not None else app.artifact_root,
        )
        github_status = adapter.connection_status(target_repository(target))
        print(
            ask(
                app,
                args.mission_id,
                args.question,
                ClaudeCliReasoner(model=args.claude_model, workdir=Path.cwd()),
                github_status=github_status,
            )
        )
        return 0
    if args.mission_command == "redirect":
        try:
            selected, ai_decision = redirect(
                app,
                args.mission_id,
                args.instruction,
                ClaudeCliReasoner(model=args.claude_model, workdir=Path.cwd()),
                projects_root=args.projects_root,
            )
        except MissionBlocked as blocked:
            print(
                json.dumps(
                    {
                        "mission_id": str(blocked.mission_id),
                        "state": "BLOCKED",
                        "boundary": blocked.code,
                        "detail": blocked.detail,
                    },
                    indent=2,
                )
            )
            return 2
        try:
            current_target = app.database.get_project_target_for_mission(args.mission_id)
            project_path = current_target.local_path
        except KeyError:
            project_path = None
        print(
            json.dumps(
                {
                    "now_pursuing": selected.product_thesis,
                    "target_user": selected.target_user,
                    "winning_mechanism": selected.winning_mechanism,
                    "because": ai_decision.rationale,
                    "ai_decision_id": str(ai_decision.id),
                    "project_path": project_path,
                },
                indent=2,
            )
        )
        return 0
    if args.mission_command == "attach-entrant":
        profile = EntrantProfile(
            display_name=args.display_name,
            attribution_name=args.attribution_name,
            github_identity=args.github_identity,
            discord_identity=args.discord_identity,
            team_members=list(args.team_member),
            default_public_attribution=args.default_public_attribution,
        )
        app.attach_entrant_profile(args.mission_id, profile)
        print(json.dumps(profile.model_dump(mode="json"), indent=2))
        return 0
    if args.mission_command == "build-project":
        if args.max_repairs < 0:
            raise ValueError("--max-repairs cannot be negative")
        target = app.database.get_project_target_for_mission(args.mission_id)
        if args.hermes:
            implementer = HermesImplementer(
                model=args.hermes_model,
                reasoning=args.hermes_reasoning,
            )
        elif args.claude:
            implementer = ClaudeCodeImplementer(model=args.claude_model)
        else:
            implementation_command = _parse_command_vectors([args.implementation_command])[0]
            implementer = CommandImplementer(implementation_command)
        specification = args.spec
        if specification and Path(specification).is_file():
            specification = Path(specification).read_text(encoding="utf-8")
        change_set = build_project_for_mission(
            app,
            args.mission_id,
            implementer,
            specification=specification,
            max_repairs=args.max_repairs,
            # Git operations (including an approval-bound push) must run
            # inside the target checkout, not its parent directory.
            github=github_adapter(home, target.local_path),
        )
        print(json.dumps(change_set.model_dump(mode="json"), indent=2))
        return 0
    if args.mission_command in {"index-eligibility", "request-verification"}:
        repository_root = Path(__file__).resolve().parents[1]
        service = AgentIndexService(
            app.database,
            client=PinnedCliAgentIndexClient(workspace=str(repository_root)),
        )
        # Both inputs are observed, never assumed: the license comes off this
        # repository and the reporting signal off the live Index. Either one
        # that cannot be read stays unknown in the report.
        try:
            active_days = PlowMetricsReader(args.agent).snapshot().active_days
        except MetricsUnavailable:
            active_days = None
        report = service.eligibility(
            args.agent,
            license_spdx=observe_license_spdx(repository_root),
            reporting_active_days=active_days,
        )
        if args.mission_command == "index-eligibility":
            print(json.dumps(report.model_dump(mode="json"), indent=2))
            return 0 if report.eligible_to_win else 1
        action = service.request_verification(
            args.mission_id,
            agent_id=args.agent,
            eligibility=report,
            contact_route=args.contact,
            repository_url=args.repo_url,
            commit_sha=args.commit,
            # Keyed on the candidate, not just the agent: re-requesting for the
            # same commit is idempotent, while a new candidate is a genuinely
            # new request. Keying on the agent alone would bind the first
            # handoff forever and refuse every later one.
            idempotency_key=f"verification:{args.agent}:{args.commit}",
        )
        # The proposal is durable and unapproved. Delivery is a separate,
        # approved step, so printing this never publishes anything.
        print(action.payload["handoff"])
        print(json.dumps({"action_id": str(action.id), "approval_id": str(action.approval_id)}))
        return 0
    if args.mission_command == "prepare-project-submission":
        prepared = prepare_project_submission(app, args.mission_id)
        print(json.dumps(mission_status(app, prepared), indent=2))
        return 0 if prepared.state == MissionState.READY_FOR_SUBMISSION else 1
    if args.mission_command == "compete-run":
        if args.max_repairs < 0:
            raise ValueError("--max-repairs cannot be negative")
        mission = app.database.get_mission(args.mission_id)
        try:
            target = app.database.get_project_target_for_mission(mission.id)
        except KeyError:
            target = None
        github = (
            github_adapter(home, target.local_path)
            if target is not None and target.repository_url
            else None
        )
        workdir = target.local_path if target is not None else mission.workspace_path
        reasoner = (
            ClaudeCliReasoner(model=args.claude_model, workdir=workdir)
            if args.claude
            else HermesOneShotReasoner(
                workdir,
                model=args.hermes_model,
                reasoning=args.hermes_reasoning,
            )
        )
        allowed = {CompetitionActionType.CUSTOM, CompetitionActionType.RESEARCH}
        executors = {
            CompetitionActionType.CUSTOM: ObservationOnlyExecutor(),
            CompetitionActionType.RESEARCH: RealResearchActionExecutor(app.database),
        }
        if target is not None:
            allowed.add(CompetitionActionType.BUILD_PROJECT)
            executors[CompetitionActionType.BUILD_PROJECT] = RealBuildActionExecutor(
                app.database,
                app.artifact_root,
                ClaudeCodeImplementer(model=args.claude_model)
                if args.claude
                else HermesImplementer(
                    model=args.hermes_model,
                    reasoning=args.hermes_reasoning,
                ),
                github=github,
            )
        metrics_ingestor = None
        agent_id = os.environ.get("AGENT_ID")
        if agent_id:
            metrics_ingestor = PlowMetricsIngestor(
                CompetitionIntelligence(app.database),
                PlowMetricsReader(agent_id),
            )
        iteration_runner = CompetitionIterationRunner(
            app.database,
            CompetitionObserver(app.database, github=github, metrics=metrics_ingestor),
            HermesCompetitionPlanner(
                app.database,
                reasoner,
                allowed_action_types=allowed,
                max_repairs=args.max_repairs,
            ),
            CompetitionActionDispatcher(app.database, executors),
            ReadinessMeasurer(),
        )
        monitor_result = MonitoredCompetitionRunner(
            app.database,
            iteration_runner,
        ).run(
            mission.id,
            holder=f"joust-cli:{os.getpid()}",
        )
        cycles = app.database.list_competition_cycles(mission.id)
        next_cycle = cycles[-1] if cycles and cycles[-1].completed_at is None else None
        print(
            json.dumps(
                {
                    "mission": str(mission.id),
                    "status": app.database.get_mission(mission.id).status.value,
                    "monitor_outcome": monitor_result.outcome.value,
                    "next_attempt_at": (
                        monitor_result.next_attempt_at.isoformat()
                        if monitor_result.next_attempt_at
                        else None
                    ),
                    "next_cycle": str(next_cycle.id) if next_cycle else None,
                    "next_stage": next_cycle.stage.value if next_cycle else None,
                },
                indent=2,
            )
        )
        return 1 if monitor_result.outcome == MonitorOutcome.FAILED else 0
    if args.mission_command == "github-status":
        try:
            target = app.database.get_project_target_for_mission(args.mission_id)
        except KeyError:
            target = None
        adapter = github_adapter(
            home,
            target.local_path if target is not None else app.artifact_root,
        )
        status = GitHubConnectionObserver(app.database, adapter).observe(
            args.mission_id,
            target_repository(target),
        )
        print(json.dumps(status.model_dump(mode="json"), indent=2))
        return 0 if status.connected else 1
    if args.mission_command == "pause":
        mission = app.lifecycle.pause(args.mission_id)
        target = app.database.get_project_target_for_mission(mission.id) if mission.project_target_id else None
        print(json.dumps({
            "mission_id": str(mission.id), "state": mission.state.value,
            "project_preserved": target is not None,
        }, indent=2))
        return 0
    if args.mission_command == "cancel":
        mission = app.lifecycle.cancel(args.mission_id)
        target = app.database.get_project_target_for_mission(mission.id) if mission.project_target_id else None
        print(json.dumps({
            "mission_id": str(mission.id), "state": mission.state.value,
            "project_preserved": target is not None,
        }, indent=2))
        return 0
    if args.mission_command == "resume":
        mission = app.resume_mission(args.mission_id)
    elif args.mission_command == "refresh-rules":
        spec = refresh_official_rules(app, args.mission_id, args.url)
        mission = app.database.get_mission(args.mission_id)
        print(
            json.dumps(
                {
                    "mission": str(mission.id),
                    "rules_version": spec.version,
                    "state": mission.state.value,
                },
                indent=2,
            )
        )
        return 0
    else:
        mission = app.database.get_mission(args.mission_id)
    if args.mission_command in {"show", "resume"}:
        print(json.dumps(mission_status(app, mission), indent=2))
    elif args.mission_command == "tasks":
        print(
            json.dumps(
                [task.model_dump(mode="json") for task in app.database.list_tasks(mission.id)],
                indent=2,
            )
        )
    elif args.mission_command == "export":
        if args.bundle:
            path = export_mission_bundle(app.database, mission.id, args.bundle)
            print(json.dumps({"mission": str(mission.id), "bundle": str(path)}, indent=2))
        else:
            print(json.dumps(app.database.export_mission(mission.id), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
