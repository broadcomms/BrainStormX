# BrainStormX Docker Deployment Guide

## Overview
This guide provides step-by-step instructions to deploy BrainStormX in a single Docker container for production use. It covers building the image, configuring environment variables, running the container, and troubleshooting.

---

## Prerequisites
- Docker Engine 20.10+
- Docker Compose (optional, for multi-container setups)
- Access to production `.env` file (see EC2 guide for template)
- AWS credentials for Bedrock (if using AI features)

---

## 1. Prepare the Project

1. Clone the repository:
   ```bash
   git clone -b production https://github.com/broadcomms/brainstorm_x.git
   cd brainstorm_x
   ```

2. Ensure you have the Docker-specific environment configuration:
   ```bash
   # Use .env.docker for Docker deployments (contains correct paths)
   # Do not copy to .env - use --env-file .env.docker in docker run commands
   ls -la .env.docker
   ```

---

## 2. Build the Docker Image

A sample Dockerfile is provided. It uses Python 3.10, installs all dependencies, and sets up the application.

### Dockerfile Example
```dockerfile
FROM python:3.10-slim

# System dependencies including espeak-ng for Piper TTS
RUN apt-get update && apt-get install -y \
    build-essential libssl-dev libffi-dev python3-dev \
    sqlite3 tesseract-ocr libtesseract-dev ffmpeg \
    nginx curl wget unzip libespeak-ng1 && \
    rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy code
COPY . /app

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install gunicorn eventlet

# (Optional) Install Piper TTS Engine
RUN wget https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz && \
    tar -xzf piper_linux_x86_64.tar.gz && \
    cp piper/piper /usr/local/bin/piper && \
    chmod +x /usr/local/bin/piper && \
    cp piper/lib*.so* /usr/local/lib/ && \
    mkdir -p /usr/share/espeak-ng-data && \
    cp -r piper/espeak-ng-data/* /usr/share/espeak-ng-data/ && \
    cp piper/libespeak-ng.so* /usr/local/lib/ && \
    ldconfig && \
    rm -rf piper piper_linux_x86_64.tar.gz

# (Optional) Install Vosk Speech Recognition Model
RUN mkdir -p /app/stt_models && \
    cd /app/stt_models && \
    wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip && \
    unzip -o vosk-model-small-en-us-0.15.zip && \
    rm vosk-model-small-en-us-0.15.zip

# Create instance directories
RUN mkdir -p instance/uploads instance/photos instance/reports instance/transcripts instance/logs instance/tmp

# Copy SSL-enabled run script
COPY ssl_run.py /app/ssl_run.py

# Expose app port
EXPOSE 5001

# Entrypoint (default to regular run, SSL can be enabled via docker run command)
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--worker-class", "eventlet", "--workers", "1", "run:app"]
```

**Note:** In Docker, Piper is installed to `/usr/local/bin/piper` instead of the venv directory. Update your `.env` file to set `PIPER_BIN=/usr/local/bin/piper` when using Docker deployment.

---

## 3. Build and Run the Container

1. Build the image:
   ```bash
   docker build -t brainstormx:latest .
   ```

2. Run the container:
   ```bash
   docker run -d --name brainstormx \
     --env-file .env.docker \
     -p 5001:5001 \
     -v $(pwd)/instance:/app/instance \
     brainstormx:latest
   ```
   - The `instance` directory is mounted for persistent uploads, logs, and database.

   **For video conference features**, you may need to restart with additional WebRTC ports:
   ```bash
   # Stop current container
   docker stop brainstormx && docker rm brainstormx
   
   # Run with WebRTC ports (if needed for TURN/STUN servers)
   docker run -d --name brainstormx \
     --env-file .env.docker \
     -p 5001:5001 \
     -p 3478:3478/udp \
     -p 5349:5349/tcp \
     -v $(pwd)/instance:/app/instance \
     brainstormx:latest
   ```

---

## 4. Piper TTS & Vosk Configuration (Optional)

If you included Piper TTS and Vosk in your Docker build, ensure your `.env.docker` file has the correct paths:

