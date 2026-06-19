from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.bridge.cctp_queue import CctpClaimQueue, CctpQueueStatus


@pytest.fixture
def queue_path(tmp_path):
    return tmp_path / "cctp_queue.json"


def test_enqueue_dedup(queue_path):
    q = CctpClaimQueue(path=queue_path)
    item1, new1 = q.enqueue(source_tx="abc123", source_domain=5, dest_domain=0, intent="test")
    item2, new2 = q.enqueue(source_tx="abc123", source_domain=5, dest_domain=0, intent="test")
    assert new1 is True
    assert new2 is False
    assert item1.id == item2.id
    assert len(q._store.items) == 1


def test_enqueue_dedup_eth_with_without_0x(queue_path):
    q = CctpClaimQueue(path=queue_path)
    tx = "0x5b99d4a8ca5a84ae2c3b5b85f758a669ac9acbd60e47e6c2c0e3e9ec02c39e08"
    item1, new1 = q.enqueue(source_tx=tx, source_domain=0, dest_domain=5, intent="a")
    item2, new2 = q.enqueue(source_tx=tx[2:], source_domain=0, dest_domain=5, intent="b")
    assert new1 is True
    assert new2 is False
    assert len(q._store.items) == 1


def test_coalesce_marks_duplicate_claimed(queue_path):
    q = CctpClaimQueue(path=queue_path)
    claimed, _ = q.enqueue(
        source_tx="0xabc",
        source_domain=0,
        dest_domain=5,
        intent="claimed",
    )
    claimed.status = CctpQueueStatus.CLAIMED.value
    claimed.dest_tx = "solclaim"
    dup, _ = q.enqueue(source_tx="abc", source_domain=0, dest_domain=5, intent="dup")
    CctpClaimQueue._coalesce_duplicates(q._store)
    assert len(q._store.items) == 1
    assert q._store.items[0].status == CctpQueueStatus.CLAIMED.value


@pytest.mark.asyncio
async def test_process_once_claims_ready(queue_path):
    q = CctpClaimQueue(path=queue_path)
    item, _ = q.enqueue(source_tx="burn1", source_domain=5, dest_domain=0, intent="unit")
    item.status = CctpQueueStatus.READY.value
    item.message_hex = "0xdead"
    item.attestation_hex = "0xbeef"
    q.save()

    mock_client = AsyncMock()
    with patch.object(q, "_refresh_iris", new_callable=AsyncMock), patch.object(
        q, "claim_item", new_callable=AsyncMock
    ) as mock_claim:
        mock_claim.return_value = True
        n = await q.process_once(mock_client)
    assert n == 1
    mock_claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_iris_pending(queue_path):
    q = CctpClaimQueue(path=queue_path)
    item, _ = q.enqueue(source_tx="tx1", source_domain=5, dest_domain=0)

    mock_client = AsyncMock()
    with patch("src.bridge.cctp_queue.fetch_messages", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = [{"attestation": "PENDING", "message": "0x"}]
        await q._refresh_iris(mock_client, item)
    assert item.status == CctpQueueStatus.PENDING_ATTESTATION.value


@pytest.mark.asyncio
async def test_refresh_iris_ready(queue_path):
    q = CctpClaimQueue(path=queue_path)
    item, _ = q.enqueue(source_tx="tx2", source_domain=5, dest_domain=0)

    mock_client = AsyncMock()
    with patch("src.bridge.cctp_queue.fetch_messages", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = [{"attestation": "0xabc", "message": "0xmsg"}]
        await q._refresh_iris(mock_client, item)
    assert item.status == CctpQueueStatus.READY.value
    assert item.attestation_hex == "0xabc"
