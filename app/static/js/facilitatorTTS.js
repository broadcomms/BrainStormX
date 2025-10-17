// Facilitator TTS controller: single-audio fetch via REST, pause/resume/replay/stop with persisted state.
(function () {
  const LS_KEY = (wid, tid) => `facTTS:${wid}:${tid}`;
  const PREF_KEY = (wid) => `facTTS:prefs:${wid}`;
  const hashText = (t) => {
    try { return `${t.length}:${(t.slice(0,64)||'')}`; } catch(_) { return String(t||''); }
  };

  function notify(kind, msg, ttlMs=2000) {
    try {
      const area = document.getElementById('notification-area');
      if (!area) { console[kind==='danger'?'error':'log']('[FacTTS]', msg); return; }
      const div = document.createElement('div');
      div.className = `alert alert-${kind} alert-dismissible fade show shadow-sm py-2 px-3`;
      div.role = 'alert';
      div.innerHTML = `${msg} <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>`;
      area.appendChild(div);
      if (ttlMs>0) setTimeout(()=>{ try { bootstrap.Alert.getOrCreateInstance(div).close(); } catch(_) {} }, ttlMs);
    } catch (_) {}
  }

  class FacilitatorTTSController {
    constructor(workshopId) {
      this.workshopId = workshopId;
      this.taskId = null;
      this.text = '';
      this._audio = null;
      this._objectUrl = null;
      this._abort = null;
      this._isLoading = false;
      this._ui = null;
      this._defaults = (window.ttsDefaults || {});
      this._provider = null;
      this._voice = null;
      this._speed = null;
      this._format = null;
      this._pendingSeekRatio = null;
      this._prefs = this._loadPrefs() || {};
      this._progressTimer = null;
      this._words = [];
      this._marks = null; // [{time(ms), value(word)}]
      this._marksLoadedForHash = null; // cache key: provider+voice+text hash
      // Audio level analysis for UI (e.g., AI tile): minimal CPU loop
      this._levelCtx = null; // AudioContext
      this._levelSrc = null; // MediaElementSourceNode
      this._levelAnalyser = null; // AnalyserNode
      this._levelBuf = null; // Uint8Array
      this._levelRAF = null;
      this._levelListeners = [];
      this._speakListeners = [];
      this._speaking = false;
    }

    _ensureAudio() {
      if (!this._audio) {
        this._audio = new Audio();
        this._audio.preload = 'auto';
        this._audio.muted = false;
        try { this._audio.volume = 1.0; } catch(_) {}
        this._audio.addEventListener('timeupdate', () => this._onTimeUpdate());
        this._audio.addEventListener('loadedmetadata', () => this._onLoadedMeta());
        this._audio.addEventListener('ended', () => this._onEnded());
        this._audio.addEventListener('pause', () => this._onPause());
        this._audio.addEventListener('play', () => this._onPlay());
        this._audio.addEventListener('error', () => this._onError());
        // Initialize level analyser on first audio creation
        try { this._initLevelAnalyser(); } catch(_) {}
      }
      return this._audio;
    }

    bindUI(ui) {
      // ui: { playBtn, pauseBtn, stopBtn, replayBtn, progressBar, progressContainer, elapsed, duration, icon, settings }
      this._ui = ui || null;
      this._syncUI();
      if (!ui) return;
      if (ui.playBtn) ui.playBtn.addEventListener('click', () => this.playSpeech());
      if (ui.pauseBtn) ui.pauseBtn.addEventListener('click', () => this.pauseSpeech());
      if (ui.stopBtn) ui.stopBtn.addEventListener('click', () => this.stopSpeech());
      if (ui.replayBtn) ui.replayBtn.addEventListener('click', () => this.replaySpeech());
      if (ui.progressContainer) ui.progressContainer.addEventListener('click', (ev) => this._onSeekClick(ev));
      // Settings wiring (optional)
      if (ui.settings) {
        const s = ui.settings;
        const prefs = this._loadPrefs() || {};
        if (s.providerSel) s.providerSel.value = prefs.provider || this._defaults.provider || 'piper';
        if (s.voiceInput) s.voiceInput.value = prefs.voice || this._defaults.voice || '';
        if (s.speedInput) s.speedInput.value = String((typeof prefs.speed === 'number' ? prefs.speed : (typeof this._defaults.speed === 'number' ? this._defaults.speed : 1.0)));
        if (s.saveBtn) s.saveBtn.addEventListener('click', (e) => {
          e.preventDefault();
          const next = {
            provider: s.providerSel ? s.providerSel.value || undefined : undefined,
            voice: s.voiceInput ? (s.voiceInput.value || undefined) : undefined,
            speed: s.speedInput ? parseFloat(s.speedInput.value || '1') : 1.0,
          };
          this._applyPrefs(next);
          try {
            if (typeof window.displayNotification === 'function') {
              window.displayNotification('Voice settings saved');
            } else {
              console.info('[FacTTS] Voice settings saved');
            }
          } catch(_) {}
        });
        if (s.cancelBtn) s.cancelBtn.addEventListener('click', (e) => { e.preventDefault(); this._populateSettingsUI(); });
      }
    }

    initForTask(taskId, text, opts={}) {
      try {
        const isNewTask = (this.taskId !== taskId);
        this.taskId = taskId;
        this.text = String(text||'');
  const prefs = this._loadPrefs() || {};
  this._provider = (opts.provider || prefs.provider || this._defaults.provider || 'piper');
  this._voice = (typeof opts.voice !== 'undefined' ? opts.voice : (typeof prefs.voice !== 'undefined' ? prefs.voice : this._defaults.voice)) || undefined;
  this._speed = (typeof opts.speed === 'number' ? opts.speed : (typeof prefs.speed === 'number' ? prefs.speed : (typeof this._defaults.speed === 'number' ? this._defaults.speed : 1.0)));
  this._format = opts.format || prefs.format || (this._provider === 'polly' ? 'mp3' : 'wav');
  // Reset audio source if the task changed
        if (isNewTask) this._disposeObjectUrl();
  // Pre-split words for progress streaming
  try { this._words = String(this.text||'').trim().split(/\s+/).filter(Boolean); } catch(_) { this._words = []; }
    // Reset marks cache when task/text changes
    this._marks = null;
    this._marksLoadedForHash = null;
        const state = this._loadState();
        // If text changed vs stored, reset state
        if (state && state.textHash && state.textHash !== hashText(this.text)) {
          this._saveState({ hasPlayedOnce: false, status: 'stopped', currentTime: 0, duration: 0, textHash: hashText(this.text) });
        }
        // Autoplay once per task when enabled and narration exists; do not replay after refresh
        const stNow = this._loadState();
        const auto = !!((this._defaults && this._defaults.autoread) || (window.ttsDefaults && window.ttsDefaults.autoread));
        const hasText = !!(this.text && this.text.trim().length > 0);
        if (auto && hasText && !(stNow && stNow.hasPlayedOnce)) {
          // fire-and-forget; playSpeech will set hasPlayedOnce in state
          this.playSpeech();
        } else {
          // Honor persisted pause/stop without autoplay
          this._syncUI();
        }
      } catch (e) { console.warn('[FacTTS] initForTask error', e); }
    }

    async _fetchAudio() {
      if (this._isLoading) return;
      this._isLoading = true;
      if (this._abort) try { this._abort.abort(); } catch(_) {}
      this._abort = new AbortController();
      try {
        const qs = new URLSearchParams();
        qs.set('text', this.text);
        if (this._provider) qs.set('provider', this._provider);
        if (this._voice) qs.set('voice', this._voice);
        if (this._speed) qs.set('speed', String(this._speed));
        if (this._format) qs.set('format', this._format);
        const url = `/service/speech/speak?${qs.toString()}`;
        const res = await fetch(url, { signal: this._abort.signal });
        if (!res.ok) throw new Error(`TTS fetch failed: ${res.status}`);
        const blob = await res.blob();
        this._disposeObjectUrl();
        this._objectUrl = URL.createObjectURL(blob);
        const audio = this._ensureAudio();
        audio.src = this._objectUrl;
        this._isLoading = false;
        return audio;
      } catch (e) {
        this._isLoading = false;
        notify('danger', (e && e.message) || 'Failed to load speech audio');
        throw e;
      }
    }

    async _ensureMarks() {
      try {
        // Only attempt for providers that may support marks (currently Polly)
        const provider = (this._provider || '').toLowerCase();
        if (provider !== 'polly') return false;
        const key = `${provider}|${this._voice||''}|${hashText(this.text)}`;
        if (this._marks && this._marksLoadedForHash === key) return true;
        const qs = new URLSearchParams();
        qs.set('text', this.text);
        if (this._provider) qs.set('provider', this._provider);
        if (this._voice) qs.set('voice', this._voice);
        if (typeof this._speed === 'number') qs.set('speed', String(this._speed));
        const url = `/service/speech/marks?${qs.toString()}`;
        const res = await fetch(url, { signal: this._abort?.signal });
        if (!res.ok) throw new Error(`marks ${res.status}`);
        const data = await res.json();
        if (data && data.success && Array.isArray(data.marks) && data.marks.length > 0) {
          // Normalize times and words
          this._marks = data.marks.map(m => ({ time: Math.max(0, parseInt(m.time, 10)||0), value: String(m.value||'') }));
          this._marksLoadedForHash = key;
          return true;
        }
        this._marks = null;
        this._marksLoadedForHash = key;
        return false;
      } catch (_) {
        this._marks = null; return false;
      }
    }

    async playSpeech({ fromStart = false } = {}) {
      try {
        if (!this.text || this.text.trim().length === 0) {
          notify('info', 'No narration available for this phase');
          this._syncUI();
          return;
        }
        // Pause live STT capture to avoid organizer mic duplicating AI lines
        try { if (window.pauseTranscriptionForFacilitator) window.pauseTranscriptionForFacilitator(); } catch(_) {}
        // Stop transcript/section TTS if it's playing to avoid overlap
        try { if (window.TTS && typeof window.TTS.stop === 'function') window.TTS.stop(); } catch(_) {}
        // Broadcast that facilitator narration is about to start so other players can suppress
        try { window.dispatchEvent(new CustomEvent('facilitator-tts-starting', { detail: { workshopId: this.workshopId, taskId: this.taskId } })); } catch(_) {}
        const audio = this._ensureAudio();
        if (!audio.src) await this._fetchAudio();
  // Fire-and-forget attempt to load word marks for precise progress
  this._ensureMarks().catch(()=>{});
        // Either restart from beginning or resume from persisted time
        const st = this._loadState();
        const isFirstPlayThisTask = !(st && st.hasPlayedOnce);
        if (fromStart) {
          try { audio.currentTime = 0; } catch(_) {}
        } else if (st && st.currentTime && !isNaN(st.currentTime) && audio.currentTime < st.currentTime - 0.25) {
          try { audio.currentTime = st.currentTime; } catch(_) {}
        }
        let playErr = null;
        try {
          // Some browsers (iOS/Safari/Chrome after inactivity) require resuming the AudioContext
          try { if (this._levelCtx && this._levelCtx.state === 'suspended') await this._levelCtx.resume(); } catch(_) {}
          const p = audio.play();
          if (p && p.catch) await p;
        } catch (e) {
          playErr = e;
        }
        // Mark as played once
        const saved = this._loadState() || {};
        this._saveState({ ...saved, hasPlayedOnce: true, status: 'playing', textHash: hashText(this.text) });
        // Only include narration text on the first play to persist a single facilitator transcript row
        this._telemetry('play', { includeText: isFirstPlayThisTask });
        // Start progress streaming while audio is playing
        this._startProgressTimer();
        // If autoplay was blocked by browser policy, try a one-time user-gesture unlock
        if (playErr && (playErr.name === 'NotAllowedError' || playErr.message?.toLowerCase().includes('user gesture'))) {
          try {
            const unlock = () => {
              try { document.removeEventListener('pointerdown', unlock, true); document.removeEventListener('keydown', unlock, true); } catch(_) {}
              try { this.playSpeech({ fromStart }); } catch(_) {}
            };
            document.addEventListener('pointerdown', unlock, true);
            document.addEventListener('keydown', unlock, true);
            notify('info', 'Tap anywhere to start facilitator audio');
          } catch(_) {}
        }
        this._syncUI();
      } catch (_) {}
    }

    pauseSpeech() {
      try {
        const a = this._ensureAudio();
        a.pause();
        this._stopProgressTimer();
        // Do not resume STT on pause (user might resume shortly); only on stop/ended
        const st = this._loadState() || {};
        this._saveState({ ...st, status: 'paused', currentTime: a.currentTime, duration: isNaN(a.duration)?(st.duration||0):a.duration, textHash: hashText(this.text), hasPlayedOnce: true });
        this._telemetry('pause');
        this._syncUI();
      } catch (_) {}
    }

    stopSpeech() {
      try {
        const a = this._ensureAudio();
        a.pause();
        try { a.currentTime = 0; } catch(_) {}
        this._stopProgressTimer();
        const st = this._loadState() || {};
        this._saveState({ ...st, status: 'stopped', currentTime: 0, textHash: hashText(this.text), hasPlayedOnce: true });
        this._telemetry('stop');
        // Clear partials for listeners
        this._telemetry('progress', { partial: '' });
        // Resume STT if it was active before playback started
        try { if (window.resumeTranscriptionAfterFacilitator) window.resumeTranscriptionAfterFacilitator(); } catch(_) {}
        // Notify others that facilitator narration stopped
        try { window.dispatchEvent(new CustomEvent('facilitator-tts-stopped', { detail: { workshopId: this.workshopId, taskId: this.taskId } })); } catch(_) {}
        this._syncUI();
      } catch (_) {}
    }

    replaySpeech() {
      try {
        if (!this.text || this.text.trim().length === 0) {
          notify('info', 'No narration to replay');
          this._syncUI();
          return;
        }
        // Always (re)start from the beginning; if audio isn’t loaded yet, playSpeech will fetch it
        const a = this._ensureAudio();
        try { a.currentTime = 0; } catch(_) {}
        this.playSpeech({ fromStart: true });
      } catch (_) {}
    }

    cancelSpeech() {
      try {
        if (this._abort) try { this._abort.abort(); } catch(_) {}
        const a = this._ensureAudio();
        a.pause();
        this._disposeObjectUrl();
        this._isLoading = false;
        const st = this._loadState() || {};
        this._saveState({ ...st, status: 'stopped' });
        this._syncUI();
      } catch (_) {}
    }

    isSpeaking() {
      try { const a = this._ensureAudio(); return !a.paused && !a.ended; } catch(_) { return false; }
    }

    _onTimeUpdate() {
      const a = this._audio;
      if (!a || !this._ui) return;
      const dur = isNaN(a.duration) ? 0 : a.duration;
      const cur = isNaN(a.currentTime) ? 0 : a.currentTime;
      if (this._ui.progressBar) this._ui.progressBar.style.width = dur>0 ? `${Math.min(100, Math.max(0, (cur/dur)*100))}%` : '0%';
      if (this._ui.elapsed) this._ui.elapsed.textContent = this._fmtTime(cur);
      if (this._ui.duration) this._ui.duration.textContent = dur ? this._fmtTime(dur) : '--:--';
      const st = this._loadState() || {};
      this._saveState({ ...st, currentTime: cur, duration: dur, status: this.isSpeaking()?'playing':'paused', textHash: hashText(this.text), hasPlayedOnce: (st.hasPlayedOnce || this.isSpeaking()) });
    }

    _onLoadedMeta() {
      if (this._pendingSeekRatio != null && this._audio && !isNaN(this._audio.duration)) {
        try { this._audio.currentTime = Math.max(0, Math.min(this._audio.duration, this._pendingSeekRatio * this._audio.duration)); } catch(_) {}
        this._pendingSeekRatio = null;
      }
      this._syncUI();
    }
    _onEnded() {
      const st = this._loadState() || {};
      this._saveState({ ...st, status: 'stopped', currentTime: 0, hasPlayedOnce: true });
      this._stopProgressTimer();
      // Emit final to let server broadcast transcript_final at end
      this._telemetry('ended', { includeText: true });
      // Resume STT after narration finishes
      try { if (window.resumeTranscriptionAfterFacilitator) window.resumeTranscriptionAfterFacilitator(); } catch(_) {}
      // Signal to the rest of the app that narration finished
      try { window.dispatchEvent(new CustomEvent('facilitator-tts-ended', { detail: { workshopId: this.workshopId, taskId: this.taskId } })); } catch(_) {}
      this._syncUI();
    }
    _onPause() {
      // Inform listeners that playback is paused (optional)
      try { window.dispatchEvent(new CustomEvent('facilitator-tts-paused', { detail: { workshopId: this.workshopId, taskId: this.taskId } })); } catch(_) {}
      this._syncUI();
    }
    _onPlay() {
      // Inform listeners that playback actually started
      try { window.dispatchEvent(new CustomEvent('facilitator-tts-play', { detail: { workshopId: this.workshopId, taskId: this.taskId } })); } catch(_) {}
      this._syncUI();
    }
    _onError() { notify('danger', 'Audio playback error'); this._syncUI(); }

    _disposeObjectUrl() {
      if (this._objectUrl) { try { URL.revokeObjectURL(this._objectUrl); } catch(_) {} this._objectUrl = null; }
      if (this._audio) { try { this._audio.removeAttribute('src'); this._audio.load(); } catch(_) {} }
    }

    _fmtTime(sec) {
      const s = Math.max(0, Math.floor(sec||0));
      const mm = String(Math.floor(s/60)).padStart(2,'0');
      const ss = String(s%60).padStart(2,'0');
      return `${mm}:${ss}`;
    }

    _syncUI() {
      if (!this._ui) return;
      const speaking = this.isSpeaking();
      if (this._ui.icon) this._ui.icon.classList.toggle('speaking', speaking);
      const noText = !(this.text && this.text.trim().length > 0);
      // Enable Play whenever we have narration text (even before audio loads) and not already speaking
      if (this._ui.playBtn) this._ui.playBtn.disabled = speaking || noText;
      if (this._ui.pauseBtn) this._ui.pauseBtn.disabled = !speaking;
      if (this._ui.stopBtn) this._ui.stopBtn.disabled = !speaking && (!this._audio || this._audio.currentTime===0);
      const hasAudioSrc = !!(this._audio && this._audio.src);
      // Replay is allowed if there's text (to fetch & start from 0) or an audio source already
      if (this._ui.replayBtn) this._ui.replayBtn.disabled = (noText && !hasAudioSrc);
    }

    _loadState() {
      try { const raw = localStorage.getItem(LS_KEY(this.workshopId, this.taskId)); return raw? JSON.parse(raw): null; } catch(_) { return null; }
    }
    _saveState(obj) {
      try { localStorage.setItem(LS_KEY(this.workshopId, this.taskId), JSON.stringify(obj)); } catch(_) {}
    }

    _loadPrefs() {
      try { const raw = localStorage.getItem(PREF_KEY(this.workshopId)); return raw ? JSON.parse(raw) : null; } catch(_) { return null; }
    }
    _savePrefs(obj) {
      try { localStorage.setItem(PREF_KEY(this.workshopId), JSON.stringify(obj)); } catch(_) {}
    }
    _applyPrefs({ provider, voice, speed, format }) {
      if (provider) this._provider = provider;
      if (typeof voice !== 'undefined') this._voice = voice || undefined;
      if (typeof speed === 'number' && !Number.isNaN(speed)) this._speed = speed;
      if (format) this._format = format;
      const prefs = { provider: this._provider, voice: this._voice, speed: this._speed, format: this._format };
      this._savePrefs(prefs);
      this._prefs = prefs;
      this.cancelSpeech();
      this._populateSettingsUI();
    }
    _populateSettingsUI() {
      if (!this._ui || !this._ui.settings) return;
      const s = this._ui.settings;
      const prefs = this._loadPrefs() || {};
      if (s.providerSel) s.providerSel.value = prefs.provider || this._provider || this._defaults.provider || 'piper';
      if (s.voiceInput) s.voiceInput.value = prefs.voice || this._voice || this._defaults.voice || '';
      if (s.speedInput) s.speedInput.value = String((typeof prefs.speed === 'number' ? prefs.speed : (typeof this._speed === 'number' ? this._speed : 1.0)));
    }
    _onSeekClick(ev) {
      try {
        const bar = ev.currentTarget;
        const rect = bar.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
        const a = this._ensureAudio();
        if (!isNaN(a.duration) && a.duration > 0) {
          a.currentTime = ratio * a.duration;
        } else {
          this._pendingSeekRatio = ratio;
        }
      } catch(_) {}
    }
    _telemetry(kind, opts={}) {
      try {
        const payload = { kind, workshop_id: this.workshopId, task_id: this.taskId, provider: this._provider, voice: this._voice, speed: this._speed, ts: Date.now() };
        // Include narration text on first play and on ended
        if ((kind === 'play' || kind === 'ended') && opts && opts.includeText) {
          try { payload.text = this.text || ''; } catch(_) { /* noop */ }
        }
        // Attach partial for progress kind
        if (kind === 'progress' && opts && typeof opts.partial === 'string') {
          payload.partial = opts.partial;
        }
        if (typeof payload.task_id !== 'number' && window.currentTaskId) {
          try { payload.task_id = Number(window.currentTaskId) || undefined; } catch(_) {}
        }
        if (window._workshopSocket && typeof window._workshopSocket.emit === 'function') window._workshopSocket.emit('facilitator_tts_event', payload);
        console.debug('[FacTTS telemetry]', payload);
      } catch(_) {}
    }

    _startProgressTimer() {
      try {
        this._stopProgressTimer();
        const a = this._ensureAudio();
        if (!a) return;
        const tick = () => {
          try {
            if (!this.isSpeaking()) return; // stopped/paused
            const dur = isNaN(a.duration) ? 0 : a.duration;
            const cur = isNaN(a.currentTime) ? 0 : a.currentTime;
            if (!this._words || this._words.length === 0) return;
            let partial = '';
            // Prefer precise marks when available
            if (this._marks && this._marks.length > 0) {
              const tms = Math.max(0, Math.floor(cur * 1000));
              // Count words whose mark time <= current time
              let count = 0;
              // Simple linear scan works for short scripts; could binary search if needed
              for (let i=0; i<this._marks.length; i++) {
                if ((this._marks[i].time||0) <= tms) count++; else break;
              }
              if (count > 0) partial = this._words.slice(0, Math.min(count, this._words.length)).join(' ');
            }
            if (!partial) {
              // Fallback to duration-based heuristic
              if (dur <= 0) return;
              const ratio = Math.max(0, Math.min(1, cur / dur));
              const count = Math.max(1, Math.floor(this._words.length * ratio));
              partial = this._words.slice(0, count).join(' ');
            }
            this._telemetry('progress', { partial });
          } catch(_) {}
        };
        this._progressTimer = setInterval(tick, 250);
      } catch(_) {}
    }
    _stopProgressTimer() {
      try { if (this._progressTimer) { clearInterval(this._progressTimer); this._progressTimer = null; } } catch(_) {}
    }

    // --- Level analysis and subscriptions ---
    _initLevelAnalyser(){
      try {
        if (!this._audio) return;
        if (!this._levelCtx) this._levelCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (!this._levelSrc) this._levelSrc = this._levelCtx.createMediaElementSource(this._audio);
        if (!this._levelAnalyser) {
          this._levelAnalyser = this._levelCtx.createAnalyser();
          this._levelAnalyser.fftSize = 256;
          this._levelBuf = new Uint8Array(this._levelAnalyser.frequencyBinCount);
          // Route media element through analyser AND to destination so audio is audible.
          // Creating a MediaElementSourceNode detaches the element's default output path,
          // so we must connect to destination explicitly.
          this._levelSrc.connect(this._levelAnalyser);
          try { this._levelSrc.connect(this._levelCtx.destination); } catch(_) {}
        }
        const loop = () => {
          try {
            if (!this._levelAnalyser) { this._levelRAF = null; return; }
            this._levelAnalyser.getByteTimeDomainData(this._levelBuf);
            let sum = 0; for (let i=0;i<this._levelBuf.length;i++){ const v = (this._levelBuf[i]-128)/128; sum += v*v; }
            const rms = Math.sqrt(sum / this._levelBuf.length);
            const level = Math.min(1, rms * 6); // amplify for visibility
            const speaking = level > 0.18 && this.isSpeaking();
            if (speaking !== this._speaking) {
              this._speaking = speaking;
              this._speakListeners.forEach(fn=>{ try { fn(!!speaking); } catch(_){} });
            }
            this._levelListeners.forEach(fn=>{ try { fn(level); } catch(_){} });
            this._levelRAF = requestAnimationFrame(loop);
          } catch(_) { this._levelRAF = requestAnimationFrame(loop); }
        };
        if (!this._levelRAF) this._levelRAF = requestAnimationFrame(loop);
      } catch(_){}
    }
    onLevel(cb){ if (typeof cb==='function') this._levelListeners.push(cb); }
    onSpeakingChange(cb){ if (typeof cb==='function') this._speakListeners.push(cb); }
    getAudioElement(){ return this._ensureAudio(); }
  }

  window.FacilitatorTTS = new FacilitatorTTSController(window.workshopId || undefined);
})();
