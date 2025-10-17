#!/usr/bin/env python3
"""
SSL-enabled Flask server for Docker deployment
Enables HTTPS access required for WebRTC camera/microphone access
"""

import os
import ssl
from app import create_app, socketio

# Get SSL certificate paths from environment or default
cert_path = os.environ.get('SSL_CERT_PATH', '/app/ssl/cert.pem')
key_path = os.environ.get('SSL_KEY_PATH', '/app/ssl/key.pem')
port = int(os.environ.get('PORT', 5001))

# Validate SSL files exist
if not os.path.exists(cert_path):
    print(f"ERROR: SSL certificate not found at {cert_path}")
    exit(1)
    
if not os.path.exists(key_path):
    print(f"ERROR: SSL private key not found at {key_path}")
    exit(1)

print(f"Starting with SSL: cert={cert_path}, key={key_path}")

# Create Flask app
app = create_app()

# Create SSL context
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(cert_path, key_path)

if __name__ == '__main__':
    # Run with SSL context
    socketio.run(
        app, 
        host="0.0.0.0", 
        port=port, 
        debug=False,
        ssl_context=context,
        allow_unsafe_werkzeug=True  # Suppress production warning
    )
