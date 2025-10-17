import multiprocessing

bind = '0.0.0.0:5001'

# Socket.IO requires single worker with eventlet
workers = 1
worker_class = 'eventlet'

worker_connections = 1000
max_requests = 1000
max_requests_jitter = 50
timeout = 120
keepalive = 2
preload_app = True

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# For Docker deployment
user = None
group = None