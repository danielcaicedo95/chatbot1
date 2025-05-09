# app/services/conversation.py

from datetime import datetime, timezone
import json
import re
import traceback
from difflib import get_close_matches
import logging

from app.utils.memory import user_histories # Asumo que user_orders y user_pending_data se manejan en orders.py
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order # Esta es tu función clave de services/orders.py
from app.utils.extractors import extract_order_data

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Campos que el LLM debe intentar obtener para completar un pedido ANTES de generar el JSON
# El JSON final que espera process_order puede ser ligeramente diferente (lo define services/orders.py)
# Esto es para guiar al LLM en la conversación.
REQUIRED_CONVERSATIONAL_ORDER_FIELDS = ["name", "address", "phone", "payment_method"]


# --- Funciones Auxiliares de Catálogo y Matching ---

def build_structured_catalog_for_logic(productos_list: list) -> list:
    """
    Construye un catálogo detallado para la lógica interna (matching, búsqueda de imágenes).
    productos_list: lista de productos como viene de tu BD/API.
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
                    continue
                
                display_label_parts = [f"{k_opt}:{v_opt_val}" for k_opt, v_opt_val in opts.items()]
                # Para matching simple, usamos los valores de las opciones en minúscula
                value_for_matching_parts = [str(v_opt_val).strip().lower() for v_opt_val in opts.values()]
                # Para matching con product_images.variant_label (ej: "option:amarillo")
                catalog_variant_label_parts = [f"{str(k_opt).strip().lower()}:{str(v_opt_val).strip().lower()}" for k_opt, v_opt_val in opts.items()]

                variants_details.append({
                    "id": v_data["id"], # ID de la variante
                    "display_label": ", ".join(display_label_parts), # Ej: "option:Amarillo"
                    "value_for_matching": " ".join(value_for_matching_parts), # Ej: "amarillo"
                    "catalog_variant_label_for_images": ",".join(catalog_variant_label_parts), # Ej: "option:amarillo"
                    "price": v_data.get("price"),
                    "stock": v_data.get("stock"),
                    # Imágenes específicas de esta variante (si las hay directamente en product_images por variant_id)
                    "images": [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") == v_data["id"]]
                })
            
            main_product_images = [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") is None]

            structured_catalog.append({
                "id": p_data["id"], # ID del producto
                "name": p_data["name"],
                "description": p_data.get("description"),
                "base_price": p_data.get("price"),
                "base_stock": p_data.get("stock"),
                "variants": variants_details,
                "main_images": main_product_images,
                "all_product_images_raw": p_data.get("product_images", []) # Todas las imágenes del producto para búsquedas
            })
        except Exception as e:
            logger.error(f"Error construyendo catálogo estructurado para producto {p_data.get('name', 'ID desconocido')}: {e}", exc_info=True)
            continue
    return structured_catalog

def format_catalog_for_llm_context(s_catalog: list) -> list:
    """Formatea el catálogo para que el LLM lo use como contexto en los prompts."""
    llm_catalog_summary = []
    for p_entry in s_catalog:
        variants_summary = []
        for v_entry in p_entry.get("variants", []):
            variants_summary.append(
                f"{v_entry['display_label']} (Precio: {v_entry.get('price', 'N/A')}, Stock: {v_entry.get('stock', 'N/A')})"
            )
        
        product_info = {
            "name": p_entry["name"],
            "description": p_entry.get("description", "No disponible"),
            "price_info": f"Desde COP {p_entry.get('base_price')}" if p_entry.get('base_price') and not variants_summary else "Ver variantes",
            "stock_info": f"Base: {p_entry.get('base_stock')}" if p_entry.get('base_stock') and not variants_summary else "Ver variantes",
            "variants": variants_summary if variants_summary else "No tiene variantes específicas listadas.",
            "images_available": bool(p_entry.get("main_images") or any(v.get("images") for v in p_entry.get("variants",[])) or p_entry.get("all_product_images_raw"))
        }
        llm_catalog_summary.append(product_info)
    return llm_catalog_summary


def match_target_in_catalog_for_images(
    structured_catalog_for_logic: list, 
    target_query_str: str
) -> tuple[dict | None, dict | None]:
    """
    Busca un producto y opcionalmente una variante en el catálogo para mostrar imágenes.
    Devuelve (producto_encontrado_del_catalogo_estructurado, variante_encontrada_del_catalogo_estructurado | None).
    """
    if not target_query_str or not structured_catalog_for_logic:
        return None, None
    
    target_lower = target_query_str.strip().lower()

    # Búsqueda por nombre de producto y/o valor de variante
    for prod_entry in structured_catalog_for_logic:
        prod_name_lower = prod_entry["name"].lower()
        # Caso 1: El query es el nombre del producto y posiblemente una variante
        if prod_name_lower in target_lower:
            # Si el query es solo el nombre del producto
            if prod_name_lower == target_lower:
                 return prod_entry, None 
            # Buscar si también menciona una variante de este producto
            for var_entry in prod_entry["variants"]:
                # var_entry["value_for_matching"] es ej: "amarillo"
                if var_entry["value_for_matching"] in target_lower:
                    return prod_entry, var_entry
            # Si el nombre del producto estaba en el query pero no se encontró variante específica, devolver solo el producto
            return prod_entry, None 
        
        # Caso 2: El query es solo el valor de una variante (ej. "amarillo")
        # Esto es más propenso a ambigüedad si varios productos tienen la misma variante.
        # Aquí devolvemos la primera coincidencia. Se podría mejorar pidiendo clarificación.
        for var_entry in prod_entry["variants"]:
            if var_entry["value_for_matching"] == target_lower:
                return prod_entry, var_entry

    # Búsqueda difusa como último recurso
    all_matchable_texts = []
    # (texto_para_match, objeto_producto_catalogo, objeto_variante_catalogo | None)
    for prod_entry in structured_catalog_for_logic:
        all_matchable_texts.append((prod_entry["name"].lower(), prod_entry, None))
        for var_entry in prod_entry["variants"]:
            full_name = f"{prod_entry['name'].lower()} {var_entry['value_for_matching']}"
            all_matchable_texts.append((full_name, prod_entry, var_entry))
            all_matchable_texts.append((var_entry['value_for_matching'], prod_entry, var_entry)) # Variante sola

    choices_for_diff = list(set([item[0] for item in all_matchable_texts]))
    best_matches = get_close_matches(target_lower, choices_for_diff, n=1, cutoff=0.65)

    if best_matches:
        matched_text = best_matches[0]
        for item_text, prod_obj, var_obj in all_matchable_texts:
            if item_text == matched_text:
                logger.info(f"Coincidencia difusa para imágenes de '{target_lower}': '{matched_text}'")
                return prod_obj, var_obj
    
    return None, None


# --- Lógica de Manejo de Solicitud de Imágenes (Basada en tu segundo código funcional) ---
async def handle_image_request_logic(
    from_number: str,
    user_raw_text: str,
    current_history: list,
    structured_catalog_data: list # Usar el catálogo detallado
) -> bool:
    try:
        # Prompt para el LLM para determinar si se quieren imágenes
        catalog_summary_for_llm = [
            {"name": p["name"], "variants": [v["display_label"] for v in p.get("variants", [])]}
            for p in structured_catalog_data
        ]
        
        image_intent_prompt = {
            "user_request": user_raw_text,
            "available_products_summary": catalog_summary_for_llm,
            "task": "Analiza 'user_request'. Si el usuario pide imágenes/fotos, responde con JSON: {\"want_images\": true, \"target\": \"nombre producto/variante\"}. Si no, {\"want_images\": false}. Si pide fotos pero el target no es claro (ej: 'muéstrame una foto'), añade 'clarification_needed': 'mensaje de clarificación'.",
            "examples": [
                {"user": "¿Tienes fotos del tequila?", "bot_json": {"want_images": True, "target": "tequila"}},
                {"user": "foto porfa", "bot_json": {"want_images": True, "target": "producto del contexto", "clarification_needed": "¡Claro! ¿De qué producto o variante te gustaría ver la foto? 😊"}},
            ]
        }

        llm_input_img_intent = current_history[-3:] + [{"role": "user", "text": json.dumps(image_intent_prompt, ensure_ascii=False)}]
        
        logger.info(f"🧠 Gemini (Image Intent) - Input: ...") # Evitar loggear todo el prompt aquí por brevedad
        llm_response_text = await ask_gemini_with_history(llm_input_img_intent)
        logger.info(f"🧠 Gemini (Image Intent) - Raw Response: {llm_response_text}")

        json_match = re.search(r"\{[\s\S]*\}", llm_response_text)
        if not json_match:
            logger.warning("No JSON en respuesta de LLM para intención de imagen.")
            return False 
        
        try:
            action = json.loads(json_match.group())
        except json.JSONDecodeError:
            logger.error(f"Error decodificando JSON de intención de imagen. Respuesta: {json_match.group()}")
            return False

        if not action.get("want_images"):
            return False # No quiere imágenes

        if action.get("clarification_needed") and isinstance(action["clarification_needed"], str):
            send_whatsapp_message(from_number, action["clarification_needed"])
            return True 

        target_description = action.get("target")
        if not target_description or not isinstance(target_description, str):
            send_whatsapp_message(from_number, "¡Entendido! Quieres ver fotos. ¿Podrías decirme de qué producto o variante te gustaría verlas, por favor? 🤔")
            return True 

        # Usar el catálogo ESTRUCTURADO para encontrar el producto y las URLs
        # `match_target_in_catalog_for_images` es la función que definimos antes
        matched_product_cat_obj, matched_variant_cat_obj = match_target_in_catalog_for_images(
            structured_catalog_data, target_description
        )

        if not matched_product_cat_obj:
            send_whatsapp_message(from_number, f"Lo siento, no encontré '{target_description}' en nuestro catálogo para mostrarte imágenes. 😔")
            return True

        image_urls_to_send = []
        display_name_for_caption = matched_product_cat_obj["name"]

        if matched_variant_cat_obj:
            display_name_for_caption = f"{matched_product_cat_obj['name']} ({matched_variant_cat_obj['display_label']})"
            image_urls_to_send.extend(matched_variant_cat_obj.get("images", [])) # Imágenes directas de la variante
            
            # Fallback: buscar en todas las imágenes del producto por ID o label de variante
            if not image_urls_to_send:
                variant_id_to_match = matched_variant_cat_obj["id"]
                # label como "option:amarillo"
                variant_label_to_match_img = matched_variant_cat_obj["catalog_variant_label_for_images"].lower() 
                
                for img_obj in matched_product_cat_obj.get("all_product_images_raw", []):
                    if img_obj.get("variant_id") == variant_id_to_match or \
                       (img_obj.get("variant_label") and img_obj.get("variant_label").lower() == variant_label_to_match_img):
                        if img_obj["url"] not in image_urls_to_send:
                             image_urls_to_send.append(img_obj["url"])
        
        # Si no hay imágenes de variante (o no se pidió variante), usar imágenes principales del producto
        if not image_urls_to_send:
            image_urls_to_send.extend(matched_product_cat_obj.get("main_images", []))
            image_urls_to_send = list(set(image_urls_to_send)) # Eliminar duplicados

        if not image_urls_to_send:
            send_whatsapp_message(from_number, f"No tenemos imágenes disponibles para *{display_name_for_caption}* en este momento. ¿Te puedo ayudar con algo más?")
            return True 

        send_whatsapp_message(from_number, f"¡Claro! Aquí tienes las imágenes de *{display_name_for_caption}*:")
        for img_url in image_urls_to_send:
            try:
                send_whatsapp_image(from_number, img_url, caption=display_name_for_caption)
            except Exception as e_img:
                logger.error(f"❌ Error enviando imagen {img_url}: {e_img}", exc_info=True)
        return True
    except Exception as e_img_handler:
        logger.error(f"⚠️ Error en handle_image_request_logic: {e_img_handler}", exc_info=True)
        return False


# --- Flujo Principal de Mensajes ---
async def handle_user_message(body: dict):
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value_data = changes.get("value", {})
        messages = value_data.get("messages")

        if not messages:
            # Manejar actualizaciones de estado si es necesario, o ignorar
            if value_data.get("statuses"): logger.info(f"Status update: {value_data['statuses']}")
            else: logger.info("Webhook sin 'messages'. Ignorando.")
            return

        msg = messages[0]
        user_raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        
        # Usar timestamp del mensaje si está disponible, sino el actual.
        # WhatsApp envía timestamp en segundos.
        msg_timestamp_unix = msg.get("timestamp")
        if msg_timestamp_unix:
            message_time = datetime.fromtimestamp(int(msg_timestamp_unix), tz=timezone.utc)
        else:
            message_time = datetime.now(timezone.utc)

        if not user_raw_text or not from_number:
            logger.warning("Mensaje sin texto o remitente. Ignorando.")
            return

        logger.info(f"Mensaje de {from_number} ({message_time.isoformat()}): '{user_raw_text}'")

        current_user_history = user_histories.setdefault(from_number, [])
        current_user_history.append({
            "role": "user",
            "text": user_raw_text,
            "time": message_time.isoformat() # Guardar con zona horaria
        })
        # CORRECCIÓN: save_message_to_supabase según tu primer código no lleva timestamp.
        # La función en supabase.py o la DB se encarga de 'created_at'.
        await save_message_to_supabase(from_number, "user", user_raw_text)

        productos_db_data = await get_all_products()
        if not productos_db_data:
            logger.error("CRÍTICO: No se pudieron obtener productos.")
            send_whatsapp_message(from_number, "Lo siento, estoy teniendo problemas técnicos para acceder a nuestro catálogo en este momento. 🛠️ Por favor, inténtalo más tarde.")
            return
        
        # Catálogo estructurado para lógica interna (imágenes, etc.)
        s_catalog_logic = build_structured_catalog_for_logic(productos_db_data)

        # 1. Intentar manejar como solicitud de imagen primero
        image_request_was_handled = await handle_image_request_logic(
            from_number, user_raw_text, current_user_history, s_catalog_logic
        )
        if image_request_was_handled:
            logger.info("Solicitud de imagen manejada. Finalizando.")
            return

        # 2. Si no fue imagen, procesar como conversación general/pedido
        logger.info(f"Procesando como mensaje general/pedido para {from_number}")

        # Catálogo formateado para el prompt del LLM general
        catalog_for_llm_prompt = format_catalog_for_llm_context(s_catalog_logic)
        
        # INSTRUCCIONES PARA EL LLM (TOMANDO LA BASE DE TU PRIMER CÓDIGO FUNCIONAL PARA PEDIDOS)
        # Y añadiendo la naturalidad y el flujo conversacional que buscamos.
        order_taking_instructions = (
            f"Eres 'VendiBot', un asistente de ventas virtual amigable y eficiente para una licorera. Tu tono es cercano y usas emojis. Ayuda al cliente a encontrar productos y tomar su pedido.\n\n"
            f"MENSAJE ACTUAL DEL USUARIO: \"{user_raw_text}\"\n\n"
            f"NUESTRO CATÁLOGO (Precios en COP, stock indicado):\n{json.dumps(catalog_for_llm_prompt, indent=2, ensure_ascii=False)}\n\n"
            f"**INSTRUCCIONES PARA TOMAR EL PEDIDO:**\n"
            f"1.  **Identifica Intención de Compra:** Si el usuario quiere comprar (ej: 'quiero X', 'me llevo Y'), confirma los productos, cantidades y precios. Calcula un subtotal.\n"
            f"2.  **Costo de Envío:** Informa que el envío cuesta COP 5.000 y súmalo al total.\n"
            f"3.  **¿Algo Más?:** Pregunta si desea añadir algo más. Si es así, vuelve al paso 1 con los nuevos productos. Recomienda SUTILMENTE UN producto adicional si es apropiado.\n"
            f"4.  **Pedir Datos (SI DICE 'NO' a '¿Algo Más?'):** Si el cliente está listo para finalizar, PIDE SECUENCIALMENTE (uno por uno, esperando respuesta) los siguientes datos:\n"
            f"    - Nombre completo.\n"
            f"    - Dirección detallada (con ciudad/barrio si es relevante).\n"
            f"    - Número de teléfono de contacto.\n"
            f"    - Método de pago preferido (ej: Nequi, Daviplata, Bancolombia, contraentrega en efectivo en [tu ciudad]).\n"
            f"    *Importante: Revisa el historial para no pedir datos ya dados. Si ya tienes un dato, confírmalo.*\n"
            f"5.  **Confirmación Final y JSON (SOLO SI TIENES TODOS LOS DATOS DEL PASO 4 Y EL CARRITO ESTÁ ARMADO):\n**"
            f"    - Resume el pedido completo (productos, cantidades, precios, subtotal, envío, total, y todos los datos de envío del cliente).\n"
            f"    - Pregunta: '¿Está todo correcto para confirmar tu pedido?'.\n"
            f"    - **SI EL USUARIO CONFIRMA ('sí', 'ok', 'confirmo'), ENTONCES Y SÓLO ENTONCES**, tu respuesta DEBE terminar con este bloque JSON EXACTO (sin 'json' antes, ni comentarios):\n"
            f"      ```json\n"
            f"      {{\"order_details\":{{\"name\":\"NOMBRE_COMPLETO\",\"address\":\"DIRECCION_DETALLADA\",\"phone\":\"TELEFONO_CONTACTO\",\"payment_method\":\"METODO_PAGO_ELEGIDO\",\"products\":[{{\"name\":\"NOMBRE_PRODUCTO_1\",\"quantity\":CANTIDAD_1,\"price\":PRECIO_UNITARIO_1}}],\"total\":TOTAL_PEDIDO_NUMERICO}}}}\n"
            f"      ```\n"
            f"    - Tu texto conversacional ANTES del JSON debe ser una confirmación. Ej: '¡Perfecto! Pedido confirmado. 🎉 Ya estamos alistando todo.'\n"
            f"    - **SI EL USUARIO NO CONFIRMA O PIDE CAMBIOS, NO INCLUYAS EL JSON.** Continúa la conversación para ajustar.\n"
            f"6.  **Otras Interacciones:** Si solo pregunta por productos, precios, o charla, responde amablemente sin forzar el pedido. Si un producto está agotado, sugiere alternativas.\n"
            f"7.  **Imágenes:** Si el usuario pide fotos y no se manejó antes, puedes decir: 'Claro, te puedo mostrar fotos. ¿De qué producto te gustaría ver?' (No necesitas generar JSON aquí, solo responder)."
        )

        llm_general_history = current_user_history[-8:] # Últimos 4 intercambios
        llm_input_general_conv = llm_general_history + [{"role": "user", "text": order_taking_instructions}]

        logger.info(f"🧠 Gemini (General/Order) - Enviando prompt...")
        llm_response_general = await ask_gemini_with_history(llm_input_general_conv)
        logger.info(f"🧠 Gemini (General/Order) - Raw Response: {llm_response_general}")

        # `extract_order_data` debe separar el JSON de `order_details` del texto conversacional.
        # El JSON es el que se enviará a `process_order`.
        order_data_for_processing, llm_text_response_to_user = extract_order_data(llm_response_general)
        
        model_response_time = datetime.now(timezone.utc)

        # Enviar respuesta del LLM al usuario (si hay texto)
        if llm_text_response_to_user and llm_text_response_to_user.strip():
            send_whatsapp_message(from_number, llm_text_response_to_user)
            current_user_history.append({
                "role": "model",
                "text": llm_text_response_to_user,
                "time": model_response_time.isoformat()
            })
            # CORRECCIÓN: save_message_to_supabase según tu primer código no lleva timestamp.
            await save_message_to_supabase(from_number, "model", llm_text_response_to_user)
        else:
            logger.warning("LLM no proporcionó texto conversacional para el usuario.")

        # Procesar el pedido si el LLM generó el JSON `order_details`
        # `order_data_for_processing` aquí debe ser el diccionario `order_details` directamente,
        # o si `extract_order_data` devuelve `{"order_details": {...}}`, entonces `order_data_for_processing.get("order_details")`
        
        # Ajustar según lo que devuelva tu `extract_order_data`.
        # Asumiré que `extract_order_data` devuelve el payload de `order_details` directamente si lo encuentra, o None.
        final_order_payload = None
        if order_data_for_processing and isinstance(order_data_for_processing, dict):
            if "order_details" in order_data_for_processing and isinstance(order_data_for_processing["order_details"], dict):
                final_order_payload = order_data_for_processing["order_details"]
            elif all(k in order_data_for_processing for k in ["name", "products", "total"]): # Si ya es el payload directo
                final_order_payload = order_data_for_processing


        if final_order_payload:
            logger.info(f"🛍️ Payload de pedido para procesar: {json.dumps(final_order_payload, indent=2)}")
            
            # `process_order` se encarga de validar campos faltantes internamente y fusionar con `user_pending_data`
            # según tu `services/orders.py`.
            result_order_processing = await process_order(from_number, final_order_payload)
            status_from_processing = result_order_processing.get("status")
            
            # Mensajes basados en el status de `process_order` (como en tu primer código)
            if status_from_processing == "missing":
                missing_fields = result_order_processing.get("fields", [])
                campos_str = ", ".join([f.replace('_',' ') for f in missing_fields])
                send_whatsapp_message(from_number, f"📋 ¡Casi listo! Para completar tu pedido, aún necesitamos estos datos: {campos_str}. ¿Podrías proporcionarlos, por favor?")
            elif status_from_processing == "created":
                # El LLM ya debería haber enviado el mensaje de "Pedido confirmado"
                # Aquí podrías solo loggear o enviar un mensaje adicional si es necesario
                logger.info(f"✅ Pedido CREADO para {from_number} vía process_order.")
                # Activar recomendaciones
                products_ordered = final_order_payload.get("products", [])
                if products_ordered:
                    recommendations = await get_recommended_products(products_ordered)
                    if recommendations:
                        rec_text_parts = [f"- {r['name']} (COP {r.get('price', 0):,})" for r in recommendations]
                        send_whatsapp_message(from_number, f"✨ ¡Excelente! Para complementar tu pedido, también te podrían interesar:\n{chr(10).join(rec_text_parts)}\n¿Te animas por alguno más? 😉")

            elif status_from_processing == "updated":
                logger.info(f"♻️ Pedido ACTUALIZADO para {from_number} vía process_order.")
            elif status_from_processing == "error":
                logger.error(f"❌ Error desde process_order para {from_number}: {result_order_processing.get('error', 'Error desconocido')}")
                send_whatsapp_message(from_number, "¡Ups! Tuvimos un pequeño inconveniente técnico al registrar tu pedido en nuestro sistema. 🛠️ Por favor, inténtalo de nuevo o contacta a soporte. ¡Gracias por tu paciencia!")
            else:
                logger.warning(f"⚠️ Estado no manejado de process_order: '{status_from_processing}' para {from_number}")
        
        elif not llm_text_response_to_user or not llm_text_response_to_user.strip(): # Si no hubo ni JSON de pedido ni texto
            logger.error(f"LLM no proporcionó respuesta útil (ni texto, ni JSON de pedido) para: '{user_raw_text}'")
            send_whatsapp_message(from_number, "¡Uy! Parece que me enredé un poquito con tu último mensaje. 😅 ¿Podrías intentar decírmelo de otra forma, porfa?")

    except Exception as e_global:
        logger.critical(f"❌ [ERROR CRÍTICO GLOBAL en handle_user_message]: {e_global}", exc_info=True)
        try:
            send_whatsapp_message(from_number, "¡Ups! Algo no salió bien de mi lado y no pude procesar tu solicitud. 🤖 Un técnico ya fue notificado. Por favor, intenta de nuevo en un momento. ¡Lamento las molestias!")
        except Exception as e_send_fallback:
            logger.error(f"Falló el envío del mensaje de fallback de error global a {from_number}: {e_send_fallback}")