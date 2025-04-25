from collections import defaultdict, deque

# Memoria temporal en RAM: guarda hasta 15 mensajes por usuario
user_histories = defaultdict(lambda: deque(maxlen=15))

# Pedidos temporales confirmados (id + timestamp)
user_orders = {}

# Datos parciales que va dando el usuario antes de confirmar
# phone_number -> { name, address, phone, payment_method, products, total }
user_pending_data = {}
