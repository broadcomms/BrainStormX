# run.py
import eventlet
eventlet.monkey_patch()
import logging
import os
from app import create_app, socketio
from app.extensions import db

app = create_app()

# Configure logging to include line number
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s',
    level=logging.INFO
)


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5001))
    socketio.run(app, host="0.0.0.0", port=port, debug=True)