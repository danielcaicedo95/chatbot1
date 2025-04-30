# app/utils/extractors.py
import json

def extract_order_data(text: str):
    """Extrae el bloque JSON de pedido y devuelve (order_data_dict, texto_sin_json)."""
    try:
        idx = text.rfind('{"order_details":')
        if idx != -1:
            end = text.rfind('}') + 1
            js = text[idx:end]
            parsed = json.loads(js)
            return parsed.get("order_details"), text[:idx].strip()
    except Exception as e:
        print("⚠️ Error extrayendo JSON:", e)
    return None, text
