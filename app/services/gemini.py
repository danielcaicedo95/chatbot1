import httpx
from app.config import GOOGLE_API_KEY

async def ask_gemini(prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-pro:generateContent?key={GOOGLE_API_KEY}"

    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body)
        result = response.json()
        print("Respuesta de Gemini:", result)

        try:
            return result['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print("Error extrayendo respuesta de Gemini:", e)
            return "Lo siento, hubo un error generando la respuesta."
