import httpx
from app.core.config import GOOGLE_API_KEY

async def ask_gemini_with_history(history_messages: list[dict]) -> str:
    url = (
        "https://generativelanguage.googleapis.com"
        f"/v1/models/gemini-2.0-flash-lite:generateContent?key={GOOGLE_API_KEY}"
    )

    system_prompt = {
        "role": "user",
        "parts": [
            {
                "text": (
                    "Eres un asesor de ventas experto que NO comienza vendiendo, sino identificando necesidades del cliente, "
                    "haciendo preguntas estratégicas. Luego guías la conversación de forma natural, creando confianza. "
                    "Finalmente, haces una oferta relevante y manejas objeciones con empatía, sin presionar. Tu tono es amable, profesional y persuasivo. "
                    "Evita sonar robótico."
                )
            }
        ]
    }

    contents = [system_prompt] + [
        {"role": msg["role"], "parts": [{"text": msg["text"]}]}
        for msg in history_messages
    ]

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"contents": contents})
        result = resp.json()
        print("Respuesta de Gemini:", result)

        try:
            return result['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print("Error extrayendo respuesta de Gemini:", e)
            return "Lo siento, hubo un error generando la respuesta."