```env
# TTS Configuration for Docker
TTS_PROVIDER=piper
PIPER_BIN=/usr/local/bin/piper
PIPER_MODEL=/app/tts_models/en_US-hfc_male-medium.onnx

# Transcription Configuration for Docker
TRANSCRIPTION_PROVIDER=vosk
VOSK_MODEL_PATH=/app/stt_models/vosk-model-small-en-us-0.15
```

You can verify installations in the running container:
```bash
# Test Piper TTS
docker exec brainstormx /usr/local/bin/piper --version

# Test Vosk model
docker exec brainstormx ls -la /app/stt_models/
```

---

## 5. Nginx Reverse Proxy (Optional)
For SSL and production-grade serving, run Nginx on the host or in a separate container. Proxy requests to the Flask app on port 5001. See EC2 guide for Nginx config.

---

## 5. SSL Certificate (Optional)
Use Certbot on the host for SSL. Mount `/etc/letsencrypt` into the Nginx container if needed.

---

## 6. Database
- SQLite is used by default. For PostgreSQL, set `DATABASE_URI` in `.env` and mount credentials.

---

## 7. Health Checks & Logs
- Check container logs:
  ```bash
  docker logs brainstormx
  ```
- App logs are in `instance/logs/`.

---

## 8. Troubleshooting

### Common Issues

**Database Lock Errors:**
If you see `(sqlite3.OperationalError) database is locked`:
```bash
# Stop and restart the container
docker stop brainstormx && docker start brainstormx

# Or rebuild with fresh database
docker stop brainstormx && docker rm brainstormx
# Remove instance/app_database.sqlite and restart
```

**Piper TTS Errors:**
```bash
# Verify Piper installation
docker exec brainstormx /usr/local/bin/piper --version
docker exec brainstormx ls -la /usr/local/lib/libpiper*
docker exec brainstormx ls -la /usr/share/espeak-ng-data/
```

**Vosk Transcription Errors:**
```bash
# Verify Vosk model
docker exec brainstormx ls -la /app/stt_models/vosk-model-small-en-us-0.15/
docker exec brainstormx test -f /app/stt_models/vosk-model-small-en-us-0.15/conf/model.conf && echo "Model OK"
```

**Video Conference Camera Issues:**

The video conference feature uses WebRTC's `navigator.mediaDevices.getUserMedia()` API, which has strict browser security requirements:

- **HTTPS Requirement**: Modern browsers (Chrome, Firefox, Safari) **require HTTPS** to access camera/microphone except for `localhost`. This is a browser security policy, not a Docker issue.

- **LAN Access Problem**: When accessing from another machine on your LAN (e.g., `http://192.168.2.46:5001`), browsers block camera access without HTTPS.

- **SSL Solutions for LAN/Production**:
  
  **Option 1: Quick HTTPS with ngrok (Testing)**:
  ```bash
  # Install ngrok if not available
  # Run ngrok to create HTTPS tunnel
  ngrok http 5001
  # Access via the https://xxx.ngrok.io URL provided
  ```
  
  **Option 2: Self-signed Certificate (LAN)**:
  ```bash
  # Generate self-signed certificate for your LAN IP
  # Replace 192.168.2.46 with your actual IP address
  YOUR_IP=$(ip route get 1.1.1.1 | awk '{print $7; exit}')
  mkdir -p ssl
  openssl req -x509 -newkey rsa:4096 -keyout ssl/key.pem -out ssl/cert.pem -days 365 -nodes \
    -subj "/C=US/ST=State/L=City/O=BrainStormX/OU=Workshop/CN=${YOUR_IP}"
  
  # Update Dockerfile to include SSL support (add to existing Dockerfile)
  echo "COPY ssl_run.py /app/ssl_run.py" >> Dockerfile
  
  # Rebuild image with SSL support
  docker build -t brainstormx:latest .
  
  # Run with SSL enabled
  docker stop brainstormx && docker rm brainstormx
  docker run -d --name brainstormx \
    --env-file .env.docker \
    -p 5001:5001 \
    -v $(pwd)/instance:/app/instance \
    -v $(pwd)/ssl:/app/ssl \
    -v $(pwd)/ssl_run.py:/app/ssl_run.py \
    -e SSL_CERT_PATH=/app/ssl/cert.pem \
    -e SSL_KEY_PATH=/app/ssl/key.pem \
    brainstormx:latest python ssl_run.py
  
  # Access via: https://192.168.2.46:5001 (accept browser security warning)
  ```

  **SSL Script Required:** The SSL option requires `ssl_run.py` script:
  ```bash
  # Create ssl_run.py if not present
  cat > ssl_run.py << 'EOF'
  import os
  import ssl
  from app import create_app, socketio
  
  cert_path = os.environ.get('SSL_CERT_PATH', '/app/ssl/cert.pem')
  key_path = os.environ.get('SSL_KEY_PATH', '/app/ssl/key.pem')
  port = int(os.environ.get('PORT', 5001))
  
  if not os.path.exists(cert_path) or not os.path.exists(key_path):
      print(f"ERROR: SSL files not found at {cert_path}, {key_path}")
      exit(1)
  
  print(f"Starting with SSL: cert={cert_path}, key={key_path}")
  app = create_app()
  context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
  context.load_cert_chain(cert_path, key_path)
  
  if __name__ == '__main__':
      socketio.run(app, host="0.0.0.0", port=port, debug=False,
                  ssl_context=context, allow_unsafe_werkzeug=True)
  EOF
  ```
  
  **Option 3: Production with Let's Encrypt**:
  ```bash
  # Use Certbot for real SSL certificate
  # See EC2-DEPLOYMENT-WITH-PUBLIC-DNS.md for full nginx+SSL setup
  ```

