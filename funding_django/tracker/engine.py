import asyncio
import json
import os
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation, ROUND_CEILING

import aiohttp
from django.conf import settings

LAMPORTS_PER_SOL = 1_000_000_000
SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMMITMENT = "finalized"


def sol(lamports):
    return format(Decimal(lamports) / Decimal(LAMPORTS_PER_SOL), ".9f")


def valid_address(address):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not 32 <= len(address) <= 44:
        return False
    value = 0
    for character in address:
        if character not in alphabet:
            return False
        value = value * 58 + alphabet.index(character)
    zeros = len(address) - len(address.lstrip("1"))
    return zeros + (value.bit_length() + 7) // 8 == 32


def extract_transfers(entry):
    transaction = entry.get("transaction")
    meta = entry.get("meta")
    if not isinstance(transaction, dict) or not isinstance(meta, dict):
        raise ValueError("Transaction Helius invalide.")
    if meta.get("err") is not None:
        return []

    signatures = transaction.get("signatures") or []
    if not signatures or entry.get("slot") is None:
        raise ValueError("Signature ou slot absent.")

    signature = signatures[0]
    instructions = [
        (f"outer:{index}", instruction)
        for index, instruction in enumerate(
            (transaction.get("message") or {}).get("instructions") or []
        )
    ]

    for group in meta.get("innerInstructions") or []:
        instructions.extend(
            (f"inner:{group['index']}:{index}", instruction)
            for index, instruction in enumerate(group.get("instructions") or [])
        )

    transfers = []

    for position, instruction in instructions:
        is_system = (
            instruction.get("program") == "system"
            or instruction.get("programId") == SYSTEM_PROGRAM
        )
        if not is_system:
            continue

        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            raise ValueError(f"Instruction System non décodée dans {signature}.")

        if parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue

        info = parsed.get("info") or {}
        source = info.get("source")
        destination = info.get("destination")
        lamports = info.get("lamports")

        if not source or not destination or lamports is None:
            raise ValueError(f"Transfert incomplet dans {signature}.")

        lamports = int(lamports)
        if lamports <= 0 or source == destination:
            continue

        transfers.append(
            {
                "transfer_id": f"{signature}:{position}",
                "source": source,
                "destination": destination,
                "lamports": lamports,
                "sol": sol(lamports),
                "slot": int(entry["slot"]),
                "signature": signature,
                "instruction_position": position,
                "timestamp": entry.get("blockTime"),
            }
        )

    return transfers


def build_graph(tracking, transfers):
    period = tracking.tampon_count + 1 if tracking.schema == 2 else 1
    root = tracking.root_address
    start_slot = tracking.start_slot

    ordered = sorted(transfers, key=lambda row: (row.slot, row.transfer_id))
    arrivals = {root: start_slot - 1}
    queue = deque([(root, start_slot - 1, 0)])
    accepted = []
    positions = defaultdict(list)
    positions[root].append((0, start_slot - 1))

    while queue:
        address, arrival, steps = queue.popleft()

        for transfer in ordered:
            if transfer.source != address:
                continue
            if transfer.slot <= arrival:
                continue

            destination = transfer.destination
            next_arrival = min(arrivals.get(destination, transfer.slot), transfer.slot)

            if next_arrival == transfer.slot:
                arrivals[destination] = transfer.slot
                positions[destination].append((steps + 1, transfer.slot))
                queue.append((destination, transfer.slot, steps + 1))

            accepted.append(transfer)

    nodes = []

    for address, possibilities in positions.items():
        possibilities = sorted(set(possibilities))
        depth = possibilities[0][0]
        roles = {"dev" if step % period == 0 else "tampon" for step, _ in possibilities}
        role = "dev" if depth % period == 0 else "tampon"
        position = depth % period

        nodes.append(
            {
                "id": address,
                "depth": depth,
                "role": role,
                "position": position,
                "role_candidates": sorted(roles),
                "conflict": len(roles) > 1,
                "label": "DEV" if role == "dev" else f"TAMPON {position}",
                "color": "#2563eb" if role == "dev" else "#9ca3af",
                "root": address == root,
            }
        )

    edges = {}

    for transfer in accepted:
        key = (transfer.source, transfer.destination)
        edge = edges.setdefault(
            key,
            {
                "source": transfer.source,
                "destination": transfer.destination,
                "lamports": 0,
                "count": 0,
            },
        )
        edge["lamports"] += transfer.lamports
        edge["count"] += 1

    return {
        "nodes": nodes,
        "edges": list(edges.values()),
        "watched": sorted(arrivals),
        "same_slot_unresolved": [],
    }


class HeliusClient:
    def __init__(self):
        self.api_key = settings.HELIUS_API_KEY
        self.url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def stop(self):
        if self.session:
            await self.session.close()

    def safe_error(self, error):
        return str(error).replace(self.api_key, "[CLE_MASQUEE]")

    async def call(self, method, params):
        last_error = "Erreur réseau"

        for attempt in range(6):
            try:
                async with self.session.post(
                    self.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": attempt + 1,
                        "method": method,
                        "params": params,
                    },
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as response:
                    if response.status == 429 or response.status >= 500:
                        last_error = f"HTTP {response.status}"
                    elif response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    else:
                        payload = await response.json()
                        if payload.get("error"):
                            raise RuntimeError(payload["error"].get("message", "Erreur RPC"))
                        return payload["result"]
            except (aiohttp.ClientError, asyncio.TimeoutError):
                last_error = "Connexion interrompue ou délai dépassé"

            if attempt < 5:
                await asyncio.sleep(min(2 ** (attempt + 1), 60))

        raise RuntimeError(last_error)

    async def current_slot(self):
        result = await self.call("getSlot", [{"commitment": COMMITMENT}])
        return int(result)

    async def history(self, address, start_slot, end_slot=None, token=None):
        options = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1,
            "commitment": COMMITMENT,
            "sortOrder": "asc",
            "limit": 1000,
            "filters": {
                "status": "succeeded",
                "tokenAccounts": "none",
                "slot": {"gte": start_slot},
            },
        }

        if end_slot is not None:
            options["filters"]["slot"]["lte"] = end_slot

        if token:
            options["paginationToken"] = token

        return await self.call("getTransactionsForAddress", [address, options])