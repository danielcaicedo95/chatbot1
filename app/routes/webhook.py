from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

VERIFY_TOKEN = "gemini-bot-token"

router = APIRouter()

@router.get("/webhook")
async def verify_webhook(request: Request):
    hub_mode = request.query_params.get("hub.mode")
    hub_challenge = request.query_params.get("hub.challenge")
    hub_verify_token = request.query_params.get("hub.verify_token")

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge, status_code=200)
    return PlainTextResponse(content="Invalid verification token", status_code=403)
