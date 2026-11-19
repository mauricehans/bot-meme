"""Moteur temporel et stockage ; aucune dependance reseau."""
import csv
import io
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

SYSTEM = "11111111111111111111111111111111"
LAMPORTS = 1_000_000_000


def sol(value):
    return format(Decimal(value) / LAMPORTS, ".9f")


def valid_address(address):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not 32 <= len(address) <= 44:
        return False
    value = 0
    for char in address:
        if char not in alphabet:
            return False
        value = value * 58 + alphabet.index(char)
    return len(address) - len(address.lstrip("1")) + (value.bit_length() + 7) // 8 == 32


def normalize_notification(result):
    # Envelope transactionSubscribe : result.transaction contient transaction + meta.
    bundle = result.get("transaction")
    if not isinstance(bundle, dict) or "meta" not in bundle:
        raise ValueError("Notification Helius inattendue : transaction/meta absents.")
    tx = bundle.get("transaction")
    if not isinstance(tx, dict):
        raise ValueError("Notification non jsonParsed : transaction non decodee.")
    if not tx.get("signatures") and result.get("signature"):
        tx = {**tx, "signatures": [result["signature"]]}
    return {"transaction": tx, "meta": bundle["meta"], "slot": result["slot"],
            "transactionIndex": result.get("transactionIndex"), "blockTime": result.get("blockTime")}


def extract(entry):
    tx, meta = entry.get("transaction"), entry.get("meta")
    if not isinstance(tx, dict) or not isinstance(meta, dict):
        raise ValueError("Transaction complete invalide : traitement refuse.")
    if meta.get("err") is not None:
        return []
    signatures = tx.get("signatures") or []
    if not signatures or entry.get("slot") is None:
        raise ValueError("Signature ou slot absent.")
    signature = signatures[0]
    positioned = [(f"outer:{i}", ins) for i, ins in enumerate((tx.get("message") or {}).get("instructions") or [])]
    for group in meta.get("innerInstructions") or []:
        positioned.extend((f"inner:{group['index']}:{i}", ins) for i, ins in enumerate(group.get("instructions") or []))
    rows = []
    for position, ins in positioned:
        if ins.get("program") != "system" and ins.get("programId") != SYSTEM:
            continue
        parsed = ins.get("parsed")
        if not isinstance(parsed, dict):
            raise ValueError(f"Instruction System non decodee dans {signature}.")
        if parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = parsed.get("info") or {}
        if not info.get("source") or not info.get("destination") or info.get("lamports") is None:
            raise ValueError(f"Transfert incomplet dans {signature}.")
        amount = int(info["lamports"])
        if amount <= 0 or info["source"] == info["destination"]:
            continue
        rows.append({"transfer_id": f"{signature}:{position}", "source": info["source"],
                     "destination": info["destination"], "lamports": amount, "sol": sol(amount),
                     "slot": int(entry["slot"]), "signature": signature,
                     "transaction_index": entry.get("transactionIndex"),
                     "instruction_position": position, "timestamp": entry.get("blockTime")})
    return rows


