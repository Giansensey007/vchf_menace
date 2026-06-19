from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class ProviderQuote:
    provider: str
    amount_in: int
    amount_out: int
    route_dexs: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.amount_out > 0


@dataclass
class QuoteResult:
    provider: str
    amount_in: int
    amount_out: int
    route_dexs: list[str]
    all_providers: list[ProviderQuote]
    token_in: str
    token_out: str
    chain_key: str
    hub_stable: str


def to_human(amount: int, decimals: int) -> Decimal:
    return Decimal(amount) / Decimal(10**decimals)


def from_human(amount: Decimal | float | int, decimals: int) -> int:
    return int(Decimal(str(amount)) * Decimal(10**decimals))
