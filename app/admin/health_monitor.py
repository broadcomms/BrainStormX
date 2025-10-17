# app/admin/health_monitor.py
import psutil
import time
from datetime import datetime
from app.extensions import db

class HealthMonitor:
    @staticmethod
    def get_system_health():
        """Complete system health metrics"""
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'system': {
                'cpu_percent': psutil.cpu_percent(interval=0.05),
                'memory': {
                    'total': psutil.virtual_memory().total,
                    'available': psutil.virtual_memory().available,
                    'percent': psutil.virtual_memory().percent
                },
                'disk': {
                    'total': psutil.disk_usage('/').total,
                    'free': psutil.disk_usage('/').free,
                    'percent': psutil.disk_usage('/').percent
                }
            },
            'database': {
                'connection_pool': db.engine.pool.size(),
                'active_connections': db.engine.pool.checkedout()
            },
            'application': {
                'uptime': time.time() - psutil.Process().create_time(),
                'thread_count': psutil.Process().num_threads()
            }
        }