class Store:
    def __init__(self, path, config=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS movements(id TEXT PRIMARY KEY,slot INTEGER NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS movement_slot ON movements(slot);
        CREATE TABLE IF NOT EXISTS scans(wallet TEXT PRIMARY KEY,last_complete INTEGER NOT NULL,
            low INTEGER, high INTEGER, token TEXT);
        """)
        if config is not None and self.get_meta("config") is None:
            self.set_meta("config", config)
        self.config = self.get_meta("config")
        if self.config is None:
            raise ValueError("Configuration de session absente.")

    def close(self):
        self.db.close()

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(value)))

    def ingest(self, entries):
        rows = []
        cfg = self.config
        for entry in entries:
            rows.extend(row for row in extract(entry)
                        if row["slot"] >= cfg["start_slot"] and row["lamports"] >= cfg["minimum_lamports"]
                        and (cfg.get("end_slot") is None or row["slot"] <= cfg["end_slot"]))
        with self.db:
            for row in rows:
                existing = self.db.execute("SELECT payload FROM movements WHERE id=?", (row["transfer_id"],)).fetchone()
                if existing:
                    old = json.loads(existing[0])
                    for key in ("timestamp", "transaction_index"):
                        if row.get(key) is None:
                            row[key] = old.get(key)
                self.db.execute("INSERT OR REPLACE INTO movements VALUES(?,?,?)",
                                (row["transfer_id"], row["slot"], json.dumps(row)))
        return len(rows)

    def rows(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM movements ORDER BY slot,id")]

    def scan(self, wallet):
        row = self.db.execute("SELECT last_complete,low,high,token FROM scans WHERE wallet=?", (wallet,)).fetchone()
        if row is None:
            return {"last_complete": self.config["start_slot"]-1, "low": None, "high": None, "token": None}
        return dict(zip(("last_complete", "low", "high", "token"), row))

    def save_scan(self, wallet, state):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?,?,?)", (
                wallet, state["last_complete"], state["low"], state["high"], state["token"]))


def build_graph(config, rows):
    """Suivi conservateur : chaque etape doit etre dans un slot strictement ulterieur.

    Une arrivee minimale par adresse et longueur suffit a determiner si un
    mouvement peut prolonger un parcours. Les evenements peuvent arriver en
    desordre : tout est recalcule a partir du journal persistant.
    """
    root, limit = config["root"], config["depth"]
    period = config.get("tampon_count", 0) + 1
    ordered = sorted(rows, key=lambda t: (t["slot"], t["transfer_id"]))
    arrivals = [{root: config["start_slot"]-1}] + [{} for _ in range(limit)]
    accepted = {}
    ambiguous = {}
    for depth in range(limit):
        previous, following = arrivals[depth], arrivals[depth+1]
        for row in ordered:
            arrival = previous.get(row["source"])
            if arrival is None:
                continue
            if row["slot"] > arrival:
                target = row["destination"]
                following[target] = min(following.get(target, row["slot"]), row["slot"])
                accepted[row["transfer_id"]] = row
            elif row["slot"] == arrival:
                ambiguous[row["transfer_id"]] = row
    for tid in accepted:
        ambiguous.pop(tid, None)
    positions = defaultdict(list)
    for depth, mapping in enumerate(arrivals):
        for address, arrival in mapping.items():
            positions[address].append((depth, arrival))
    nodes = []
    for address, possibilities in positions.items():
        possibilities.sort()
        depth = possibilities[0][0]
        roles = {"dev" if d % period == 0 else "tampon" for d, _ in possibilities}
        role = ("dev" if depth % period == 0 else "tampon") if config["schema"] == 2 else "adresse"
        conflict = len(roles) > 1 if config["schema"] == 2 else False
        position = depth % period
        label = "DEV" if role == "dev" else f"TAMPON {position}" if role == "tampon" else "ADRESSE"
        nodes.append({"id": address, "depth": depth, "role": role, "position": position,
                      "role_candidates": sorted(roles) if config["schema"] == 2 else [],
                      "conflict": conflict, "positions": [d for d, _ in possibilities],
                      "arrival_slots": {str(d): slot for d, slot in possibilities},
                      "label": label, "root": address == root,
                      "color": "#2563eb" if role == "dev" else "#9ca3af" if role == "tampon" else "#60a5fa",
                      "source_role": "modele_utilisateur" if config["schema"] == 2 else "sans_classification"})
    edges = {}
    for row in accepted.values():
        key = (row["source"], row["destination"])
        edge = edges.setdefault(key, {"source": key[0], "destination": key[1], "lamports": 0,
                                      "count": 0, "transfer_ids": []})
        edge["lamports"] += row["lamports"]
        edge["count"] += 1
        edge["transfer_ids"].append(row["transfer_id"])
    for edge in edges.values():
        edge["sol"] = sol(edge["lamports"])
    return {"configuration": config, "nodes": sorted(nodes, key=lambda n: (n["depth"], n["id"])),
            "edges": list(edges.values()), "transfers": sorted(accepted.values(), key=lambda t: (t["slot"], t["transfer_id"])),
            "same_slot_unresolved": list(ambiguous.values()), "stored_movements": len(rows),
            "temporal_policy": "slots_strictement_croissants; meme_slot_non_propage",
            "watched": sorted(n["id"] for n in nodes if n["depth"] < limit)}


def csv_text(rows):
    output = io.StringIO(newline="")
    fields = ["transfer_id", "source", "destination", "lamports", "sol", "slot", "signature",
              "transaction_index", "instruction_position", "timestamp"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
