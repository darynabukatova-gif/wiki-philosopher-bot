import threading

persistence_lock = threading.Lock()
stats_lock = threading.Lock()