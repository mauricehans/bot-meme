import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
HELIUS_API_KEY = os.environ["HELIUS_API_KEY"]

BLOOM = os.environ.get("BLOOM_BOT", "BloomSolana_bot")
WALLET = os.environ["WALLET"]
NAME = os.environ.get("NAME", "bot")

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

client = TelegramClient("session_bloom", API_ID, API_HASH)

def show(msg, label):
    print(f"--- {label} ---")
    print(msg.text)
    for row in (msg.buttons or []):
        print([repr(b.text) for b in row])


async def last_msg():
    await asyncio.sleep(2)
    return (await client.get_messages(BLOOM, limit=1))[0]


async def main():
    await client.send_message(BLOOM, "/start")
    msg = await last_msg()
    show(msg, "après /start")

    await msg.click(text=lambda t: "Copy Trade" in t)
    msg = await last_msg()
    show(msg, "après Copy Trade")

    await msg.click(text=lambda t: "Add new config" in t)
    msg = await last_msg()
    show(msg, "après Add new config")

    await client.send_message(BLOOM, WALLET)
    msg = await last_msg()
    show(msg, "après envoi de l'adresse")

    if "name" in (msg.text or "").lower():
        await client.send_message(BLOOM, NAME)
        msg = await last_msg()
        show(msg, "après envoi du nom")
    else:
        print("Le bot ne demande pas de nom : voir le message ci-dessus.")


with client:
    client.loop.run_until_complete(main())