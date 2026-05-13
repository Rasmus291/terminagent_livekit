import os, asyncio
from dotenv import load_dotenv
load_dotenv()
from livekit.api import LiveKitAPI, ListSIPOutboundTrunkRequest

async def main():
    url = os.getenv("LIVEKIT_URL", "")
    key = os.getenv("LIVEKIT_API_KEY", "")
    secret = os.getenv("LIVEKIT_API_SECRET", "")
    async with LiveKitAPI(url, key, secret) as lk:
        # First get current trunk to preserve all fields
        resp = await lk.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
        trunks = list(resp.items) if hasattr(resp, "items") else list(resp)
        trunk = trunks[0]
        
        # Update address to de1
        trunk.address = "lavita-livekit.pstn.de1.twilio.com"
        
        result = await lk.sip.update_outbound_trunk(trunk.sip_trunk_id, trunk)
        print(f"Updated trunk address: {result.address}")
        print(f"Trunk ID: {result.sip_trunk_id}")
        print(f"Name: {result.name}")

asyncio.run(main())
