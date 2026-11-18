import argparse
import csv
import json
import os
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import networkx as nx
from pyvis.network import Network

BASE_DIR = Path(__file__).resolve().parent
STATUSES = {"dev_confirme", "aucune_creation_observee", "inconnu"}
COLORS = {"dev_confirme": "#a855f7", "aucune_creation_observee": "#2563eb", "inconnu": "#94a3b8"}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_text(path, content):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def save_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def sol(amount):
    return format(Decimal(amount) / Decimal(1_000_000_000), ".9f")


def annotations_from(path):
    if not path.exists():
        save_json(path, {})
    result = load_json(path)
    if not isinstance(result, dict):
        raise ValueError("Les annotations doivent être un objet JSON indexé par adresse.")
    for address, item in result.items():
        if not isinstance(item, dict) or item.get("dev_status", "inconnu") not in STATUSES:
            raise ValueError(f"Annotation invalide pour {address}.")
    return result


def make_graph(report, annotations):
    graph = nx.DiGraph()
    for node in report.get("nodes", []):
        attributes = dict(node)
        address = attributes.pop("id")
        graph.add_node(address, **attributes)
    for edge in report.get("edges", []):
        graph.add_edge(edge["source"], edge["destination"],
                       lamports=int(edge.get("lamports", 0)), count=int(edge.get("count", 0)))
    for address in graph:
        annotation = annotations.get(address, {})
        graph.nodes[address].update(
            dev_status=annotation.get("dev_status", "inconnu"),
            notes=str(annotation.get("notes", "")),
            evidence=annotation.get("evidence", []),
            coverage=annotation.get("coverage", {}),
        )
    return graph


def classify_links(graph):
    devs = {n for n, d in graph.nodes(data=True) if d["dev_status"] == "dev_confirme"}
    links = []
    intermediaries = set()
    for source in sorted(devs):
        # Un parcours par dev ; les autres devs sont des terminaux de segment.
        parents = {}
        queue = deque()
        found = {}
        for target in sorted(graph.successors(source)):
            if target == source:
                continue
            if target in devs:
                links.append({"schema": 1, "source": source, "destination": target,
                              "path": [source, target], "intermediate_count": 0})
            else:
                parents[target] = source
                queue.append(target)
        while queue:
            current = queue.popleft()
            for target in sorted(graph.successors(current)):
                if target == source:
                    continue
                if target in devs:
                    if target not in found:
                        reverse = [current]
                        while parents[reverse[-1]] != source:
                            reverse.append(parents[reverse[-1]])
                        path = [source] + list(reversed(reverse)) + [target]
                        found[target] = path
                    continue
                if target not in parents:
                    parents[target] = current
                    queue.append(target)
        for target, path in sorted(found.items()):
            middle = path[1:-1]
            intermediaries.update(middle)
            unknown = any(graph.nodes[n]["dev_status"] == "inconnu" for n in middle)
            links.append({"schema": 2, "source": source, "destination": target,
                          "path": path, "intermediate_count": len(middle),
                          "qualification": "activite_dev_inconnue_sur_le_chemin" if unknown else "aucune_creation_observee_sur_les_intermediaires",
                          "temporal_validation": "non_effectuee"})
    return links, intermediaries


def write_network(graph, path, heading, root=None, intermediaries=None, summary=False):
    intermediaries = intermediaries or set()
    net = Network(height="900px", width="100%", directed=True, notebook=False,
                  bgcolor="#0f172a", font_color="#f8fafc", cdn_resources="in_line")
    net.heading = heading
    # Mise en page par niveaux déterministe, sans simulation ou arbre imposé.
    levels = {}
    for address, item in graph.nodes(data=True):
        levels.setdefault(int(item.get("depth", 0)), []).append(address)
    for depth, addresses in sorted(levels.items()):
        for index, address in enumerate(sorted(addresses)):
            item = graph.nodes[address]
            status = item.get("dev_status", "inconnu")
            role = "intermediaire sur un chemin selectionne" if address in intermediaries else "adresse"
            title = (f"{address}\nActivite : {status}\nRole : {role}"
                     f"\nCollecte : {item.get('status', 'inconnu')}"
                     f"\nNiveau : {depth}\nNotes : {item.get('notes', '')}")
            net.add_node(address, label=f"{address[:6]}...{address[-5:]}", title=title,
                         color=COLORS[status], shape="diamond" if address == root else "dot",
                         size=25 if status == "dev_confirme" else 16,
                         x=(index - (len(addresses)-1)/2)*240, y=depth*200)
    for source, target, item in graph.edges(data=True):
        if summary:
            kinds = item["schemas"]
            label = " + ".join(f"schema {k}" for k in kinds)
            title = "\n\n".join(
                f"Schema {p['schema']} : " + " -> ".join(p["path"])
                for p in item["links"]
            )
            title += "\nLien de connectivite, chronologie non verifiee."
            indirect_only = kinds == [2]
        else:
            label = f"{sol(item['lamports'])} SOL"
            title = f"{label}\n{item['count']} transfert(s) retenu(s)"
            indirect_only = False
        net.add_edge(source, target, arrows="to", label=label, title=title,
                     dashes=indirect_only, color="#94a3b8", width=2)
    net.set_options(json.dumps({
        "physics": {"enabled": False},
        "layout": {"hierarchical": {"enabled": False}},
        "interaction": {"hover": True, "navigationButtons": True, "keyboard": True},
        "edges": {"smooth": {"enabled": True, "type": "curvedCW", "roundness": 0.12},
                  "font": {"size": 10, "strokeWidth": 0, "color": "#e2e8f0"}}
    }))
    document = net.generate_html(notebook=False)
    if not document.strip() or "<html" not in document.lower():
        raise RuntimeError("PyVis n'a pas produit un document HTML valide.")
    atomic_text(path, document)


