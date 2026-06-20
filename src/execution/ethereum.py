from __future__ import annotations

import logging
import os

from eth_account import Account
from web3 import Web3

from src.config_loader import ChainConfig, is_dry_run
from src.execution.eth_rpc import connect_eth_web3
from src.execution.base import ERC20_ABI
from src.quotes.addresses import checksum
from src.quotes.sync_throttle import retry_backoff_sec, sync_throttle

logger = logging.getLogger(__name__)

TOKEN_MESSENGER_V2_ABI = [
    {
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "destinationDomain", "type": "uint32"},
            {"name": "mintRecipient", "type": "bytes32"},
            {"name": "burnToken", "type": "address"},
            {"name": "destinationCaller", "type": "bytes32"},
            {"name": "maxFee", "type": "uint256"},
            {"name": "minFinalityThreshold", "type": "uint32"},
        ],
        "name": "depositForBurn",
        "outputs": [{"name": "nonce", "type": "uint64"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

MESSAGE_TRANSMITTER_V2_ABI = [
    {
        "inputs": [
            {"name": "message", "type": "bytes"},
            {"name": "attestation", "type": "bytes"},
        ],
        "name": "receiveMessage",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

WORMHOLE_TRANSFER_ABI = [
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "recipientChain", "type": "uint16"},
            {"name": "recipient", "type": "bytes32"},
            {"name": "arbiterFee", "type": "uint256"},
            {"name": "nonce", "type": "uint32"},
        ],
        "name": "transferTokens",
        "outputs": [{"name": "sequence", "type": "uint64"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "encodedVm", "type": "bytes"}],
        "name": "completeTransfer",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e0eC007cac778ea9Fa758"
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
SWAP_ROUTER02_ADDRESS = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"

PERMIT2_ABI = [
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint160"},
            {"name": "expiration", "type": "uint48"},
        ],
        "name": "approve",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [
            {
                "components": [
                    {"name": "amount", "type": "uint160"},
                    {"name": "expiration", "type": "uint48"},
                    {"name": "nonce", "type": "uint48"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

WETH_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "payable": False,
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "", "type": "address"}],
        "name": "balanceOf",
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
                    {"name": "deadline", "type": "uint256"},
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

ZERO_BYTES32 = b"\x00" * 32


def _load_eth_key() -> str:
    pk = os.getenv("ETH_PRIVATE_KEY", "").strip()
    if pk:
        return pk
    pk = os.getenv("BASE_PRIVATE_KEY", "").strip()
    if pk:
        return pk
    raise ValueError("ETH_PRIVATE_KEY or BASE_PRIVATE_KEY not set")


class EthereumExecutor:
    def __init__(self, chain: ChainConfig) -> None:
        self.chain = chain
        self.account = Account.from_key(_load_eth_key())
        self.w3 = connect_eth_web3(chain.rpc_url)
        self.last_error: str | None = None

    @property
    def address(self) -> str:
        return self.account.address

    def balance_native(self) -> int:
        return self.w3.eth.get_balance(self.account.address)

    def balance_erc20(self, token: str) -> int:
        contract = self.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
        return contract.functions.balanceOf(self.account.address).call()

    def _base_tx(self, fn) -> dict:
        import os

        max_attempts = int(os.getenv("RPC_RETRY_MAX", "4"))
        for attempt in range(max_attempts):
            try:
                sync_throttle("eth_rpc")
                nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
                gas_price = int(self.w3.eth.gas_price)
                priority = min(gas_price // 10, self.w3.to_wei(2, "gwei"))
                max_fee = max(gas_price * 2, priority + gas_price)
                try:
                    gas = fn.estimate_gas({"from": self.account.address})
                except Exception as exc:
                    self.last_error = str(exc)
                    low = str(exc).lower()
                    for arg in getattr(exc, "args", ()):
                        low += " " + str(arg).lower()
                    if "transfer already completed" in low or (
                        "already completed" in low and "transfer" in low
                    ):
                        raise RuntimeError("wormhole-already-claimed") from exc
                    if "nonce already used" in low or "message already received" in low:
                        raise
                    logger.warning("gas estimate failed, using default: %s", exc)
                    gas = 220_000
                balance = self.w3.eth.get_balance(self.account.address)
                reserve = self.w3.to_wei(0.00005, "ether")
                spendable = max(0, balance - reserve)
                if spendable > 0 and gas > 0:
                    affordable = int(spendable * 95 // 100 // gas)
                    if affordable > 0 and max_fee > affordable:
                        logger.info(
                            "Capping maxFeePerGas %s→%s wei (balance %.6f ETH, gas %s)",
                            max_fee,
                            affordable,
                            float(self.w3.from_wei(balance, "ether")),
                            gas,
                        )
                        max_fee = affordable
                        priority = min(priority, max_fee)
                if spendable <= 0 or max_fee * gas > spendable:
                    raise RuntimeError(
                        f"insufficient ETH for gas: have {float(self.w3.from_wei(balance, 'ether')):.6f} ETH"
                    )
                return {
                    "from": self.account.address,
                    "nonce": nonce,
                    "gas": gas,
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority,
                    "chainId": self.chain.chain_id,
                }
            except Exception as exc:
                if str(exc) == "wormhole-already-claimed":
                    raise
                if attempt + 1 >= max_attempts:
                    raise
                logger.warning("ETH RPC read failed, reconnecting: %s", exc)
                import time

                time.sleep(retry_backoff_sec(attempt))
                self.w3 = connect_eth_web3(None)
        raise RuntimeError("ETH RPC unreachable")

    def _build_and_send(self, tx: dict, *, fn=None) -> str | None:
        self.last_error = None
        if is_dry_run():
            logger.info("[DRY_RUN] ETH tx to=%s", tx.get("to"))
            return "dry-run-eth-tx"

        import os

        max_attempts = int(os.getenv("TX_RETRY_MAX", "4"))
        for attempt in range(max_attempts):
            try:
                if attempt > 0 and fn is not None:
                    tx = fn.build_transaction(self._base_tx(fn))
                sync_throttle("eth_rpc")
                signed = self.account.sign_transaction(tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
                if receipt.status != 1:
                    self.last_error = "transaction reverted"
                    logger.error("ETH tx reverted: %s", tx_hash.hex())
                    if fn is not None:
                        try:
                            fn.call({"from": self.account.address})
                        except Exception as call_exc:
                            low = str(call_exc).lower()
                            for arg in getattr(call_exc, "args", ()):
                                low += " " + str(arg).lower()
                            if "transfer already completed" in low or (
                                "already completed" in low and "transfer" in low
                            ):
                                logger.info("Wormhole transfer already completed on ETH (revert)")
                                return "already-claimed"
                    return None
                return tx_hash.hex()
            except Exception as exc:
                self.last_error = str(exc)
                low = str(exc).lower()
                if any(k in low for k in ("nonce already used", "message already received", "already known")):
                    logger.info("ETH tx already processed: %s", exc)
                    return "already-claimed"
                if attempt + 1 >= max_attempts:
                    logger.error("ETH tx failed: %s", exc)
                    return None
                logger.warning("ETH send failed, reconnecting RPC: %s", exc)
                import time

                time.sleep(retry_backoff_sec(attempt))
                self.w3 = connect_eth_web3(None)
        return None

    def approve_if_needed(self, token: str, spender: str, amount: int) -> str | None:
        contract = self.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
        allowance = contract.functions.allowance(self.account.address, checksum(spender)).call()
        if allowance >= amount:
            return "already-approved"
        approve_amount = 2**256 - 1
        fn = contract.functions.approve(checksum(spender), approve_amount)
        tx = fn.build_transaction(self._base_tx(fn))
        return self._build_and_send(tx, fn=fn)

    def _uses_permit2(self, router_addr: str) -> bool:
        return checksum(router_addr).lower() == checksum(SWAP_ROUTER02_ADDRESS).lower()

    def approve_permit2_if_needed(self, token: str, router_addr: str, amount: int) -> str | None:
        """SwapRouter02 pulls via Permit2 — ERC20 approve to router alone is not enough."""
        import time

        if not self._uses_permit2(router_addr):
            return self.approve_if_needed(token, router_addr, amount)

        self.approve_if_needed(token, PERMIT2_ADDRESS, amount)
        permit2 = self.w3.eth.contract(address=checksum(PERMIT2_ADDRESS), abi=PERMIT2_ABI)
        current = permit2.functions.allowance(
            self.account.address, checksum(token), checksum(router_addr)
        ).call()
        if int(current[0]) >= amount and int(current[1]) > time.time():
            return "already-approved"
        expiration = int(time.time()) + 86400 * 30
        fn = permit2.functions.approve(
            checksum(token), checksum(router_addr), min(amount, 2**160 - 1), expiration
        )
        tx = fn.build_transaction(self._base_tx(fn))
        return self._build_and_send(tx, fn=fn)

    def deposit_for_burn(
        self,
        *,
        token_messenger: str,
        usdc: str,
        amount: int,
        destination_domain: int,
        mint_recipient: bytes,
        max_fee: int,
        min_finality_threshold: int,
    ) -> str | None:
        if len(mint_recipient) != 32:
            raise ValueError("mint_recipient must be 32 bytes")

        tm = self.w3.eth.contract(address=checksum(token_messenger), abi=TOKEN_MESSENGER_V2_ABI)
        self.approve_if_needed(usdc, token_messenger, amount)

        fn = tm.functions.depositForBurn(
            amount,
            destination_domain,
            mint_recipient,
            checksum(usdc),
            ZERO_BYTES32,
            max_fee,
            min_finality_threshold,
        )
        tx = fn.build_transaction(self._base_tx(fn))
        return self._build_and_send(tx, fn=fn)

    def receive_message(
        self,
        *,
        message_transmitter: str,
        message_hex: str,
        attestation_hex: str,
    ) -> str | None:
        mt = self.w3.eth.contract(address=checksum(message_transmitter), abi=MESSAGE_TRANSMITTER_V2_ABI)
        message = bytes.fromhex(message_hex.removeprefix("0x"))
        attestation = bytes.fromhex(attestation_hex.removeprefix("0x"))
        fn = mt.functions.receiveMessage(message, attestation)
        try:
            base = self._base_tx(fn)
        except Exception as exc:
            if "nonce already used" in str(exc).lower():
                logger.info("CCTP message already claimed on ETH (nonce used)")
                return "already-claimed"
            raise
        if self.last_error and "nonce already used" in self.last_error.lower():
            logger.info("CCTP message already claimed on ETH (nonce used)")
            return "already-claimed"
        tx = fn.build_transaction(base)
        return self._build_and_send(tx, fn=fn)

    def transfer_erc20(self, token: str, to: str, amount: int) -> str | None:
        contract = self.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
        fn = contract.functions.transfer(checksum(to), amount)
        tx = fn.build_transaction(self._base_tx(fn))
        return self._build_and_send(tx, fn=fn)

    def transfer_tokens_wormhole(
        self,
        *,
        bridge: str,
        token: str,
        amount: int,
        dest_chain_id: int,
        recipient: bytes,
        nonce: int | None = None,
    ) -> str | None:
        import time

        self.approve_if_needed(token, bridge, amount)
        contract = self.w3.eth.contract(address=checksum(bridge), abi=WORMHOLE_TRANSFER_ABI)
        erc20 = self.w3.eth.contract(address=checksum(token), abi=ERC20_ABI)
        allowance = erc20.functions.allowance(self.account.address, checksum(bridge)).call()
        if allowance < amount:
            self.last_error = f"USDT allowance {allowance} < amount {amount} (approve likely failed — low ETH gas?)"
            logger.error(self.last_error)
            return None
        fn = contract.functions.transferTokens(
            checksum(token),
            amount,
            dest_chain_id,
            recipient,
            0,
            nonce if nonce is not None else int(time.time()) & 0xFFFFFFFF,
        )
        tx = fn.build_transaction(self._base_tx(fn))
        return self._build_and_send(tx, fn=fn)

    def complete_transfer_wormhole(self, bridge: str, vaa: bytes) -> str | None:
        contract = self.w3.eth.contract(address=checksum(bridge), abi=WORMHOLE_TRANSFER_ABI)
        fn = contract.functions.completeTransfer(vaa)
        try:
            base = self._base_tx(fn)
        except RuntimeError as exc:
            if str(exc) == "wormhole-already-claimed":
                logger.info("Wormhole transfer already completed on ETH")
                return "already-claimed"
            raise
        except Exception as exc:
            low = str(exc).lower()
            if "transfer already completed" in low or "already completed" in low:
                logger.info("Wormhole transfer already completed on ETH")
                return "already-claimed"
            raise
        tx = fn.build_transaction(base)
        result = self._build_and_send(tx, fn=fn)
        if result == "already-claimed":
            return result
        if not result and self.last_error:
            low = self.last_error.lower()
            if "transfer already completed" in low or "already completed" in low:
                logger.info("Wormhole transfer already completed on ETH (revert)")
                return "already-claimed"
        return result

    def swap_exact_input(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        fee: int = 100,
    ) -> str | None:
        router_addr = os.getenv("ETH_SWAP_ROUTER") or self.chain.swap_router
        if not router_addr:
            self.last_error = "ETH swap router not configured"
            return None
        import time

        self.approve_permit2_if_needed(token_in, router_addr, amount_in)
        router = self.w3.eth.contract(address=checksum(router_addr), abi=SWAP_ROUTER_ABI)
        deadline = int(time.time()) + 600
        params = (
            checksum(token_in),
            checksum(token_out),
            fee,
            self.account.address,
            amount_in,
            amount_out_min,
            0,
            deadline,
        )
        fn = router.functions.exactInputSingle(params)
        tx = fn.build_transaction(self._base_tx(fn))
        return self._build_and_send(tx, fn=fn)

    def simulate_swap(
        self, token_in: str, token_out: str, amount_in: int, fee: int = 100
    ) -> dict | None:
        from src.quotes.onchain import quote_pool

        if not self.chain.quoter_v2:
            return None
        q = quote_pool(self.w3, self.chain.quoter_v2, token_in, token_out, amount_in, fee)
        if not q.ok:
            return None
        return {"amount_in": amount_in, "amount_out": q.amount_out, "provider": q.provider}

    def unwrap_weth(self, amount_wei: int | None = None) -> str | None:
        """Unwrap WETH balance to native ETH."""
        weth = self.w3.eth.contract(address=checksum(WETH_ADDRESS), abi=WETH_ABI)
        balance = weth.functions.balanceOf(self.account.address).call()
        if balance <= 0:
            self.last_error = "no WETH balance to unwrap"
            return None
        amount = min(balance, amount_wei) if amount_wei is not None else balance
        fn = weth.functions.withdraw(amount)
        tx = fn.build_transaction(self._base_tx(fn))
        return self._build_and_send(tx, fn=fn)

    def swap_usdc_to_native_eth(self, amount_usdc: float, *, slippage_bps: int = 100) -> str | None:
        """Swap USDC → WETH (Uniswap V3) then unwrap to native ETH."""
        from src.quotes.types import from_human

        amount_in = from_human(amount_usdc, 6)
        min_out = 1
        best_fee: int | None = None
        if self.chain.quoter_v2:
            best_out = 0
            for fee in (500, 3000, 100, 10000):
                q = self.simulate_swap(self.chain.hub_token, WETH_ADDRESS, amount_in, fee=fee)
                if q and q["amount_out"] > best_out:
                    best_out = q["amount_out"]
                    best_fee = fee
            if best_out > 0:
                min_out = int(best_out * (1 - slippage_bps / 10_000))
        else:
            eth_per_usdc = float(os.getenv("ETH_USD_ESTIMATE", "4000"))
            expected_wei = int(amount_usdc / eth_per_usdc * 1e18)
            min_out = max(1, int(expected_wei * (1 - slippage_bps / 10_000)))

        tx = None
        fee_order = ([best_fee] if best_fee is not None else []) + [500, 3000, 100, 10000]
        seen: set[int] = set()
        for fee in fee_order:
            if fee in seen:
                continue
            seen.add(fee)
            tx = self.swap_exact_input(
                self.chain.hub_token, WETH_ADDRESS, amount_in, min_out, fee=fee
            )
            if tx:
                break
        if not tx:
            return None
        return self.unwrap_weth()

    def swap_usdc_to_native_eth_paraswap(self, amount_usdc: float, *, slippage_bps: int = 100) -> str | None:
        """Fallback: USDC → native ETH via ParaSwap (works when Uniswap RPC quotes fail)."""
        import httpx

        from src.quotes.types import from_human

        amount_in = from_human(amount_usdc, 6)
        if amount_in <= 0:
            self.last_error = "zero amount"
            return None
        proxy = "0x216b4b4ba9f3e719726886d34a177484278bfcae"
        usdc = checksum(self.chain.hub_token)
        wallet = checksum(self.account.address)
        slippage = max(1, slippage_bps // 100)
        try:
            with httpx.Client(timeout=30) as client:
                q = client.get(
                    "https://apiv5.paraswap.io/prices/",
                    params={
                        "srcToken": usdc,
                        "destToken": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
                        "amount": str(amount_in),
                        "srcDecimals": 6,
                        "destDecimals": 18,
                        "side": "SELL",
                        "network": 1,
                        "userAddress": wallet,
                    },
                )
                q.raise_for_status()
                route = q.json()["priceRoute"]
                self.approve_if_needed(self.chain.hub_token, proxy, amount_in)
                txr = client.post(
                    "https://apiv5.paraswap.io/transactions/1",
                    json={
                        "srcToken": route["srcToken"],
                        "destToken": route["destToken"],
                        "srcAmount": route["srcAmount"],
                        "priceRoute": route,
                        "userAddress": wallet,
                        "slippage": slippage,
                    },
                )
                txr.raise_for_status()
                body = txr.json()
        except Exception as exc:
            self.last_error = f"paraswap quote/build failed: {exc}"[:200]
            return None

        tx = {
            "from": self.account.address,
            "to": checksum(body["to"]),
            "data": body["data"],
            "value": int(body.get("value") or 0),
        }
        try:
            tx["gas"] = self.w3.eth.estimate_gas(tx)
        except Exception:
            tx["gas"] = int(body.get("gas") or 400_000)
        base = self._base_tx(type("_Fn", (), {"estimate_gas": lambda _s, _x: tx["gas"]})())
        for key in ("nonce", "maxFeePerGas", "maxPriorityFeePerGas", "chainId"):
            tx[key] = base[key]
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        if receipt.status != 1:
            self.last_error = "paraswap swap reverted"
            return None
        return tx_hash.hex()

    def swap_usdt_to_native_eth(self, amount_usdt: float, *, slippage_bps: int = 100) -> str | None:
        """Swap USDT → WETH then unwrap to native ETH."""
        from src.quotes.types import from_human

        wh_usdt = os.getenv("ETH_USDT") or "0xdAC17F958D2ee523a2206206994597C13D831ec7"
        amount_in = from_human(amount_usdt, 6)
        eth_per_usd = float(os.getenv("ETH_USD_ESTIMATE", "2500"))
        expected_wei = int(amount_usdt / eth_per_usd * 1e18)
        min_out = int(expected_wei * (1 - slippage_bps / 10_000))
        tx = None
        for fee in (500, 3000, 100):
            tx = self.swap_exact_input(wh_usdt, WETH_ADDRESS, amount_in, min_out, fee=fee)
            if tx:
                break
        if not tx:
            return None
        return self.unwrap_weth()
