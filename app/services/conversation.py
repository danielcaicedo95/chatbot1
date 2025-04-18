from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import search_products_by_keyword
from app.utils.nlp import extract_keywords, quiere_ver_todos_los_productos, detecta_pedido_de_productos  # ✅ Integración completa de NLP

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

        # 1) Memoria RAM: almacenar mensaje del usuario
        user_histories[from_number].append({"role": "user", "text": text})

        # 2) Guardar en Supabase (mensaje del usuario)
        await save_message_to_supabase(from_number, "user", text)

        # 3) Determinar qué productos mostrar
        productos = []
        keywords_en_catalogo = ["tequila", "cerveza", "ron", "aguardiente", "whisky", "vino", "ginebra"]

        # 3a) Primero, intentar con IA para detectar pedido general
        if await detecta_pedido_de_productos(text):
            print("🔍 IA detectó pedido de todos los productos.")
            productos = await search_products_by_keyword("")  # trae todo
            print("📦 Buscando TODOS los productos (IA)")
        # 3b) Si no, revisar frases comunes
        elif quiere_ver_todos_los_productos(text):
            print("🔍 Frase común detectada para ver todos los productos.")
            productos = await search_products_by_keyword("")
            print("📦 Buscando TODOS los productos (frase)")
        # 3c) Si no es pedido general, buscar por palabras clave específicas
        else:
            palabras_clave = extract_keywords(text, keywords_en_catalogo)
            for kw in palabras_clave:
                productos = await search_products_by_keyword(kw)
                print(f"📦 Buscando productos con keyword: {kw}")
                print("📦 Productos encontrados:", productos)
                if productos:
                    break  # detener búsqueda cuando haya resultados

        # 4) Si hay productos, formatearlos como contexto adicional para Gemini
        if productos:
            productos_texto = "🛍️ Estos son los productos disponibles en la tienda:\n\n"
            for prod in productos:
                productos_texto += (
                    f"- {prod['name']}: {prod['description']}. Precio: ${prod['price']}. Stock: {prod['stock']}\n"
                )
            print("📦 Texto final con productos:", productos_texto)

            # Reemplazar el mensaje original en historial con versión ampliada
            mensaje_con_contexto = (
                f"{text}\n\n"
                f"(Responde únicamente usando esta información de productos disponibles en tienda):\n"
                f"{productos_texto}"
            )
            user_histories[from_number][-1] = {"role": "user", "text": mensaje_con_contexto}

        # 5) Generar respuesta de Gemini con historial actualizado
        history = list(user_histories[from_number])
        respuesta = await ask_gemini_with_history(history)

        # 6) Memoria RAM: guardar respuesta del bot
        user_histories[from_number].append({"role": "model", "text": respuesta})

        # 7) Guardar en Supabase (respuesta del bot)
        await save_message_to_supabase(from_number, "model", respuesta)

        # 8) Enviar respuesta por WhatsApp
        send_whatsapp_message(from_number, respuesta)

    except Exception as e:
        print("Error procesando el mensaje:", e)
