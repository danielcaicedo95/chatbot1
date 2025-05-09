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
from app.services.orders import process_order # Asegúrate que esta función esté bien definida
from app.utils.extractors import extract_order_data # Asegúrate que esta función esté bien definida

# Campos requeridos para que un pedido se considere completo y se pueda procesar desde el JSON del LLM
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

        current_time = datetime.utcnow()
        user_histories.setdefault(from_number, []).append({
            "role": "user",
            "text": raw_text,
            "time": current_time.isoformat()
        })
        await save_message_to_supabase(from_number, "user", raw_text, timestamp=current_time)

        productos = await get_all_products()
        if not productos:
            print("⚠️ No se pudieron obtener los productos.")
            send_whatsapp_message(from_number, "Lo siento, estoy teniendo problemas para acceder a nuestro catálogo en este momento. Por favor, inténtalo más tarde. 🙏")
            return

        # --- Funciones auxiliares (build_catalog, match_target_in_catalog, etc.) ---
        # (Tu código existente para estas funciones. Asegúrate que `build_catalog` y `match_target_in_catalog`
        #  estén bien implementadas y sean robustas como discutimos anteriormente)
        def extract_labels(obj):
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
                    option_key = next(iter(opts.keys()))
                    option_value = str(opts.get(option_key, "")).lower()
                    
                    display_label_parts = [f"{k_opt}:{v_opt}" for k_opt, v_opt in opts.items()]
                    catalog_variant_label_parts = [f"{k_opt}:{str(v_opt).lower()}" for k_opt, v_opt in opts.items()]

                    variants_data.append({
                        "id": v["id"],
                        "value": option_value, 
                        "display_label": ", ".join(display_label_parts),
                        "catalog_variant_label": ",".join(catalog_variant_label_parts),
                        "images": [img["url"] for img in p.get("product_images", []) if img.get("variant_id") == v["id"]]
                    })
                main_imgs = [img["url"] for img in p.get("product_images", []) if img.get("variant_id") is None]
                catalog.append({"name": p["name"], "variants": variants_data, "images": main_imgs, "description": p.get("description","")}) # Añadí descripción
            return catalog

        def match_target_in_catalog(catalog_list, productos_list, target_str):
            target_str = target_str.strip().lower()
            if not target_str: return None, None

            # Búsqueda exacta y combinada primero
            for entry_cat in catalog_list:
                prod_name_lower = entry_cat["name"].lower()
                for v_catalog in entry_cat["variants"]:
                    if v_catalog["value"] == target_str: # Ej: target "amarillo"
                        prod = next((p for p in productos_list if p["name"] == entry_cat["name"]), None)
                        return prod, v_catalog
                    if prod_name_lower in target_str and v_catalog["value"] in target_str: # Ej: target "tequila amarillo"
                        prod = next((p for p in productos_list if p["name"] == entry_cat["name"]), None)
                        return prod, v_catalog
            
            for entry_cat in catalog_list: # Ej: target "tequila jose cuervo"
                if entry_cat["name"].lower() == target_str:
                    prod = next((p for p in productos_list if p["name"] == entry_cat["name"]), None)
                    return prod, None

            # Búsqueda difusa
            all_choices = []
            choice_to_item_map = {}
            for entry_cat in catalog_list:
                pn_lower = entry_cat["name"].lower()
                all_choices.append(pn_lower)
                choice_to_item_map[pn_lower] = (entry_cat["name"], None)
                for v_catalog in entry_cat["variants"]:
                    variant_full_name = f"{pn_lower} {v_catalog['value']}"
                    all_choices.append(v_catalog["value"])
                    choice_to_item_map[v_catalog["value"]] = (entry_cat["name"], v_catalog["value"])
                    all_choices.append(variant_full_name)
                    choice_to_item_map[variant_full_name] = (entry_cat["name"], v_catalog["value"])

            best_matches = get_close_matches(target_str, list(set(all_choices)), n=1, cutoff=0.65) # Aumentar un poco el cutoff
            if best_matches:
                match_str = best_matches[0]
                matched_prod_name_map, matched_variant_value_map = choice_to_item_map[match_str]
                
                prod_obj = next((p for p in productos_list if p["name"] == matched_prod_name_map), None)
                if not prod_obj: return None, None

                if matched_variant_value_map:
                    catalog_entry_found = next((ce for ce in catalog_list if ce["name"] == matched_prod_name_map), None)
                    if catalog_entry_found:
                        variant_obj_catalog = next((vo for vo in catalog_entry_found["variants"] if vo["value"] == matched_variant_value_map), None)
                        return prod_obj, variant_obj_catalog
                return prod_obj, None
            return None, None


        async def handle_image_request_logic():
            try:
                catalog_data = build_catalog(productos)
                # Simplificar el catálogo para el prompt de imágenes
                simplified_catalog_for_prompt = []
                for p_entry in catalog_data:
                    variant_display_labels = [v["display_label"] for v in p_entry["variants"]]
                    simplified_catalog_for_prompt.append({
                        "name": p_entry["name"],
                        "variants": variant_display_labels if variant_display_labels else "No tiene variantes específicas listadas"
                    })

                prompt_obj = {
                    "user_request": raw_text,
                    "catalog_summary": simplified_catalog_for_prompt,
                    "instructions": [
                        "Detecta si el usuario quiere ver una imagen de un producto o variante del catálogo.",
                        "Responde en JSON plano (sin Markdown).",
                        "Si quiere imágenes: {\"want_images\": true, \"target\": \"nombre exacto del producto o variante, ej: 'Tequila Jose Cuervo amarillo', 'Aguardiente Nariño', 'azul' (si el producto ya está en contexto)\"}",
                        "Si no quiere imágenes: {\"want_images\": false}",
                        "Si no estás seguro del producto/variante: {\"want_images\": true, \"target\": null, \"clarification_needed\": \"¡Claro! ¿De qué producto o variante te gustaría ver una foto? Por favor, sé lo más específico posible. 😊\"}",
                    ]
                }

                hist = [m for m in user_histories.get(from_number, []) if m["role"] in ("user", "model")]
                llm_input_messages = hist[-5:] + [{"role": "user", "text": json.dumps(prompt_obj, ensure_ascii=False)}]
                
                print(f"🧠 Enviando a Gemini para análisis de imagen: {json.dumps(prompt_obj, ensure_ascii=False, indent=2)}")
                llm_resp_text = await ask_gemini_with_history(llm_input_messages)
                print(f"🧠 Respuesta de Gemini (imagen): {llm_resp_text}")

                match_json = re.search(r"\{[\s\S]*\}", llm_resp_text)
                if not match_json:
                    print("⚠️ No se encontró JSON en la respuesta del modelo para imágenes.")
                    return False 
                
                action = json.loads(match_json.group())
                
                if not action.get("want_images", False):
                    return False

                if action.get("clarification_needed"):
                    send_whatsapp_message(from_number, action["clarification_needed"])
                    return True 

                target_description = action.get("target")
                if not target_description:
                    send_whatsapp_message(from_number, "No estoy seguro de qué imágenes mostrar. ¿Podrías ser más específico, por favor? 🤔")
                    return True 

                matched_product, matched_variant_catalog_obj = match_target_in_catalog(catalog_data, productos, target_description)

                if not matched_product:
                    send_whatsapp_message(from_number, f"Lo siento, no pude encontrar '{target_description}' en nuestro catálogo para mostrarte imágenes. 😔 ¿Quizás te refieres a otro producto?")
                    return True

                image_urls_to_send = []
                display_name = matched_product["name"]

                if matched_variant_catalog_obj:
                    display_name = f"{matched_product['name']} ({matched_variant_catalog_obj['display_label']})"
                    variant_id_for_images = matched_variant_catalog_obj["id"]
                    # Primero, imágenes por ID de variante
                    image_urls_to_send = [
                        img["url"] for img in matched_product.get("product_images", [])
                        if img.get("variant_id") == variant_id_for_images
                    ]
                    # Fallback a label si no hay por ID (o si product_images no tiene variant_id pero sí variant_label)
                    if not image_urls_to_send:
                        target_v_label_lower = matched_variant_catalog_obj["catalog_variant_label"].lower()
                        image_urls_to_send = [
                            img["url"] for img in matched_product.get("product_images", [])
                            if img.get("variant_label") and img.get("variant_label").lower() == target_v_label_lower
                        ]
                
                # Si no se encontraron específicas de variante O no se pidió variante, usar generales del producto
                if not image_urls_to_send:
                    image_urls_to_send = [
                        img["url"] for img in matched_product.get("product_images", [])
                        if img.get("variant_id") is None 
                    ]
                
                if not image_urls_to_send:
                    msg_no_img = f"¡Ay! Parece que no tengo imágenes para *{display_name}* en este momento. 🖼️🚫 Pero puedo contarte más sobre el producto si quieres. 😊"
                    send_whatsapp_message(from_number, msg_no_img)
                    return True 

                send_whatsapp_message(from_number, f"¡Claro que sí! Aquí tienes unas foticos de *{display_name}*:")
                for img_url in image_urls_to_send:
                    try:
                        print(f"🖼️ Enviando imagen: {img_url} para {display_name}")
                        send_whatsapp_image(from_number, img_url, caption=display_name)
                    except Exception as e:
                        print(f"❌ Error enviando imagen {img_url}: {e}")
                return True
            except Exception:
                print(f"⚠️ Error en handle_image_request_logic:\n{traceback.format_exc()}")
                return False


        # --- Comienzo del flujo principal de handle_user_message ---
        image_request_handled = await handle_image_request_logic()
        if image_request_handled:
            print("✅ Solicitud de imagen manejada.")
            return

        print("📝 Procesando como mensaje general o de pedido.")
        
        def build_order_context(productos_list):
            contexto_lines = []
            catalog_data = build_catalog(productos_list) # Usa la misma función para consistencia
            for p_entry in catalog_data:
                line = f"**{p_entry['name']}**"
                if p_entry.get('description'):
                    line += f"\n   📝 _{p_entry['description']}_" # Descripción más visible

                product_obj = next((p for p in productos_list if p['name'] == p_entry['name']), None)
                if not product_obj: continue

                if not product_obj.get("product_variants"):
                    price = product_obj.get('price', 0)
                    stock = product_obj.get('stock', 0)
                    line += f"\n   💰 Precio: COP {price:,} (Stock: {stock if stock > 0 else 'Agotado 😟'})"
                else:
                    opts = []
                    for v_prod in product_obj.get("product_variants", []):
                        price = v_prod.get("price", product_obj.get("price", 0))
                        stock = v_prod.get("stock", 0)
                        options_str_parts = [f"{k_opt}:{v_opt_val}" for k_opt, v_opt_val in v_prod.get("options", {}).items()]
                        options_str = ", ".join(options_str_parts)
                        opts.append(f"   variant {options_str} — 💰 COP {price:,} (Stock: {stock if stock > 0 else 'Agotado 😟'})")
                    if opts:
                        line += "\n" + "\n".join(opts)
                    else: # Si hay product_variants pero está vacío o malformado
                        price = product_obj.get('price', 0)
                        stock = product_obj.get('stock', 0)
                        line += f"\n   💰 Precio base: COP {price:,} (Stock: {stock if stock > 0 else 'Agotado 😟'})"
                
                if p_entry.get("images") or any(v.get("images") for v in p_entry.get("variants",[])):
                     line += f"\n   🖼️ ¡Tenemos fotos disponibles! Pídemelas si quieres verlas."
                contexto_lines.append(line)
            return "--- \n🛍️ **Nuestro Catálogo Actual** 🛍️\n(Precios en COP. ¡Pregúntame si quieres ver fotos!)\n\n" + "\n\n".join(contexto_lines) + "\n---"


        instrucciones_gemini = (
            f"Historial de conversación previo con el usuario (ignorar si está vacío).\n"
            f"Mensaje actual del usuario:```{raw_text}```\n\n"
            f"{build_order_context(productos)}\n\n"
            "**INSTRUCCIONES PARA EL BOT (VENDEDOR HUMANO, AMIGABLE Y PROACTIVO):**\n\n"
            "**Tu Personalidad:**\n"
            "- Eres [Nombre del Bot, ej: VendiBot], un asistente de ventas virtual súper amigable, paciente y con mucho entusiasmo. ¡Tu meta es que el cliente se sienta como si estuviera hablando con el mejor vendedor de la tienda!\n"
            "- Usa un lenguaje natural, cercano, con jerga colombiana apropiada (ej: '¡De una!', '¡Qué chévere!', '¡Con gusto!'). Utiliza emojis para darle vida a tus mensajes (🎉🛒🛍️😊👍😉🇨🇴).\n"
            "- Varía tus frases. No seas repetitivo. Muestra empatía y comprensión.\n\n"

            "**Flujo de Conversación y Ventas:**\n"
            "1.  **Interacción Inicial:** Responde con calidez. '¡Hola! 👋 Soy [Nombre del Bot], tu asesor de compras hoy. ¿En qué te puedo colaborar?' o '¡Qué más! ¿Antojado de algo hoy? Cuéntame qué buscas. 😉'\n"
            "2.  **Información de Productos:** Proporciona detalles, precios y stock de forma clara. Si te piden fotos y no se manejó antes, di: '¡Claro que sí! ¿De qué producto o variante te gustaría ver una fotico para antojarnos más? 📸'.\n"
            "3.  **Intención de Compra (Momento Clave):** Cuando el usuario muestre interés claro en comprar (ej: 'quiero ese', 'me lo llevo', 'voy a pedir X'):\n"
            "    a. **Confirma Productos y Cantidades:** '¡Excelente elección! Entonces, para confirmar: llevas [Producto 1, Cantidad 1] y [Producto 2, Cantidad 2], ¿correcto?'. Si no especifica cantidad, asume 1 pero pregunta si quiere más.\n"
            "    b. **Calcula Subtotal y Envío:** '¡Perfecto! Tu subtotal sería de COP [Subtotal]. El envío a cualquier parte tiene un costo de COP 5.000. ¿Estamos de acuerdo?'.\n"
            "    c. **Pregunta si Desea Algo Más (Venta Cruzada Sutil):** '¿Alguna cosita más que quieras añadir a tu pedido o algo más en lo que te pueda ayudar hoy? 😊'.\n"
            "4.  **Recopilación de Datos para el Pedido (¡Hazlo como una conversación, no un interrogatorio!):**\n"
            "    - **SOLO SI EL USUARIO CONFIRMA QUE NO DESEA NADA MÁS Y QUIERE PROCEDER**, comienza a pedir los datos UNO POR UNO. NO LOS PIDAS TODOS DE GOLPE.\n"
            "    - **Nombre:** '¡Súper! Para coordinar tu envío, ¿me regalas tu nombre completo, porfa?'\n"
            "    - **Dirección:** '¡Mil gracias, [Nombre]! Ahora, ¿cuál es la dirección completa para hacerte llegar esta maravilla? (Incluye ciudad, barrio, y cualquier detalle extra que nos ayude a encontrarte fácil 😉 Ej: Apto, casa, conjunto).' \n"
            "    - **Teléfono:** '¡Anotadísimo! Y un número de teléfono de contacto, por si el mensajero necesita alguna indicación el día de la entrega.'\n"
            "    - **Método de Pago:** '¡Ya casi terminamos, [Nombre]! Para el pago, ¿cómo te queda mejor? Aceptamos [Lista tus métodos de pago EJ: Nequi, Daviplata, Bancolombia, o si manejas, pago contra entrega].'\n"
            "    - **IMPORTANTE:** Revisa siempre el historial para no pedir datos que ya te hayan dado. Si ya tienes un dato, confírmalo en lugar de volverlo a pedir (Ej: 'Confírmame tu teléfono, ¿sigue siendo XXXXX?').\n"
            "5.  **Confirmación Final del Pedido ANTES del JSON:**\n"
            "    - **UNA VEZ TENGAS ABSOLUTAMENTE TODOS LOS DATOS REQUERIDOS** (productos con nombre, cantidad y precio unitario; nombre del cliente, dirección completa, teléfono, método de pago, y el total final incluyendo envío), resume TODO el pedido de forma clara y amigable: '¡Listo, [Nombre]! ✨ Entonces, tu pedido para envío es:\n      [Lista de productos con cantidad y precio unitario]\n      Subtotal: COP [Subtotal]\n      Envío: COP 5.000\n      **Total a Pagar: COP [Total Final]**\n      Se enviará a: [Dirección Completa]\n      Te contactaremos al: [Teléfono]\n      Forma de pago: [Método de Pago]\n      ¿Está todo perfecto para que lo ingresemos al sistema y lo despachemos?'\n"
            "    - **SI EL USUARIO CONFIRMA (ej: 'sí', 'perfecto', 'confirmo'), ENTONCES Y SÓLO ENTONCES, en tu RESPUESTA FINAL (que debe ser el mensaje de confirmación del pedido), incluye el bloque JSON.**\n"
            "    - **Formato JSON EXACTO (sin anteponer 'json' ni explicaciones adicionales, solo el bloque):**\n"
            "      ```json\n"
            "      {\"order_details\":{\"name\":\"NOMBRE_COMPLETO\",\"address\":\"DIRECCION_DETALLADA\",\"phone\":\"TELEFONO_CONTACTO\",\"payment_method\":\"METODO_PAGO_ELEGIDO\",\"products\":[{\"name\":\"NOMBRE_PRODUCTO_1\",\"quantity\":CANTIDAD_1,\"price\":PRECIO_UNITARIO_1}],\"total\":TOTAL_PEDIDO_CON_ENVIO}}\n"
            "      ```\n"
            "6.  **Manejo de Stock y Alternativas:** '¡Ay, qué embarrada! 😔 Justo ahora el [Producto] se nos agotó. Pero no te preocupes, te podría ofrecer [Alternativa 1] que es súper parecido y delicioso, o quizás el [Alternativa 2] que también está volando. ¿Te suena alguno?'.\n"
            "7.  **Preguntas Generales:** Sé siempre servicial. '¡Con todo el gusto!', '¡Para eso estamos!'.\n\n"
            "**Ejemplo de cómo pedir el siguiente dato si ya tienes algunos:**\n"
            "USER: (Ya dio nombre y dirección)\n"
            "BOT: ¡Perfecto, [Nombre]! Ya tengo tu dirección. Ahora, para estar en contacto, ¿me podrías dar un número de teléfono donde te podamos localizar? 📱\n\n"
            "**Recuerda:** El objetivo es que el usuario disfrute la conversación. ¡Sé creativo y natural! Si el usuario divaga, trata de guiarlo amablemente de vuelta al proceso de compra si ya había mostrado intención."
        )

        hist_gemini_general = [m for m in user_histories.get(from_number, []) if m["role"] in ("user", "model")]
        # Enviar un historial más corto para no exceder límites y mantener relevancia
        llm_input_general = hist_gemini_general[-8:] + [{"role": "user", "text": instrucciones_gemini}]
        
        print(f"🧠 Enviando a Gemini para respuesta general/pedido (últimos {len(llm_input_general)} mensajes)...")
        llm_response_text_general = await ask_gemini_with_history(llm_input_general)
        print(f"🧠 Respuesta de Gemini (general/pedido): {llm_response_text_general}")

        order_data_from_json, clean_text_response = extract_order_data(llm_response_text_general)
        current_time_model = datetime.utcnow()

        if clean_text_response and clean_text_response.strip():
            user_histories[from_number].append({
                "role": "model",
                "text": clean_text_response,
                "time": current_time_model.isoformat()
            })
            send_whatsapp_message(from_number, clean_text_response)
            await save_message_to_supabase(from_number, "model", clean_text_response, timestamp=current_time_model)
        else:
            print("⚠️ clean_text_response es None o vacío. El LLM no proporcionó texto conversacional.")
            if not order_data_from_json: # Si tampoco hay JSON, el LLM no respondió útilmente
                 send_whatsapp_message(from_number, "Hmm, parece que me enredé un poquito. 😅 ¿Podrías repetirme tu consulta, por favor?")


        # Procesar el pedido si el LLM proporcionó order_data_from_json VÁLIDO Y COMPLETO
        if order_data_from_json and isinstance(order_data_from_json.get("order_details"), dict):
            order_details_payload = order_data_from_json["order_details"]
            print(f"ℹ️ JSON 'order_details' extraído: {json.dumps(order_details_payload, indent=2)}")

            missing_fields = [
                field for field in REQUIRED_FIELDS 
                if not order_details_payload.get(field) or not str(order_details_payload.get(field)).strip()
            ]
            
            # Adicionalmente, verificar que products y total existan y products no esté vacío
            if not order_details_payload.get("products") or not isinstance(order_details_payload["products"], list) or not order_details_payload["products"]:
                missing_fields.append("products (lista no vacía)")
            if order_details_payload.get("total") is None: # total puede ser 0, pero no None
                missing_fields.append("total")


            if not missing_fields:
                print(f"✅ Datos de pedido COMPLETOS en JSON listos para procesar: {order_details_payload.get('name')}")
                
                result_order = await process_order(from_number, order_details_payload) 
                status = result_order.get("status")
                # El LLM ya debería haber enviado el mensaje de "pedido confirmado" ANTES del JSON.
                # Aquí podrías añadir logs o acciones adicionales basadas en el resultado de process_order.

                if status == "created":
                    print(f"✅ Pedido para {from_number} (Cliente: {order_details_payload.get('name')}) CREADO exitosamente en DB. ID: {result_order.get('order_id', 'N/A')}")
                    # Podrías enviar un mensaje de recomendación aquí si lo deseas
                    recommended_prods = await get_recommended_products(order_details_payload.get("products", []))
                    if recommended_prods:
                        texto_recomendaciones = "\n".join(f"  - {r['name']} (COP {r.get('price', 0):,})" for r in recommended_prods)
                        send_whatsapp_message(
                            from_number,
                            f"✨ ¡Por cierto, {order_details_payload.get('name','')}! Ya que tu pedido está en camino, quizás te interesen estos otros productos para una próxima ocasión o para complementar:\n{texto_recomendaciones}\n\n¡Avísame si algo te llama la atención! 😉"
                        )
                elif status == "updated":
                     print(f"♻️ Pedido para {from_number} (Cliente: {order_details_payload.get('name')}) ACTUALIZADO exitosamente en DB. ID: {result_order.get('order_id', 'N/A')}")
                elif status == "missing_in_db_logic" or status == "error_saving_to_db": # Estados de error de process_order
                    print(f"⚠️ Error desde process_order para {from_number} (Cliente: {order_details_payload.get('name')}). Status: {status}, Detalles: {result_order.get('fields') or result_order.get('error')}")
                    # Informar al usuario que algo salió mal en el backend, pero de forma amigable
                    send_whatsapp_message(from_number, f"¡Hola {order_details_payload.get('name','')}! Tuvimos un pequeño inconveniente técnico al registrar tu pedido en el sistema final. 🛠️ No te preocupes, nuestro equipo ya está enterado y lo revisará. Si no te contactamos pronto, por favor escríbenos de nuevo. ¡Gracias por tu paciencia! 🙏")
                else: 
                    print(f"⚠️ Estado no manejado de process_order: {status} para {from_number}")
            
            else: # El JSON existe pero está incompleto según nuestros REQUIRED_FIELDS
                print(f"⚠️ El LLM envió un JSON 'order_details', pero está INCOMPLETO. Campos faltantes: {missing_fields}. Target: {order_details_payload.get('name', 'N/A')}. La respuesta conversacional del LLM debería haber pedido estos datos.")
                # No llamamos a process_order. El flujo conversacional del LLM debe continuar pidiendo datos.
                # Si el clean_text_response no fue una pregunta, aquí se podría enviar un mensaje genérico pidiendo el dato,
                # pero idealmente el LLM lo maneja.
        
    except Exception:
        print(f"❌ [ERROR CRÍTICO en handle_user_message]:\n{traceback.format_exc()}")
        # No enviar el traceback al usuario.
        send_whatsapp_message(from_number, "¡Uy! Parece que tuve un pequeño enredo técnico por aquí. 🤖 ¿Podrías intentar tu consulta de nuevo en un momentico, por favor? Mil gracias por la paciencia.")