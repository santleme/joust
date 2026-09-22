import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_variant_uses_immutable_official_base_and_does_not_vendor_runtime():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(
        r"^FROM public\.ecr\.aws/.+:base-[0-9a-f]{40}@sha256:[0-9a-f]{64}$",
        dockerfile,
        re.MULTILINE,
    )
    assert "base-67021a7029e33e80bcb27899be6515a5a0e9b37b" in dockerfile
    assert "sha256:0c3892e93c1a001c61fb7106396e0a4b7e0219008184fd90719caa84a3390ff0" in dockerfile
    assert not (ROOT / "image/s6-overlay/scripts/plow-init.py").exists()
    assert not (ROOT / "image/seed/SOUL.md").exists()
    assert (ROOT / "runtime/persona.md").is_file()
    assert "HERMES_HOME_MODE=3770" in dockerfile
    assert "find /opt/joust -type d -exec chmod 0755" in dockerfile
    assert "gh=2.46.0-3" in dockerfile


def test_linux_control_files_stay_lf_in_windows_clones():
    attributes = (ROOT / ".gitattributes").read_text()
    assert "Dockerfile text eol=lf" in attributes
    assert "vendor/*.pin text eol=lf" in attributes
    assert "image/s6-overlay/** text eol=lf" in attributes


def test_variant_persona_owns_the_public_agent_identity():
    persona = (ROOT / "runtime/persona.md").read_text()
    assert "Your public name is Joust" in persona
    assert "Never introduce yourself by that label" in persona


def test_variant_persona_has_model_driven_first_contact_onboarding():
    persona = (ROOT / "runtime/persona.md").read_text()
    assert "## First contact and onboarding" in persona
    assert "Match the user's language and tone" in persona
    assert "invite the user to share a" in persona
    assert "not a keyword-triggered script or a deterministic branch" in persona


def test_usage_reporter_comes_from_the_base_image():
    # The base ships the agent-index service and client; a copy here shadows it.
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "vendor/client.pin" not in dockerfile
    assert not (ROOT / "vendor/client.pin").exists()
    assert not (ROOT / "image/s6-overlay/s6-rc.d/agent-index").exists()
    assert not (ROOT / "image/s6-overlay/s6-rc.d/user/contents.d/agent-index").exists()


def test_secret_bearing_paths_are_excluded_from_git_and_build_context():
    for filename in (".gitignore", ".dockerignore"):
        text = (ROOT / filename).read_text()
        assert "plow-credentials" in text
        assert ".env" in text
    tracked_text = "\n".join(
        path.read_text(errors="ignore")
        for path in ROOT.rglob("*")
        if path.is_file()
        and not {".git", ".venv", "__pycache__", ".pytest_cache"}.intersection(path.parts)
        # The real local credential is intentionally present beside the
        # checkout while the container is running.  It is ignored by Git and
        # excluded from the Docker context; do not read it as source text in
        # this repository-wide secret scan.
        and path.name not in {"plow-credentials", ".env"}
    )
    assert not re.search(r"(?:sk|aik|plow)_[A-Za-z0-9_-]{24,}", tracked_text)


def test_provisioning_owns_agent_id_and_github_auth_is_volume_scoped():
    compose = (ROOT / "compose.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "AGENT_ID: ${AGENT_ID:?" in compose
    assert "env_file: ${PLOW_CREDENTIALS:" in compose
    assert "GH_CONFIG_DIR: /var/lib/hermes/.config/gh" in compose
    assert "agent-home:/var/lib/hermes" in compose
    assert "restart: unless-stopped" in compose
    assert "credentials.host" not in compose
    assert "COPY .env" not in dockerfile
    assert "COPY plow-credentials" not in dockerfile
    assert "hosts.yml" not in dockerfile


def test_docs_distinguish_self_hosted_credentials_from_hosted_identity():
    readme = (ROOT / "README.md").read_text()
    assert "https://github.com/santleme/joust.git" in readme
    assert "https://github.com/baskpascal/joust.git" not in readme
    assert "env_file" in readme
    assert "PLOW_API_BASE" in readme
    assert "mounts the Plow credential at runtime" not in readme


def test_one_click_wrapper_delegates_to_current_official_cli_without_secrets():
    wrapper = (ROOT / "scripts/deploy.ps1").read_text()
    assert "plow-agents" in wrapper
    assert '"image", "build"' in wrapper
    assert '"image", "push"' in wrapper
    assert '"deploy"' in wrapper
    assert "sha256:[0-9a-f]{64}" in wrapper
    assert "--local" in wrapper
    assert "PLOW_AGENT_TOKEN" not in wrapper
    assert "PLOW_API_BASE" not in wrapper
