from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from src.config_loader import load_bridge_config
from src.quotes.addresses import checksum
from src.quotes.sync_throttle import sync_throttle

logger = logging.getLogger(__name__)

LOG_MESSAGE_PUBLISHED_TOPIC = (
    "0x6eb224fb001ed210e379b335e35efe88672a8ce935d981a6896b27ffdf52a3b2"
)


def token_bridge_emitter(bridge_address: str) -> str:
    """Wormholescan emitter address: 32-byte hex without 0x prefix."""
    return checksum(bridge_address)[2:].lower().rjust(64, "0")


def _norm_topic(topic: str) -> str:
    return str(topic).lower().removeprefix("0x")


def parse_sequence_from_receipt(receipt: Any, core_bridge: str | None = None) -> int | None:
    """Extract Wormhole sequence from LogMessagePublished in a tx receipt."""
    topic = _norm_topic(LOG_MESSAGE_PUBLISHED_TOPIC)
    for log in receipt.get("logs") or []:
        topics = [_norm_topic(t) for t in (log.get("topics") or [])]
        if not topics or topics[0] != topic:
            continue
        if core_bridge and log.get("address", "").lower() != checksum(core_bridge).lower():
            continue
        data = log.get("data") or "0x"
        raw = bytes.fromhex(str(data).removeprefix("0x"))
        if len(raw) >= 32:
            return int.from_bytes(raw[24:32], "big")
    return None


def _extract_vaa_bytes(payload: dict[str, Any]) -> bytes | None:
    for key in ("vaaBytes", "vaa", "bytes", "encodedVaa"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            if val.startswith("0x"):
                return bytes.fromhex(val.removeprefix("0x"))
            try:
                return base64.b64decode(val)
            except Exception:
                try:
                    return bytes.fromhex(val)
                except Exception:
                    continue
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_vaa_bytes(data)
    return None


async def fetch_signed_vaa(
    client: httpx.AsyncClient,
    *,
    chain_id: int,
    emitter: str,
    sequence: int,
    timeout_sec: float | None = None,
    poll_sec: float | None = None,
) -> bytes | None:
    """Poll Wormholescan / Certus for a signed VAA."""
    wh = load_bridge_config()["wormhole"]
    timeout_sec = timeout_sec or float(wh.get("attestation_timeout_sec", 900))
    poll_sec = poll_sec or float(wh.get("attestation_poll_sec", 15))
    emitter = emitter.lower().removeprefix("0x").rjust(64, "0")
    urls = (
        f"https://api.wormholescan.io/v1/signed_vaa/{chain_id}/{emitter}/{sequence}",
        f"https://api.wormholescan.io/api/v1/vaas/{chain_id}/{emitter}/{sequence}",
        f"https://api.wormholescan.io/api/v1/vaas?chainId={chain_id}&emitter={emitter}&sequence={sequence}",
    )
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for url in urls:
            try:
                sync_throttle("wormholescan")
                resp = await client.get(url, timeout=30.0)
                if resp.status_code == 200:
                    body = resp.json()
                    vaa = _extract_vaa_bytes(body)
                    if not vaa and isinstance(body, dict):
                        for row in body.get("data") or []:
                            if isinstance(row, dict) and int(row.get("sequence") or -1) == int(sequence):
                                vaa = _extract_vaa_bytes(row)
                                if vaa:
                                    break
                    if vaa:
                        logger.info("Wormhole VAA ready chain=%s seq=%s (%d bytes)", chain_id, sequence, len(vaa))
                        return vaa
            except Exception as exc:
                logger.debug("VAA fetch %s: %s", url[:48], exc)
        await asyncio.sleep(poll_sec)
    logger.warning("Wormhole VAA timeout chain=%s emitter=%s seq=%s", chain_id, emitter[:16], sequence)
    return None


async def fetch_vaa_by_tx_hash(
    client: httpx.AsyncClient,
    tx_hash: str,
    *,
    timeout_sec: float | None = None,
    poll_sec: float | None = None,
) -> bytes | None:
    """Resolve VAA bytes from a source-chain initiate tx hash via Wormholescan operations API."""
    wh = load_bridge_config()["wormhole"]
    timeout_sec = timeout_sec or float(wh.get("attestation_timeout_sec", 900))
    poll_sec = poll_sec or float(wh.get("attestation_poll_sec", 15))
    tx_hash = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    url = f"https://api.wormholescan.io/api/v1/operations?txHash={tx_hash}"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            sync_throttle("wormholescan")
            resp = await client.get(url, timeout=30.0)
            if resp.status_code == 200:
                body = resp.json()
                ops = body if isinstance(body, list) else body.get("operations") or body.get("data") or []
                for op in ops:
                    vaa = _extract_vaa_bytes(op if isinstance(op, dict) else {})
                    if vaa:
                        return vaa
                    content = op.get("content") if isinstance(op, dict) else None
                    if isinstance(content, dict):
                        vaa = _extract_vaa_bytes(content)
                        if vaa:
                            return vaa
                    payload = op.get("payload") if isinstance(op, dict) else None
                    if isinstance(payload, dict):
                        vaa = _extract_vaa_bytes(payload)
                        if vaa:
                            return vaa
                if isinstance(body, dict):
                    vaa = _extract_vaa_bytes(body)
                    if vaa:
                        return vaa
        except Exception as exc:
            logger.debug("operations lookup %s: %s", tx_hash[:18], exc)
        await asyncio.sleep(poll_sec)
    return None
