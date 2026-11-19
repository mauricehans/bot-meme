from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .engine import HeliusClient, build_graph, valid_address
from .forms import AddAddressForm, TrackingForm
from .models import Transfer, WatchedAddress

RUNNING_TRACKINGS = {}


async def helius_history(client, tracking, address):
    token = None
    total = 0

    while True:
        result = await client.history(
            address,
            tracking.start_slot,
            token=token,
        )
        entries = result.get("data", [])

        for entry in entries:
            for transfer in extract_transfers(entry):
                if transfer["lamports"] < tracking.minimum_lamports:
                    continue

                Transfer.objects.update_or_create(
                    tracking=tracking,
                    transfer_id=transfer["transfer_id"],
                    defaults={
                        "source": transfer["source"],
                        "destination": transfer["destination"],
                        "lamports": transfer["lamports"],
                        "slot": transfer["slot"],
                        "signature": transfer["signature"],
                        "instruction_position": transfer["instruction_position"],
                        "timestamp": transfer["timestamp"],
                    },
                )
                total += 1

        token = result.get("paginationToken")
        if not token:
            break

    return total


async def start_tracking_task(tracking_id):
    tracking = await Tracking.objects.aget(id=tracking_id)
    client = HeliusClient()
    await client.start()

    try:
        while True:
            addresses = [item.address for item in await tracking.addresses.all()]
            graph = build_graph(tracking, await tracking.transfers.all())

            for address in graph["watched"]:
                if address not in addresses:
                    continue

                await helius_history(client, tracking, address)

            await asyncio.sleep(30)
    finally:
        await client.stop()