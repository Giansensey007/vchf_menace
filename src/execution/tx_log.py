from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config_loader import data_dir

logger = logging.getLogger(__name__)


def tx_log_path() -> Path:
    return data_dir() / "tx_log.jsonl"

class _LazyTxLogPath:
    def _path(self) -> Path:
        return tx_log_path()
    def exists(self) -> bool:
        return self._path().exists()
    def write_text(self, *a, **k):
        return self._path().write_text(*a, **k)
    def read_text(self, *a, **k):
        return self._path().read_text(*a, **k)
    def open(self, *a, **k):
        return self._path().open(*a, **k)
    def __str__(self) -> str:
        return str(self._path())
    def __repr__(self) -> str:
        return repr(self._path())
TX_LOG_PATH = _LazyTxLogPath()

EXPLORERS: dict[str, str] = {
    "base": "https://basescan.org/tx/{tx}",
    "celo": "https://basescan.io/tx/{tx}",
    "ethereum": "https://etherscan.io/tx/{tx}",
    "eth": "https://etherscan.io/tx/{tx}",
    "solana": "https://solscan.io/tx/{tx}",
    "sol": "https://solscan.io/tx/{tx}",
}


@dataclass
class TxRecord:
    intent: str
    chain: str
    tx_hash: str
    ok: bool = True
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.url and self.tx_hash and not self.tx_hash.startswith("dry-run"):
            tpl = EXPLORERS.get(self.chain.lower())
            if tpl:
                self.url = tpl.format(tx=self.tx_hash)
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat()


def tx_url(chain: str, tx_hash: str) -> str:
    if not tx_hash or tx_hash.startswith("dry-run"):
        return ""
    tpl = EXPLORERS.get(chain.lower())
    return tpl.format(tx=tx_hash) if tpl else ""


def log_tx(
    intent: str,
    chain: str,
    tx_hash: str,
    *,
    ok: bool = True,
    extra: dict[str, Any] | None = None,
) -> TxRecord:
    """Append a transaction record to data/tx_log.jsonl and emit a log line."""
    rec = TxRecord(
        intent=intent,
        chain=chain,
        tx_hash=tx_hash,
        ok=ok,
        extra=extra or {},
    )
    path = tx_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(rec), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    status = "OK" if ok else "FAIL"
    url_part = f" {rec.url}" if rec.url else ""
    logger.info("TX %s [%s] %s%s intent=%s", status, chain, tx_hash, url_part, intent)
    print(f"TX {status} | {intent} | {chain} | {tx_hash}{url_part}", flush=True)
    return rec


def log_platform_order(intent: str, ordid: int | None, **extra: Any) -> TxRecord:
    return log_tx(
        intent,
        "platform",
        f"ordid:{ordid}" if ordid is not None else "ordid:unknown",
        extra={"ordid": ordid, "platform_url": "https://platform.vnx.li", **extra},
    )
