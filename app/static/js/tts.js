// Lightweight client TTS helper using Socket.IO events defined in tts_gateway.py
// Exposes window.TTS.play(text, opts?) and wires minimal UI bindings if present.

(function () {
  function notify(kind, msg, ttlMs=2500) {
    try {
      const area = document.getElementById('notification-area') || null;
      if (!area) { console[kind==='danger'?'error':'log']('[TTS]', msg); return; }
      const div = document.createElement('div');
      div.className = `alert alert-${kind} alert-dismissible fade show shadow-sm py-2 px-3`;
      div.role = 'alert';
      div.innerHTML = `${msg} <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>`;
      area.appendChild(div);
      if (ttlMs > 0) setTimeout(()=>{ try { const inst = bootstrap.Alert.getOrCreateInstance(div); inst.close(); } catch(_) { div.remove(); } }, ttlMs);
    } catch (_) {}
  }
  const TTS = {
    _socket: null,
    // Buffer for the current sentence/segment
    _segmentChunks: [],
    // FIFO queue of object URLs to play sequentially
    _queue: [],
    _urlsToRevoke: new Set(),
    _mime: 'audio/wav',
    _currentAudio: null,
    _playing: false,
    _canceled: false,
      _currentMeta: null,
      _playbackEndedNotified: false,
      _emitPlaybackEvent(type, extra = {}) {
        const detail = Object.assign({}, this._currentMeta || {}, extra);
        try {
          window.dispatchEvent(new CustomEvent(type, { detail }));
        } catch (_) {
          try {
            const evt = document.createEvent('CustomEvent');
            evt.initCustomEvent(type, false, false, detail);
            window.dispatchEvent(evt);
          } catch (err) {
            console.warn('TTS: failed to dispatch event', type, err);
          }
        }
        if (type === 'tts-playback-start') {
          this._playbackEndedNotified = false;
        } else if (type === 'tts-playback-ended') {
          this._playbackEndedNotified = true;
        }
      },
    _resetPlayback() {
      try {
        this._queue = [];
        this._segmentChunks = [];
        this._playing = false;
        this._canceled = false;
          this._playbackEndedNotified = false;
        if (this._currentAudio) {
          try { this._currentAudio.pause(); } catch(_) {}
          this._currentAudio.removeAttribute('src');
          try { this._currentAudio.load?.(); } catch(_) {}
        }
        // Revoke any pending object URLs
        this._urlsToRevoke.forEach(u => { try { URL.revokeObjectURL(u); } catch(_) {} });
        this._urlsToRevoke.clear();
      } catch (_) {}
    },
    _ensureAudio() {
      if (!this._currentAudio) {
        this._currentAudio = new Audio();
        this._currentAudio.addEventListener('ended', () => this._playNext());
        this._currentAudio.addEventListener('error', () => this._playNext());
      }
      return this._currentAudio;
    },
    _enqueueBlob(blob) {
      try {
        const url = URL.createObjectURL(blob);
        this._urlsToRevoke.add(url);
        this._queue.push(url);
      } catch (_) {}
    },
    _playNext() {
      if (this._playing) {
        // If currently playing, let ended handler advance
      }
      const next = this._queue.shift();
      if (!next) {
        this._playing = false;
          if (!this._playbackEndedNotified) {
            this._emitPlaybackEvent('tts-playback-ended', {
              canceled: this._canceled,
              reason: this._canceled ? 'stop' : 'complete',
            });
            this._currentMeta = null;
          }
        return;
      }
      if (this._canceled) { // drop remaining queue on cancel
        try { URL.revokeObjectURL(next); this._urlsToRevoke.delete(next); } catch(_) {}
        this._queue = [];
        this._playing = false;
        return;
      }
      this._playing = true;
      const audio = this._ensureAudio();
        this._emitPlaybackEvent('tts-playback-start', { sourceUrl: next });
      audio.src = next;
      const p = audio.play();
      if (p && p.catch) p.catch(() => { /* On failure, try next */ this._playing = false; this._playNext(); });
      // Revoke after a short delay to ensure decoder has fetched data
      setTimeout(() => { try { URL.revokeObjectURL(next); this._urlsToRevoke.delete(next); } catch(_) {} }, 10000);
    },
    ensureSocket() {
      if (this._socket && this._socket.connected) return this._socket;
      if (typeof io === 'undefined') throw new Error('Socket.IO not loaded');
      this._socket = io();
      this._bindHandlers();
      return this._socket;
    },
    _bindHandlers() {
      if (!this._socket || this._bound) return;
      this._bound = true;
      // Listen for facilitator narration lifecycle to avoid overlapping audio
      try {
        window.addEventListener('facilitator-tts-starting', () => {
          // If we're about to speak, cancel our playback
          if (this._playing) {
            this.stop();
          }
          // Mark as canceled so in-flight chunks are ignored
          this._canceled = true;
        });
        // When facilitator stops/ends, allow new TTS requests
        const allowFn = () => { this._canceled = false; };
        window.addEventListener('facilitator-tts-stopped', allowFn);
        window.addEventListener('facilitator-tts-ended', allowFn);
      } catch (_) {}
      this._socket.on('tts_audio_start', (meta) => {
        // New request starting: clear previous playback/queue and set mime
        this._resetPlayback();
        this._mime = (meta && meta.mime) || 'audio/wav';
        notify('info', 'Synthesizing speech…', 1200);
      });
      this._socket.on('tts_audio_chunk', (chunk) => {
        // chunk should arrive as binary ArrayBuffer converted to a Uint8Array/Buffer-like
        if (chunk == null) return;
        if (this._canceled) return; // ignore chunks if canceled
        if (chunk instanceof ArrayBuffer) {
          this._segmentChunks.push(new Uint8Array(chunk));
        } else if (chunk && chunk.buffer instanceof ArrayBuffer) {
          // Handle typed arrays
          this._segmentChunks.push(new Uint8Array(chunk.buffer));
        } else if (typeof chunk === 'string') {
          // If server ever mis-sends as base64 string (not expected), decode
          try { this._segmentChunks.push(Uint8Array.from(atob(chunk), c => c.charCodeAt(0))); } catch(_) {}
        }
      });
      this._socket.on('tts_complete', () => {
        try {
          if (this._canceled) { this._resetPlayback(); return; }
          // Finalize any trailing segment that didn't get flushed
          if (this._segmentChunks.length) {
            const blob = new Blob(this._segmentChunks, { type: this._mime });
            this._segmentChunks = [];
            this._enqueueBlob(blob);
          }
          // Kick off playback if nothing is playing yet
          if (!this._playing) this._playNext();
        } catch (e) { console.warn('TTS playback error', e); }
      });
      // Optional early playback per sentence chunk
      this._socket.on('tts_flush', () => {
        try {
          if (this._canceled) return;
          if (!this._segmentChunks.length) return;
          const blob = new Blob(this._segmentChunks, { type: this._mime });
          this._segmentChunks = [];
          this._enqueueBlob(blob);
          if (!this._playing) this._playNext();
        } catch (_) {}
      });
      this._socket.on('tts_error', (e) => {
        const msg = (e && (e.message || e.error || e.toString())) || 'TTS error';
        const hint = e && e.hint ? ` ${e.hint}` : '';
        notify('danger', `TTS: ${msg}${hint ? ' — '+hint : ''}`, 4000);
        console.warn('TTS error', e);
        if (!this._playbackEndedNotified) {
          this._emitPlaybackEvent('tts-playback-ended', { canceled: true, reason: 'error' });
          this._currentMeta = null;
        }
      });
    },
    play(text, opts = {}) {
      try {
        // Concurrency guard: if facilitator is speaking, do not start transcript/task TTS
        if (window.FacilitatorTTS && typeof window.FacilitatorTTS.isSpeaking === 'function' && window.FacilitatorTTS.isSpeaking()) {
          notify('warning', 'Facilitator is speaking. Please pause/stop before starting another read.', 2200);
          return;
        }
      } catch(_) {}
      // Clear any stale cancellation from a previous facilitator block
      this._canceled = false;
      if (!text || !text.trim()) { notify('warning', 'Nothing to read in this section yet.', 1800); return; }
      const s = this.ensureSocket();
      const defaults = (window.ttsDefaults || {});
      const provider = opts.provider || defaults.provider || (window.ttsDefaultProvider || 'piper');
      const fmt = opts.format || (provider === 'polly' ? 'mp3' : 'wav');
      const payload = {
        text: text.trim(),
        provider,
        voice: (typeof opts.voice !== 'undefined' ? opts.voice : (defaults.voice || undefined)) || undefined,
        speed: (typeof opts.speed === 'number' ? opts.speed : (typeof defaults.speed === 'number' ? defaults.speed : 1.0)),
        format: fmt,
        workshop_id: (typeof opts.workshop_id !== 'undefined' ? opts.workshop_id : (defaults.workshop_id || (typeof window !== 'undefined' ? window.workshopId : undefined)))
      };
      const requestId = `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
      const metaExtra = (opts && typeof opts.meta === 'object' && opts.meta) ? opts.meta : {};
      this._currentMeta = Object.assign({
        requestId,
        provider,
        voice: payload.voice,
        speed: payload.speed,
        format: fmt,
        textLength: payload.text.length,
      }, metaExtra);
      this._playbackEndedNotified = false;
      // Stop any ongoing playback and clear queue for a new request
      this._resetPlayback();
      s.emit('tts_request', payload);
    },
    stop() {
      // Signal client-side cancel; we ignore further chunks for this request
      this._canceled = true;
      this._queue = [];
      this._segmentChunks = [];
      if (this._currentAudio) {
        try { this._currentAudio.pause(); } catch(_) {}
        this._currentAudio.removeAttribute('src');
        try { this._currentAudio.load?.(); } catch(_) {}
      }
      this._urlsToRevoke.forEach(u => { try { URL.revokeObjectURL(u); } catch(_) {} });
      this._urlsToRevoke.clear();
      this._playing = false;
      if (!this._playbackEndedNotified) {
        this._emitPlaybackEvent('tts-playback-ended', { canceled: true, reason: 'stop' });
      }
      this._currentMeta = null;
    }
  };

  window.TTS = TTS;

  // Optional: auto-bind any elements with [data-tts] attribute
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-tts]');
    if (!el) return;
    const selector = el.getAttribute('data-tts');
    if (!selector) return;
    const srcEl = document.querySelector(selector);
    if (!srcEl) return;
    const text = srcEl.value || srcEl.textContent || srcEl.innerText || '';
    const provider = el.getAttribute('data-tts-provider') || undefined;
    const voice = el.getAttribute('data-tts-voice') || undefined;
    const format = el.getAttribute('data-tts-format') || undefined;
  const speedAttr = el.getAttribute('data-tts-speed');
  const speed = speedAttr ? parseFloat(speedAttr) : undefined;
  TTS.play(text, { provider, voice, format, speed });
  });
})();
