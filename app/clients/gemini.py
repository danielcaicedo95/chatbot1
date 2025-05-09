# app/clients/gemini.py
import httpx
import asyncio
import time # Para el backoff
import json # Para decodificar errores JSON
import logging # Para un mejor logging
from app.core.config import GOOGLE_API_KEY # Cambiado de GEMINI_API_KEY a GOOGLE_API_KEY según tu código

logger = logging.getLogger(__name__) # Usar el logger del módulo

# Usar el modelo que especificaste. Si necesitas "gemini-pro" u otro, cámbialo aquí.
GEMINI_MODEL = "gemini-1.5-flash-latest" # Usar gemini-1.5-flash-latest que es más reciente o el que tengas acceso
# O "models/gemini-1.5-pro-latest" si es el Pro
# O "gemini-pro" si usas la v1beta

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com"
    # f"/v1beta/models/{GEMINI_MODEL}:generateContent?key={GOOGLE_API_KEY}" # Para v1beta
    f"/v1beta/models/{GEMINI_MODEL}:generateContent?key={GOOGLE_API_KEY}" # Para v1 (asegúrate que el endpoint sea correcto para tu modelo)

)


# Configuración de reintentos
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2 # Empezar con 2 segundos para el primer reintento
BACKOFF_FACTOR = 2 # Cada reintento duplica la espera (2s, 4s, 8s)
REQUEST_TIMEOUT_SECONDS = 45.0 # Aumentar un poco el timeout para dar margen a Gemini

