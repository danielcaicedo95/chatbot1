# app/services/gemini.py
import httpx
from app.config import GOOGLE_API_KEY

async def ask_gemini_with_history(history_messages: list[dict]) -> str:
    url = (
        "https://generativelanguage.googleapis.com"
        f"/v1/models/gemini-2.0-flash-lite:generateContent?key={GOOGLE_API_KEY}"
    )

    # Construimos el array de contents con role + parts
    contents = [
        {"role": msg["role"], "parts": [{"text": msg["text"]}]}
        for msg in history_messages
    ]

    body = {"contents": contents}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=body)
        result = resp.json()
        print("Respuesta de Gemini:", result)

        try:
            return result['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print("Error extrayendo respuesta de Gemini:", e)
            return "Lo siento, hubo un error generando la respuesta."
