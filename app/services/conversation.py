# app/services/conversation.py

from datetime import datetime
import json
import re
import traceback
from difflib import get_close_matches

from app.utils.memory import user_histories
from app.clients.gemini import ask_gemini_with_history
from app.clients.whatsapp import send_whatsapp_message, send_whatsapp_image # Asumimos que estas NO son async
from app.services.supabase import save_message_to_supabase
from app.services.products import get_all_products, get_recommended_products
from app.services.orders import process_order
from app.utils.extractors import extract_order_data

REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]

async def handle_user_message(body: dict):
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages")

        if not messages:
            return

        msg = messages[0]
        raw_text = msg.get("text", {}).get("body", "").strip()
        from_number = msg.get("from")

        if not raw_text or not from_number:
            return

        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": datetime.utcnow().isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text) # Supabase client might be async

        productos = await get_all_products() # Assumed async
        if not productos:
            print("⚠️ No se pudieron obtener los productos.")
            # Considerar enviar un mensaje al usuario si esto es crítico
            # send_whatsapp_message(from_number, "Lo siento, estoy teniendo problemas para acceder a nuestro catálogo en este momento. Por favor, inténtalo más tarde.")
            return

        # --- Funciones auxiliares ---
        def extract_labels(obj):
            # ... (tu código existente, parece estar bien)
            labels = []
            def _extract(o):
                if isinstance(o, dict):
                    for v in o.values():
                        _extract(v)
                elif isinstance(o, list):
                    for v in o:
                        _extract(v)
                elif isinstance(o, str):
                    labels.append(o)
            _extract(obj)
            return labels

        def build_catalog(productos_list):
            catalog = []
            for p in productos_list:
                variants_data = []
                for v in p.get("product_variants", []):
                    opts = v.get("options", {})
                    if not opts:
                        continue
                    # Asegurar que el valor de la opción se tome y se ponga en minúscula
                    option_key = next(iter(opts.keys()))
                    option_value = str(opts[option_key]).lower() # Convertir a str y luego lower
                    
                    # Construir la etiqueta de la variante consistentemente
                    # product_images tiene variant_label como "option:Amarillo", "option:blanco"
                    # Necesitamos que coincida con esto, o normalizar en la búsqueda
                    # variant_label_from_db = v.get("variant_label") # Esto no existe en product_variants, está en product_images
                                                               # variant_label en product_images es como "option:Amarillo"
                    # La 'label' en el catálogo es para que el LLM la use y para nuestra propia lógica de match.
                    # Es importante que esta 'label' pueda ser reconstruida o comparada con 'variant_label' de product_images
                    
                    # Usaremos el valor de la opción directamente como 'value' para la búsqueda
                    # y una 'display_label' para mostrar al LLM y posiblemente al usuario.
                    display_label_parts = []
                    for k_opt, v_opt in opts.items():
                        display_label_parts.append(f"{k_opt}:{v_opt}")
                    
                    # 'value' para matching interno: ej: "amarillo"
                    # 'label' para el LLM y reconstrucción: ej: "option:Amarillo" (original) o "option:amarillo" (normalizado)
                    # El `variant_label` en `product_images` es la clave: "option:Amarillo" o "option:azul"
                    # Mantengamos `value` como el valor simple (ej: "amarillo") y `catalog_variant_label` como la etiqueta completa de la variante (ej: "option:amarillo")
                    
                    catalog_variant_label_parts = []
                    for k_opt, v_opt in opts.items():
                         catalog_variant_label_parts.append(f"{k_opt}:{str(v_opt).lower()}") # ej: "option:amarillo"

                    variants_data.append({
                        "id": v["id"],
                        "value": option_value, # ej: "amarillo"
                        "display_label": ", ".join(display_label_parts), # ej: "option:Amarillo"
                        "catalog_variant_label": ",".join(catalog_variant_label_parts), # ej: "option:amarillo"
                        "images": [img["url"] for img in p.get("product_images", []) if img.get("variant_id") == v["id"]]
                    })
                main_imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") is None]
                catalog.append({"name": p["name"], "variants": variants_data, "images": main_imgs})
            return catalog

        def match_target_in_catalog(catalog_list, productos_list, target_str):
            target_str = target_str.strip().lower()
            # Priorizar coincidencia de variante primero si el target incluye nombre de producto y variante
            # Ejemplo: "tequila amarillo"
            
            # Búsqueda más específica primero (ej. "amarillo" si el producto ya está en contexto o "tequila amarillo")
            for entry in catalog_list:
                prod_name_lower = entry["name"].lower()
                for v_catalog in entry["variants"]:
                    # Si el target es solo el valor de la variante (ej: "amarillo")
                    if v_catalog["value"] == target_str:
                        prod = next((p for p in productos_list if p["name"] == entry["name"]), None)
                        return prod, v_catalog
                    # Si el target es nombre de producto + valor de variante (ej: "tequila amarillo")
                    if prod_name_lower in target_str and v_catalog["value"] in target_str:
                        prod = next((p for p in productos_list if p["name"] == entry["name"]), None)
                        return prod, v_catalog
            
            # Búsqueda por nombre de producto
            for entry in catalog_list:
                if entry["name"].lower() == target_str:
                    prod = next((p for p in productos_list if p["name"] == entry["name"]), None)
                    return prod, None # Solo producto, sin variante específica

            # Búsqueda difusa como último recurso
            all_choices = []
            choice_to_item_map = {}

            for entry in catalog_list:
                all_choices.append(entry["name"].lower())
                choice_to_item_map[entry["name"].lower()] = (entry["name"], None) # Mapea a nombre de producto
                for v_catalog in entry["variants"]:
                    # ej: "amarillo" o "tequila amarillo"
                    variant_full_name = f"{entry['name'].lower()} {v_catalog['value']}"
                    all_choices.append(v_catalog["value"])
                    choice_to_item_map[v_catalog["value"]] = (entry["name"], v_catalog["value"]) # Mapea a valor de variante
                    all_choices.append(variant_full_name)
                    choice_to_item_map[variant_full_name] = (entry["name"], v_catalog["value"])


            best_matches = get_close_matches(target_str, all_choices, n=1, cutoff=0.6) # Ajustar cutoff
            if best_matches:
                match_str = best_matches[0]
                matched_prod_name, matched_variant_value = choice_to_item_map[match_str]
                
                prod_obj = next((p for p in productos_list if p["name"] == matched_prod_name), None)
                if not prod_obj: return None, None

                if matched_variant_value:
                    # Encontrar el objeto de variante del catálogo original
                    catalog_entry = next((ce for ce in catalog_list if ce["name"] == matched_prod_name), None)
                    variant_obj_catalog = next((vo for vo in catalog_entry["variants"] if vo["value"] == matched_variant_value), None)
                    return prod_obj, variant_obj_catalog
                return prod_obj, None
            
            return None, None

        # --- Fin Funciones auxiliares ---

        async def handle_image_request_logic(): # Renombrado para claridad, el original es handle_image_request
            try:
                catalog_data = build_catalog(productos)
                prompt_obj = {
                    "user_request": raw_text,
                    "catalog": [{"name": p["name"], "variants": [v["display_label"] for v in p["variants"]]} for p in catalog_data], # Simplificado para el LLM
                    "instructions": [
                        "Tu tarea es detectar si el usuario quiere ver una imagen de un producto o variante.",
                        "Si el usuario quiere una imagen, responde con JSON plano (sin Markdown) así:",
                        "  {\"want_images\": true, \"target\": \"nombre del producto o variante exacta como 'Tequila Jose Cuervo amarillo' o 'Aguardiente Nariño azul' o solo 'Tequila' o solo 'amarillo' si el contexto es claro\"}",
                        "Si no quiere imágenes, responde con: {\"want_images\": false}",
                        "Si el target no es claro, pide clarificación en el JSON: {\"want_images\": true, \"target\": null, \"clarification_needed\": \"Por favor, especifica de qué producto o variante quieres la imagen.\"}",
                        "Ejemplos de solicitudes de imagen:",
                        "- '¿Tienes fotos del tequila?' -> {\"want_images\": true, \"target\": \"tequila\"}",
                        "- 'Muéstrame el tequila amarillo' -> {\"want_images\": true, \"target\": \"tequila amarillo\"}",
                    ]
                }

                hist = [m for m in user_histories.get(from_number, []) if m["role"] in ("user", "model")]
                llm_input_messages = hist[-5:] + [{"role": "user", "text": json.dumps(prompt_obj, ensure_ascii=False)}]
                
                print(f"🧠 Enviando a Gemini para análisis de imagen: {json.dumps(prompt_obj, ensure_ascii=False)}")
                llm_resp_text = await ask_gemini_with_history(llm_input_messages) # Assumed async
                print(f"🧠 Respuesta de Gemini (imagen): {llm_resp_text}")

                match = re.search(r"\{[\s\S]*\}", llm_resp_text)
                if not match:
                    print("⚠️ No se encontró JSON en la respuesta del modelo para imágenes.")
                    return False 
                
                action = json.loads(match.group())
                
                if not action.get("want_images", False):
                    return False # Usuario no quiere imágenes

                if action.get("clarification_needed"):
                    send_whatsapp_message(from_number, action["clarification_needed"]) # SIN AWAIT
                    return True # Se manejó la solicitud (pidiendo clarificación)

                target_description = action.get("target")
                if not target_description:
                    print("⚠️ El LLM indicó que se quieren imágenes pero no especificó un 'target'.")
                    # Podrías enviar un mensaje genérico o dejar que el flujo principal continúe
                    send_whatsapp_message(from_number, "No estoy seguro de qué imágenes mostrar. ¿Podrías ser más específico?") # SIN AWAIT
                    return True 

                matched_product, matched_variant_catalog_obj = match_target_in_catalog(catalog_data, productos, target_description)

                if not matched_product:
                    send_whatsapp_message(from_number, f"Lo siento, no encontré el producto '{target_description}' para mostrarte imágenes.") # SIN AWAIT
                    return True

                image_urls_to_send = []
                display_name = matched_product["name"]

                if matched_variant_catalog_obj: # Si se encontró una variante específica
                    display_name = f"{matched_product['name']} ({matched_variant_catalog_obj['display_label']})"
                    # `product_images` tiene `variant_id` y `variant_label` (ej: "option:Amarillo")
                    # `matched_variant_catalog_obj['id']` es el ID de la variante.
                    # `matched_variant_catalog_obj['catalog_variant_label']` es ej: "option:amarillo"
                    
                    # Priorizar imágenes por variant_id si está disponible
                    variant_specific_images = [
                        img["url"] for img in matched_product.get("product_images", [])
                        if img.get("variant_id") == matched_variant_catalog_obj["id"]
                    ]
                    if variant_specific_images:
                        image_urls_to_send.extend(variant_specific_images)
                    else: # Fallback a variant_label si no hay por ID (o si el ID no está en product_images)
                        # Normalizar la comparación de variant_label
                        target_v_label_lower = matched_variant_catalog_obj["catalog_variant_label"].lower()
                        images_by_label = [
                            img["url"] for img in matched_product.get("product_images", [])
                            if img.get("variant_label") and img.get("variant_label").lower() == target_v_label_lower
                        ]
                        image_urls_to_send.extend(images_by_label)
                
                # Si no se encontraron imágenes específicas de variante O si no se pidió variante, buscar imágenes generales del producto
                if not image_urls_to_send:
                    general_product_images = [
                        img["url"] for img in matched_product.get("product_images", [])
                        if img.get("variant_id") is None # Imágenes generales del producto
                    ]
                    image_urls_to_send.extend(general_product_images)
                
                # Si después de todo, no hay URLs
                if not image_urls_to_send:
                    # Si se especificó una variante pero no se encontraron sus imágenes, y tampoco generales.
                    msg_no_img = f"No tenemos imágenes disponibles para *{display_name}* en este momento. ¿Te puedo ayudar con algo más?"
                    if not matched_variant_catalog_obj and matched_product: # Si solo fue producto y no hay imágenes
                         msg_no_img = f"No tenemos imágenes generales para *{matched_product['name']}*. Si buscas una variante específica, por favor indícamelo."
                    send_whatsapp_message(from_number, msg_no_img) # SIN AWAIT
                    return True 

                send_whatsapp_message(from_number, f"¡Claro! Aquí tienes las imágenes de *{display_name}*:") # SIN AWAIT
                for img_url in image_urls_to_send:
                    try:
                        print(f"🖼️ Enviando imagen: {img_url} para {display_name}")
                        send_whatsapp_image(from_number, img_url, caption=display_name) # SIN AWAIT
                    except Exception as e:
                        print(f"❌ Error enviando imagen {img_url}: {e}")
                        # Considerar enviar un mensaje de error parcial al usuario si algunas imágenes fallan
                return True # Se manejó la solicitud de imagen

            except Exception as e:
                print(f"⚠️ Error en handle_image_request_logic:\n{traceback.format_exc()}")
                # No enviar mensaje de error genérico al usuario aquí, podría ser confuso.
                # Dejar que el flujo principal intente manejar el mensaje como texto normal.
                return False # Indica que la solicitud de imagen no se completó exitosamente


        # --- Comienzo del flujo principal de handle_user_message ---
        image_request_handled = await handle_image_request_logic() # Es async por el ask_gemini
        if image_request_handled:
            print("✅ Solicitud de imagen manejada.")
            return

        # Si no fue una solicitud de imagen (o falló de forma que debe continuar), procesar como mensaje general
        print("📝 Procesando como mensaje general o de pedido.")
        
        def build_order_context(productos_list):
            # ... (tu código existente, parece estar bien)
            contexto_lines = []
            for p in productos_list:
                try:
                    variantes = p.get("product_variants") or []

                    if not variantes: # Asumiendo que price y stock están en el producto principal si no hay variantes
                        line = f"- {p['name']}: COP {p.get('price', 0)} (stock {p.get('stock', 0)})"
                    else:
                        line = f"- {p['name']}:"
                        opts = []
                        for v_prod in variantes:
                            price = v_prod.get("price", p.get("price", 0)) # Fallback al precio del producto si la variante no lo tiene
                            stock = v_prod.get("stock", "N/A")
                            options_str_parts = []
                            for k_opt, v_opt_val in v_prod.get("options", {}).items():
                                options_str_parts.append(f"{k_opt}:{v_opt_val}")
                            options_str = ", ".join(options_str_parts)
                            opts.append(f"    • {options_str} — COP {price} (stock {stock})")
                        if opts: # Solo añadir si hay opciones procesadas
                           line += "\n" + "\n".join(opts)
                        else: # Si no hay variantes procesables, mostrar info base del producto
                            line += f" (Precio base COP {p.get('price',0)}, stock base {p.get('stock',0)})"


                    if p.get("product_images"):
                        line += f"\n    🖼️ Imágenes disponibles: {len(p['product_images'])}"
                    contexto_lines.append(line)
                except Exception as e:
                    print(f"⚠️ Error construyendo línea de catálogo para {p.get('name', 'Producto Desconocido')}: {e}")
            return "🛍️ Catálogo actual (puedes pedirme fotos de cualquiera):\n\n" + "\n\n".join(contexto_lines)

        instrucciones_gemini = (
            f"Historial de conversación previo con el usuario (ignorar si está vacío):\n"
            # TODO: Podrías querer incluir un resumen del historial aquí si es muy largo.
            # Por ahora, el historial se pasa directamente a ask_gemini_with_history.
            f"\n\nMensaje actual del usuario: {raw_text}\n\n"
            f"{build_order_context(productos)}\n\n"
            "INSTRUCCIONES PARA EL BOT:\n"
            "1. Actúa como un vendedor amigable y servicial. Usa emojis para un tono cercano. 😊🛒\n"
            "2. Si el usuario pregunta por un producto que no está en el catálogo o no tiene stock, informa amablemente y sugiere alternativas si es posible.\n"
            "3. Si el usuario muestra intención de compra (ej: 'quiero llevar esto', 'me interesa comprar'), guía la conversación para completar el pedido:\n"
            "   - Confirma los productos, cantidades y precios.\n"
            "   - Calcula un subtotal.\n"
            "   - Informa que el envío cuesta COP 5.000 y súmalo al total.\n"
            "   - Pregunta si desea agregar algo más.\n"
            "   - Si el usuario dice 'no' o que eso es todo, procede a pedir los datos de envío: nombre completo, dirección detallada (con ciudad/barrio si es relevante), número de teléfono de contacto.\n"
            "   - Finalmente, pregunta el método de pago preferido (ej: transferencia, contraentrega). No proceses el pago, solo registra la preferencia.\n"
            "4. Cuando tengas TODOS los datos para un pedido (productos, total, nombre, dirección, teléfono, método de pago), resume el pedido y al final de tu respuesta, incluye un bloque JSON EXACTO con los detalles. NO incluyas el JSON si falta algún dato.\n"
            "   Formato JSON esperado (SOLO si el pedido está completo):\n"
            "   ```json\n" # Indicar a Gemini que no ponga "json" antes de las llaves
            "   {\"order_details\":{\"name\":\"NOMBRE_COMPLETO\",\"address\":\"DIRECCION_DETALLADA\",\"phone\":\"TELEFONO_CONTACTO\",\"payment_method\":\"METODO_PAGO_ELEGIDO\",\"products\":[{\"name\":\"NOMBRE_PRODUCTO_1\",\"quantity\":CANTIDAD_1,\"price\":PRECIO_UNITARIO_1}, {\"name\":\"NOMBRE_PRODUCTO_2\",\"quantity\":CANTIDAD_2,\"price\":PRECIO_UNITARIO_2}],\"total\":TOTAL_PEDIDO_CON_ENVIO}}\n"
            "   ```\n"
            "5. Si el usuario solo está conversando, preguntando por productos, o pidiendo información, responde de manera natural sin intentar forzar un pedido o pedir datos personales prematuramente.\n"
            "6. Si el usuario pide imágenes, y no se manejó antes, puedes decir algo como 'Claro, puedo mostrarte imágenes. ¿De qué producto o variante te gustaría ver fotos?'.\n"
            "7. Sé conciso pero completo en tus respuestas."
        )

        hist_gemini_general = [m for m in user_histories.get(from_number, []) if m["role"] in ("user", "model")]
        llm_input_general = hist_gemini_general[-10:] + [{"role": "user", "text": instrucciones_gemini}]
        
        print(f"🧠 Enviando a Gemini para respuesta general/pedido...") # No mostrar todo el prompt, puede ser muy largo
        llm_response_text_general = await ask_gemini_with_history(llm_input_general) # Assumed async
        print(f"🧠 Respuesta de Gemini (general/pedido): {llm_response_text_general}")

        order_data, clean_text_response = extract_order_data(llm_response_text_general)

        if clean_text_response and clean_text_response.strip(): # Solo si hay texto válido para enviar
            user_histories[from_number].append({
                "role": "model",
                "text": clean_text_response,
                "time": datetime.utcnow().isoformat()
            })
            send_whatsapp_message(from_number, clean_text_response) # SIN AWAIT
            await save_message_to_supabase(from_number, "model", clean_text_response) # Assumed async
        else:
            print("⚠️ clean_text_response es None o vacío. No se enviará mensaje del modelo ni se guardará.")
            # Considerar enviar un mensaje de fallback si el LLM no da una respuesta útil
            # send_whatsapp_message(from_number, "Lo siento, no pude procesar tu última solicitud. ¿Podrías intentarlo de nuevo o de otra manera?")


        if order_data and order_data.get("products"):
            print(f"🛍️ Datos de pedido extraídos: {order_data}")
            # process_order podría ser async si interactúa con DB/API
            result_order = await process_order(from_number, order_data) # Assumed async
            status = result_order.get("status")
            
            # Los mensajes de process_order se envían desde allí según el enunciado del problema original.
            # Si quieres que se envíen desde aquí:
            if status == "missing":
                campos_faltantes = "\n".join(f"- {f.replace('_',' ').capitalize()}" for f in result_order.get("fields", []))
                send_whatsapp_message(from_number, f"📋 Para completar tu pedido, aún necesitamos algunos datos:\n{campos_faltantes}\n¿Podrías proporcionarlos, por favor?") # SIN AWAIT
            elif status == "created":
                send_whatsapp_message(from_number, "✅ ¡Tu pedido ha sido confirmado y creado con éxito! Muchas gracias por tu compra. 🎉 En breve te contactaremos sobre el envío.") # SIN AWAIT
            elif status == "updated":
                send_whatsapp_message(from_number, "♻️ Tu pedido ha sido actualizado correctamente con la nueva información.") # SIN AWAIT
            else:
                print(f"⚠️ Estado inesperado de process_order: {result_order}")
                # No enviar mensaje de error al usuario directamente desde aquí, process_order debería haberlo manejado
                # o el clean_text_response ya contenía la respuesta adecuada.
            
            # Lógica de recomendaciones (opcional, si el pedido ya se confirmó)
            # Podrías mover esto a DESPUÉS de que un pedido es 'created' o 'updated'
            # y si el usuario no ha preguntado por no más recomendaciones.
            if status in ["created", "updated"]: # Solo recomendar si el pedido es firme
                recommended_prods = await get_recommended_products(order_data["products"]) # Assumed async
                if recommended_prods:
                    texto_recomendaciones = "\n".join(f"- {r['name']}: COP {r['price']}" for r in recommended_prods)
                    send_whatsapp_message(
                        from_number,
                        f"✨ ¡Excelente elección! Para complementar tu pedido, también te podrían interesar:\n{texto_recomendaciones}\n¿Te gustaría añadir alguno o tienes alguna otra pregunta?"
                    ) # SIN AWAIT

    except Exception as e:
        print(f"❌ [ERROR CRÍTICO en handle_user_message]:\n{traceback.format_exc()}")