- **Browser Behavior by Platform**:
  - **Chrome**: Strict HTTPS requirement for camera/microphone on non-localhost
  - **Firefox**: Similar to Chrome but may show security warnings
  - **Safari**: Very strict HTTPS requirements
  - **Mobile browsers**: Always require HTTPS for camera access

- **Debugging Camera Access**:
  ```bash
  # Check browser console (F12) for specific errors:
  # "NotAllowedError: Permission denied" = User blocked permissions
  # "NotSecureError: Invalid state" = HTTPS required
  # "NotFoundError: No devices found" = Hardware/driver issue
  # "NotReadableError: Could not start video source" = Camera in use by another app
  ```

- **Camera Hardware Issues**:
  If you get `NotReadableError`, the camera is likely being used by another application:
  ```bash
  # Close applications that might use camera
  pkill -f "zoom|skype|teams|discord|cheese"
  
  # Reset camera hardware (Linux)
  sudo modprobe -r uvcvideo && sudo modprobe uvcvideo
  
  # Test camera outside browser
  cheese  # or any camera app to verify hardware works
  ```

- **Docker Container vs Browser**:
  The Docker container doesn't need camera access - it only serves the web application. The camera is accessed by the browser running on the client machine, which requires HTTPS for security.

### General
- Ensure `.env.docker` is present and correct (use `--env-file .env.docker`).
- Check for missing system dependencies in Dockerfile.
- For AI features, verify AWS credentials and Bedrock access.
- For file uploads, ensure `instance` directory is writable and mounted.
- For SSL features, ensure `ssl/` directory and `ssl_run.py` are mounted correctly.

---

## 9. Updating the Container
1. Pull new code, rebuild, and restart:
   ```bash
   git pull
   docker build -t brainstormx:latest .
   docker stop brainstormx && docker rm brainstormx
   docker run ... # as above
   ```

---

## 10. Backup & Restore
- Backup `instance/app_database.sqlite` and `instance/uploads/` regularly.
- Restore by copying files into the mounted `instance` directory.

---

## 11. Security
- Do not expose port 5001 directly to the internet; use Nginx with SSL.
- Keep `.env` and credentials secure.
- Regularly update the base image and dependencies.

---

## 12. Verification
- Access the app at `http://localhost:5001` for HTTP mode.
- For SSL mode: `https://localhost:5001` or `https://[your-lan-ip]:5001`.
- Accept self-signed certificate warnings for SSL access.
- Test video conference camera access (requires HTTPS for WebRTC).
- Verify Piper TTS and Vosk transcription functionality.
- Run health checks and test all features as documented in EC2 guide.

---

**Publisher:** BroadComms (https://www.broadcomms.net)
**Deployment Support Contact:** patrick@broadcomms.net
**Last Updated:** October 13, 2025
