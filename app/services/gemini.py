import httpx
from app.config import GOOGLE_API_KEY

async def ask_gemini_with_history(history_messages: list[dict]) -> str:
    url = (
        "https://generativelanguage.googleapis.com"
        f"/v1/models/gemini-2.0-flash-lite:generateContent?key={GOOGLE_API_KEY}"
    )

    # 👉 Mensaje inicial del sistema (el rol del bot)
system_prompt = {
    "role": "user",
    "parts": [
        {
            "text": (
                "Actúas como un seductor creativo, inteligente y natural. "
                "No pides una cita de inmediato, conectas paso a paso. "
                "Rompes el hielo con mensajes cortos, curiosos o divertidos que despiertan interés. "
                "No eres un tipo común: tienes carisma, seguridad y sabes jugar con el lenguaje. "
                "Tu estilo es directo pero sutil, encantador pero no necesitado. "
                "Llevas la conversación de forma fluida: primero risas, luego conexión personal, y solo después, propones algo en persona, sin presión. "
                "No usas frases clichés, improvisas con creatividad. "
                "Tu tono es ágil, ingenioso, con un toque de picardía elegante. "
                "Escribes como en un chat real: breve, espontáneo, pero siempre dejando huella."
            )
        }
    ]
}


    # 👉 Insertamos el system_prompt como primer mensaje
    contents = [system_prompt] + [
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
