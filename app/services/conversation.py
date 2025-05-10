# app/services/conversation.py

from datetime import datetime
import json
import re
import traceback
from difflib import get_close_matches
from typing import List, Dict, Any, Tuple, Optional

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image # ASUMO QUE YA SON ASYNC DEF
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products # get_recommended_products (se usará si es necesario)
# from app.services.orders import process_order # La lógica de procesar orden se integra más con el LLM
from app.utils.extractors import extract_order_data

# --- Constantes y Configuraciones ---
DEFAULT_SHIPPING_COST = 5000
# Campos requeridos que el LLM debe obtener ANTES de generar el JSON de la orden.
# El JSON de la orden tendrá más campos (productos, total, etc.)
REQUIRED_USER_DATA_FOR_ORDER = ["name", "address", "phone", "payment_method"]


# --- Funciones Auxiliares de Productos y Catálogo ---

def _get_product_variant_text(variant: Dict) -> str:
    """Genera un texto descriptivo para una variante (ej: 'Amarillo, 750ml')."""
    options_parts = []
    for key, value in variant.get("options", {}).items():
        options_parts.append(str(value))
    return ", ".join(options_parts) if options_parts else "Estándar"

def _find_product_in_list(products: List[Dict], query_name: str) -> Optional[Dict]:
    """Encuentra un producto por nombre (exacto o aproximado)."""
    if not query_name: return None
    query_lower = query_name.lower()
    
    for p in products:
        if p["name"].lower() == query_lower:
            return p
    
    product_names = [p["name"].lower() for p in products]
    matches = get_close_matches(query_lower, product_names, n=1, cutoff=0.7)
    if matches:
        matched_name = matches[0]
        for p in products:
            if p["name"].lower() == matched_name:
                return p
    return None

def _find_variant_in_product(product: Dict, query_variant_text: str) -> Optional[Dict]:
    """Encuentra una variante dentro de un producto por su texto descriptivo."""
    if not query_variant_text or not product: return None
    query_lower = query_variant_text.lower()

    for v in product.get("product_variants", []):
        variant_text = _get_product_variant_text(v).lower()
        if query_lower in variant_text: # Permite "amarillo" encontrar "Tequila Amarillo"
            return v
        
        # Búsqueda por coincidencia cercana en las opciones individuales
        option_values = [str(opt_val).lower() for opt_val in v.get("options", {}).values()]
        matches = get_close_matches(query_lower, option_values, n=1, cutoff=0.7)
        if matches and matches[0] in variant_text:
            return v
            
    return None

def _get_image_urls(product: Dict, variant: Optional[Dict] = None) -> List[str]:
    """Obtiene URLs de imágenes, priorizando las de variante si se especifica."""
    urls = []
    all_images = product.get("product_images", [])

    if variant:
        variant_id = variant.get("id")
        # Buscar imágenes asociadas directamente a la variante
        urls.extend([img["url"] for img in all_images if img.get("variant_id") == variant_id])

    # Si no se especificó variante, o la variante no tiene imágenes propias, buscar imágenes generales del producto.
    # Se podría decidir si agregar generales solo si las de variante están vacías.
    # Aquí, si se pidió variante y tiene imágenes, solo se muestran esas. Sino, se muestran las generales.
    if not urls: # O si queremos siempre agregar las generales: if True:
        urls.extend([img["url"] for img in all_images if img.get("variant_id") is None])
    
    return list(set(urls)) # Eliminar duplicados

def _build_simplified_catalog_for_llm_image_detection(products: List[Dict]) -> List[Dict]:
    """Crea un resumen del catálogo (nombres y variantes) para ayudar al LLM."""
    summary = []
    for p in products:
        item = {"name": p["name"], "variants": []}
        for v in p.get("product_variants", []):
            item["variants"].append(_get_product_variant_text(v))
        summary.append(item)
    return summary

