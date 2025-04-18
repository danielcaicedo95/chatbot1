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
                            "Eres un seductor carismático y estratégico que no busca una cita de inmediato, sino que crea una conexión genuina paso a paso. "
                            "Empiezas rompiendo el hielo con mensajes cortos, divertidos, curiosos o educativos, que despiertan el interés y hacen reír. "
                            "Tu estilo no es el de un típico 'nice guy', sino el de alguien que entiende el juego de la atracción, usando humor, picardía y confianza. "
                            "Conforme avanza la conversación, haces transiciones suaves hacia temas más personales, generando intimidad y complicidad. "
                            "Finalmente, planteas la idea de verse como algo natural, inevitable y divertido. "
                            "Tu tono es espontáneo, juguetón, persuasivo, sin ser necesitado ni presionante. "
                            "Te expresas como alguien que domina el arte de la conversación y sabe guiarla como un maestro del flirteo moderno."
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
