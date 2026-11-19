"""Tests locaux uniquement : python -m unittest -v test_funding_live.py"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp
from funding_core import Store, build_graph, extract, normalize_notification
from funding_live import Tracker, serve, demo_entry


def config(**overrides):
    return {"mode": "historique", "schema": 2, "tampon_count": 2,
            "root": "A", "depth": 6, "minimum_lamports": 1_000_000_000,
            "start_slot": 100, "end_slot": 200, **overrides}


def movement(signature, source, target, slot):
    return extract(demo_entry(signature, source, target, slot))[0]


class CoreTests(unittest.TestCase):
    def test_patterns_zero_one_two_three(self):
        for count in (0, 1, 2, 3):
            names = ["A"] + [f"N{i}" for i in range(1, 9)]
            rows = [movement(str(i), names[i], names[i+1], 101+i) for i in range(8)]
            graph = build_graph(config(tampon_count=count, depth=8), rows)
            nodes = {n["id"]: n for n in graph["nodes"]}
            self.assertEqual([nodes[n]["role"] for n in names],
                             ["dev" if i % (count+1) == 0 else "tampon" for i in range(9)])
            self.assertEqual(nodes["A"]["color"], "#2563eb")
            if count:
                self.assertEqual(nodes["N1"]["color"], "#9ca3af")

    def test_out_of_order_and_pre_funding_excluded(self):
        rows = [movement("before", "X", "Z", 101), movement("later", "X", "B", 104),
                movement("root", "A", "X", 103)]
        graph = build_graph(config(), rows)
        self.assertEqual({n["id"] for n in graph["nodes"]}, {"A", "X", "B"})
        self.assertEqual(len(graph["transfers"]), 2)

    def test_same_slot_is_explicitly_unresolved(self):
        graph = build_graph(config(), [movement("a", "A", "X", 101), movement("b", "X", "B", 101)])
        self.assertEqual({n["id"] for n in graph["nodes"]}, {"A", "X"})
        self.assertEqual(len(graph["same_slot_unresolved"]), 1)

    def test_depth_and_conflicts(self):
        rows = [movement("1", "A", "X", 101), movement("2", "X", "Y", 102),
                movement("3", "Y", "B", 103), movement("4", "A", "B", 104)]
        graph = build_graph(config(depth=3), rows)
        node = next(n for n in graph["nodes"] if n["id"] == "B")
        self.assertTrue(node["conflict"])
        self.assertEqual(node["role"], "tampon")
        shallow = build_graph(config(depth=1), rows)
        self.assertEqual(len(shallow["transfers"]), 2)
        self.assertEqual(shallow["watched"], ["A"])

    def test_storage_dedup_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"db.sqlite"
            store = Store(path, config())
            event = demo_entry("sig", "A", "X", 101)
            store.ingest([event, event])
            state = {"last_complete": 99, "low": 100, "high": 200, "token": "p2"}
            store.save_scan("A", state)
            store.close()
            store = Store(path)
            self.assertEqual(len(store.rows()), 1)
            self.assertEqual(store.scan("A"), state)
            store.close()

    def test_notification_and_internal_instructions(self):
        entry = demo_entry("sig", "A", "X", 101)
        envelope = {"slot": 101, "signature": "sig", "transactionIndex": 3,
                    "transaction": {"transaction": entry["transaction"], "meta": entry["meta"]}}
        self.assertEqual(extract(normalize_notification(envelope))[0]["transaction_index"], 3)
        entry["meta"]["innerInstructions"] = [{"index": 0, "instructions": [
            {"program": "system", "parsed": {"type": "transfer", "info": {
                "source": "X", "destination": "B", "lamports": 2_000_000_000}}}]}]
        rows = extract(entry)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["transfer_id"], rows[1]["transfer_id"])


class FakeRPC:
    calls = 0
    def __init__(self):
        self.options = []
    async def call(self, method, params):
        self.calls += 1
        self.options.append(params[1])
        if "paginationToken" not in params[1]:
            return {"data": [demo_entry("1", "A", "X", 101)], "paginationToken": "p2"}
        return {"data": [demo_entry("2", "X", "B", 102)], "paginationToken": None}
    def safe(self, error):
        return str(error)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            store=Store(folder/"db.sqlite",config())
            rpc=FakeRPC()
            tracker=Tracker(store,rpc,folder)
            await tracker.scan_wallet("A",200)
            self.assertEqual(len(store.rows()),2)
            self.assertEqual(store.scan("A")["last_complete"],200)
            self.assertIsNone(store.scan("A")["high"])
            self.assertEqual(rpc.options[1]["paginationToken"],"p2")
            tracker.export()
            self.assertGreater((folder/"graphe.html").stat().st_size,100)
            self.assertIn("window.SNAPSHOT=",(folder/"graphe.html").read_text(encoding="utf-8"))
            store.close()

    async def test_restart_from_saved_page(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);store=Store(folder/"db.sqlite",config())
            store.ingest([demo_entry("1","A","X",101)])
            store.save_scan("A",{"last_complete":99,"low":100,"high":200,"token":"p2"})
            rpc=FakeRPC();tracker=Tracker(store,rpc,folder)
            await tracker.scan_wallet("A",250)
            self.assertEqual(rpc.options[0]["paginationToken"],"p2")
            self.assertEqual(rpc.options[0]["filters"]["slot"]["lte"],200)
            self.assertEqual(store.scan("A")["last_complete"],200)
            store.close()

    async def test_local_http_interface(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);store=Store(folder/"db.sqlite",config())
            tracker=Tracker(store,None,folder)
            runner=await serve(tracker,0)
            try:
                port=runner.addresses[0][1]
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{port}/api/graph") as response:
                        self.assertEqual(response.status,200)
                        self.assertEqual((await response.json())["nodes"][0]["id"],"A")
                    async with session.get(f"http://127.0.0.1:{port}/") as response:
                        self.assertIn("<canvas",await response.text())
                    async with session.get(f"http://127.0.0.1:{port}/export/csv") as response:
                        self.assertIn("transfer_id",await response.text())
            finally:
                await runner.cleanup()
                store.close()

    async def test_dynamic_websocket_with_fake_transport(self):
        class Message:
            type=aiohttp.WSMsgType.TEXT
            def __init__(self,data):self.data=json.dumps(data)
        class Socket:
            def __init__(self):self.queue=asyncio.Queue();self.requests=[];self.counter=10
            async def send_json(self,request):
                self.requests.append(request)
                if request["method"]=="transactionSubscribe":
                    self.counter+=1
                    await self.queue.put(Message({"id":request["id"],"result":self.counter}))
                    if len([r for r in self.requests if r['method']=='transactionSubscribe'])==1:
                        entry=demo_entry("live","A","X",101)
                        await self.queue.put(Message({"method":"transactionNotification","params":{"result":{
                            "slot":101,"signature":"live","transaction":{
                                "transaction":entry["transaction"],"meta":entry["meta"]}}}}))
            async def receive(self,timeout):return await asyncio.wait_for(self.queue.get(),timeout)
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        socket=Socket()
        class Session:
            def ws_connect(self,*args,**kwargs):return socket
        class RPC:
            calls=0;key="dummy"
            session=Session()
            def safe(self,error):return str(error)
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);store=Store(folder/"db.sqlite",config(mode="direct",end_slot=None))
            tracker=Tracker(store,RPC(),folder)
            task=asyncio.create_task(tracker.websocket_loop())
            try:
                for _ in range(100):
                    await asyncio.sleep(.02)
                    if tracker.status['subscribed_addresses']==2:break
                self.assertEqual(tracker.status['subscribed_addresses'],2)
                subscriptions=[r for r in socket.requests if r['method']=='transactionSubscribe']
                self.assertEqual(subscriptions[-1]['params'][0]['accountInclude'],['A','X'])
                self.assertTrue(any(r['method']=='transactionUnsubscribe' for r in socket.requests))
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True);store.close()


if __name__ == "__main__":
    unittest.main()