async def ask_gemini_with_history(history_messages: list[dict], generation_config: dict = None) -> str:
    """
    Envía el historial de mensajes a la API de Gemini y devuelve la respuesta del modelo.
    Incluye reintentos con backoff exponencial para errores 503 y de red.
    """
    
    # 🧠 Prompt del sistema/instrucción inicial
    # Gemini usa "user" y "model" para los roles en `contents`.
    # El "system prompt" se puede simular como el primer mensaje del "user".
    system_instruction_text = (
        "Eres 'VendiBot', un vendedor experto y muy amigable de la licorera 'Licores El Roble'. "
        "Tu objetivo es ayudar a los clientes, responder sus preguntas sobre licores como un conocedor, "
        "y guiarlos en el proceso de compra. Tus respuestas deben ser humanas, conversacionales y concisas. "
        "Usa emojis para un tono cercano. ¡Haz que el cliente se sienta bien atendido!"
    )

    # Construir el historial para enviar a Gemini
    # El formato de `contents` para Gemini es una lista de objetos,
    # donde cada objeto tiene "role" ("user" o "model") y "parts".
    contents = [{"role": "user", "parts": [{"text": system_instruction_text}]}]
    
    for msg in history_messages:
        # Asegurarse que el rol sea "user" o "model"
        role = "model" if msg.get("role", "").lower() == "model" or msg.get("role", "").lower() == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg.get("text", "")}]})

    # Payload para la API de Gemini
    payload = {"contents": contents}

    # Configuración de generación (puedes ajustarla)
    final_generation_config = generation_config or {
        "temperature": 0.75, # Un poco más de creatividad pero no demasiado aleatorio
        "topP": 0.95,
        "topK": 40,
        "maxOutputTokens": 1500, # Permitir respuestas más largas si es necesario para resúmenes/JSON
        "stopSequences": [], # Puedes añadir secuencias de parada si es necesario
    }
    payload["generationConfig"] = final_generation_config
    
    # Configuración de seguridad (opcional, ajusta según tus necesidades)
    # payload["safetySettings"] = [
    #     {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    #     {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    #     {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    #     {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    # ]


    headers = {"Content-Type": "application/json"}
    
    current_retry = 0
    current_backoff = INITIAL_BACKOFF_SECONDS

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        while current_retry <= MAX_RETRIES: # Permitir MAX_RETRIES intentos
            try:
                logger.info(f"Intentando llamar a Gemini API (Intento {current_retry + 1}/{MAX_RETRIES + 1})")
                # No loguear el payload completo aquí si es muy grande o contiene PII.
                # logger.debug(f"Payload para Gemini: {json.dumps(payload, indent=2, ensure_ascii=False)}") 
                response = await client.post(GEMINI_API_URL, json=payload, headers=headers)
                
                # Loguear el status code y un resumen de la respuesta
                logger.info(f"Respuesta HTTP de Gemini: Status {response.status_code}")
                # logger.debug(f"Respuesta HTTP de Gemini (cuerpo): {response.text[:500]}...") # Primeros 500 chars

                response.raise_for_status()  # Lanza excepción para errores HTTP 4xx/5xx

                response_data = response.json()
                logger.info(f"🧠 Respuesta completa de Gemini (JSON): {json.dumps(response_data, indent=2, ensure_ascii=False)}")

                # Extracción segura del texto de la respuesta
                if "candidates" in response_data and response_data["candidates"]:
                    candidate = response_data["candidates"][0]
                    # Verificar si la generación fue bloqueada por seguridad u otro motivo
                    if candidate.get("finishReason") not in [None, "STOP", "MAX_TOKENS"]:
                        finish_reason = candidate.get("finishReason")
                        safety_ratings_info = ""
                        if "safetyRatings" in candidate:
                             safety_ratings_info = f" SafetyRatings: {candidate['safetyRatings']}"
                        logger.warning(f"Generación de Gemini finalizada por razón no estándar: {finish_reason}.{safety_ratings_info}")
                        # Devolver un mensaje que indique el problema, o el texto si aún hay.
                        if "content" in candidate and "parts" in candidate["content"] and candidate["content"]["parts"]:
                             # A veces puede haber texto parcial incluso con finishReason diferente a STOP
                            partial_text = candidate["content"]["parts"][0].get("text", "")
                            if partial_text:
                                return partial_text + f" (Advertencia: Generación con finalización '{finish_reason}')"
                        return f"GEMINI_RESPONSE_ISSUE: La respuesta fue afectada. Razón: {finish_reason}."


                    if "content" in candidate and "parts" in candidate["content"] and candidate["content"]["parts"]:
                        text_response = candidate["content"]["parts"][0].get("text", "")
                        if not text_response.strip(): # Si el texto está vacío o solo espacios
                            logger.warning("Respuesta de Gemini con 'text' vacío o solo espacios.")
                            return "Lo siento, no pude generar un contenido textual en este momento."
                        return text_response
                
                # Si la estructura no es la esperada o está vacía después de una respuesta 200 OK
                logger.error(f"Respuesta OK de Gemini pero sin 'candidates' o 'text' válidos: {response_data}")
                return "Lo siento, recibí una respuesta inesperada del modelo de IA."

            except httpx.HTTPStatusError as e:
                logger.error(f"Error HTTP {e.response.status_code} llamando a Gemini API: {e.response.text[:500]}") # Primeros 500 chars del error
                # Intentar decodificar el error JSON de Gemini
                error_message_detail = e.response.text
                try:
                    error_payload = e.response.json()
                    if "error" in error_payload and "message" in error_payload["error"]:
                        error_message_detail = error_payload["error"]["message"]
                except json.JSONDecodeError:
                    pass # Mantener el texto original si no es JSON

                if e.response.status_code == 503 or e.response.status_code == 429: # Service Unavailable o Too Many Requests
                    if current_retry < MAX_RETRIES:
                        logger.warning(f"Gemini API (error {e.response.status_code}). Reintentando en {current_backoff}s... (Intento {current_retry + 1}/{MAX_RETRIES + 1})")
                        await asyncio.sleep(current_backoff)
                        current_retry += 1
                        current_backoff *= BACKOFF_FACTOR
                        continue 
                    else: # Se agotaron los reintentos
                        logger.error(f"Se agotaron los reintentos para Gemini API (error {e.response.status_code}).")
                        return f"GEMINI_API_ERROR: El servicio de IA está experimentando alta demanda ({error_message_detail}). Por favor, intenta más tarde."
                elif e.response.status_code == 400: # Bad Request (ej. prompt inválido, safety block)
                     logger.error(f"Error 400 (Bad Request) de Gemini: {error_message_detail}")
                     return f"GEMINI_API_ERROR: Hubo un problema con la solicitud enviada al servicio de IA ({error_message_detail})."
                else: # Otros errores HTTP
                    return f"GEMINI_API_ERROR: Error de comunicación ({e.response.status_code}) con el servicio de IA: {error_message_detail}"

            except httpx.RequestError as e: # Errores de red, timeouts, etc.
                logger.error(f"Error de red llamando a Gemini API: {type(e).__name__} - {e}")
                if current_retry < MAX_RETRIES:
                    logger.warning(f"Error de red. Reintentando en {current_backoff}s... (Intento {current_retry + 1}/{MAX_RETRIES + 1})")
                    await asyncio.sleep(current_backoff)
                    current_retry += 1
                    current_backoff *= BACKOFF_FACTOR
                    continue
                logger.error("Se agotaron los reintentos por error de red.")
                return "GEMINI_API_ERROR: Problema de conexión con el servicio de IA. Verifica tu red."
            
            except json.JSONDecodeError as e: # Si la respuesta no es JSON válido (inesperado para Gemini si el status es 200)
                logger.error(f"Error decodificando respuesta JSON de Gemini (inesperado para status 200 OK): {e}. Respuesta: {response.text[:500]}")
                return "GEMINI_API_ERROR: El servicio de IA devolvió una respuesta en un formato inesperado."

            except Exception as e: # Capturar cualquier otro error inesperado
                logger.critical(f"Error inesperado y no manejado en ask_gemini_with_history: {type(e).__name__} - {e}", exc_info=True)
                return "GEMINI_API_ERROR: Ocurrió un error interno inesperado al procesar tu solicitud con el servicio de IA."
        
        # Si el bucle while termina porque se agotaron los reintentos
        logger.error(f"Llamada a Gemini falló después de {MAX_RETRIES + 1} intentos.")
        return "GEMINI_API_ERROR: El servicio de IA no está respondiendo después de varios intentos. Por favor, intenta de nuevo más tarde."