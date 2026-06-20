from __future__ import annotations

import logging
import os

from eth_account import Account
from web3 import Web3

from src.config_loader import ChainConfig, is_dry_run
from src.execution.base_rpc import connect_base_web3
from src.quotes.sync_throttle import retry_backoff_sec, sync_throttle
from src.quotes.addresses import checksum

logger = logging.getLogger(__name__)

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

SWAP_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

WORMHOLE_COMPLETE_ABI = [
    {
        "inputs": [{"name": "encodedVm", "type": "bytes"}],
        "name": "completeTransfer",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


class BaseExecutor:
    def __init__(self, chain: ChainConfig) -> None:
        self.chain = chain
        pk = os.getenv("BASE_PRIVATE_KEY", "").strip()
        if not pk:
            raise ValueError("BASE_PRIVATE_KEY not set")
        self.account = Account.from_key(pk)
        router = os.getenv("BASE_SWAP_ROUTER") or chain.swap_router
        if not router:
            raise ValueError("BASE swap router not configured")
        self.router = checksum(router)
        self.w3 = connect_base_web3(chain.rpc_url)

    @property
    def address(self) -> str:
        return self.account.address

    def balance_native(self) -> int:
        return self.w3.eth.get_balance(self.account.address)

    def balance_erc20(self, token: str) -> int:
        contract = self.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
        return contract.functions.balanceOf(self.account.address).call()

    def _build_and_send(self, tx: dict) -> str | None:
        if is_dry_run():
            logger.info("[DRY_RUN] Base tx to=%s data=%s", tx.get("to"), (tx.get("data") or "")[:20])
            return "dry-run-base-tx"

        import os

        max_attempts = int(os.getenv("TX_RETRY_MAX", "4"))
        for attempt in range(max_attempts):
            try:
                sync_throttle("base_rpc")
                signed = self.account.sign_transaction(tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
                if receipt.status != 1:
                    logger.error("Base tx reverted: %s", tx_hash.hex())
                    return None
                return tx_hash.hex()
            except Exception as exc:
                if attempt + 1 >= max_attempts:
                    logger.error("Base send failed: %s", exc)
                    return None
                logger.warning("Base send failed (attempt %s/%s): %s", attempt + 1, max_attempts, exc)
                import time

                time.sleep(retry_backoff_sec(attempt))
                try:
                    self.w3 = connect_base_web3(self.chain.rpc_url)
                except Exception:
                    pass
        return None

    def _tx_base(self, fn=None) -> dict:
        import os

        max_attempts = int(os.getenv("RPC_RETRY_MAX", "4"))
        for attempt in range(max_attempts):
            try:
                sync_throttle("base_rpc")
                nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
                base = {
                    "from": self.account.address,
                    "nonce": nonce,
                    "chainId": self.chain.chain_id,
                    "gasPrice": self.w3.eth.gas_price,
                }
                if fn is not None:
                    try:
                        base["gas"] = fn.estimate_gas({"from": self.account.address})
                    except Exception as exc:
                        logger.warning("gas estimate failed, using default: %s", exc)
                        low = str(exc).lower()
                        if "transfer already completed" in low or (
                            "already completed" in low and "transfer" in low
                        ):
                            raise RuntimeError("wormhole-already-claimed") from exc
                        base["gas"] = 350_000
                return base
            except Exception as exc:
                if attempt + 1 >= max_attempts:
                    raise
                logger.warning("Base RPC read failed, reconnecting: %s", exc)
                import time

                time.sleep(retry_backoff_sec(attempt))
                self.w3 = connect_base_web3(self.chain.rpc_url)
        raise RuntimeError("Base RPC unreachable")

    def approve_if_needed(self, token: str, spender: str, amount: int) -> str | None:
        contract = self.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
        allowance = contract.functions.allowance(self.account.address, checksum(spender)).call()
        if allowance >= amount:
            return "already-approved"
        fn = contract.functions.approve(checksum(spender), amount)
        tx = fn.build_transaction(self._tx_base(fn))
        result = self._build_and_send(tx)
        if not result:
            return None
        return result

    def swap_exact_input(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        fee: int = 100,
    ) -> str | None:
        self.approve_if_needed(token_in, self.router, amount_in)
        router = self.w3.eth.contract(address=self.router, abi=SWAP_ROUTER_ABI)
        params = (
            checksum(token_in),
            checksum(token_out),
            fee,
            self.account.address,
            amount_in,
            amount_out_min,
            0,
        )
        fn = router.functions.exactInputSingle(params)
        tx = fn.build_transaction(self._tx_base(fn))
        return self._build_and_send(tx)

    def transfer_erc20(self, token: str, to: str, amount: int) -> str | None:
        contract = self.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
        fn = contract.functions.transfer(checksum(to), amount)
        tx = fn.build_transaction(self._tx_base(fn))
        return self._build_and_send(tx)

    def complete_transfer_wormhole(self, bridge: str, vaa: bytes) -> str | None:
        contract = self.w3.eth.contract(address=checksum(bridge), abi=WORMHOLE_COMPLETE_ABI)
        fn = contract.functions.completeTransfer(vaa)
        try:
            base = self._tx_base(fn)
        except RuntimeError as exc:
            if str(exc) == "wormhole-already-claimed":
                logger.info("Wormhole transfer already completed on Base")
                return "already-claimed"
            raise
        tx = fn.build_transaction(base)
        result = self._build_and_send(tx)
        if result == "already-claimed":
            return result
        return result

    def simulate_swap(
        self, token_in: str, token_out: str, amount_in: int, fee: int = 100
    ) -> dict | None:
        """eth_call simulation via quoter — read-only."""
        from src.quotes.onchain import quote_pool

        q = quote_pool(self.w3, self.chain.quoter_v2 or "", token_in, token_out, amount_in, fee)
        if not q.ok:
            return None
        return {"amount_in": amount_in, "amount_out": q.amount_out, "provider": q.provider}
