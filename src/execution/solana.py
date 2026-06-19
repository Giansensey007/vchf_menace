from __future__ import annotations

import asyncio
import base64
import logging
import os

import httpx
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    create_associated_token_account,
    get_associated_token_address,
    transfer_checked,
)

from src.config_loader import ChainConfig, is_dry_run
from src.execution.sol_rpc import call_with_retry, is_jupiter_slippage_error, is_sol_retryable
from src.quotes.jupiter import JUPITER_API_KEY, JUPITER_QUOTE
from src.quotes.rate_limit import get_with_retry, post_with_retry

logger = logging.getLogger(__name__)

JUPITER_SWAP = "https://api.jup.ag/swap/v1/swap"
TX_RETRY_MAX = int(os.getenv("TX_RETRY_MAX", "4"))
JUPITER_SLIPPAGE_STEP_BPS = int(os.getenv("JUPITER_SLIPPAGE_STEP_BPS", "50"))
JUPITER_SLIPPAGE_MAX_BPS = int(os.getenv("JUPITER_SLIPPAGE_MAX_BPS", "300"))


def load_keypair():
    from solders.keypair import Keypair

    secret = os.getenv("SOLANA_SECRET_KEY", "").strip()
    if not secret:
        raise ValueError("SOLANA_SECRET_KEY not set")
    if secret.startswith("["):
        import json

        arr = json.loads(secret)
        return Keypair.from_bytes(bytes(arr))
    return Keypair.from_base58_string(secret)