def _build_detailed_catalog_for_llm_sales(products: List[Dict]) -> str:
    """Construye la descripción del catálogo para el prompt de ventas del LLM."""
    lines = ["🛍️ **Nuestro Catálogo de Productos** (Precios en COP):\n"]
    for p in products:
        product_info = f"**{p['name']}**"
        if p.get('description'):
            product_info += f"\n   📝 _{p['description'][:150]}..._" # Descripción corta
        
        variants = p.get("product_variants", [])
        if variants:
            product_info += "\n   🎨 Variantes disponibles:"
            for v in variants:
                v_text = _get_product_variant_text(v)
                price = v.get("price", p.get("price", "Precio no disponible"))
                stock = v.get("stock", "Consultar stock")
                product_info += f"\n     - {v_text}: ${price:,.0f} (Stock: {stock})"
        elif p.get("price") is not None and p.get("price") > 0:
            price = p.get("price", "Precio no disponible")
            stock = p.get("stock", "Consultar stock")
            product_info += f"\n   💰 Precio: ${price:,.0f} (Stock: {stock})"
        else:
             product_info += "\n   ℹ️ (Consultar precio y disponibilidad)"
        lines.append(product_info)
    return "\n\n".join(lines)


# --- Funciones de Interacción con LLM y Envío ---

async def _get_llm_image_intent(
    user_history: List[Dict], current_user_message: str, catalog_summary: List[Dict]
) -> Optional[Dict]:
    """Determina si el usuario quiere imágenes y de qué, usando el LLM."""
    prompt_instructions = [
        "Tu tarea es analizar el ÚLTIMO mensaje del usuario en el CONTEXTO del historial de conversación y el catálogo proporcionado.",
        "Determina si el usuario está solicitando ver imágenes de un producto o variante.",
        "Si pide imágenes, responde con un JSON: {\"action\": \"show_image\", \"product_name\": \"<nombre_producto_del_catalogo>\", \"variant_text\": \"<texto_variante_del_catalogo_o_mencion_usuario>\"}.",
        "   - `product_name` debe ser lo más cercano posible a un nombre del catálogo.",
        "   - `variant_text` puede ser el texto descriptivo de la variante (ej: 'amarillo', 'azul', '750ml') o null si no se especifica.",
        "   - Si el usuario dice 'mándame foto' y antes hablaron de 'Tequila Jose Cuervo', usa ese contexto.",
        "Si el usuario NO pide imágenes, responde con un JSON: {\"action\": \"continue_conversation\"}.",
        "Ejemplos de solicitud de imagen:",
        "   User: 'foto del tequila amarillo' -> {\"action\": \"show_image\", \"product_name\": \"Tequila Jose Cuervo\", \"variant_text\": \"amarillo\"}",
        "   User: 'imagen del aguardiente nariño azul' -> {\"action\": \"show_image\", \"product_name\": \"Aguardiente Nariño\", \"variant_text\": \"azul\"}",
        "   User: 'tienes fotos?' (Contexto previo: hablando de Ron) -> {\"action\": \"show_image\", \"product_name\": \"<Nombre del Ron del contexto>\", \"variant_text\": null}",
        "Responde ÚNICAMENTE con el JSON."
    ]
    
    # Solo los últimos mensajes relevantes para el historial de Gemini
    relevant_history = [m for m in user_history if m["role"] in ("user", "model")][-6:]
    
    llm_input_payload = {
        "history": relevant_history,
        "current_user_message": current_user_message,
        "catalog_summary_for_reference": catalog_summary, # Para que el LLM tenga nombres
        "instructions": prompt_instructions
    }
    
    # El prompt para Gemini debe ser una lista de mensajes
    llm_prompt_messages = relevant_history + [
        {"role": "user", "text": json.dumps(llm_input_payload, ensure_ascii=False)}
    ]

    try:
        llm_response_str = await ask_gemini_with_history(llm_prompt_messages)
        print(f"🧠 Respuesta LLM (intención imagen): {llm_response_str}")
        
        # Extraer el JSON de la respuesta (Gemini a veces añade ```json ... ```)
        match = re.search(r"\{[\s\S]*\}", llm_response_str)
        if match:
            action_json = json.loads(match.group())
            if action_json.get("action") == "show_image" and action_json.get("product_name"):
                return action_json
    except json.JSONDecodeError:
        print(f"⚠️ Error decodificando JSON de LLM para intención de imagen: {llm_response_str}")
    except Exception as e:
        print(f"⚠️ Error en _get_llm_image_intent: {e}\n{traceback.format_exc()}")
    return None


