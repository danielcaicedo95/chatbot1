# app/services/conversation.py

from datetime import datetime, timezone # timezone añadido para consistencia
import json
import re
import traceback
from difflib import get_close_matches
import logging # Añadido para mejor logging

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history # Tu cliente Gemini con reintentos
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

# REQUIRED_FIELDS de tu código original, usado por process_order
REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]


# --- Nuevas Funciones de Catálogo y Matching (para la lógica de imágenes mejorada) ---

def build_structured_catalog_for_logic_v2(productos_list: list) -> list:
    """
    Construye un catálogo detallado para la lógica interna de imágenes.
    Similar a la versión que desarrollamos, pero nombrada v2 para este contexto.
    """
    structured_catalog = []
    if not productos_list: return structured_catalog
    for p_data in productos_list:
        try:
            variants_details = []
            for v_data in p_data.get("product_variants", []):
                opts = v_data.get("options", {})
                if not opts: continue
                display_label_parts = [f"{k_opt}:{v_opt_val}" for k_opt, v_opt_val in opts.items()]
                value_for_matching_parts = [str(v_opt_val).strip().lower() for v_opt_val in opts.values()]
                catalog_variant_label_parts = [f"{str(k_opt).strip().lower()}:{str(v_opt_val).strip().lower()}" for k_opt, v_opt_val in opts.items()]
                variants_details.append({
                    "id": v_data["id"], 
                    "display_label": ", ".join(display_label_parts),
                    "value_for_matching": " ".join(value_for_matching_parts),
                    "catalog_variant_label_for_images": ",".join(catalog_variant_label_parts),
                    "price": v_data.get("price"), "stock": v_data.get("stock"),
                    "images": [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") == v_data["id"]]
                })
            main_product_images = [img["url"] for img in p_data.get("product_images", []) if img.get("variant_id") is None]
            structured_catalog.append({
                "id": p_data["id"], "name": p_data["name"], "description": p_data.get("description"),
                "base_price": p_data.get("price"), "base_stock": p_data.get("stock"),
                "variants": variants_details, "main_images": main_product_images,
                "all_product_images_raw": p_data.get("product_images", [])
            })
        except Exception as e:
            logger.error(f"Error v2 construyendo catálogo: {p_data.get('name', 'ID desc')}: {e}", exc_info=True)
    return structured_catalog

def match_target_in_catalog_for_images_v2(s_catalog: list, query: str) -> tuple[dict | None, dict | None]:
    """
    Busca producto/variante para imágenes en el catálogo estructurado v2.
    """
    if not query or not s_catalog: return None, None
    target = query.strip().lower()
    for prod in s_catalog:
        p_name_low = prod["name"].lower()
        if p_name_low in target: # Si el nombre del producto está en el query
            if p_name_low == target: return prod, None # Coincidencia exacta del nombre del producto
            for var in prod["variants"]: # Buscar si también hay una variante
                if var["value_for_matching"] in target: return prod, var
            return prod, None # Si no se encontró variante específica, devolver solo el producto
        # Si el query es solo el valor de una variante (ej. "amarillo")
        for var in prod["variants"]:
            if var["value_for_matching"] == target: return prod, var
    
    # Búsqueda difusa simplificada
    choices = []
    item_map = {}
    for prod_idx, prod_entry in enumerate(s_catalog):
        # Producto
        prod_name_lower = prod_entry["name"].lower()
        choices.append(prod_name_lower)
        item_map[prod_name_lower] = (prod_entry, None)
        # Variantes
        for var_idx, var_entry in enumerate(prod_entry.get("variants", [])):
            var_val_match = var_entry["value_for_matching"]
            full_name = f"{prod_name_lower} {var_val_match}"
            choices.append(full_name)
            item_map[full_name] = (prod_entry, var_entry)
            if var_val_match not in item_map : # Solo añadir si no colisiona (priorizar nombre completo)
                choices.append(var_val_match)
                item_map[var_val_match] = (prod_entry, var_entry)

    unique_choices = list(set(choices))
    best_matches = get_close_matches(target, unique_choices, n=1, cutoff=0.65) # Ajustar cutoff según sea necesario

    if best_matches:
        matched_text = best_matches[0]
        logger.info(f"Coincidencia difusa v2 para imágenes de '{target}': '{matched_text}'")
        return item_map.get(matched_text, (None, None))
        
    return None, None

# --- Nueva Lógica de Manejo de Solicitud de Imágenes (Integrada) ---
async def handle_image_request_logic_v2(
    from_number: str, user_raw_text: str, current_history: list, structured_catalog_data_v2: list
) -> tuple[bool, str | None]: # Devuelve (manejado_exitosamente, mensaje_error_api | None)
    try:
        catalog_summary_for_llm = [{"name": p["name"], "variants": [v["display_label"] for v in p.get("variants", [])]} for p in structured_catalog_data_v2]
        image_intent_prompt = {
            "user_request": user_raw_text, "available_products_summary": catalog_summary_for_llm,
            "task": "Analiza 'user_request'. Si el usuario pide imágenes o fotos, responde con JSON: {\"want_images\": true, \"target\": \"nombre del producto o variante relevante\"}. Si no pide imágenes, responde: {\"want_images\": false}. Si pide fotos pero el target es ambiguo (ej: 'muéstrame una foto'), incluye 'clarification_needed': 'mensaje para pedir clarificación'.",
            "examples": [
                {"user": "¿Tienes fotos del tequila?", "bot_json": {"want_images": True, "target": "tequila"}},
                {"user": "Foto porfa", "bot_json": {"want_images": True, "target": "el producto que se estaba discutiendo", "clarification_needed": "¡Claro! ¿De qué producto o variante te gustaría ver la foto? 😊"}},
                {"user": "cuanto cuesta", "bot_json": {"want_images": False}}
            ]
        }
        # Usar una porción más corta del historial para esta detección de intención específica
        llm_input = current_history[-3:] + [{"role": "user", "text": json.dumps(image_intent_prompt, ensure_ascii=False)}]
        
        logger.info(f"🧠 Gemini (Image Intent V2) - Enviando solicitud...")
        llm_response_text = await ask_gemini_with_history(llm_input) # USA TU CLIENTE GEMINI ACTUALIZADO

        if isinstance(llm_response_text, str) and (llm_response_text.startswith("GEMINI_API_ERROR:") or llm_response_text.startswith("GEMINI_RESPONSE_ISSUE:")):
            logger.error(f"Error/Problema de Gemini (Image Intent V2): {llm_response_text}")
            return False, llm_response_text # Devolver el mensaje de error de la API

        logger.info(f"🧠 Gemini (Image Intent V2) - Raw Response: {llm_response_text}")
        json_match = re.search(r"\{[\s\S]*\}", llm_response_text)
        if not json_match:
            logger.warning("No JSON en respuesta LLM para V2 intención de imagen. Asumiendo NO quiere imágenes.")
            return False, None 
        
        try:
            action = json.loads(json_match.group())
        except json.JSONDecodeError:
            logger.error(f"Error decodificando JSON V2 intención imagen. Respuesta: {json_match.group()}")
            return False, None

        if not action.get("want_images"):
            logger.info("V2: Usuario no quiere imágenes según LLM.")
            return False, None # No quiere imágenes, no se maneja aquí, no es un error de API.

        if action.get("clarification_needed") and isinstance(action["clarification_needed"], str):
            send_whatsapp_message(from_number, action["clarification_needed"]) # SIN await
            logger.info(f"V2: Enviada solicitud de clarificación para imágenes: {action['clarification_needed']}")
            return True, None # Se manejó pidiendo clarificación

        target_description = action.get("target")
        if not target_description or not isinstance(target_description, str):
            logger.warning("V2: LLM indicó 'want_images' pero sin 'target' válido.")
            send_whatsapp_message(from_number, "¡Entendido! Quieres ver fotos. ¿Podrías decirme de qué producto o variante te gustaría verlas, por favor? 🤔") # SIN await
            return True, None 

        matched_product_cat_obj, matched_variant_cat_obj = match_target_in_catalog_for_images_v2(
            structured_catalog_data_v2, target_description
        )

        if not matched_product_cat_obj:
            send_whatsapp_message(from_number, f"Lo siento, no encontré '{target_description}' en nuestro catálogo para mostrarte imágenes. 😔") # SIN await
            logger.info(f"V2: Producto/variante '{target_description}' no encontrado para imágenes.")
            return True, None 

        image_urls_to_send = []
        display_name_for_caption = matched_product_cat_obj["name"]
        if matched_variant_cat_obj:
            display_name_for_caption = f"{matched_product_cat_obj['name']} ({matched_variant_cat_obj['display_label']})"
            image_urls_to_send.extend(matched_variant_cat_obj.get("images", []))
            if not image_urls_to_send: # Fallback
                variant_id_to_match = matched_variant_cat_obj["id"]
                variant_label_to_match_img = matched_variant_cat_obj["catalog_variant_label_for_images"].lower()
                for img_obj in matched_product_cat_obj.get("all_product_images_raw", []):
                    if img_obj.get("variant_id") == variant_id_to_match or \
                       (img_obj.get("variant_label") and img_obj.get("variant_label").lower() == variant_label_to_match_img):
                        if img_obj["url"] not in image_urls_to_send: image_urls_to_send.append(img_obj["url"])
        if not image_urls_to_send:
            image_urls_to_send.extend(matched_product_cat_obj.get("main_images", []))
        image_urls_to_send = list(set(image_urls_to_send)) # Únicos

        if not image_urls_to_send:
            send_whatsapp_message(from_number, f"No tenemos imágenes disponibles para *{display_name_for_caption}* en este momento. ¿Te puedo ayudar con algo más?") # SIN await
            logger.info(f"V2: No se encontraron URLs de imágenes para '{display_name_for_caption}'.")
            return True, None 

        send_whatsapp_message(from_number, f"¡Claro! Aquí tienes las imágenes de *{display_name_for_caption}*:") # SIN await
        for img_url in image_urls_to_send:
            try:
                send_whatsapp_image(from_number, img_url, caption=display_name_for_caption) # SIN await
            except Exception as e_img:
                logger.error(f"❌ V2 Error enviando imagen {img_url}: {e_img}", exc_info=True)
        return True, None # Manejado exitosamente (se enviaron imágenes o se notificó)
    except Exception as e_img_handler:
        logger.error(f"⚠️ Error crítico en handle_image_request_logic_v2: {e_img_handler}", exc_info=True)
        return False, f"GEMINI_API_ERROR: Error interno procesando imágenes ({type(e_img_handler).__name__})."


# --- Flujo Principal de Mensajes (Basado en tu código original) ---
async def handle_user_message(body: dict):
    gemini_api_error_msg_for_user = None # Para almacenar un mensaje de error de API si ocurre
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value_data = changes.get("value", {}) # Añadido para obtener el timestamp del mensaje
        messages = value_data.get("messages")

        if not messages:
            if value_data.get("statuses"): logger.info(f"Status update: {value_data['statuses']}")
            else: logger.info("Webhook sin 'messages'. Ignorando.")
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")
        
        msg_timestamp_unix = msg.get("timestamp")
        message_time = datetime.fromtimestamp(int(msg_timestamp_unix), tz=timezone.utc) if msg_timestamp_unix else datetime.now(timezone.utc)


        if not raw_text or not from_number:
            return

        current_user_history = user_histories.setdefault(from_number, [])
        current_user_history.append({
            "role": "user",
            "text": raw_text,
            "time": message_time.isoformat() # Usar message_time
        })
        await save_message_to_supabase(from_number, "user", raw_text) # Sin timestamp explícito

        productos_db = await get_all_products()
        if not productos_db:
            logger.warning("No se pudieron obtener los productos desde la DB.")
            # No enviar mensaje al usuario aquí, dejar que el flujo general lo maneje si es necesario
            return

        # --- INICIO DE LA INTEGRACIÓN DE MANEJO DE IMÁGENES ---
        # Usar las nuevas funciones de catálogo para la lógica de imágenes
        s_catalog_for_images = build_structured_catalog_for_logic_v2(productos_db)
        
        image_request_handled, api_error_from_images = await handle_image_request_logic_v2(
            from_number, raw_text, current_user_history, s_catalog_for_images
        )

        if api_error_from_images: # Si hubo un error de API al intentar manejar imágenes
            gemini_api_error_msg_for_user = "Lo siento, estoy teniendo un problema con el asistente de IA en este momento. 🛠️ Intenta más tarde."
            if "overloaded" in api_error_from_images or "ocupados" in api_error_from_images:
                gemini_api_error_msg_for_user = "Nuestros sistemas de IA están un poco ocupados ahora. 😅 Por favor, intenta en unos minutos."
            # No retornamos todavía, el flujo de pedido general podría aún funcionar si el error fue solo en el flujo de imágenes.
            # Pero si el flujo general también falla, este mensaje se usará.

        if image_request_handled:
            logger.info("Solicitud de imagen manejada. Finalizando.")
            return 
        # --- FIN DE LA INTEGRACIÓN DE MANEJO DE IMÁGENES ---

        # --- LÓGICA DE PEDIDO (BASADA EN TU CÓDIGO ORIGINAL) ---
        # Funciones de catálogo y matching de tu código original (usadas para el prompt de pedido)
        # `extract_labels` ya no es necesaria si `build_catalog_original` no la usa.
        # `choice_map` tampoco parece usarse en el flujo de pedido.
        
        def build_catalog_original_for_prompt(productos_param): # Renombrada para evitar colisión
            catalog_lines = []
            for p in productos_param:
                try:
                    variants = p.get("product_variants") or []
                    if not variants:
                        line = f"- {p['name']}: COP {p.get('price',0)} (stock {p.get('stock',0)})"
                    else:
                        line = f"- {p['name']}:"
                        opts = []
                        for v_item in variants:
                            price = v_item.get("price", p.get("price",0))
                            stock = v_item.get("stock", "N/A")
                            options_str = ",".join(f"{k}:{v2}" for k, v2 in v_item.get("options", {}).items())
                            opts.append(f"    • {options_str} — COP {price} (stock {stock})")
                        line += "\n" + "\n".join(opts)

                    # No añadir info de imágenes aquí si ya se manejó
                    catalog_lines.append(line)
                except Exception as e:
                    logger.warning(f"Error en build_catalog_original_for_prompt para {p.get('name')}: {e}")
            return "🛍️ Catálogo actual:\n\n" + "\n\n".join(catalog_lines)

        # Instrucciones para el LLM (de tu código original)
        # Ajustar el prompt para que NO intente manejar imágenes aquí, ya se hizo.
        # Y para que use `build_catalog_original_for_prompt`.
        order_context_for_llm = build_catalog_original_for_prompt(productos_db)
        instrucciones_pedido_original = (
            f"MENSAJE DEL USUARIO: \"{raw_text}\"\n\n"
            f"{order_context_for_llm}\n\n"
            "INSTRUCCIONES PARA EL BOT (VENDEDOR AMIGABLE):\n"
            "1. Si un producto no está disponible (stock 0 o N/A), informa y sugiere una alternativa del catálogo si es posible.\n"
            "2. Si hay intención de compra, detalla en tu respuesta: Productos, cantidad y precio unitario. Calcula un subtotal. Informa que el envío cuesta COP 5.000 y añádelo al total.\n"
            "3. Pregunta SIEMPRE '¿Deseas algo más?' después de confirmar un ítem o el carrito.\n"
            "4. **SOLO si el usuario responde 'no', 'solo eso', 'nada más' o similar a '¿Deseas algo más?', PROCEDE A PEDIR LOS DATOS DE ENVÍO**: Nombre completo, dirección detallada, teléfono de contacto y método de pago (Nequi, Daviplata, Bancolombia, contraentrega en [tu ciudad]). Pide estos datos UNO POR UNO.\n"
            "5. Cuando tengas TODOS los datos del paso 4 y el carrito esté armado, resume TODO el pedido (productos, total, datos de envío) y pregunta: '¿Está todo correcto para confirmar tu pedido?'.\n"
            "6. **SI EL USUARIO CONFIRMA EL RESUMEN ('sí', 'ok', 'confirmo'), ENTONCES Y SÓLO ENTONCES**, tu respuesta DEBE terminar con este bloque JSON EXACTO (sin 'json' antes, ni comentarios):\n"
            "   ```json\n"
            "   {{\"order_details\":{{\"name\":\"NOMBRE_COMPLETO\",\"address\":\"DIRECCION_DETALLADA\",\"phone\":\"TELEFONO_CONTACTO\",\"payment_method\":\"METODO_PAGO_ELEGIDO\",\"products\":[{{\"name\":\"NOMBRE_PRODUCTO_1\",\"quantity\":CANTIDAD_NUMERICA,\"price\":PRECIO_UNITARIO_NUMERICO}}],\"total\":TOTAL_PEDIDO_NUMERICO}}}}\n"
            "   ```\n"
            "   Tu texto conversacional ANTES del JSON debe ser una confirmación. Ej: '¡Perfecto! Pedido confirmado. 🎉'\n"
            "7. Si el usuario solo pregunta o conversa, responde amablemente sin forzar la venta. Usa emojis.\n"
            "8. **NO intentes mostrar imágenes ni procesar solicitudes de imágenes aquí. Eso ya se manejó o no se pidió.**"
            "9. **Recomendación Sutil**: Después de que el usuario añada el primer producto y ANTES de preguntar '¿Deseas algo más?', puedes sugerir UN producto complementario del catálogo de forma sutil. Ejemplo: '¡Buena elección! Para acompañar tu [producto], ¿qué tal un [otro producto]? O si prefieres, seguimos.' SOLO UNA VEZ POR PEDIDO."
        )

        # Obtener respuesta de Gemini para el pedido
        history_for_order_llm = current_user_history[-8:] # Usar historial relevante
        llm_response_order = await ask_gemini_with_history(history_for_order_llm + [{"role": "user", "text": instrucciones_pedido_original}])

        if isinstance(llm_response_order, str) and (llm_response_order.startswith("GEMINI_API_ERROR:") or llm_response_order.startswith("GEMINI_RESPONSE_ISSUE:")):
            logger.error(f"Error de Gemini (Order Processing): {llm_response_order}")
            # Usar el error de API de la etapa de imagen si existió, sino el actual
            final_api_error_msg = gemini_api_error_msg_for_user if gemini_api_error_msg_for_user else \
                                  ("Lo siento, el asistente de IA está teniendo problemas. 🛠️ Intenta más tarde." if "overloaded" not in llm_response_order else \
                                   "Nuestros sistemas de IA están ocupados. 😅 Intenta en unos minutos.")
            
            send_whatsapp_message(from_number, final_api_error_msg) # SIN await
            # Guardar este mensaje de error
            model_err_time = datetime.now(timezone.utc)
            current_user_history.append({"role": "model", "text": final_api_error_msg, "time": model_err_time.isoformat()})
            await save_message_to_supabase(from_number, "model", final_api_error_msg)
            return

        logger.info(f"🧠 Gemini (Order Processing) - Raw Response: {llm_response_order}")

        # Extraer datos del pedido y texto limpio de la respuesta del LLM
        # `order_data_payload` debería ser el contenido de `order_details` si está presente
        order_data_payload, clean_text_for_user = extract_order_data(llm_response_order)
        model_response_time = datetime.now(timezone.utc) # Timestamp para la respuesta del modelo

        # Enviar respuesta del LLM al usuario (si hay texto)
        if clean_text_for_user and clean_text_for_user.strip():
            send_whatsapp_message(from_number, clean_text_for_user) # SIN await
            current_user_history.append({
                "role": "model",
                "text": clean_text_for_user,
                "time": model_response_time.isoformat()
            })
            await save_message_to_supabase(from_number, "model", clean_text_for_user)
        elif gemini_api_error_msg_for_user: # Si hubo error en imagen y no hubo respuesta de pedido
             send_whatsapp_message(from_number, gemini_api_error_msg_for_user) # SIN await
             # Guardar este error
             model_err_time = datetime.now(timezone.utc)
             current_user_history.append({"role": "model", "text": gemini_api_error_msg_for_user, "time": model_err_time.isoformat()})
             await save_message_to_supabase(from_number, "model", gemini_api_error_msg_for_user)
             return


        # Lógica de tu código original para recomendaciones y `process_order`
        # `order_data_payload` es lo que `extract_order_data` devuelve como datos del pedido.
        # Debe ser el diccionario contenido en `order_details`.
        
        actual_order_details = None
        if order_data_payload and isinstance(order_data_payload, dict):
            if "order_details" in order_data_payload and isinstance(order_data_payload["order_details"], dict):
                actual_order_details = order_data_payload["order_details"]
            # Si extract_order_data ya devuelve el payload de order_details directamente:
            elif all(k in order_data_payload for k in ["name", "products", "total"]): 
                actual_order_details = order_data_payload
        
        # Sección de recomendaciones (como en tu código original)
        # Se activa si hay un JSON de pedido, pero ANTES de `process_order` si quieres que sea parte del "¿Algo más?".
        # Sin embargo, para que sea MENOS fastidioso, la moví a DESPUÉS de que el pedido es CREADO.
        # El prompt ya tiene la instrucción de recomendar sutilmente UNA VEZ.

        if actual_order_details: # Si el LLM generó el JSON `order_details`
            logger.info(f"🛍️ Payload de pedido para procesar: {json.dumps(actual_order_details, indent=2)}")
            # `process_order` se encarga de la validación final y guardado.
            result = await process_order(from_number, actual_order_details)
            status = result.get("status")

            # Mensajes según el status de `process_order` (de tu código original)
            if status == "missing":
                campos = "\n".join(f"- {f.replace('_',' ')}" for f in result.get("fields", []))
                send_whatsapp_message(from_number, f"📋 ¡Casi! Para tu pedido faltan: {campos}. ¿Podrías indicarlos?") # SIN await
            elif status == "created":
                # El LLM ya envió "Pedido confirmado". Aquí la recomendación post-pedido.
                logger.info(f"✅ Pedido CREADO para {from_number} por process_order.")
                products_in_final_order = actual_order_details.get("products", [])
                if products_in_final_order:
                    recommendations = await get_recommended_products(products_in_final_order)
                    if recommendations:
                        rec_texts = [f"- {r['name']} (COP {r.get('price', 0):,})" for r in recommendations]
                        send_whatsapp_message(from_number, f"✨ ¡Para tu próxima compra! También te podrían gustar:\n{chr(10).join(rec_texts)}\n¡Avísame si te interesa alguno! 😉") # SIN await
            elif status == "updated":
                logger.info(f"♻️ Pedido ACTUALIZADO para {from_number} por process_order.")
                # El LLM debería haber manejado el mensaje de actualización.
            elif status == "error":
                logger.error(f"❌ Error desde process_order: {result.get('error', 'Desconocido')}")
                send_whatsapp_message(from_number, "Tuvimos un problema al guardar tu pedido en el sistema. 🛠️ Por favor, intenta de nuevo o contacta a un asesor.") # SIN await
            else:
                logger.warning(f"⚠️ Estado inesperado de process_order: {status}. Resultado: {result}")
        
        # Si no hubo `actual_order_details` (el LLM no generó el JSON de pedido) Y
        # no hubo texto de respuesta del LLM Y no hubo un error de API previo que ya se manejó.
        elif not (clean_text_for_user and clean_text_for_user.strip()) and not gemini_api_error_msg_for_user:
            logger.error(f"LLM no proporcionó respuesta útil (ni texto, ni JSON de pedido, ni error API previo) para: '{raw_text}'")
            send_whatsapp_message(from_number, "¡Uy! Parece que me enredé un poquito. 😅 ¿Podrías decírmelo de otra forma, porfa?")


    except Exception as e_global:
        logger.critical(f"❌ [ERROR CRÍTICO GLOBAL en handle_user_message]: {e_global}", exc_info=True)
        final_fallback_msg = gemini_api_error_msg_for_user if gemini_api_error_msg_for_user else \
                             "¡Ups! Algo no salió bien de mi lado. 🤖 Un técnico fue notificado. Intenta en un momento."
        try:
            send_whatsapp_message(from_number, final_fallback_msg) # SIN await
            # Guardar este error
            model_fb_time = datetime.now(timezone.utc)
            current_user_history = user_histories.setdefault(from_number, []) # Asegurar que exista
            current_user_history.append({"role": "model", "text": final_fallback_msg, "time": model_fb_time.isoformat()})
            await save_message_to_supabase(from_number, "model", final_fallback_msg)
        except Exception as e_send_fb:
            logger.error(f"Falló el envío del mensaje de fallback global a {from_number}: {e_send_fb}")