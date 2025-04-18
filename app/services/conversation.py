from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message
from app.services.supabase import save_message_to_supabase
from app.services.products import search_products_by_keyword
from app.utils.nlp import (
    extract_keywords,
    quiere_ver_todos_los_productos,
    detecta_pedido_de_productos,  # ← Import actualizado
)

async def handle_user_message(body: dict):
    try:
        entry = body['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        messages = value.get('messages')

        if not messages:
            return

        msg = messages[0]
        text = msg.get('text', {}).get('body', '')
        from_number = msg.get('from')

        if not text or not from_number:
            print("Mensaje sin texto o número inválido.")
            return

        # 1) Memoria RAM
        user_histories.setdefault(from_number, []).append({"role": "user", "text": text})

        # 2) Guardar en Supabase (usuario)
        await save_message_to_supabase(from_number, "user", text)

        # 3) Detectar intención de ver TODO el catálogo o buscar por keyword
        productos = []

        # 3a) IA detecta intención de ver todo catálogo
        if await detecta_pedido_de_productos(text):
            productos = await search_products_by_keyword("")  # trae todo
            print("📦 Buscando TODOS los productos")
        else:
            # 3b) Palabras clave específicas
            keywords_en_catalogo = ["tequila", "cerveza", "ron", "aguardiente", "whisky", "vino", "ginebra"]
            palabras_clave = extract_keywords(text, keywords_en_catalogo)
            for kw in palabras_clave:
                productos = await search_products_by_keyword(kw)
                print(f"📦 Buscando productos con keyword: {kw}")
                print("📦 Productos encontrados:", productos)
                if productos:
                    break  # se detiene al encontrar resultados

        # 4) Si hay productos, formatearlos en contexto para Gemini
        if productos:
            productos_texto = "🛍️ Estos son los productos disponibles en la tienda:\n\n"
            for prod in productos:
                productos_texto += (
                    f"- {prod['name']}: {prod['description']}. "
                    f"Precio: ${prod['price']}. Stock: {prod['stock']}\n"
                )

            print("📦 Texto final con productos:", productos_texto)

            # Construir un único mensaje de usuario que combine la pregunta original
            # con el contexto de los productos
            mensaje_con_contexto = (
                f"{text}\n\n"
                "(Responde *solo* usando la siguiente información de productos disponibles):\n"
                f"{productos_texto}"
            )

            # Reemplazar el último mensaje en memoria con este que incluye contexto
            user_histories[from_number][-1] = {
                "role": "user",
                "text": mensaje_con_contexto
            }

        # 5) Generar respuesta de Gemini con el historial completo
        history = user_histories[from_number]
        respuesta = await ask_gemini_with_history(history)

        # 6) Almacenar respuesta en memoria
        user_histories[from_number].append({"role": "model", "text": respuesta})

        # 7) Guardar respuesta en Supabase
        await save_message_to_supabase(from_number, "model", respuesta)

        # 8) Enviar respuesta por WhatsApp
        send_whatsapp_message(from_number, respuesta)

    except Exception as e:
        print("Error procesando el mensaje:", e)