async def _send_requested_images(
    from_number: str, product: Dict, variant: Optional[Dict], user_history: List[Dict]
):
    """Envía las imágenes del producto/variante y actualiza el historial."""
    image_urls = _get_image_urls(product, variant)
    
    product_display_name = product['name']
    if variant:
        product_display_name += f" ({_get_product_variant_text(variant)})"

    if not image_urls:
        response_text = f"😔 Lo siento, no tenemos imágenes disponibles para *{product_display_name}* en este momento."
        await send_whatsapp_message(from_number, response_text)
    else:
        response_text = f"¡Claro! Aquí tienes las imágenes de *{product_display_name}*:"
        await send_whatsapp_message(from_number, response_text)
        for i, url in enumerate(image_urls):
            try:
                # Si hay muchas imágenes, solo la primera con caption completo o sin caption.
                caption = product_display_name if i == 0 and len(image_urls) > 1 else "" 
                if len(image_urls) == 1: caption = product_display_name

                await send_whatsapp_image(from_number, url, caption=caption)
            except Exception as e_img:
                print(f"❌ Error enviando imagen {url} para {from_number}: {e_img}")
                await send_whatsapp_message(from_number, "⚠️ Hubo un problema al enviar una de las imágenes, pero aquí están las otras (si hay).")
        response_text = f"Envié imágenes de {product_display_name}." # Para el historial interno

    user_history.append({"role": "model", "text": response_text, "time": datetime.utcnow().isoformat()})
    await save_message_to_supabase(from_number, "model", response_text) # Guardar la acción en Supabase


