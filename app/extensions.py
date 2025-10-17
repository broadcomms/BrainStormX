# app/extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_login import LoginManager
from flask_mail import Mail

db = SQLAlchemy()
# SocketIO will be initialized with proper async_mode in create_app()
socketio = SocketIO(cors_allowed_origins="*")
login_manager = LoginManager()
mail = Mail()
