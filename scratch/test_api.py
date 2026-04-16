import asyncio
from google import genai
from config import LIVE_CONFIG, MODEL_ID, GEMINI_API_KEY
client = genai.Client(api_key=GEMINI_API_KEY)

async def test():
    async with client.aio.live.connect(model=MODEL_ID, config=LIVE_CONFIG) as session:
        await session.send_realtime_input(text="End the call and save to CRM. Here is my fit score: 99, summary: good, next steps: contract.")
        
        async for response in session.receive():
            if getattr(response, "tool_call", None) is not None:
                print("FOUND IN tool_call")
            if getattr(response.server_content, "model_turn", None) is not None:
                for part in response.server_content.model_turn.parts:
                    if getattr(part, "function_call", None) is not None:
                        print("FOUND IN model_turn.parts[x].function_call")

asyncio.run(test())
