from pathlib import Path

from src.db import init_db, log_cycle_step, recent_cycles, save_cycle
from src.execution.executor import CycleRecord, CycleState


def test_init_and_save_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    init_db()
    r = CycleRecord(id="t1", direction="base_to_solana", size_vchf=10, state=CycleState.DONE)
    save_cycle(r)
    log_cycle_step("t1", "test", {"ok": True})
    rows = recent_cycles()
    assert len(rows) >= 1
    assert rows[0]["id"] == "t1"


def test_no_secrets_in_gitignore():
    root = Path(__file__).resolve().parents[1]
    gi = (root / ".gitignore").read_text()
    assert ".env" in gi


def test_dockerfile_exists():
    root = Path(__file__).resolve().parents[1]
    assert (root / "Dockerfile").exists()