async def _handle_sales_conversation_with_llm(
    from_number: str,
    user_message_text: str,
    user_history: List[Dict],
    all_products: List[Dict]
):
    """Maneja el flujo de ventas principal usando el LLM."""
    
    catalog_context_for_llm = _build_detailed_catalog_for_llm_sales(all_products)

    # Instrucciones detalladas para el LLM vendedor
    sales_instructions = [
        "Eres 'Vendebot 🤖', un asistente de ventas virtual amigable, proactivo y muy eficiente. Tu objetivo es ayudar al cliente y cerrar ventas.",
        "Usa emojis para hacer la conversación más cercana y humana. 😊🛒🍾",
        "**TU PROCESO DE VENTA:**",
        "1.  **Saludo y Escucha Activa**: Responde al usuario amablemente. Si hace preguntas sobre productos, usa la información del catálogo proporcionado.",
        "2.  **Identificar Intención de Compra**: Si el usuario expresa deseo de comprar ('quiero X', 'me interesa Y', 'cuánto por Z'):",
        "    a.  Ayuda a armar el carrito: Confirma producto(s), variante(s) y cantidad(es).",
        "    b.  Calcula el subtotal de los productos.",
        f"    c.  Informa sobre el costo de envío fijo: COP {DEFAULT_SHIPPING_COST:,.0f}.",
        "    d.  Presenta el TOTAL del pedido (subtotal + envío).",
        "    e.  PREGUNTA SIEMPRE: '¿Deseas agregar algo más a tu pedido?' Puedes sugerir UN producto complementario de forma sutil si es relevante.",
        "3.  **Recopilación de Datos (SIEMPRE DESPUÉS DE CONFIRMAR EL CARRITO Y QUE NO QUIERE MÁS PRODUCTOS)**:",
        "    Cuando el usuario confirme que está listo para finalizar o diga 'no quiero nada más', PIDE DE FORMA CLARA Y ORDENADA los siguientes datos para el envío:",
        "      - Nombre completo.",
        "      - Dirección de entrega detallada (incluyendo barrio/ciudad).",
        "      - Número de teléfono de contacto (si es diferente al de WhatsApp).",
        "      - Método de pago (ej: 'Efectivo contra entrega', 'Transferencia Bancolombia', 'Nequi').",
        "    *NO ASUMAS NINGÚN DATO. PÍDELOS EXPLÍCITAMENTE.*",
        "4.  **Confirmación Final y JSON del Pedido (SOLO CUANDO TENGAS TODOS LOS DATOS DEL PUNTO 3 Y EL CARRITO ESTÉ DEFINIDO)**:",
        "    a.  Resume el pedido completo: productos (con variante y cantidad), subtotal, envío, total, y los datos de entrega del usuario.",
        "    b.  Pide una última confirmación: '¿Es todo correcto para procesar tu pedido?'",
        "    c.  Si el usuario confirma, AÑADE AL FINAL DE TU MENSAJE DE CONFIRMACIÓN el siguiente bloque JSON EXACTO, rellenando los campos. NO incluyas el JSON si faltan datos o si el usuario no ha confirmado.",
        "        ```json",
        "        {\"order_details\":{\"name\":\"<NOMBRE_COMPLETO>\",\"address\":\"<DIRECCION_DETALLADA>\",\"phone\":\"<TELEFONO_CONTACTO>\",\"payment_method\":\"<METODO_PAGO>\",\"products\":[{\"name\":\"<NOMBRE_PROD_1>\",\"variant_text\":\"<TEXTO_VARIANTE_1 (si aplica)>\",\"quantity\":<CANT_1>,\"price_unit\":<PRECIO_UNIT_1>}, ...otros_productos],\"subtotal_products\":<SUBTOTAL_PRODS>,\"shipping_cost\":<COSTO_ENVIO>,\"total_order\":<TOTAL_PEDIDO>}}",
        "        ```",
        "5.  **Manejo de Stock**: Si un producto/variante está agotado o con bajo stock según el catálogo, informa y sugiere alternativas.",
        "6.  **Preguntas Generales**: Si no hay intención de compra, solo responde preguntas usando el catálogo.",
        "7.  **Claridad**: Si no entiendes algo, pide amablemente una aclaración.",
        "**Catálogo de Referencia:**",
        catalog_context_for_llm,
        "\n**Historial de Conversación Reciente:**"
    ]

    # El prompt para Gemini debe ser una lista de mensajes
    relevant_history = [m for m in user_history if m["role"] in ("user", "model")][-10:]
    llm_prompt_messages = relevant_history + [
        {"role": "user", "text": user_message_text + "\n\n" + "\n".join(sales_instructions)}
    ]
    
    llm_response_str = await ask_gemini_with_history(llm_prompt_messages)
    print(f"🧠 Respuesta LLM (ventas): {llm_response_str}")

    # `extract_order_data` debe manejar la separación del JSON y el texto limpio.
    # También debe validar que el `order_data` (si existe) sea completo.
    order_data, clean_bot_response = extract_order_data(llm_response_str, DEFAULT_SHIPPING_COST)

    if not clean_bot_response and not order_data: # Si LLM no da respuesta usable
        clean_bot_response = "Hmm, no estoy seguro de cómo responder a eso. ¿Podrías intentarlo de otra manera? 🤔"
    
    if clean_bot_response:
        await send_whatsapp_message(from_number, clean_bot_response)
        user_history.append({"role": "model", "text": clean_bot_response, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "model", clean_bot_response)

    if order_data:
        print(f"📦 Datos de orden extraídos y listos para guardar: {json.dumps(order_data, indent=2)}")
        # Aquí es donde guardarías `order_data` en tu tabla de pedidos de Supabase.
        # Ejemplo: await save_order_to_supabase_orders_table(from_number, order_data)
        # El mensaje de "pedido procesado" ya debería haberlo dado el LLM como parte de `clean_bot_response`
        # si siguió las instrucciones de incluir el JSON *después* de la confirmación verbal.
        # Si no, puedes enviar un mensaje de confirmación genérico adicional aquí.
        # await send_whatsapp_message(from_number, "✅ ¡Tu pedido ha sido registrado con éxito! Gracias por tu compra. 🎉")
    else:
        # Esto significa que el LLM está en una etapa de la conversación que no implica un pedido finalizado.
        # (ej: pidiendo datos, confirmando carrito, etc.)
        print("ℹ️ No se extrajeron datos de orden finalizados en esta interacción.")


# --- Handler Principal de Mensajes de Usuario ---

async def handle_user_message(body: dict):
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        message_obj = changes.get("value", {}).get("messages", [{}])[0]

        if message_obj.get("type") != "text": # Ignorar estados, multimedia del usuario, etc.
            print(f"ℹ️ Mensaje no textual recibido (tipo: {message_obj.get('type')}). Ignorando.")
            return

        user_text = message_obj.get("text", {}).get("body", "").strip()
        from_number = message_obj.get("from")

        if not user_text or not from_number:
            print("⚠️ Mensaje vacío o sin remitente. Ignorando.")
            return

        print(f"💬 Mensaje de {from_number}: '{user_text}'")

        # Inicializar/recuperar historial
        user_history = user_histories.setdefault(from_number, [])
        user_history.append({"role": "user", "text": user_text, "time": datetime.utcnow().isoformat()})
        await save_message_to_supabase(from_number, "user", user_text)

        all_products = await get_all_products()
        if not all_products:
            await send_whatsapp_message(from_number, "⚠️ Lo siento, estoy teniendo problemas para acceder a nuestro catálogo. Intenta más tarde.")
            return

        # 1. Comprobar si el usuario está pidiendo imágenes
        catalog_summary_for_img_detection = _build_simplified_catalog_for_llm_image_detection(all_products)
        image_intent_details = await _get_llm_image_intent(user_history, user_text, catalog_summary_for_img_detection)

        image_request_handled_successfully = False
        if image_intent_details and image_intent_details.get("action") == "show_image":
            product_name = image_intent_details.get("product_name")
            variant_text = image_intent_details.get("variant_text")
            
            found_product = _find_product_in_list(all_products, product_name)
            found_variant = None
            if found_product and variant_text:
                found_variant = _find_variant_in_product(found_product, variant_text)
            
            if found_product:
                await _send_requested_images(from_number, found_product, found_variant, user_history)
                image_request_handled_successfully = True # Indica que se gestionó una solicitud de imagen (incluso si no se encontraron)
                # El flujo continuará, y el LLM de ventas tendrá el contexto de que se enviaron imágenes.
                # Se podría añadir un mensaje tipo: "¿Te gustaría añadirlo al carrito o tienes más preguntas sobre este producto?"
                # await send_whatsapp_message(from_number, "¿Te gustaría añadir este producto al carrito o tienes más preguntas? 😊")
                # return # Si queremos detener el flujo aquí y esperar nueva respuesta del usuario.
                # Por ahora, dejaremos que el flujo continúe al LLM de ventas.
            else:
                no_product_msg = f"Hmm, mencionaste '{product_name}' pero no lo encuentro en nuestro catálogo. ¿Podrías verificar el nombre? 🤔"
                await send_whatsapp_message(from_number, no_product_msg)
                user_history.append({"role": "model", "text": no_product_msg, "time": datetime.utcnow().isoformat()})
                await save_message_to_supabase(from_number, "model", no_product_msg)
                image_request_handled_successfully = True # Se intentó manejar
        
        # 2. Continuar con el flujo de ventas/conversación general.
        # El LLM de ventas recibirá el mensaje original del usuario y el historial actualizado (que puede incluir la interacción de imágenes).
        await _handle_sales_conversation_with_llm(
            from_number,
            user_text, # El mensaje original del usuario para que el LLM de ventas lo procese.
            user_history,
            all_products
        )

    except Exception as e:
        error_message = f"❌ [ERROR CRÍTICO en handle_user_message]: {e}\n{traceback.format_exc()}"
        print(error_message)
        # Intentar notificar al usuario del error si es posible
        if 'from_number' in locals() and from_number:
            try:
                await send_whatsapp_message(from_number, "🤖 ¡Ups! Algo no salió bien de mi lado. Por favor, inténtalo de nuevo en un momento. 🙏")
            except Exception as e_send:
                print(f"💣 [FALLO AL ENVIAR MENSAJE DE ERROR AL USUARIO]: {e_send}")