import subprocess
import zipfile
from pathlib import Path

import pytest

from hackathon_competitor.distribution import build_public_bundle, validate_public_bundle


def _commit_distribution_fixture(root: Path) -> None:
    files = {
        ".gitattributes": ".knightwatch export-ignore\n",
        ".knightwatch/siblings": "private review metadata\n",
        "Dockerfile": "FROM scratch\n",
        "LICENSE": "MIT License\n",
        "README.md": "Docker Compose docker build plow-credentials AGENT_ID\n",
        "compose.yml": "services: {}\n",
        "pyproject.toml": "[project]\nname='fixture'\nversion='0.0.0'\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Joust Test",
            "-c",
            "user.email=joust@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )


def test_public_bundle_uses_committed_tree_and_export_ignores_internal_metadata(tmp_path):
    _commit_distribution_fixture(tmp_path)
    (tmp_path / "plow-credentials").write_text("secret", encoding="utf-8")

    summary = build_public_bundle(tmp_path, tmp_path / "dist/joust.zip")

    assert summary["files"] == 6
    assert len(summary["sha256"]) == 64
    with zipfile.ZipFile(summary["path"]) as archive:
        assert "joust/plow-credentials" not in archive.namelist()
        assert "joust/.knightwatch/siblings" not in archive.namelist()


def test_public_bundle_rejects_secret_bearing_path(tmp_path):
    bundle = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        for name in (
            "Dockerfile",
            "LICENSE",
            "README.md",
            "compose.yml",
            "pyproject.toml",
        ):
            content = (
                "MIT License\n"
                if name == "LICENSE"
                else "Docker Compose docker build plow-credentials AGENT_ID\n"
            )
            archive.writestr(f"joust/{name}", content)
        archive.writestr("joust/plow-credentials", "secret")

    with pytest.raises(ValueError, match="forbidden paths"):
        validate_public_bundle(bundle)


def test_this_repo_builds_its_own_public_bundle(tmp_path):
    """The reporter is the base image's now, so nothing here requires a client pin."""
    root = Path(__file__).resolve().parents[1]
    assert build_public_bundle(root, tmp_path / "joust.zip")["files"] > 0
