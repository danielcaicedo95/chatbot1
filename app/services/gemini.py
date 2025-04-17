import httpx
from app.config import GOOGLE_API_KEY

# Esta lista guardará los mensajes anteriores y el prompt actual
message_history = []

async def ask_gemini_with_history(prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash-lite:generateContent?key={GOOGLE_API_KEY}"

    # Añadir el nuevo mensaje al historial
    message_history.append({
        "parts": [{"text": prompt}]
    })

    # Limitamos el tamaño del historial a 15 mensajes
    if len(message_history) > 15:
        message_history.pop(0)  # Eliminamos el mensaje más antiguo

    # Preparamos el cuerpo con el historial de mensajes
    body = {
        "contents": message_history
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body)
        result = response.json()
        print("Respuesta de Gemini:", result)

        try:
            # Aquí extraemos la respuesta generada por Gemini
            return result['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print("Error extrayendo respuesta de Gemini:", e)
            return "Lo siento, hubo un error generando la respuesta."
