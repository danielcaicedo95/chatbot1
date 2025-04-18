# app/services/conversation.py

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase  # ↪️ Import agregado
from app.services.products import search_products_by_keyword  # ↪️ Nuevo import


async def handle_user_message(body: dict):
    try:
        entry = body['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        messages = value.get('messages')

        if not messages:
            return

        msg = messages[0]
        text = msg.get('text', {}).get('body')
        from_number = msg.get('from')

        if not text or not from_number:
            print("Mensaje sin texto o número inválido.")
            return

        # 1) Memoria RAM
        user_histories[from_number].append({"role": "user", "text": text})

        # 2) Guardar en Supabase (usuario)
        await save_message_to_supabase(from_number, "user", text)

        # 3) Buscar productos relacionados con el mensaje del usuario
        keyword = text.lower().strip()
        products_found = await search_products_by_keyword(keyword)

        # 4) Preparar historial con contexto si hay productos
        history = list(user_histories[from_number])

        if products_found:
            product_text = "\n".join(
                [f"- {p['name']}: {p.get('description', 'Sin descripción')}" for p in products_found]
            )
            history.append({
                "role": "user",
                "text": f"Estos son los productos disponibles relacionados con '{keyword}':\n{product_text}"
            })

        # 5) Generar respuesta
        respuesta = await ask_gemini_with_history(history)

        # 6) Memoria RAM
        user_histories[from_number].append({"role": "model", "text": respuesta})

        # 7) Guardar en Supabase (bot)
        await save_message_to_supabase(from_number, "model", respuesta)

        # 8) Enviar respuesta al usuario
        send_whatsapp_message(from_number, respuesta)

    except Exception as e:
        print("Error procesando el mensaje:", e)
