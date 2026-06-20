from pathlib import Path


def test_no_secrets_in_gitignore():
    root = Path(__file__).resolve().parents[1]
    gi = (root / ".gitignore").read_text()
    assert ".env" in gi
    example = (root / ".env.example").read_text()
    assert "YOUR_BASE_PRIVATE_KEY" in example
    assert "0x982c" not in example


def test_dockerfile_exists():
    root = Path(__file__).resolve().parents[1]
    assert (root / "Dockerfile").exists()
    assert (root / "DEPLOY.md").exists()
