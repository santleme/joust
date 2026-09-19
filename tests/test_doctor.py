from pathlib import Path

from hackathon_competitor.cli import _runtime_marker_present, credential_check, doctor


def test_doctor_reports_required_runtime_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ID", "joust")
    monkeypatch.setenv("PLOW_MCP_URL", "https://relay.invalid/mcp")
    checks, _healthy = doctor(tmp_path)
    # Every check this process actually controls. Overall health additionally
    # depends on the host's credential file, which is deliberately covered by
    # its own test rather than asserted through whatever the machine happens
    # to have on disk. agent_index_service comes from the base image; outside
    # it (no s6 tree) the check must not block.
    for name in (
        "state_directory",
        "workspace",
        "database",
        "git",
        "skills",
        "plow_tools",
        "agent_id",
        "agent_index_service",
    ):
        assert checks[name]["ok"], (name, checks[name])


def test_absent_credentials_are_healthy_and_loose_modes_are_not(tmp_path):
    missing = tmp_path / "plow-credentials"
    assert credential_check([missing]) == {"ok": True, "present": False}

    missing.write_text("token")
    missing.chmod(0o600)
    assert credential_check([missing])["ok"]

    missing.chmod(0o644)
    check = credential_check([missing])
    assert not check["ok"]
    assert check["mode"] == "0o644"


def test_doctor_fails_closed_without_agent_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.setenv("PLOW_MCP_URL", "https://relay.invalid/mcp")
    checks, healthy = doctor(tmp_path)
    assert not healthy
    assert not checks["agent_id"]["ok"]


def test_runtime_marker_requires_a_nonempty_regular_file(tmp_path):
    marker = tmp_path / "PLOW_MCP_URL"
    assert not _runtime_marker_present(marker)
    marker.write_text("")
    assert not _runtime_marker_present(marker)
    marker.write_text("configured")
    assert _runtime_marker_present(marker)
    assert not _runtime_marker_present(Path(tmp_path))


def test_configured_credential_path_is_inspected_first(tmp_path):
    from hackathon_competitor.cli import credential_candidates

    configured = tmp_path / "elsewhere" / "plow-credentials"
    candidates = credential_candidates(
        tmp_path, environment={"PLOW_CREDENTIALS_PATH": str(configured)}
    )
    assert candidates[0] == configured
    assert candidates[1] == tmp_path / "plow-credentials"

    # An unset or blank variable must not inject a bogus first candidate.
    assert credential_candidates(tmp_path, environment={})[0] == tmp_path / "plow-credentials"
    assert (
        credential_candidates(tmp_path, environment={"PLOW_CREDENTIALS_PATH": "  "})[0]
        == tmp_path / "plow-credentials"
    )
    assert credential_candidates(
        tmp_path,
        environment={
            "PLOW_CREDENTIALS": "./compose-credential",
            "PLOW_CREDENTIALS_PATH": "./path-credential",
        },
    )[0] == Path("compose-credential")
