"""Suivi Helius local : python funding_live.py ; demonstration : --demo."""
import argparse
import asyncio
import json
import os
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

import aiohttp
from aiohttp import web
from funding_core import Store, build_graph, csv_text, normalize_notification, valid_address

BASE = Path(__file__).resolve().parent
COMMITMENT = "finalized"


def load_env():
    # Sous-ensemble volontaire : CLE=valeur et valeurs entre guillemets.
    path = BASE / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def integer(label, default, minimum=0):
    while True:
        try:
            value = int(input(f"{label} [{default}] : ").strip() or str(default))
            if value >= minimum:
                return value
        except ValueError:
            pass
        print(f"Entier >= {minimum} requis.")


def choice(label, default, allowed):
    while True:
        value = integer(label, default)
        if value in allowed:
            return value
        print("Choix possibles : " + ", ".join(map(str, allowed)))


def amount():
    while True:
        try:
            value = Decimal(input("Minimum par transfert SOL [1] : ").strip().replace(",", ".") or "1")
            if value.is_finite() and value > 0:
                return int((value * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))
        except InvalidOperation:
            pass
        print("Montant strictement positif requis.")


def atomic(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class RPC:
    def __init__(self, session, key):
        self.session, self.key = session, key
        self.url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.calls = 0

    def safe(self, error):
        return str(error).replace(self.key, "[CLE_MASQUEE]")

    async def call(self, method, params):
        problem = "Erreur reseau"
        for attempt in range(6):
            async with self.lock:
                await asyncio.sleep(max(0, .3 - (time.monotonic() - self.last)))
                self.last = time.monotonic()
                self.calls += 1
                try:
                    async with self.session.post(self.url, json={"jsonrpc": "2.0", "id": self.calls,
                            "method": method, "params": params}, timeout=aiohttp.ClientTimeout(total=180)) as response:
                        if response.status == 429 or response.status >= 500:
                            problem = f"HTTP {response.status}"
                        elif response.status != 200:
                            raise RuntimeError(f"HTTP {response.status} : verifier cle, droits et credits.")
                        else:
                            payload = await response.json()
                            if payload.get("error"):
                                error = payload["error"]
                                if error.get("code") not in (429, -32005):
                                    raise RuntimeError("RPC " + self.safe(error))
                                problem = "RPC " + self.safe(error)
                            elif "result" in payload:
                                return payload["result"]
                            else:
                                raise RuntimeError("Reponse RPC sans result.")
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    problem = "Connexion HTTP interrompue ou reponse invalide"
            if attempt < 5:
                await asyncio.sleep(min(2 ** (attempt+1), 60))
        raise RuntimeError(problem)

    async def slot(self):
        return int(await self.call("getSlot", [{"commitment": COMMITMENT}]))


class Tracker:
    def __init__(self, store, rpc, folder):
        self.store, self.rpc, self.folder = store, rpc, folder
        self.config = store.config
        self.dirty = True
        self.graph = build_graph(self.config, self.store.rows())
        self.status = {"websocket": "non_demarre", "backfill": "non_demarre", "error": "",
                       "http_attempts": 0, "subscribed_addresses": 0,
                       "updated_at": "", "collection_complete": False}

    def refresh(self):
        if self.dirty:
            self.graph = build_graph(self.config, self.store.rows())
            self.dirty = False
        self.status["http_attempts"] = self.rpc.calls if self.rpc else 0
        self.status["updated_at"] = datetime.now(timezone.utc).isoformat()

    def snapshot(self):
        self.refresh()
        scans = {wallet: self.store.scan(wallet) for wallet in self.graph["watched"]}
        return {**self.graph, "status": dict(self.status), "scans": scans,
                "session": self.folder.name,
                "warning": "Roles selon modele ; meme-slot non propage ; couverture fournisseur non garantie."}

    def ingest(self, entries):
        self.store.ingest(entries)
        self.dirty = True

    def export(self):
        snap = self.snapshot()
        atomic(self.folder / "graphe.json", json.dumps(snap, ensure_ascii=False, indent=2))
        atomic(self.folder / "transferts.csv", "\ufeff" + csv_text(snap["transfers"]))
        atomic(self.folder / "mouvements_collectes.csv", "\ufeff" + csv_text(self.store.rows()))
        atomic(self.folder / "configuration.json", json.dumps(self.config, ensure_ascii=False, indent=2))
        atomic(self.folder / "meme_slot_non_resolu.json", json.dumps(snap["same_slot_unresolved"], indent=2))
        # Echappement < pour eviter la fermeture prematuree d'un script HTML.
        payload = json.dumps(snap, ensure_ascii=False).replace("<", "\\u003c")
        document = (BASE / "interface.html").read_text(encoding="utf-8")
        document = document.replace("<!-- SNAPSHOT -->", "<script>window.SNAPSHOT=" + payload + ";</script>")
        atomic(self.folder / "graphe.html", document)

    async def scan_wallet(self, wallet, upper):
        state = self.store.scan(wallet)
        if state["high"] is None:
            state.update(low=max(self.config["start_slot"], state["last_complete"] - 64), high=upper, token=None)
            if state["low"] > state["high"]:
                return
            self.store.save_scan(wallet, state)
        seen_tokens = set()
        while True:
            options = {"transactionDetails": "full", "encoding": "jsonParsed",
                       "maxSupportedTransactionVersion": 1, "commitment": COMMITMENT,
                       "sortOrder": "asc", "limit": 1000,
                       "filters": {"status": "succeeded", "tokenAccounts": "none",
                                   "slot": {"gte": state["low"], "lte": state["high"]}}}
            if state["token"] is not None:
                options["paginationToken"] = state["token"]
            self.status["backfill"] = f"Analyse {wallet[:8]} | slots {state['low']}..{state['high']}"
            result = await self.rpc.call("getTransactionsForAddress", [wallet, options])
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise RuntimeError("Historique Helius : liste data absente.")
            self.ingest(result["data"])
            token = result.get("paginationToken")
            # Les mouvements sont commits AVANT le curseur : une reprise peut relire,
            # mais la cle primaire evite les doublons, sans sauter une page.
            if not token:
                state.update(last_complete=state["high"], low=None, high=None, token=None)
                self.store.save_scan(wallet, state)
                break
            if token == state["token"] or token in seen_tokens:
                raise RuntimeError("Pagination repetee ; fenetre non declaree complete.")
            seen_tokens.add(token)
            state["token"] = token
            self.store.save_scan(wallet, state)
            self.refresh()
            await asyncio.sleep(0)
        self.refresh()

    async def backfill_loop(self):
        interval = self.config.get("reconcile_seconds", 15)
        while True:
            try:
                upper = self.config.get("end_slot")
                if upper is None:
                    upper = await self.rpc.slot()
                self.refresh()
                if self.config["mode"] == "historique" and self.status.get("collection_complete"):
                    return
                wallets = list(self.graph["watched"])
                for wallet in wallets:
                    # Historique : les adresses achevees ne sont pas rescannes.
                    state = self.store.scan(wallet)
                    if self.config["mode"] == "historique" and state["high"] is None and state["last_complete"] >= upper:
                        continue
                    await self.scan_wallet(wallet, upper)
                self.refresh()
                self.status["error"] = ""
                if self.config["mode"] == "historique":
                    remaining = [w for w in self.graph["watched"]
                                 if self.store.scan(w)["last_complete"] < upper or self.store.scan(w)["high"] is not None]
                    if not remaining:
                        self.status.update(backfill="Historique termine", collection_complete=True)
                        self.export()
                        return
                    await asyncio.sleep(0)
                else:
                    self.status["backfill"] = "Tour termine ; verification periodique active"
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                message = self.rpc.safe(error)
                self.status.update(backfill="ERREUR : fenetre non complete", error=message)
                print("Rattrapage : " + message)
                await asyncio.sleep(15)

    async def websocket_loop(self):
        delay = 2
        while True:
            try:
                self.status["websocket"] = "connexion"
                url = f"wss://mainnet.helius-rpc.com/?api-key={self.rpc.key}"
                async with self.rpc.session.ws_connect(url, heartbeat=30, max_msg_size=0,
                         timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    self.status["websocket"] = "connecte ; abonnement en attente"
                    current, pending, request_id, active_id = (), None, 0, None
                    delay = 2
                    while True:
                        self.refresh()
                        wanted = tuple(self.graph["watched"])
                        if wanted != current and pending is None:
                            if len(wanted) > 50000:
                                raise RuntimeError("Filtre WebSocket > 50000 adresses ; partitionnement non implemente. Rattrapage HTTP maintenu.")
                            if wanted:
                                request_id += 1
                                await ws.send_json({"jsonrpc": "2.0", "id": request_id, "method": "transactionSubscribe",
                                    "params": [{"accountInclude": list(wanted), "failed": False, "vote": False},
                                               {"commitment": COMMITMENT, "encoding": "jsonParsed",
                                                "transactionDetails": "full", "maxSupportedTransactionVersion": 1}]})
                                pending = (request_id, wanted, time.monotonic())
                        if pending and time.monotonic() - pending[2] > 30:
                            raise RuntimeError("Accuse d'abonnement WebSocket absent.")
                        try:
                            msg = await ws.receive(timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("error"):
                                raise RuntimeError("Abonnement refuse : " + self.rpc.safe(data["error"]))
                            if pending and data.get("id") == pending[0]:
                                new_id = data.get("result")
                                if not isinstance(new_id, (int, str)) or isinstance(new_id, bool):
                                    raise RuntimeError("Identifiant d'abonnement invalide.")
                                previous_id = active_id
                                active_id, current = new_id, pending[1]
                                pending = None
                                self.status.update(websocket="actif", subscribed_addresses=len(current), websocket_error="")
                                if previous_id is not None:
                                    request_id += 1
                                    await ws.send_json({"jsonrpc": "2.0", "id": request_id,
                                                       "method": "transactionUnsubscribe", "params": [previous_id]})
                            elif data.get("method") == "transactionNotification":
                                result = data.get("params", {}).get("result", {})
                                self.ingest([normalize_notification(result)])
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            raise RuntimeError("Connexion WebSocket fermee.")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                message = self.rpc.safe(error)
                self.status.update(websocket="deconnecte/refuse ; HTTP de rattrapage maintenu", subscribed_addresses=0)
                self.status["websocket_error"] = message
                print("WebSocket : " + message)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def export_loop(self):
        while True:
            self.export()
            await asyncio.sleep(5)


async def serve(tracker, port):
    async def index(request):
        return web.FileResponse(BASE / "interface.html")
    async def graph(request):
        return web.json_response(tracker.snapshot(), headers={"Cache-Control": "no-store"})
    async def export_json(request):
        return web.Response(text=json.dumps(tracker.snapshot(), ensure_ascii=False, indent=2),
                            content_type="application/json", headers={"Content-Disposition": 'attachment; filename="graphe.json"'})
    async def export_csv(request):
        return web.Response(text="\ufeff"+csv_text(tracker.snapshot()["transfers"]), content_type="text/csv",
                            headers={"Content-Disposition": 'attachment; filename="transferts.csv"'})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/graph", graph)
    app.router.add_get("/export/json", export_json)
    app.router.add_get("/export/csv", export_csv)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def setup_store(rpc):
    directory = BASE / "sessions_funding"
    directory.mkdir(exist_ok=True)
    sessions = sorted(directory.glob("*/suivi.sqlite"))
    if sessions:
        print("0. Nouvelle session")
        for index, path in enumerate(sessions, 1):
            print(f"{index}. {path.parent.name}")
        selected = choice("Session", 0, range(len(sessions)+1))
        if selected:
            path = sessions[selected-1]
            store = Store(path)
            if store.config.get("mode") == "demo":
                store.close()
                raise ValueError("Session DEMO : utiliser --demo, ou creer une session reelle.")
            return store, path.parent
    print("1. Historique sur un intervalle de slots\n2. Direct avec rattrapage et reprise")
    mode = "historique" if choice("Mode", 2, (1, 2)) == 1 else "direct"
    print("1. Schema classique\n2. DEV -> N TAMPONS -> DEV")
    schema = choice("Schema", 2, (1, 2))
    tampon_count = integer("Nombre exact de tampons entre deux DEV", 1, 0) if schema == 2 else 0
    while True:
        root = input("Adresse initiale (supposee DEV en schema 2) : ").strip()
        if valid_address(root):
            break
        print("Adresse Solana invalide.")
    depth = integer("Profondeur en transferts", tampon_count+1 if schema == 2 else 2)
    minimum = amount()
    current = await rpc.slot()
    print(f"Slot finalise actuel : {current}")
    if mode == "direct":
        print("Entree = depuis ce slot. Saisir un slot anterieur pour reconstruire puis suivre en direct.")
        start = integer("Premier slot inclus", current)
        end = None
        if start > current:
            raise ValueError("Choisir un slot de depart <= slot actuel.")
    else:
        print("0 = tout l'historique disponible ; cela peut etre tres couteux.")
        start = integer("Premier slot inclus", 0)
        end = integer("Dernier slot inclus", current, start)
        if end > current:
            raise ValueError("Le dernier slot doit deja etre finalise.")
    reconcile_seconds = integer("Intervalle minimal entre tours de rattrapage HTTP (secondes)", 30, 1) if mode == "direct" else 30
    config = {"format_version": 1, "mode": mode, "schema": schema, "tampon_count": tampon_count,
              "root": root, "depth": depth, "minimum_lamports": minimum,
              "start_slot": start, "end_slot": end, "commitment": COMMITMENT,
              "reconcile_seconds": reconcile_seconds, "created_at": datetime.now(timezone.utc).isoformat()}
    folder = directory / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3))
    return Store(folder / "suivi.sqlite", config), folder


def demo_entry(signature, source, target, slot):
    return {"slot": slot, "transaction": {"signatures": [signature], "message": {"instructions": [
        {"program": "system", "parsed": {"type": "transfer", "info": {
            "source": source, "destination": target, "lamports": 2_000_000_000}}}]}}, "meta": {"err": None}}


async def demo(tracker):
    tracker.status.update(websocket="DEMONSTRATION SANS RESEAU HELIUS", backfill="simulation")
    for signature, source, target, slot in [
        ("demo3", "TAMPON_B", "DEV_B", 103),
        ("demo1", "DEV_A", "TAMPON_A", 101),
        ("demo2", "TAMPON_A", "TAMPON_B", 102),
        ("demo4", "DEV_B", "TAMPON_C", 104),
    ]:
        await asyncio.sleep(2)
        tracker.ingest([demo_entry(signature, source, target, slot)])
    tracker.status.update(backfill="Simulation terminee : graphe reconstitue malgre arrivee en desordre",
                          collection_complete=True)


async def run(args):
    load_env()
    tasks, runner, store, tracker = [], None, None, None
    async with aiohttp.ClientSession() as session:
        try:
            if args.demo:
                folder = BASE / "sessions_funding" / ("DEMO_" + secrets.token_hex(3))
                config = {"mode": "demo", "schema": 2, "tampon_count": 2, "root": "DEV_A",
                          "depth": 6, "minimum_lamports": 1_000_000_000, "start_slot": 100, "end_slot": 200}
                store = Store(folder / "suivi.sqlite", config)
                tracker = Tracker(store, None, folder)
            else:
                key = os.getenv("HELIUS_API_KEY", "").strip()
                if not key or key == "REMPLACER_PAR_VOTRE_CLE":
                    raise ValueError("Renseigner HELIUS_API_KEY dans .env.")
                rpc = RPC(session, key)
                store, folder = await setup_store(rpc)
                if store.config["mode"] == "demo":
                    raise ValueError("Session DEMO : utiliser --demo, ou creer une nouvelle session reelle.")
                tracker = Tracker(store, rpc, folder)
            runner = await serve(tracker, args.port)
            print(f"\nOuvrir http://127.0.0.1:{args.port} dans le navigateur.")
            print(f"Session : {folder}\nCtrl+C : arret et export. Ne pas fermer le terminal pendant le suivi.")
            print("Slots strictement croissants : les etapes dans un meme slot restent non resolues.")
            if args.demo:
                tasks.append(asyncio.create_task(demo(tracker)))
            else:
                tasks.append(asyncio.create_task(tracker.backfill_loop()))
                if tracker.config["mode"] == "direct":
                    tasks.append(asyncio.create_task(tracker.websocket_loop()))
                else:
                    tracker.status["websocket"] = "desactive (historique)"
            tasks.append(asyncio.create_task(tracker.export_loop()))
            # Une erreur de tache non interceptee arrete la session explicitement.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if tracker:
                tracker.export()
            if runner:
                await runner.cleanup()
            if store:
                store.close()


def main():
    parser = argparse.ArgumentParser(description="Graphe Helius historique ou direct")
    parser.add_argument("--demo", action="store_true", help="Interface et suivi simules sans cle API")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port entre 1 et 65535 requis.")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nArret ; journal SQLite et exports conserves.")
    except Exception as error:
        key = os.getenv("HELIUS_API_KEY", "")
        message = str(error).replace(key, "[CLE_MASQUEE]") if key else str(error)
        raise SystemExit("Erreur : " + message)


if __name__ == "__main__":
    main()