def build(report_path, annotations_path=None):
    report_path = Path(report_path).resolve()
    annotations_path = Path(annotations_path or BASE_DIR / "annotations_adresses.json").resolve()
    report = load_json(report_path)
    annotations = annotations_from(annotations_path)
    graph = make_graph(report, annotations)
    if not graph:
        raise ValueError("Aucune adresse dans le graphe source.")
    links, intermediaries = classify_links(graph)
    root = report.get("configuration", {}).get("root")
    folder = report_path.parent / "schema2"
    folder.mkdir(exist_ok=True)
    transfers_by_pair = {}
    for transfer in report.get("transfers", []):
        key = (transfer["source"], transfer["destination"])
        transfers_by_pair.setdefault(key, []).append(transfer)
    for link in links:
        link["steps"] = [
            {"source": a, "destination": b,
             "transfers": transfers_by_pair.get((a, b), [])}
            for a, b in zip(link["path"], link["path"][1:])
        ]
    enriched = dict(report)
    enriched.update({
        "annotation_file": str(annotations_path),
        "schema2_generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": [{"id": n, **d} for n, d in graph.nodes(data=True)],
        "dev_links": links,
        "path_policy": "un_plus_court_chemin_indirect_par_paire_sans_dev_confirme_interieur",
        "temporal_validation": "non_effectuee",
    })
    save_json(folder / "graphe_annote.json", enriched)
    save_json(folder / "liens_dev.json", links)
    unclassified = [{"address": n, "depth": d.get("depth"), "collect_status": d.get("status")}
                    for n, d in graph.nodes(data=True) if d["dev_status"] == "inconnu"]
    save_json(folder / "adresses_a_qualifier.json", unclassified)
    with (folder / "liens_dev.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["schema", "source", "destination", "intermediate_count", "path", "temporal_validation"])
        for link in links:
            writer.writerow([link["schema"], link["source"], link["destination"],
                             link["intermediate_count"], " -> ".join(link["path"]), "non_effectuee"])
    write_network(graph, folder / "graphe_detaille.html",
                  "Transferts SOL | violet : dev confirme | bleu : aucune creation observee | gris : inconnu", root, intermediaries)
    summary = nx.DiGraph()
    for n, d in graph.nodes(data=True):
        if d["dev_status"] == "dev_confirme":
            summary.add_node(n, **d)
    for link in links:
        a, b = link["source"], link["destination"]
        if not summary.has_edge(a, b):
            summary.add_edge(a, b, schemas=[], links=[])
        edge = summary[a][b]
        edge["schemas"].append(link["schema"])
        edge["links"].append(link)
    if summary:
        write_network(summary, folder / "graphe_devs.html",
                      "Relations entre devs annotes | liens indirects, pas des transferts directs", root, summary=True)
    elif (folder / "graphe_devs.html").exists():
        (folder / "graphe_devs.html").unlink()
    print(f"Resultats : {folder}")
    print(f"Adresses : {len(graph)} | devs confirmes : {len(summary)}")
    print(f"Schema 1 : {sum(x['schema']==1 for x in links)} | schema 2 : {sum(x['schema']==2 for x in links)}")
    if len(summary) < 2:
        print("Annote au moins deux adresses dev confirmees pour rechercher des liens entre devs.")
    print("Aucune requete Helius effectuee. Chronologie des chemins non verifiee.")
    return folder


def main():
    parser = argparse.ArgumentParser(description="Schema 2 : annotations et relations indirectes entre devs.")
    parser.add_argument("graphe", nargs="?", help="Chemin de graphe.json existant")
    parser.add_argument("--annotations", default=str(BASE_DIR / "annotations_adresses.json"))
    args = parser.parse_args()
    source = args.graphe
    if not source:
        files = sorted((BASE_DIR / "resultats_funding").glob("*/graphe.json"))
        if not files:
            raise ValueError("Aucun graphe.json : effectue d'abord une collecte.")
        for index, path in enumerate(files, 1):
            print(f"{index}. {path.parent.name}")
        selection = int(input("Numero du graphe a annoter : "))
        if not 1 <= selection <= len(files):
            raise ValueError("Numero invalide.")
        source = files[selection-1]
    build(source, args.annotations)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        raise SystemExit(f"Erreur : {error}")
