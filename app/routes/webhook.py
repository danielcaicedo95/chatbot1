from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

VERIFY_TOKEN = "gemini-bot-token"

router = APIRouter()

@router.get("/webhook")
async def verify_webhook(hub_mode: str = None, hub_challenge: str = None, hub_verify_token: str = None):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return JSONResponse(content={"hub.challenge": hub_challenge})
    return JSONResponse(content={"error": "Invalid verification token"}, status_code=403)

@router.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()
    print("Mensaje recibido:", body)
    return {"status": "received"}
