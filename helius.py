import csv
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("HELIUS_API_KEY", "").strip()

if not API_KEY:
    raise SystemExit("Erreur : HELIUS_API_KEY manque dans le fichier .env.")

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={API_KEY}"

LAMPORTS_PER_SOL = Decimal("1000000000")
SESSION = requests.Session()
LAST_REQUEST = 0.0


def rpc(method, params):
    global LAST_REQUEST

    for attempt in range(5):
        # Environ 4 requêtes/s au maximum.
        delay = 0.25 - (time.monotonic() - LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)

        try:
            LAST_REQUEST = time.monotonic()
            response = SESSION.post(
                RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
                timeout=30,
            )
        except requests.RequestException:
            if attempt == 4:
                raise RuntimeError(
                    "Connexion à Helius impossible après plusieurs essais."
                ) from None
            time.sleep(2 ** attempt)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 4:
                raise RuntimeError(
                    f"Helius indisponible ou limité : HTTP {response.status_code}."
                )
            time.sleep(2 ** attempt)
            continue

        if response.status_code != 200:
            raise RuntimeError(
                f"Erreur HTTP {response.status_code}. "
                "Vérifie ta clé et les quotas dans le dashboard Helius."
            )

        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError("Helius a renvoyé une réponse JSON invalide.") from None

        error = payload.get("error")
        if error:
            if error.get("code") == 429 and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"Erreur RPC {error.get('code')} : {error.get('message')}"
            )

        return payload.get("result")

    raise RuntimeError("Nombre maximal de tentatives atteint.")


def validate_address(address):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not address or any(char not in alphabet for char in address):
        return False

    value = 0
    for char in address:
        value = value * 58 + alphabet.index(char)

    decoded_length = (value.bit_length() + 7) // 8
    leading_zeros = len(address) - len(address.lstrip("1"))
    return decoded_length + leading_zeros == 32


def fetch_signatures(address, maximum):
    signatures = []
    before = None

    while len(signatures) < maximum:
        options = {
            "limit": min(1000, maximum - len(signatures)),
            "commitment": "finalized",
        }
        if before:
            options["before"] = before

        batch = rpc("getSignaturesForAddress", [address, options]) or []

        if not batch:
            return signatures, True

        signatures.extend(batch)
        before = batch[-1]["signature"]

        print(f"Signatures récupérées : {len(signatures)}")

    # On ne prétend pas avoir remonté tout l'historique.
    return signatures, False


def get_sol_transfers(transaction, signature, wallet):
    if not transaction:
        return []

    meta = transaction.get("meta") or {}
    if meta.get("err") is not None:
        return []

    message = transaction.get("transaction", {}).get("message", {})
    instructions = list(message.get("instructions") or [])

    for group in meta.get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])

    block_time = transaction.get("blockTime")
    date = (
        datetime.fromtimestamp(block_time, timezone.utc).isoformat()
        if block_time is not None
        else ""
    )

    transfers = []

    for instruction in instructions:
        if instruction.get("program") != "system":
            continue

        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            continue

        if parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue

        info = parsed.get("info") or {}
        source = info.get("source")
        destination = info.get("destination")
        lamports = info.get("lamports")

        if not source or not destination or lamports is None:
            continue

        if wallet not in (source, destination):
            continue

        if source == destination:
            continue

        amount = int(lamports)
        if amount <= 0:
            continue

        transfers.append({
            "date_utc": date,
            "block_time": block_time,
            "direction": "ENTRANT" if destination == wallet else "SORTANT",
            "source": source,
            "destination": destination,
            "lamports": amount,
            "sol": str(Decimal(amount) / LAMPORTS_PER_SOL),
            "signature": signature,
        })

    return transfers


def show_summary(transfers, direction, title):
    totals = defaultdict(lambda: {"lamports": 0, "count": 0})

    for transfer in transfers:
        if transfer["direction"] != direction:
            continue

        counterparty = (
            transfer["source"]
            if direction == "ENTRANT"
            else transfer["destination"]
        )
        totals[counterparty]["lamports"] += transfer["lamports"]
        totals[counterparty]["count"] += 1

    print(f"\n{title}")
    print("-" * 90)

    if not totals:
        print("Aucun transfert détecté dans les transactions analysées.")
        return

    ranked = sorted(
        totals.items(),
        key=lambda item: item[1]["lamports"],
        reverse=True,
    )

    for address, stats in ranked:
        amount = Decimal(stats["lamports"]) / LAMPORTS_PER_SOL
        print(
            f"{address} | {amount:.9f} SOL "
            f"| {stats['count']} transfert(s)"
        )


def main():
    wallet = input("Adresse Solana à analyser : ").strip()

    if not validate_address(wallet):
        raise ValueError("Adresse Solana invalide.")

    raw_limit = input(
        "Nombre de dernières transactions à analyser [100] : "
    ).strip()
    maximum = int(raw_limit or "100")

    if not 1 <= maximum <= 10000:
        raise ValueError("Choisis un nombre entre 1 et 10000.")

    print("\nRécupération des signatures...")
    signatures, history_end = fetch_signatures(wallet, maximum)

    if not signatures:
        print("Aucune transaction trouvée.")
        return

    transfers = []
    unavailable = 0

    for index, entry in enumerate(signatures, start=1):
        print(
            f"\rAnalyse : {index}/{len(signatures)}",
            end="",
            flush=True,
        )

        if entry.get("err") is not None:
            continue

        transaction = rpc(
            "getTransaction",
            [
                entry["signature"],
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        )

        if transaction is None:
            unavailable += 1
            continue

        transfers.extend(
            get_sol_transfers(transaction, entry["signature"], wallet)
        )

    print()

    show_summary(
        transfers,
        "ENTRANT",
        "ADRESSES AYANT ENVOYÉ DES SOL À CE WALLET",
    )
    show_summary(
        transfers,
        "SORTANT",
        "ADRESSES AUXQUELLES CE WALLET A ENVOYÉ DES SOL",
    )

    incoming = [
        transfer for transfer in transfers
        if transfer["direction"] == "ENTRANT"
        and transfer["block_time"] is not None
    ]

    if incoming:
        oldest = min(incoming, key=lambda item: item["block_time"])
        print("\nPLUS ANCIEN TRANSFERT ENTRANT OBSERVÉ")
        print(f"Expéditeur : {oldest['source']}")
        print(f"Montant    : {oldest['sol']} SOL")
        print(f"Date UTC   : {oldest['date_utc']}")
        print(f"Signature  : {oldest['signature']}")
        print("Ce n'est pas une preuve du financeur d'origine.")

    output = BASE_DIR / f"helius_transfers_{wallet}.csv"
    fields = [
        "date_utc", "direction", "source", "destination",
        "lamports", "sol", "signature",
    ]

    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            sorted(
                transfers,
                key=lambda item: item["block_time"] or 0,
            )
        )

    print(f"\nExport CSV : {output.name}")
    print(f"Transferts détectés : {len(transfers)}")
    print(f"Transactions indisponibles : {unavailable}")

    if history_end:
        print("Fin de l'historique renvoyé par le RPC atteinte.")
    else:
        print(
            "Limite d'analyse atteinte : "
            "les transactions plus anciennes n'ont pas été examinées."
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(f"\nErreur : {error}")
    except KeyboardInterrupt:
        print("\nAnalyse interrompue.")
    finally:
        SESSION.close()