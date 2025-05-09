# app/services/conversation.py

from datetime import datetime, timezone
import json
import re
import traceback
from difflib import get_close_matches
import logging

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order
from app.utils.extractors import extract_order_data

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Campos requeridos en el JSON 'order_details' para que process_order pueda funcionar
REQUIRED_ORDER_FIELDS_IN_JSON = ["name", "address", "phone", "payment_method", "products", "total"]


# --- Funciones Auxiliares de Catálogo y Matching (Robustecidas) ---

def build_structured_catalog(productos_list: list) -> list:
    """
    Construye un catálogo estructurado y detallado para la lógica interna
    (matching de imágenes, información para el LLM).
    """
    structured_catalog = []
    if not productos_list:
        return structured_catalog

    for p_data in productos_list:
        try:
            variants_details = []
            for v_data in p_data.get("product_variants", []):
                opts = v_data.get("options", {})
                if not opts:
                    logger.warning(f"Variante sin opciones para producto {p_data.get('name')}, ID: {v_data.get('id')}")
                    continue
                
                display_label_parts = []
                value_for_matching_parts = [] # Para matching simple de valores de opción
                catalog_variant_label_parts = [] # Para matching con product_images.variant_label

                for k_opt, v_opt_val in opts.items():
                    display_label_parts.append(f"{k_opt}:{v_opt_val}")
                    value_for_matching_parts.append(str(v_opt_val).strip().lower())
                    catalog_variant_label_parts.append(f"{str(k_opt).strip().lower()}:{str(v_opt_val).strip().lower()}")

                variants_details.append({
                    "id": v_data["id"],
                    "display_label": ", ".join(display_label_parts),
                    "value_for_matching": " ".join(value_for_matching_parts), 
                    "catalog_variant_label_for_images": ",".join(catalog_variant_label_parts),
                    "price": v_data.get("price"),
                    "stock": v_data.get("stock"),
                    "images": [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") == v_data["id"]]
                })
            
            main_product_images = [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") is None]

            structured_catalog.append({
                "id": p_data["id"],
                "name": p_data["name"],
                "description": p_data.get("description"),
                "base_price": p_data.get("price"),
                "base_stock": p_data.get("stock"),
                "variants": variants_details,
                "main_images": main_product_images,
                "all_product_images_raw": p_data.get("product_images", [])
            })
        except Exception as e:
            logger.error(f"Error construyendo entrada de catálogo para producto {p_data.get('name', 'ID desconocido')}: {e}", exc_info=True)
            continue
            
    return structured_catalog

def format_catalog_for_llm_prompt(s_catalog: list) -> list:
    """Formatea el catálogo estructurado para un prompt más conciso para el LLM."""
    llm_catalog_representation = []
    for p_entry in s_catalog:
        variants_info = []
        for v_entry in p_entry.get("variants", []):
            variants_info.append(
                f"{v_entry['display_label']} (Precio: {v_entry.get('price', 'N/A')}, Stock: {v_entry.get('stock', 'N/A')})"
            )
        
        product_summary = {
            "name": p_entry["name"],
            "description": p_entry.get("description", "No disponible"),
            "base_price_if_no_variants": p_entry.get("base_price") if not variants_info else None,
            "base_stock_if_no_variants": p_entry.get("base_stock") if not variants_info else None,
            "variants_available": variants_info if variants_info else "No tiene variantes específicas listadas.",
            "images_available": bool(p_entry.get("main_images") or any(v.get("images") for v in p_entry.get("variants",[])) or p_entry.get("all_product_images_raw"))
        }
        llm_catalog_representation.append(product_summary)
    return llm_catalog_representation


def match_target_in_structured_catalog(
    s_catalog: list, 
    target_query_str: str
) -> tuple[dict | None, dict | None]:
    if not target_query_str or not s_catalog:
        return None, None
    
    target_lower = target_query_str.strip().lower()

    # Prioridad 1: Coincidencia exacta de nombre de producto + variante
    for cat_prod_entry in s_catalog:
        prod_name_lower = cat_prod_entry["name"].lower()
        if prod_name_lower in target_lower: # El nombre del producto está en el query
            for cat_variant_entry in cat_prod_entry["variants"]:
                # cat_variant_entry["value_for_matching"] es ej: "amarillo" o "azul m"
                if all(term in target_lower for term in cat_variant_entry["value_for_matching"].split()):
                    return cat_prod_entry, cat_variant_entry

    # Prioridad 2: Coincidencia exacta de nombre de producto
    for cat_prod_entry in s_catalog:
        if cat_prod_entry["name"].lower() == target_lower:
            return cat_prod_entry, None

    # Prioridad 3: Coincidencia exacta de valor de variante (más ambiguo, usar con cuidado)
    # Puede ser útil si el producto ya está en contexto
    possible_variant_matches = []
    for cat_prod_entry in s_catalog:
        for cat_variant_entry in cat_prod_entry["variants"]:
            if cat_variant_entry["value_for_matching"] == target_lower: # Ej: target_lower = "amarillo"
                 possible_variant_matches.append((cat_prod_entry, cat_variant_entry))
    
    if len(possible_variant_matches) == 1:
        return possible_variant_matches[0]
    elif len(possible_variant_matches) > 1:
        logger.info(f"Target '{target_lower}' es ambiguo, coincide con múltiples variantes. Se necesita clarificación.")
        # No retornamos nada, para que el flujo principal pueda pedir clarificación.

    # Prioridad 4: Búsqueda difusa
    all_matchable_items = [] 
    for cat_prod_entry in s_catalog:
        # Producto solo
        all_matchable_items.append({"name_to_match": cat_prod_entry["name"].lower(), "prod": cat_prod_entry, "var": None})
        # Producto + Variante
        for cat_variant_entry in cat_prod_entry["variants"]:
            full_variant_name = f"{cat_prod_entry['name'].lower()} {cat_variant_entry['value_for_matching']}"
            all_matchable_items.append({"name_to_match": full_variant_name, "prod": cat_prod_entry, "var": cat_variant_entry})
            # Variante sola (menos prioritario en difuso, pero puede ayudar)
            all_matchable_items.append({"name_to_match": cat_variant_entry['value_for_matching'], "prod": cat_prod_entry, "var": cat_variant_entry})


    choices_for_difflib = list(set([item["name_to_match"] for item in all_matchable_items]))
    best_diff_matches = get_close_matches(target_lower, choices_for_difflib, n=1, cutoff=0.7) # Cutoff más alto

    if best_diff_matches:
        matched_name_str = best_diff_matches[0]
        for item in all_matchable_items:
            if item["name_to_match"] == matched_name_str:
                logger.info(f"Coincidencia difusa para '{target_lower}': '{matched_name_str}' -> Producto: {item['prod']['name']}, Variante: {item['var']['display_label'] if item['var'] else 'N/A'}")
                return item["prod"], item["var"]
    
    logger.info(f"No se encontró coincidencia (directa o difusa) para '{target_query_str}' en el catálogo.")
    return None, None


# --- Lógica de Manejo de Solicitud de Imágenes ---
async def handle_image_request(
    from_number: str,
    user_raw_text: str,
    current_history: list,
    structured_catalog_data: list
) -> bool:
    try:
        catalog_summary_for_llm_images = [
            {"name": p["name"], "variants": [v["display_label"] for v in p.get("variants", [])]}
            for p in structured_catalog_data
        ]
        
        image_intent_prompt_obj = {
            "user_request": user_raw_text,
            "conversation_context": "El usuario está chateando con un bot de ventas y podría estar pidiendo ver un producto.",
            "available_products_summary": catalog_summary_for_llm_images,
            "task": "Analiza la 'user_request'. Si el usuario está pidiendo explícitamente ver imágenes o fotos de un producto o variante, responde con un JSON. Si no, responde con {\"want_images\": false}.",
            "json_format_if_images_wanted": {
                "want_images": True,
                "target_product_or_variant": "Nombre lo más exacto posible del producto o variante. Ej: 'Tequila Jose Cuervo amarillo', 'Aguardiente Nariño', 'la variante azul del aguardiente'.",
                "clarification_needed_message": "OPCIONAL: Si el target no es claro pero la intención sí (ej: 'muéstrame una foto'), incluye aquí un mensaje para pedir clarificación. Ej: '¡Claro! ¿De qué producto o variante te gustaría ver la foto? 😊'"
            },
            "examples": [
                {"user": "¿Tienes fotos del tequila?", "bot_json": {"want_images": True, "target_product_or_variant": "tequila"}},
                {"user": "Muéstrame el tequila amarillo", "bot_json": {"want_images": True, "target_product_or_variant": "tequila amarillo"}},
                {"user": "Y el precio?", "bot_json": {"want_images": False}},
                {"user": "foto porfa", "bot_json": {"want_images": True, "target_product_or_variant": "producto mencionado antes", "clarification_needed_message": "¡Absolutamente! ¿De qué producto o variante te gustaría ver la foto? Así te muestro la correcta. 😉"}},
            ]
        }

        llm_input_for_image_intent = current_history[-3:] + [
            {"role": "user", "text": json.dumps(image_intent_prompt_obj, ensure_ascii=False)}
        ]
        
        logger.info(f"🧠 Gemini (Image Intent) - Input: {json.dumps(image_intent_prompt_obj, ensure_ascii=False, indent=2)}")
        llm_response_text = await ask_gemini_with_history(llm_input_for_image_intent)
        logger.info(f"🧠 Gemini (Image Intent) - Raw Response: {llm_response_text}")

        json_match = re.search(r"\{[\s\S]*\}", llm_response_text)
        if not json_match:
            logger.warning("No JSON en respuesta de LLM para intención de imagen. Asumiendo no quiere imágenes.")
            return False
        
        try:
            action = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            logger.error(f"Error decodificando JSON de intención de imagen: {e}. Respuesta: {json_match.group()}")
            return False # Falla segura, continuar flujo general

        if not action.get("want_images"):
            logger.info("Usuario no expresó intención de ver imágenes según el LLM.")
            return False

        if action.get("clarification_needed_message") and isinstance(action["clarification_needed_message"], str):
            send_whatsapp_message(from_number, action["clarification_needed_message"])
            logger.info(f"Enviada solicitud de clarificación para imágenes: {action['clarification_needed_message']}")
            return True 

        target_description = action.get("target_product_or_variant")
        if not target_description or not isinstance(target_description, str):
            logger.warning("LLM indicó 'want_images' pero sin 'target_product_or_variant' válido.")
            send_whatsapp_message(from_number, "¡Entendido! Quieres ver fotos. ¿Podrías decirme de qué producto o variante te gustaría verlas, por favor? 🤔")
            return True

        matched_product_cat_entry, matched_variant_cat_entry = match_target_in_structured_catalog(
            structured_catalog_data, target_description
        )

        if not matched_product_cat_entry:
            msg_not_found = f"Hmm, busqué '{target_description}' pero no lo encontré en nuestro catálogo para mostrarte imágenes. 😔 ¿Quizás te referías a otro producto o escribiste el nombre diferente?"
            send_whatsapp_message(from_number, msg_not_found)
            logger.info(f"Producto/variante '{target_description}' no encontrado para imágenes.")
            return True

        image_urls_to_send = []
        display_name_for_caption = matched_product_cat_entry["name"]

        if matched_variant_cat_entry:
            display_name_for_caption = f"{matched_product_cat_entry['name']} ({matched_variant_cat_entry['display_label']})"
            image_urls_to_send.extend(matched_variant_cat_entry.get("images", []))
            
            if not image_urls_to_send: # Fallback a buscar en todas las imágenes del producto
                variant_id_to_match = matched_variant_cat_entry["id"]
                variant_label_to_match = matched_variant_cat_entry["catalog_variant_label_for_images"].lower()
                
                for img_obj in matched_product_cat_entry.get("all_product_images_raw", []):
                    if img_obj.get("variant_id") == variant_id_to_match or \
                       (img_obj.get("variant_label") and img_obj.get("variant_label").lower() == variant_label_to_match):
                        image_urls_to_send.append(img_obj["url"])
            image_urls_to_send = list(set(image_urls_to_send))

        if not image_urls_to_send: # Si aún no hay, usar las principales del producto
            image_urls_to_send.extend(matched_product_cat_entry.get("main_images", []))
            image_urls_to_send = list(set(image_urls_to_send))

        if not image_urls_to_send:
            msg_no_img = f"¡Vaya! Parece que justo para *{display_name_for_caption}* no tengo foticos guardadas en este momento. 🖼️🚫 Pero si quieres, te puedo contar más detalles. 😊"
            send_whatsapp_message(from_number, msg_no_img)
            logger.info(f"No se encontraron URLs de imágenes para '{display_name_for_caption}'.")
            return True

        send_whatsapp_message(from_number, f"¡De una! 📸 Aquí tienes las foticos de *{display_name_for_caption}* para que te antojes:")
        for img_url in image_urls_to_send:
            try:
                logger.info(f"🖼️ Enviando imagen: {img_url} para {display_name_for_caption}")
                send_whatsapp_image(from_number, img_url, caption=display_name_for_caption)
            except Exception as e_img:
                logger.error(f"❌ Error enviando imagen {img_url} a {from_number}: {e_img}", exc_info=True)
        return True

    except Exception as e_main_img_handler:
        logger.error(f"⚠️ Error crítico en handle_image_request: {e_main_img_handler}", exc_info=True)
        return False


# --- Flujo Principal de Mensajes ---
async def handle_user_message(body: dict):
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value_data = changes.get("value", {})
        messages = value_data.get("messages")

        if not messages:
            if value_data.get("statuses"):
                logger.info(f"Recibida actualización de estado: {value_data['statuses']}")
            else:
                logger.info("Webhook recibido sin 'messages' ni 'statuses'. Ignorando.")
            return

        msg = messages[0]
        user_raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        timestamp_msg = datetime.fromtimestamp(int(msg.get("timestamp", datetime.now(timezone.utc).timestamp())), tz=timezone.utc)

        if not user_raw_text or not from_number:
            logger.warning("Mensaje sin texto o remitente. Ignorando.")
            return

        logger.info(f"Mensaje de {from_number}: '{user_raw_text}'")

        current_user_history = user_histories.setdefault(from_number, [])
        current_user_history.append({
            "role": "user",
            "text": user_raw_text,
            "time": timestamp_msg.isoformat()
        })
        await save_message_to_supabase(from_number, "user", user_raw_text, timestamp=timestamp_msg)

        productos_raw_db = await get_all_products()
        if not productos_raw_db:
            logger.error("CRÍTICO: No se pudieron obtener productos. El bot no puede operar sin catálogo.")
            send_whatsapp_message(from_number, "Lo siento mucho, estoy teniendo problemas técnicos para acceder a nuestro catálogo en este momento. 🛠️ Por favor, inténtalo de nuevo en unos minutos. ¡Mil gracias por tu paciencia!")
            return
        
        structured_catalog_data = build_structured_catalog(productos_raw_db)

        # 1. Intentar manejar como solicitud de imagen
        image_request_was_handled = await handle_image_request(
            from_number, user_raw_text, current_user_history, structured_catalog_data
        )
        if image_request_was_handled:
            logger.info("Solicitud de imagen manejada. Finalizando flujo para este mensaje.")
            return

        # 2. Procesar como conversación general o de pedido
        logger.info(f"Procesando como mensaje general/pedido para {from_number}: '{user_raw_text}'")

        catalog_for_llm_prompt_formatted = format_catalog_for_llm_prompt(structured_catalog_data)
        
        general_conversation_prompt_text = (
            f"Eres 'VendiBot', un asistente de ventas virtual experto para una licorera en Colombia. Eres SÚPER AMIGABLE, conversador, paciente, y usas un tono cercano con emojis y algo de jerga colombiana apropiada (ej: '¡De una!', '¡Qué nota!', '¡Hágale pues!'). Tu objetivo es ayudar al cliente, vender, y asegurar una experiencia de compra agradable.\n\n"
            f"**Contexto de la Conversación Actual:**\n"
            f"- Último mensaje del usuario: \"{user_raw_text}\"\n"
            f"- Revisa el historial de conversación (si se proporciona más abajo) para entender el contexto y no repetir preguntas.\n\n"
            f"**Nuestro Catálogo Actual (Precios en COP. Stock indicado es el actual. Si dice agotado, no hay):**\n{json.dumps(catalog_for_llm_prompt_formatted, indent=2, ensure_ascii=False)}\n\n"
            f"**== TU MISIÓN COMO VENDEDOR ESTRELLA ==**\n\n"
            f"1.  **SALUDO Y ATENCIÓN:** Si es un nuevo chat o el usuario saluda, responde con entusiasmo. Ej: '¡Hola! 👋 Soy VendiBot, tu parcero para los mejores tragos. ¿Qué se te antoja hoy?'\n"
            f"2.  **INFO DE PRODUCTOS:** Si preguntan por productos, da detalles (descripción, precio, variantes, stock). Menciona si hay fotos disponibles si el catálogo dice 'images_available: true'. Ej: '¡Claro! El Tequila Jose Cuervo es una delicia, cuesta COP 75.000 la variante Amarillo. ¡Y sí, tengo foticos si quieres ver!'\n"
            f"3.  **MANEJO DE STOCK:** Si algo está agotado, informa con empatía y OFRECE ALTERNATIVAS. Ej: '¡Ay, qué embarrada! 😔 Justo el producto X se nos acabó. Pero, ¿qué tal si pruebas el producto Y que es muy parecido y está volando?'\n"
            f"4.  **INTENCIÓN DE COMPRA (¡CLAVE!):** Cuando el usuario diga que quiere comprar algo (ej: 'quiero ese', 'me lo llevo', 'voy a pedir X'):\n"
            f"    a. **Confirma Productos y Cantidades:** '¡Excelente elección! Entonces, para confirmar: ¿llevas [Producto 1, Cantidad 1]?'. Si no dice cantidad, asume 1 pero pregunta si quiere más.\n"
            f"    b. **Subtotal y Envío:** '¡Perfecto! Tu subtotal sería de COP [Subtotal]. El envío tiene un costo de COP 5.000. ¿Estamos de acuerdo?'.\n"
            f"    c. **¿Algo Más?:** '¿Alguna otra cosita que quieras añadir a tu pedido o algo más en lo que te pueda ayudar hoy? 😉'\n"
            f"5.  **RECOPILACIÓN DE DATOS (UNO POR UNO, CONVERSACIONAL):**\n"
            f"    - **SOLO SI EL USUARIO DICE 'NO' A '¿Algo Más?' Y QUIERE CONTINUAR**, empieza a pedir los datos. NO LOS PIDAS TODOS DE GOLPE.\n"
            f"    - **Nombre:** '¡De una! Para coordinar tu envío, ¿me regalas tu nombre completo, porfa?'\n"
            f"    - **Dirección:** '¡Gracias, [Nombre]! Ahora, ¿cuál es la dirección completa para el envío? (Incluye ciudad, barrio, apto/casa, y detalles extra 😉).'\n"
            f"    - **Teléfono:** '¡Anotadísimo! Y un número de teléfono de contacto.'\n"
            f"    - **Método de Pago:** '¡Ya casi, [Nombre]! Para el pago, ¿cómo te queda mejor? Aceptamos Nequi, Daviplata, Bancolombia, o pago contra entrega en efectivo (solo en [tu ciudad/área si aplica]).'\n"
            f"    - **IMPORTANTE:** Revisa el historial. Si ya tienes un dato, CONFÍRMALO. Ej: 'Confírmame tu cel, ¿sigue siendo 3001234567?'.\n"
            f"6.  **CONFIRMACIÓN FINAL DEL PEDIDO (ANTES DEL JSON):**\n"
            f"    - **CUANDO TENGAS TODOS LOS DATOS REQUERIDOS** ({', '.join(REQUIRED_ORDER_FIELDS_IN_JSON[:-2])}), resume TODO el pedido:\n"
            f"      '¡Listo, [Nombre]! ✨ Tu pedido es:\n"
            f"        [Lista de productos con cantidad y precio unitario]\n"
            f"        Subtotal: COP [Subtotal]\n"
            f"        Envío: COP 5.000\n"
            f"        **Total a Pagar: COP [Total Final]**\n"
            f"        Se enviará a: [Dirección Completa]\n"
            f"        Contacto: [Teléfono]\n"
            f"        Pago: [Método de Pago]\n\n"
            f"      ¿Está todo perfecto para que lo ingresemos? ¡Dime 'sí' o 'confirmo'!'\n"
            f"7.  **GENERACIÓN DEL JSON (SOLO TRAS CONFIRMACIÓN DEL USUARIO):**\n"
            f"    - **SI EL USUARIO RESPONDE 'SÍ', 'CONFIRMO', 'OK', 'LISTO' o similar, ENTONCES Y SÓLO ENTONCES**, tu respuesta DEBE terminar con este bloque JSON exacto (sin la palabra 'json' antes, ni comentarios, solo el bloque). La parte conversacional de tu respuesta debe ser algo como '¡Pedido confirmado! 🎉 Ya estamos preparando todo. ¡Gracias por tu compra!'.\n"
            f"      ```json\n"
            f"      {{\"order_details\":{{\"name\":\"NOMBRE_COMPLETO\",\"address\":\"DIRECCION_DETALLADA\",\"phone\":\"TELEFONO_CONTACTO\",\"payment_method\":\"METODO_PAGO_ELEGIDO\",\"products\":[{{\"name\":\"NOMBRE_PRODUCTO_1\",\"quantity\":CANTIDAD_1,\"price\":PRECIO_UNITARIO_1}}],\"total\":TOTAL_PEDIDO_NUMERICO}}}}\n"
            f"      ```\n"
            f"    - **SI EL USUARIO PIDE UN CAMBIO o NO CONFIRMA, NO INCLUYAS EL JSON.** Sigue la conversación.\n"
            f"8.  **RESPUESTAS GENERALES:** Siempre amable. Si no entiendes, pide clarificación.\n\n"
            f"**HISTORIAL DE CONVERSACIÓN RECIENTE (PARA TU CONTEXTO):**\n"
            # El historial real se pasa en la lista de mensajes a ask_gemini_with_history
        )

        llm_general_input_history = current_user_history[-8:] # Enviar últimos 4 intercambios (user/model)
        llm_input_messages_general = llm_general_input_history + [
            {"role": "user", "text": general_conversation_prompt_text}
        ]

        logger.info(f"🧠 Gemini (General/Order) - Enviando prompt...")
        
        llm_response_text_general = await ask_gemini_with_history(llm_input_messages_general)
        logger.info(f"🧠 Gemini (General/Order) - Raw Response: {llm_response_text_general}")

        order_data_from_llm_json, clean_text_for_user = extract_order_data(llm_response_text_general)
        
        model_response_timestamp = datetime.now(timezone.utc)

        if clean_text_for_user and clean_text_for_user.strip():
            send_whatsapp_message(from_number, clean_text_for_user)
            current_user_history.append({
                "role": "model",
                "text": clean_text_for_user,
                "time": model_response_timestamp.isoformat()
            })
            await save_message_to_supabase(from_number, "model", clean_text_for_user, timestamp=model_response_timestamp)
        else:
            logger.warning("LLM no proporcionó texto conversacional limpio para el usuario.")

        if order_data_from_llm_json and isinstance(order_data_from_llm_json.get("order_details"), dict):
            order_details_payload = order_data_from_llm_json["order_details"]
            logger.info(f"ℹ️ JSON 'order_details' extraído: {json.dumps(order_details_payload, indent=2)}")

            missing_json_fields = [
                field_name for field_name in REQUIRED_ORDER_FIELDS_IN_JSON
                if order_details_payload.get(field_name) is None or \
                   (isinstance(order_details_payload.get(field_name), str) and not str(order_details_payload.get(field_name)).strip()) or \
                   (field_name == "products" and (not isinstance(order_details_payload.get(field_name), list) or not order_details_payload.get(field_name)))
            ]
            
            if not missing_json_fields:
                logger.info(f"✅ Datos de pedido COMPLETOS en JSON. Cliente: {order_details_payload.get('name')}")
                
                result_from_process_order = await process_order(from_number, order_details_payload)
                order_status = result_from_process_order.get("status")
                
                if order_status == "created":
                    logger.info(f"✅ Pedido CREADO para {from_number}. ID: {result_from_process_order.get('order_id', 'N/A')}")
                    # El LLM ya envió el mensaje de confirmación. Aquí podemos añadir recomendaciones.
                    products_in_order = order_details_payload.get("products", [])
                    recommended_prods_list = await get_recommended_products(products_in_order) # Asegúrate que esta función toma la lista de productos del pedido
                    if recommended_prods_list:
                        rec_texts = [f"  - {r['name']} (COP {r.get('price', 0):,})" for r in recommended_prods_list]
                        recommendation_msg = (
                            f"✨ ¡Por cierto, {order_details_payload.get('name','cliente')}! Ya que tu pedido está en camino, "
                            f"quizás te interesen estos otros productos para una próxima ocasión:\n"
                            f"{chr(10).join(rec_texts)}\n\n"
                            "¡Avísame si algo te llama la atención! 😉"
                        )
                        send_whatsapp_message(from_number, recommendation_msg)

                elif order_status == "updated":
                     logger.info(f"♻️ Pedido ACTUALIZADO para {from_number}. ID: {result_from_process_order.get('order_id', 'N/A')}")
                
                elif order_status == "missing" or result_from_process_order.get("error"):
                    error_msg = result_from_process_order.get('fields', result_from_process_order.get('error', "Error desconocido al guardar."))
                    logger.error(f"❌ Error/Faltante desde process_order para {from_number}: {error_msg}")
                    # Este es un error del backend después de que el LLM confirmó.
                    send_whatsapp_message(from_number, f"¡Hola {order_details_payload.get('name','')}! Tuvimos un inconveniente técnico al registrar tu pedido en el sistema. 🛠️ No te preocupes, nuestro equipo ya está enterado. Si no te contactamos pronto, por favor escríbenos de nuevo. ¡Gracias por tu paciencia! 🙏")
                else:
                    logger.warning(f"⚠️ Estado no manejado de process_order: '{order_status}' para {from_number}")
            else:
                logger.warning(f"⚠️ JSON 'order_details' INCOMPLETO. Faltantes: {missing_json_fields}. Cliente: {order_details_payload.get('name', 'N/A')}. El LLM debería seguir pidiendo datos.")
                if not clean_text_for_user or "?" not in clean_text_for_user:
                     send_whatsapp_message(from_number, "Parece que aún nos faltan algunos detallitos para completar tu pedido. ¿Podemos continuar? 😊")
        
        elif not clean_text_for_user or not clean_text_for_user.strip(): # Si no hay ni JSON ni texto
            logger.error(f"LLM no proporcionó respuesta útil para: '{user_raw_text}'")
            send_whatsapp_message(from_number, "¡Uy! Parece que me enredé un poquito con tu último mensaje. 😅 ¿Podrías intentar decírmelo de otra forma, porfa?")

    except Exception as e_global:
        logger.critical(f"❌ [ERROR CRÍTICO GLOBAL en handle_user_message]: {e_global}", exc_info=True)
        try:
            send_whatsapp_message(from_number, "¡Ups! Algo no salió bien de mi lado y no pude procesar tu solicitud. 🤖 Un técnico ya fue notificado. Por favor, intenta de nuevo en un momento. ¡Lamento las molestias!")
        except Exception as e_send_fallback:
            logger.error(f"Falló el envío del mensaje de fallback de error global a {from_number}: {e_send_fallback}")