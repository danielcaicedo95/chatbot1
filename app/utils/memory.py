from collections import defaultdict, deque

# Memoria temporal en RAM: guarda hasta 15 mensajes por usuario
user_histories = defaultdict(lambda: deque(maxlen=15))

# Pedidos temporales antes de ser confirmados
user_orders = {}
