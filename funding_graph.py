"""Deux schémas : classique ou DEV -> N TAMPONS -> DEV.
DEV bleu ; TAMPON gris. Rôles inférés selon un modèle, pas vérifiés par l'API.
"""
import csv
import hashlib
import html
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

import networkx as nx
import requests
from dotenv import load_dotenv
from pyvis.network import Network


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

LAMPORTS_PER_SOL = 1_000_000_000
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Taille d'une page API, et non limite totale par adresse.
PAGE_SIZE = 1000

# Cadence volontairement modérée ; aucun plafond total d'appels.
REQUEST_INTERVAL = 0.3
MAX_ATTEMPTS = 6


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def utc_date(timestamp):
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).isoformat()


def sol_text(lamports):
    return format(
        Decimal(lamports) / Decimal(LAMPORTS_PER_SOL),
        ".9f",
    )


def valid_address(address):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

    if not 32 <= len(address) <= 44:
        return False

    value = 0
    for character in address:
        if character not in alphabet:
            return False
        value = value * 58 + alphabet.index(character)

    leading_zeros = len(address) - len(address.lstrip("1"))
    byte_length = (value.bit_length() + 7) // 8
    return leading_zeros + byte_length == 32


class HeliusRPC:
    def __init__(self, api_key):
        self.api_key = api_key
        self.url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        self.session = requests.Session()
        self.last_request = 0.0
        self.calls = 0
        self.cache_hits = 0

    def close(self):
        self.session.close()

    def call(self, method, params):
        last_problem = "Erreur inconnue"

        for attempt in range(MAX_ATTEMPTS):
            wait = REQUEST_INTERVAL - (
                time.monotonic() - self.last_request
            )
            if wait > 0:
                time.sleep(wait)

            self.last_request = time.monotonic()
            self.calls += 1

            try:
                response = self.session.post(
                    self.url,
                    json={
                        "jsonrpc": "2.0",
                        "id": str(self.calls),
                        "method": method,
                        "params": params,
                    },
                    timeout=(15, 180),
                )
            except requests.RequestException:
                last_problem = "Erreur réseau ou délai dépassé"
            else:
                status = response.status_code

                if status == 429 or status >= 500:
                    last_problem = f"Helius HTTP {status}"
                elif status != 200:
                    raise RuntimeError(
                        f"Helius HTTP {status}. Vérifie l'accès à la "
                        "méthode, ta clé et tes crédits dans le dashboard."
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        last_problem = "Réponse JSON invalide"
                    else:
                        error = payload.get("error")

                        if error:
                            code = error.get("code")
                            message = str(error.get("message", ""))
                            message = message.replace(
                                self.api_key, "[CLE_MASQUEE]"
                            )

                            if code in (429, -32005):
                                last_problem = f"RPC {code}: {message}"
                            else:
                                raise RuntimeError(
                                    f"RPC {code}: {message}"
                                )
                        elif "result" not in payload:
                            raise RuntimeError(
                                "Réponse RPC sans champ result."
                            )
                        else:
                            return payload["result"]

            if attempt < MAX_ATTEMPTS - 1:
                delay = min(2 ** (attempt + 1), 60)
                print(
                    f"  {last_problem}. Nouvelle tentative "
                    f"dans {delay} secondes..."
                )
                time.sleep(delay)

        raise RuntimeError(
            f"{last_problem}. Arrêt après {MAX_ATTEMPTS} essais. "
            "Relance le script pour reprendre depuis le cache."
        )

    def history_page(self, wallet, token, snapshot_slot, cache_dir):
        options = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1,
            "commitment": "finalized",
            "sortOrder": "asc",
            "limit": PAGE_SIZE,
            "filters": {
                "status": "succeeded",
                "tokenAccounts": "none",
                "slot": {"lte": snapshot_slot},
            },
        }

        if token is not None:
            options["paginationToken"] = token

        params = [wallet, options]
        fingerprint = hashlib.sha256(
            json.dumps(
                params, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

        path = cache_dir / f"{fingerprint}.json"

        if path.exists():
            self.cache_hits += 1
            result = read_json(path)
        else:
            result = self.call("getTransactionsForAddress", params)

            if not isinstance(result, dict):
                raise RuntimeError(
                    "Format getTransactionsForAddress inattendu."
                )

            if not isinstance(result.get("data"), list):
                raise RuntimeError(
                    "La réponse ne contient pas une liste data."
                )

            save_json(path, result)

        return result


def extract_transfers(entry):
    transaction = entry.get("transaction")
    meta = entry.get("meta")

    if not isinstance(transaction, dict) or not isinstance(meta, dict):
        raise RuntimeError(
            "Une transaction complète n'a pas le format attendu. "
            "Collecte arrêtée pour éviter un résultat silencieusement incomplet."
        )

    if meta.get("err") is not None:
        return []

    signatures = transaction.get("signatures") or []
    if not signatures:
        raise RuntimeError("Transaction sans signature.")

    signature = signatures[0]
    message = transaction.get("message") or {}
    positioned = []

    for index, instruction in enumerate(message.get("instructions") or []):
        positioned.append((f"outer:{index}", instruction))

    for group in meta.get("innerInstructions") or []:
        parent_index = group.get("index")
        for index, instruction in enumerate(group.get("instructions") or []):
            positioned.append(
                (f"inner:{parent_index}:{index}", instruction)
            )

    transfers = []

    for position, instruction in positioned:
        is_system = (
            instruction.get("program") == "system"
            or instruction.get("programId") == SYSTEM_PROGRAM
        )
        if not is_system:
            continue

        parsed = instruction.get("parsed")

        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"Instruction System non décodée dans {signature}. "
                "Arrêt pour ne pas omettre silencieusement un mouvement."
            )

        if parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue

        info = parsed.get("info") or {}
        source = info.get("source")
        destination = info.get("destination")
        amount = info.get("lamports")

        if not source or not destination or amount is None:
            raise RuntimeError(
                f"Transfert incomplet dans {signature}."
            )

        amount = int(amount)
        if amount <= 0 or source == destination:
            continue

        transfers.append({
            "transfer_id": f"{signature}:{position}",
            "source": source,
            "destination": destination,
            "lamports": amount,
            "sol": sol_text(amount),
            "timestamp": entry.get("blockTime"),
            "date_utc": utc_date(entry.get("blockTime")),
            "slot": entry.get("slot"),
            "signature": signature,
            "instruction_position": position,
        })

    return transfers


def explore(rpc, config, cache_dir, graph, transfers):
    root = config["root"]
    max_depth = config["depth"]
    minimum = config["minimum_lamports"]

    graph.add_node(root, depth=0, status="a_analyser", transactions=0)
    if config.get("schema", 1) == 2:
        graph.nodes[root].update(role="dev", role_source="modele_utilisateur")
    queue = deque([root])

    while queue:
        wallet = queue.popleft()
        node = graph.nodes[wallet]
        depth = node["depth"]

        if depth >= max_depth:
            node["status"] = "limite_profondeur"
            continue

        node["status"] = "en_cours"
        print(f"\nNiveau {depth} — {wallet}")

        token = None
        seen_tokens = set()
        page_number = 0

        try:
            while True:
                result = rpc.history_page(
                    wallet,
                    token,
                    config["snapshot_slot"],
                    cache_dir,
                )

                entries = result["data"]
                page_number += 1

                for entry in entries:
                    movements = extract_transfers(entry)
                    node["transactions"] += 1

                    for movement in movements:
                        if movement["source"] != wallet:
                            continue
                        if movement["lamports"] < minimum:
                            continue

                        transfer_id = movement["transfer_id"]

                        if transfer_id in transfers:
                            continue

                        transfers[transfer_id] = movement
                        destination = movement["destination"]

                        if destination not in graph:
                            child_depth = depth + 1
                            graph.add_node(
                                destination,
                                depth=child_depth,
                                status=(
                                    "a_analyser"
                                    if child_depth < max_depth
                                    else "limite_profondeur"
                                ),
                                transactions=0,
                            )

                            if config.get("schema", 1) == 2:
                                position = child_depth % (config["tampon_count"] + 1)
                                graph.nodes[destination].update(
                                    role="dev" if position == 0 else "tampon",
                                    role_source="modele_utilisateur",
                                    role_primary_position=position,
                                )
                            if child_depth < max_depth:
                                queue.append(destination)

                        if not graph.has_edge(wallet, destination):
                            graph.add_edge(
                                wallet,
                                destination,
                                lamports=0,
                                count=0,
                            )

                        edge = graph[wallet][destination]
                        edge["lamports"] += movement["lamports"]
                        edge["count"] += 1

                print(
                    f"  Page {page_number} : {len(entries)} transactions"
                    f" | total adresse : {node['transactions']}"
                    f" | adresses découvertes : {graph.number_of_nodes()}"
                )

                next_token = result.get("paginationToken")
                if not next_token:
                    break

                if next_token in seen_tokens or next_token == token:
                    raise RuntimeError(
                        "Jeton de pagination répété : "
                        "historique non déclaré complet."
                    )

                seen_tokens.add(next_token)
                token = next_token

            node["status"] = "analyse"
            print("  Historique renvoyé par Helius parcouru.")

        except KeyboardInterrupt:
            node["status"] = "interrompu"
            raise
        except Exception:
            node["status"] = "erreur"
            raise


def ask_integer(label, default, minimum=0):
    while True:
        try:
            value = int(input(f"{label} [{default}] : ").strip() or str(default))
            if value >= minimum:
                return value
        except ValueError:
            pass
        print(f"Saisis un entier supérieur ou égal à {minimum}.")


def choose_schema():
    print("1. Schéma 1 : graphe classique des transferts sortants")
    print("2. Schéma 2 : DEV -> N TAMPONS -> DEV (motif répété)")
    while True:
        choice = ask_integer("Schéma", 1, 1)
        if choice in (1, 2):
            return choice
        print("Choisis 1 ou 2.")


def pattern_text(count):
    if count > 10:
        return f"DEV -> {count} TAMPONS -> DEV (puis répétition)"
    return " -> ".join(["DEV"] + [f"TAMPON {i}" for i in range(1, count + 1)] + ["DEV"])


def apply_roles(graph, config):
    """Classe selon la distance modulo (N+1), sans vérifier une activité dev.

    Recherche les positions possibles sur les parcours orientés collectés,
    cycles compris, jusqu'à la profondeur de collecte. Un état = adresse +
    position dans le motif. Un même état n'est développé qu'une fois.
    """
    if config.get("schema", 1) != 2:
        return
    count = config["tampon_count"]
    period = count + 1
    root = config["root"]
    if root not in graph:
        return
    distances = {(root, 0): 0}
    queue = deque([(root, 0)])
    positions_by_address = {root: {0: 0}}
    while queue:
        address, position = queue.popleft()
        distance = distances[(address, position)]
        if distance >= config["depth"]:
            continue
        next_position = (position + 1) % period
        for target in graph.successors(address):
            state = (target, next_position)
            if state not in distances:
                distances[state] = distance + 1
                positions_by_address.setdefault(target, {})[next_position] = distance + 1
                queue.append(state)
    for address, attrs in graph.nodes(data=True):
        available = positions_by_address.get(address, {})
        if not available:
            attrs.update(role="non_classe", role_conflict=False, role_candidates=[],
                         role_positions={}, role_source="modele_utilisateur")
            continue
        primary = min(available, key=available.get)
        candidates = sorted({"dev" if position == 0 else "tampon" for position in available})
        attrs.update(
            role="dev" if primary == 0 else "tampon",
            role_primary_position=primary,
            role_primary_distance=available[primary],
            role_candidates=candidates,
            role_conflict=len(candidates) > 1,
            role_multiple_positions=len(available) > 1,
            role_positions={str(k): v for k, v in sorted(available.items())},
            role_source="modele_utilisateur",
            role_rule=f"distance modulo {period} : 0 = dev ; 1 à {count} = tampon",
            role_activity_verified=False,
        )


def style_node(address, attrs, config):
    root = address == config["root"]
    short = f"{address[:6]}…{address[-5:]}"
    title = (
        f"Adresse : {html.escape(address)}<br>"
        f"Distance minimale : {attrs['depth']} transfert(s)<br>"
        f"Collecte : {html.escape(attrs['status'])}<br>"
        f"Transactions examinées : {attrs['transactions']}"
    )
    if config.get("schema", 1) == 1:
        colors = {"analyse": "#22c55e", "a_analyser": "#94a3b8",
                  "limite_profondeur": "#60a5fa", "en_cours": "#facc15",
                  "interrompu": "#f59e0b", "erreur": "#ef4444"}
        return dict(label=short, title=title, color="#a855f7" if root else colors[attrs["status"]],
                    size=28 if root else 17)
    role = attrs.get("role", "non_classe")
    position = attrs.get("role_primary_position", 0)
    conflict = attrs.get("role_conflict", False)
    label_role = "DEV" if role == "dev" else f"TAMPON {position}" if role == "tampon" else "NON CLASSÉ"
    label = f"{label_role}\n{short}" + ("\nCONFLIT" if conflict else "")
    background = {"dev": "#2563eb", "tampon": "#9ca3af", "non_classe": "#64748b"}[role]
    title += (
        f"<br>Rôle selon schéma : {label_role}"
        f"<br>Nombre de tampons par motif : {config['tampon_count']}"
        "<br>Source du rôle : modèle utilisateur, pas information API"
        "<br>Création de tokens : non vérifiée"
    )
    positions = attrs.get("role_positions", {})
    if attrs.get("role_multiple_positions"):
        details = "; ".join(("DEV" if key == "0" else f"TAMPON {key}") + f" : distance {dist}"
                            for key, dist in positions.items())
        title += "<br>Positions possibles : " + details
    if conflict:
        title += ("<br>CONFLIT : DEV et TAMPON possibles selon les parcours."
                  "<br>Fond = rôle à la distance minimale ; cycles compris."
                  "<br>Chronologie non vérifiée.")
    border = "#f59e0b" if conflict else "#ffffff" if root else background
    return dict(label=label, title=title, size=28 if root else 19,
                shape="diamond" if root else "dot", borderWidth=4 if conflict or root else 1,
                color={"background": background, "border": border,
                       "highlight": {"background": background, "border": "#ffffff"},
                       "hover": {"background": background, "border": "#ffffff"}})


def legend(config, outcome):
    text = (
        '<aside style="background:#172033;color:#f8fafc;padding:14px 20px;'
        'font:14px Arial;line-height:1.6;border:1px solid #475569">'
        f'Schéma {config.get("schema", 1)} — Collecte : {html.escape(outcome)} — '
        f'Seuil individuel : {html.escape(config["minimum_sol"])} SOL<br>'
    )
    if config.get("schema", 1) == 2:
        text += (
            '<span style="color:#60a5fa">DEV : bleu</span> | '
            '<span style="color:#d1d5db">TAMPON : gris</span> | '
            '<span style="color:#f59e0b">Bordure orange : conflit de rôle</span><br>'
            f'Motif choisi : {html.escape(pattern_text(config["tampon_count"]))}<br>'
            'La racine est supposée DEV. Rôles attribués selon votre modèle, '
            'pas vérifiés par l’API. Le motif se répète tant que la profondeur le permet.<br>'
            'En cas de conflit, le fond garde le rôle du parcours le plus court. '
            'Cycles compris dans la profondeur demandée ; chronologie non vérifiée.<br>'
            'Ni preuve de circulation des mêmes fonds, ni preuve de création de tokens '
            'ou de contrôle commun des adresses.'
        )
    else:
        text += 'Schéma classique : couleurs selon le statut de collecte.'
    return text + '</aside>'


def render_html(folder, config, graph, outcome):
    network = Network(height="900px", width="100%", directed=True,
                      bgcolor="#101827", font_color="#eeeeee", notebook=False,
                      cdn_resources="in_line")
    network.heading = html.escape(f"Schéma {config.get('schema', 1)} — Transferts SOL — {outcome}")
    levels = {}
    for address, attrs in graph.nodes(data=True):
        levels.setdefault(attrs["depth"], []).append(address)
    for depth, addresses in sorted(levels.items()):
        for index, address in enumerate(sorted(addresses)):
            network.add_node(address, **style_node(address, graph.nodes[address], config),
                             x=(index - (len(addresses) - 1) / 2) * 240, y=depth * 190)
    for source, destination, attrs in graph.edges(data=True):
        amount = sol_text(attrs["lamports"])
        network.add_edge(source, destination, arrows="to", label=f"{amount} SOL", width=2,
                         title=f"{amount} SOL retenus<br>{attrs['count']} transfert(s)<br>Signatures dans le CSV")
    network.set_options(json.dumps({
        "layout": {"hierarchical": {"enabled": False}}, "physics": {"enabled": False},
        "interaction": {"hover": True, "navigationButtons": True, "keyboard": True},
        "edges": {"smooth": {"enabled": True, "type": "curvedCW", "roundness": 0.1},
                  "font": {"size": 11, "color": "#f8fafc", "strokeWidth": 0}},
    }))
    document = network.generate_html(notebook=False)
    if not isinstance(document, str) or not document.strip():
        raise RuntimeError("PyVis a généré un HTML vide.")
    body_start = document.lower().find("<body")
    body_end = document.find(">", body_start) if body_start >= 0 else -1
    if body_end < 0:
        raise RuntimeError("HTML PyVis invalide : balise body absente.")
    document = document[:body_end+1] + legend(config, outcome) + document[body_end+1:]
    path = folder / "graphe.html"
    temporary = folder / "graphe.html.tmp"
    temporary.write_text(document, encoding="utf-8")
    if temporary.stat().st_size == 0:
        raise RuntimeError("HTML temporaire vide.")
    os.replace(temporary, path)
    print(f"HTML généré : {path}\nTaille : {path.stat().st_size:,} octets")


def export_results(folder, config, graph, transfers, outcome, rpc):
    apply_roles(graph, config)
    rows = sorted(
        transfers.values(),
        key=lambda row: (
            row["slot"] if row["slot"] is not None else -1,
            row["signature"],
            row["instruction_position"],
        ),
    )

    fields = [
        "transfer_id", "source", "destination",
        "lamports", "sol", "date_utc", "slot",
        "signature", "instruction_position",
    ]

    with (folder / "transferts.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "configuration": config,
        "outcome": outcome,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "http_attempts_this_launch": rpc.calls,
        "cached_pages_this_launch": rpc.cache_hits,
        "nodes": [
            {"id": address, **attributes}
            for address, attributes in graph.nodes(data=True)
        ],
        "edges": [
            {
                "source": source,
                "destination": destination,
                **attributes,
            }
            for source, destination, attributes
            in graph.edges(data=True)
        ],
        "transfers": rows,
    }
    save_json(folder / "graphe.json", report)

    if config.get("schema", 1) == 2:
        roles = [{"address": address, "depth": attrs["depth"],
                  **{k: v for k, v in attrs.items() if k.startswith("role")}}
                 for address, attrs in graph.nodes(data=True)]
        save_json(folder / "roles_schema2.json", roles)
        conflicts = [r for r in roles if r["role_conflict"]]
        save_json(folder / "conflits_roles.json", conflicts)
        print(f"Motif : {pattern_text(config['tampon_count'])}")
        print(f"DEV : {sum(r['role'] == 'dev' for r in roles)} | "
              f"TAMPON : {sum(r['role'] == 'tampon' for r in roles)} | "
              f"Conflits : {len(conflicts)}")
    save_json(folder / "visualisation.json", {"status": "en_cours", "collection_outcome": outcome})
    try:
        render_html(folder, config, graph, outcome)
    except Exception as error:
        # Ne jamais présenter l'ancien HTML comme le rendu de la collecte actuelle.
        old_html = folder / "graphe.html"
        if old_html.exists():
            os.replace(old_html, folder / "graphe_precedent.html")
        save_json(folder / "visualisation.json", {"status": "erreur", "collection_outcome": outcome,
                  "message": type(error).__name__})
        raise RuntimeError("JSON/CSV disponibles, mais génération HTML échouée : " + type(error).__name__) from error
    save_json(folder / "visualisation.json", {"status": "terminee", "collection_outcome": outcome})


def main():
    api_key = os.getenv("HELIUS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("HELIUS_API_KEY manque dans le fichier .env.")

    schema = choose_schema()
    tampon_count = ask_integer("Nombre exact de tampons entre deux DEV", 1, 1) if schema == 2 else None
    if schema == 2:
        print("Motif suivi : " + pattern_text(tampon_count))
        print("Les rôles sont inférés selon le modèle, sans vérification de l'activité dev.")
    root = input("Adresse Solana de départ : ").strip()
    if not valid_address(root):
        raise ValueError("Adresse Solana invalide.")

    default_depth = tampon_count + 1 if schema == 2 else 2
    depth = ask_integer("Profondeur en nombre de transferts", default_depth, 0)
    if schema == 2 and depth < tampon_count + 1:
        print("Attention : profondeur insuffisante pour atteindre le DEV suivant dans le motif choisi.")

    raw_minimum = (
        input("Minimum par transfert sortant en SOL [1] : ")
        .strip()
        .replace(",", ".")
        or "1"
    )
    minimum_sol = Decimal(raw_minimum)

    if not minimum_sol.is_finite() or minimum_sol <= 0:
        raise ValueError("Le minimum doit être strictement positif.")

    minimum_lamports = int(
        (minimum_sol * LAMPORTS_PER_SOL).to_integral_value(
            rounding=ROUND_CEILING
        )
    )

    identity = {
        "schema": schema,
        "root": root,
        "depth": depth,
        "minimum_lamports": minimum_lamports,
    }
    if schema == 2:
        identity["tampon_count"] = tampon_count
    identifier = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    folder = BASE_DIR / "resultats_funding" / identifier
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "configuration.json"
    cache_dir = folder / "cache"

    rpc = HeliusRPC(api_key)
    graph = nx.DiGraph()
    transfers = {}

    try:
        reuse = False
        if manifest.exists():
            answer = input("Reprendre le snapshot existant depuis le cache ? [O/n] : ").strip().lower()
            reuse = answer in ("", "o", "oui", "y", "yes")
        if reuse:
            config = read_json(manifest)
            print(
                "\nExploration existante : reprise depuis le cache."
                f"\nSnapshot au slot : {config['snapshot_slot']}"
            )
        else:
            snapshot_slot = rpc.call(
                "getSlot", [{"commitment": "finalized"}]
            )
            config = {
                **identity,
                "minimum_sol": sol_text(minimum_lamports),
                "snapshot_slot": snapshot_slot,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            save_json(manifest, config)

        print(f"\nDossier des résultats : {folder}")
        print("Ctrl+C : interrompre et exporter le graphe partiel.")
        print(
            "Pas de limite totale d'adresses, de transactions ou d'appels."
        )

        outcome = "termine"

        try:
            explore(
                rpc, config, cache_dir, graph, transfers
            )
        except KeyboardInterrupt:
            outcome = "interrompu"
            print("\nInterruption demandée.")
        except Exception as error:
            outcome = "incomplet_erreur"
            safe_error = str(error).replace(api_key, "[CLE_MASQUEE]")
            print(f"\nCollecte arrêtée : {safe_error}")
        finally:
            print("\nExport du résultat...")
            export_results(
                folder, config, graph, transfers, outcome, rpc
            )

        print(f"\nÉtat : {outcome}")
        print(f"Adresses enregistrées : {graph.number_of_nodes()}")
        print(f"Relations orientées : {graph.number_of_edges()}")
        print(f"Transferts retenus : {len(transfers)}")
        print(f"Tentatives HTTP ce lancement : {rpc.calls}")
        print(f"Pages relues depuis le cache : {rpc.cache_hits}")
        print(f"\nOuvre dans ton navigateur : {folder / 'graphe.html'}")
        print("Les preuves individuelles sont dans transferts.csv.")

    finally:
        rpc.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, InvalidOperation, RuntimeError) as error:
        print(f"\nErreur : {error}")
    except KeyboardInterrupt:
        print("\nArrêt.")