"""Polymarket CLOB order submission.

================================  GATE 4  ================================
This is the real-money execution path. It is **dry-run by default**:
`submit()` logs the order it WOULD send and returns a simulated ack without
touching the network. Live submission requires BOTH:

  1. constructing the client with `live=True`, AND
  2. credentials present in the environment (never passed as arguments).

Per the approved deployment roadmap, live orders must NOT be enabled until
Gates 1-3 (statistical evidence, calibration, historical backtest) pass.
A `STOP.lock` file in the working directory hard-blocks every live order.
===========================================================================

Built against py-clob-client-v2 (the original py-clob-client is archived).
Install only when moving to Gate 4:  pip install py-clob-client-v2

Environment variables read for live mode:
  POLYBOT_POLY_PK          wallet private key            (required)
  POLYBOT_POLY_FUNDER      funder address                (optional)
  POLYBOT_POLY_SIG_TYPE    signature type 0/1/2          (optional)
  POLYBOT_CLOB_API_KEY     L2 api key   — if absent, derived from the wallet
  POLYBOT_CLOB_SECRET      L2 api secret
  POLYBOT_CLOB_PASSPHRASE  L2 api passphrase
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
KILL_FILE = Path("STOP.lock")


@dataclass
class OrderTicket:
    """Bot-side order intent, before it is signed and sent."""

    token_id: str
    side: str  # "BUY" | "SELL"
    kind: str  # "market" | "limit"
    usdc_amount: float | None = None  # market orders: USDC notional to spend
    price: float | None = None  # limit orders
    size: float | None = None  # limit orders: shares
    order_type: str = "FOK"  # FOK | FAK | GTC | GTD
    tick_size: str = "0.01"
    note: str = ""


@dataclass
class OrderResult:
    ticket: OrderTicket
    submitted_unix: float
    dry_run: bool
    accepted: bool = False
    response: dict[str, Any] | None = None
    error: str | None = None


def _ticket_summary(t: OrderTicket) -> dict[str, Any]:
    return {
        "token_id": t.token_id,
        "side": t.side,
        "kind": t.kind,
        "usdc_amount": t.usdc_amount,
        "price": t.price,
        "size": t.size,
        "order_type": t.order_type,
        "tick_size": t.tick_size,
        "note": t.note,
    }


class ClobOrderClient:
    """Dry-run-by-default wrapper around the Polymarket v2 CLOB client.

    For "be first" capture the right ticket is a market FOK BUY: take the
    resting liquidity now or kill — never rest, never partial. If the new
    market has no book yet, FOK kills and the caller can fall back to a
    GTC limit at fair value.
    """

    def __init__(
        self,
        *,
        live: bool = False,
        host: str = CLOB_HOST,
        chain_id: int = POLYGON_CHAIN_ID,
        log_path: str | Path = "data/clob_orders.jsonl",
    ) -> None:
        self.live = bool(live)
        self.host = host
        self.chain_id = int(chain_id)
        self.log_path = Path(log_path)
        self._client: Any = None  # lazily-built py_clob_client_v2.ClobClient

    @staticmethod
    def _env(name: str, default: str | None = None) -> str | None:
        v = os.getenv(name)
        return v if v not in (None, "") else default

    def _ensure_client(self) -> Any:
        """Build (once) the underlying v2 ClobClient. Live mode only."""
        if self._client is not None:
            return self._client
        pk = self._env("POLYBOT_POLY_PK")
        if not pk:
            raise RuntimeError("POLYBOT_POLY_PK not set — cannot build a live CLOB client")
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
        except ImportError as exc:
            raise RuntimeError(
                "py-clob-client-v2 is not installed (pip install py-clob-client-v2)"
            ) from exc

        kwargs: dict[str, Any] = {"host": self.host, "chain_id": self.chain_id, "key": pk}
        sig = self._env("POLYBOT_POLY_SIG_TYPE")
        if sig is not None:
            kwargs["signature_type"] = int(sig)
        funder = self._env("POLYBOT_POLY_FUNDER")
        if funder:
            kwargs["funder"] = funder

        api_key = self._env("POLYBOT_CLOB_API_KEY")
        if api_key:
            kwargs["creds"] = ApiCreds(
                api_key=api_key,
                api_secret=self._env("POLYBOT_CLOB_SECRET"),
                api_passphrase=self._env("POLYBOT_CLOB_PASSPHRASE"),
            )

        client = ClobClient(**kwargs)
        if api_key is None:
            # No L2 creds supplied — derive them from the L1 wallet signature.
            client.set_api_creds(client.create_or_derive_api_key())
        self._client = client
        return client

    def place_market_buy(
        self,
        token_id: str,
        usdc_amount: float,
        order_type: str = "FOK",
        tick_size: str = "0.01",
        note: str = "",
    ) -> OrderResult:
        return self.submit(
            OrderTicket(
                token_id=token_id,
                side="BUY",
                kind="market",
                usdc_amount=float(usdc_amount),
                order_type=order_type,
                tick_size=tick_size,
                note=note,
            )
        )

    def place_limit(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
        tick_size: str = "0.01",
        note: str = "",
    ) -> OrderResult:
        return self.submit(
            OrderTicket(
                token_id=token_id,
                side=side.upper(),
                kind="limit",
                price=float(price),
                size=float(size),
                order_type=order_type,
                tick_size=tick_size,
                note=note,
            )
        )

    def submit(self, ticket: OrderTicket) -> OrderResult:
        res = OrderResult(ticket=ticket, submitted_unix=time.time(), dry_run=not self.live)
        if not self.live:
            res.accepted = True
            res.response = {"dry_run": True, "would_send": _ticket_summary(ticket)}
        else:
            try:
                res.response = self._submit_live(ticket)
                res.accepted = True
            except Exception as exc:
                res.error = str(exc)
        self._log(res)
        return res

    def _submit_live(self, ticket: OrderTicket) -> dict[str, Any]:
        if KILL_FILE.exists():
            raise RuntimeError(f"kill switch active ({KILL_FILE}) — live order refused")
        from py_clob_client_v2 import (
            MarketOrderArgs,
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        client = self._ensure_client()
        side = Side.BUY if ticket.side == "BUY" else Side.SELL
        otype = getattr(OrderType, ticket.order_type)
        opts = PartialCreateOrderOptions(tick_size=ticket.tick_size)

        if ticket.kind == "market":
            if ticket.usdc_amount is None:
                raise ValueError("market order requires usdc_amount")
            resp = client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=ticket.token_id,
                    amount=ticket.usdc_amount,
                    side=side,
                    order_type=otype,
                ),
                options=opts,
                order_type=otype,
            )
        else:
            if ticket.price is None or ticket.size is None:
                raise ValueError("limit order requires price and size")
            resp = client.create_and_post_order(
                order_args=OrderArgs(
                    token_id=ticket.token_id,
                    price=ticket.price,
                    side=side,
                    size=ticket.size,
                ),
                options=opts,
                order_type=otype,
            )
        return resp if isinstance(resp, dict) else {"raw": str(resp)}

    def _log(self, res: OrderResult) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": res.submitted_unix,
                            "dry_run": res.dry_run,
                            "accepted": res.accepted,
                            "error": res.error,
                            "ticket": _ticket_summary(res.ticket),
                            "response": res.response,
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass


if __name__ == "__main__":
    # Dry-run demo — safe, sends nothing, touches no network.
    client = ClobOrderClient(live=False)
    result = client.place_market_buy("DEMO_TOKEN_ID", 25.0, note="dry-run demo")
    print(f"live={client.live}  dry_run={result.dry_run}  accepted={result.accepted}")
    print(json.dumps(result.response, indent=2))