class SolanaExecutor:
    def __init__(self, chain: ChainConfig) -> None:
        self.chain = chain
        self.keypair = load_keypair()
        self.client = Client(chain.rpc_url)

    @property
    def pubkey(self) -> str:
        return str(self.keypair.pubkey())

    def balance_lamports(self) -> int:
        resp = call_with_retry(lambda: self.client.get_balance(self.keypair.pubkey()), label="get_balance")
        return resp.value or 0

    def token_account_balance(self, ata: Pubkey):
        return call_with_retry(
            lambda: self.client.get_token_account_balance(ata),
            label="get_token_account_balance",
        )

    def token_balance_ui(self, ata: Pubkey) -> float:
        try:
            return float(self.token_account_balance(ata).value.ui_amount or 0)
        except Exception:
            return 0.0

    def _jupiter_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if JUPITER_API_KEY:
            h["x-api-key"] = JUPITER_API_KEY
        return h

    async def get_swap_transaction(
        self,
        http: httpx.AsyncClient,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int,
    ) -> bytes | None:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_in),
            "slippageBps": slippage_bps,
            "restrictIntermediateTokens": "true",
        }
        q_resp = await get_with_retry(
            http, JUPITER_QUOTE, params=params, headers=self._jupiter_headers() or None, timeout=25.0
        )
        if q_resp.status_code >= 400:
            logger.error("Jupiter quote failed: %s", q_resp.text[:200])
            return None
        quote = q_resp.json()

        body = {
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
        }
        s_resp = await post_with_retry(
            http, JUPITER_SWAP, json=body, headers=self._jupiter_headers() or None, timeout=30.0
        )
        if s_resp.status_code >= 400:
            logger.error("Jupiter swap build failed: %s", s_resp.text[:200])
            return None
        swap_tx_b64 = s_resp.json().get("swapTransaction")
        if not swap_tx_b64:
            return None
        return base64.b64decode(swap_tx_b64)

    def _send_signed_tx(self, signed: VersionedTransaction) -> str:
        if is_dry_run():
            sim = call_with_retry(lambda: self.client.simulate_transaction(signed), label="simulate")
            logger.info("[DRY_RUN] Solana simulate: %s", sim)
            return "dry-run-solana-tx"

        resp = call_with_retry(
            lambda: self.client.send_transaction(signed, opts=TxOpts(skip_preflight=False)),
            label="send_transaction",
        )
        sig = str(resp.value)
        logger.info("Solana swap sent: %s", sig)
        try:
            from solders.signature import Signature

            call_with_retry(
                lambda: self.client.confirm_transaction(Signature.from_string(sig)),
                label="confirm_transaction",
            )
        except Exception as exc:
            logger.warning("Solana confirm wait: %s", exc)
        return sig

    async def swap(
        self,
        http: httpx.AsyncClient,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage_bps: int = 50,
    ) -> str | None:
        slip = slippage_bps
        last_exc: Exception | None = None
        for attempt in range(TX_RETRY_MAX):
            try:
                raw = await self.get_swap_transaction(http, input_mint, output_mint, amount_in, slip)
                if not raw:
                    if attempt + 1 < TX_RETRY_MAX:
                        await asyncio.sleep(1.0 + attempt)
                        continue
                    return None
                tx = VersionedTransaction.from_bytes(raw)
                signed = VersionedTransaction(tx.message, [self.keypair])
                return self._send_signed_tx(signed)
            except Exception as exc:
                last_exc = exc
                if is_jupiter_slippage_error(exc) and attempt + 1 < TX_RETRY_MAX:
                    slip = min(slip + JUPITER_SLIPPAGE_STEP_BPS, JUPITER_SLIPPAGE_MAX_BPS)
                    logger.warning(
                        "Jupiter slippage error (6024) — re-quote with slippage %s bps (attempt %s/%s)",
                        slip,
                        attempt + 1,
                        TX_RETRY_MAX,
                    )
                    await asyncio.sleep(1.0 + attempt * 0.5)
                    continue
                if is_sol_retryable(exc) and attempt + 1 < TX_RETRY_MAX:
                    wait = 2**attempt
                    logger.warning("Solana swap retry in %.1fs: %s", wait, exc)
                    await asyncio.sleep(wait)
                    continue
                raise
        if last_exc:
            raise last_exc
        return None

    def transfer_spl(self, mint: str, to: str, amount: int, decimals: int) -> str | None:
        """SPL transfer with automatic ATA creation for recipient."""
        if is_dry_run():
            logger.info("[DRY_RUN] SPL transfer %s -> %s amount=%s", mint[:8], to[:8], amount)
            return "dry-run-spl-transfer"

        owner = self.keypair.pubkey()
        mint_pk = Pubkey.from_string(mint)
        to_pk = Pubkey.from_string(to)
        source_ata = get_associated_token_address(owner, mint_pk)

        last_exc: Exception | None = None
        for attempt in range(TX_RETRY_MAX):
            try:
                ixs = []
                to_info = call_with_retry(lambda: self.client.get_account_info(to_pk), label="get_account_info")
                if to_info.value is not None and to_info.value.owner == TOKEN_PROGRAM_ID:
                    dest_ata = to_pk
                else:
                    dest_ata = get_associated_token_address(to_pk, mint_pk)
                    dest_info = call_with_retry(
                        lambda: self.client.get_account_info(dest_ata), label="get_account_info"
                    )
                    if dest_info.value is None:
                        ixs.append(create_associated_token_account(payer=owner, owner=to_pk, mint=mint_pk))
                ixs.append(
                    transfer_checked(
                        TransferCheckedParams(
                            program_id=TOKEN_PROGRAM_ID,
                            source=source_ata,
                            mint=mint_pk,
                            dest=dest_ata,
                            owner=owner,
                            amount=amount,
                            decimals=decimals,
                        )
                    )
                )
                blockhash_resp = call_with_retry(
                    lambda: self.client.get_latest_blockhash(), label="get_latest_blockhash"
                )
                blockhash = blockhash_resp.value.blockhash
                msg = MessageV0.try_compile(
                    payer=owner,
                    instructions=ixs,
                    address_lookup_table_accounts=[],
                    recent_blockhash=blockhash,
                )
                tx = VersionedTransaction(msg, [self.keypair])
                resp = call_with_retry(
                    lambda: self.client.send_transaction(tx, opts=TxOpts(skip_preflight=False)),
                    label="spl_send",
                )
                sig = str(resp.value)
                logger.info("SPL transfer sent: %s", sig)
                return sig
            except Exception as exc:
                last_exc = exc
                if attempt + 1 >= TX_RETRY_MAX or not is_sol_retryable(exc):
                    raise
                wait = 2**attempt
                logger.warning("SPL transfer retry in %.1fs: %s", wait, exc)
                import time

                time.sleep(wait)
        if last_exc:
            raise last_exc
        return None
