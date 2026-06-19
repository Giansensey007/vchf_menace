from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import httpx

from src.config_loader import load_bridge_config
from src.quotes.rate_limit import get_with_retry

logger = logging.getLogger(__name__)

CCTP_POLL_SEC = float(os.getenv("CCTP_IRIS_POLL_SEC", "5"))
CCTP_IRIS_429_BACKOFF_SEC = float(os.getenv("CCTP_IRIS_429_BACKOFF_SEC", "300"))


def normalize_cctp_tx_hash(source_domain: int, tx_hash: str, *, ethereum_domain: int = 0) -> str:
    """Canonical burn tx id for queue dedup and Iris lookups (ETH always 0x-prefixed)."""
    tx = tx_hash.strip()
    if source_domain == ethereum_domain:
        h = tx.lower()
        if h.startswith("0x"):
            h = h[2:]
        return f"0x{h}"
    return tx


def iris_tx_variants(source_domain: int, tx_hash: str, *, ethereum_domain: int = 0) -> list[str]:
    """Hash forms to try with Iris (some burns indexed with/without 0x)."""
    tx = tx_hash.strip()
    if source_domain != ethereum_domain:
        return [tx]
    norm = normalize_cctp_tx_hash(source_domain, tx, ethereum_domain=ethereum_domain)
    bare = norm[2:]
    out: list[str] = []
    for v in (norm, bare, tx):
        if v and v not in out:
            out.append(v)
    return out


@dataclass
class CctpAttestation:
    message: str
    attestation: str
    status: str


async def fetch_messages(
    client: httpx.AsyncClient,
    source_domain: int,
    tx_hash: str,
) -> list[dict]:
    """Fetch Iris messages for a source burn tx (empty if none yet)."""
    cfg = load_bridge_config()["cctp"]
    base = cfg["iris_api"].rstrip("/")
    url = f"{base}/v2/messages/{source_domain}"
    eth_domain = int(cfg["ethereum_domain"])

    variants = iris_tx_variants(source_domain, tx_hash, ethereum_domain=eth_domain)
    for idx, variant in enumerate(variants):
        resp = await get_with_retry(
            client,
            url,
            params={"transactionHash": variant},
            timeout=30.0,
        )
        if resp.status_code == 404:
            if idx == 0 and len(variants) > 1:
                continue
            return []
        if resp.status_code >= 400:
            logger.debug("Iris fetch HTTP %s (%s): %s", resp.status_code, variant[:18], resp.text[:120])
            continue
        messages = list(resp.json().get("messages") or [])
        if messages:
            return messages
    return []


async def poll_attestation(
    client: httpx.AsyncClient,
    source_domain: int,
    tx_hash: str,
    *,
    poll_sec: float | None = None,
    timeout_sec: float | None = None,
) -> CctpAttestation | None:
    """Poll Iris v2 until attestation is ready (not PENDING)."""
    cfg = load_bridge_config()["cctp"]
    base = cfg["iris_api"].rstrip("/")
    timeout = timeout_sec or float(cfg.get("attestation_timeout_sec", 1800))
    poll = poll_sec if poll_sec is not None else CCTP_POLL_SEC
    url = f"{base}/v2/messages/{source_domain}"
    deadline = time.monotonic() + timeout
    eth_domain = int(cfg["ethereum_domain"])
    variants = iris_tx_variants(source_domain, tx_hash, ethereum_domain=eth_domain)
    variant_idx = 0

    while time.monotonic() < deadline:
        tx_param = variants[min(variant_idx, len(variants) - 1)]
        resp = await get_with_retry(
            client,
            url,
            params={"transactionHash": tx_param},
            timeout=30.0,
        )
        if resp.status_code == 429:
            logger.warning("Iris rate limited; backing off %.0fs", CCTP_IRIS_429_BACKOFF_SEC)
            await asyncio.sleep(CCTP_IRIS_429_BACKOFF_SEC)
            continue
        if resp.status_code == 404:
            await asyncio.sleep(poll)
            continue
        if resp.status_code >= 400:
            logger.debug("Iris poll HTTP %s: %s", resp.status_code, resp.text[:200])
            await asyncio.sleep(poll)
            continue

        data = resp.json()
        messages = data.get("messages") or []
        if not messages:
            if variant_idx + 1 < len(variants):
                variant_idx += 1
            await asyncio.sleep(poll)
            continue

        msg = messages[0]
        att = msg.get("attestation") or ""
        if att and att != "PENDING":
            return CctpAttestation(
                message=str(msg.get("message") or ""),
                attestation=str(att),
                status=str(msg.get("status") or "complete"),
            )

        await asyncio.sleep(poll)

    logger.error("CCTP attestation timeout for tx %s (domain %s)", tx_hash, source_domain)
    return None
