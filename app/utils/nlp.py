def extract_keywords(text: str, keywords: list[str]) -> list[str]:
    """
    Extrae palabras clave definidas manualmente si aparecen en el texto.
    """
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]
