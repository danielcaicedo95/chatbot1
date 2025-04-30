# app/utils/validators.py

# Campos obligatorios para confirmar pedido
default_REQUIRED_FIELDS = ["name", "address", "phone", "payment_method"]
# Valores de placeholder que deben considerarse faltantes
PLACEHOLDER_VALUES = {"NOMBRE", "DIRECCIÓN", "TELÉFONO", "TIPO_PAGO"}

# Exportar REQUIRED_FIELDS para usarlo en otros módulos
REQUIRED_FIELDS = default_REQUIRED_FIELDS

def get_missing_fields(data: dict) -> list[str]:
    """
    Devuelve la lista de campos REQUIRED_FIELDS que estén vacíos, nulos
    o sean placeholders.
    """
    missing = []
    for f in REQUIRED_FIELDS:
        val = data.get(f)
        if (not val
            or (isinstance(val, str) and (
                    val.upper() in PLACEHOLDER_VALUES
                    or val.strip().lower().startswith("tu ")
               ))
           ):
            missing.append(f)
    return missing
