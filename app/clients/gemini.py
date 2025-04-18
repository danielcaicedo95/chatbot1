import httpx
from app.core.config import GOOGLE_API_KEY

async def ask_gemini_with_history(history_messages: list[dict]) -> str:
    url = (
        "https://generativelanguage.googleapis.com"
        f"/v1/models/gemini-2.0-flash-lite:generateContent?key={GOOGLE_API_KEY}"
    )

    # 🧠 Prompt inicial para guiar la conversación
    system_prompt = {
        "role": "user",  # Gemini no permite 'system'
        "parts": [{
            "text": (
                "Eres un vendedor de una licorera llamada licores el roble, "
                
            )
        }]
    }

    # 🧾 Construir el historial para enviar a Gemini
    contents = [system_prompt] + [
        {"role": msg["role"], "parts": [{"text": msg["text"]}]}
        for msg in history_messages
    ]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"contents": contents})
            result = response.json()

            print("🧠 Respuesta completa de Gemini:", result)

            # ✅ Extraer texto de forma segura
            if "candidates" in result and result["candidates"]:
                return result["candidates"][0]["content"]["parts"][0]["text"]

            print("⚠️ Respuesta sin candidatos válidos.")
            return "Lo siento, no pude generar una respuesta en este momento."

    except httpx.HTTPError as e:
        print("❌ Error HTTP al llamar a Gemini:", str(e))
        return "Hubo un problema de conexión al generar la respuesta."

    except Exception as e:
        print("❌ Error inesperado en Gemini:", str(e))
        return "Lo siento, ocurrió un error al generar la respuesta."
