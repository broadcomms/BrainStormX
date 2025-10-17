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

