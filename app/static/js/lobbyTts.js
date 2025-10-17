(function () {
  'use strict';

  const STATE = {
    content: {},
    buttons: new Map(), // key -> Set<HTMLButtonElement>
    status: new Map(),
    cards: new Map(),
    pendingKey: null,
    activeKey: null,
    socketBound: false,
    awaitingPlaybackEnd: null,
  };

  const BUTTON_ICONS = {
    idle: '<i class="bi bi-volume-up" aria-hidden="true"></i>',
    playing: '<i class="bi bi-stop-fill" aria-hidden="true"></i>',
    loading: '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>',
  };

  const CARD_CLASSES = {
    loading: 'tts-card-loading',
    playing: 'tts-card-playing',
  };

  function injectStyles() {
    if (document.getElementById('lobby-tts-style')) {
      return;
    }
    const style = document.createElement('style');
    style.id = 'lobby-tts-style';
    style.textContent = `
      [data-tts-card].${CARD_CLASSES.loading} {
        opacity: 0.85;
      }
      [data-tts-card].${CARD_CLASSES.playing} {
        box-shadow: 0 0 0 0.25rem rgba(var(--bs-primary-rgb, 13,110,253), .22);
        border-color: rgba(var(--bs-primary-rgb, 13,110,253), .6);
      }
      .tts-trigger[aria-pressed="true"] {
        color: #fff;
        background-color: var(--bs-primary);
        border-color: var(--bs-primary);
      }
    `;
    document.head.appendChild(style);
  }

  function normalizeText(value) {
    if (typeof value !== 'string') {
      return '';
    }
    return value.replace(/\s+/g, ' ').trim();
  }

  function loadInitialContent() {
    const script = document.getElementById('lobby-tts-json');
    if (!script) {
      return {};
    }
    try {
      const parsed = JSON.parse(script.textContent || '{}');
      const result = {};
      Object.keys(parsed || {}).forEach((key) => {
        result[key] = normalizeText(parsed[key]);
      });
      return result;
    } catch (err) {
      console.warn('LobbyTTS: failed to parse initial content', err);
      return {};
    }
  }

  function getOrCreateButtonSet(key) {
    let set = STATE.buttons.get(key);
    if (!set) {
      set = new Set();
      STATE.buttons.set(key, set);
    }
    return set;
  }

  function getCardForKey(key) {
    const cached = STATE.cards.get(key);
    if (cached && document.body.contains(cached)) {
      return cached;
    }
    const card = document.querySelector(`[data-tts-card="${key}"]`);
    if (card) {
      STATE.cards.set(key, card);
    }
    return card;
  }

  function toast(kind, message) {
    try {
      if (typeof window.notify === 'function') {
        const mapped = kind === 'danger' ? 'danger' : kind;
        window.notify(mapped, message);
        return;
      }
      const fn = kind === 'danger' ? 'error' : kind === 'warning' ? 'warn' : 'log';
      console[fn](`[LobbyTTS] ${message}`);
    } catch (err) {
      console.log('[LobbyTTS]', message);
    }
  }

  function updateTooltip(btn, label) {
    try {
      btn.setAttribute('data-bs-title', label);
      btn.setAttribute('title', label);
      if (typeof bootstrap !== 'undefined') {
        const instance = bootstrap.Tooltip.getInstance(btn);
        if (instance && typeof instance.setContent === 'function') {
          instance.setContent({ '.tooltip-inner': label });
        }
      }
    } catch (_) {
      // Ignore tooltip update failures.
    }
  }

  function updateButtonAppearance(btn, key, state) {
    const label = btn.dataset.ttsLabel || key;
    let srLabel;
    if (state === 'playing') {
      srLabel = `Stop ${label}`;
    } else if (state === 'loading') {
      srLabel = `Loading ${label}`;
    } else {
      srLabel = `Play ${label}`;
    }
    btn.innerHTML = `${BUTTON_ICONS[state] || BUTTON_ICONS.idle}<span class="visually-hidden">${srLabel}</span>`;
    btn.disabled = state === 'loading';
    btn.setAttribute('aria-pressed', state === 'playing' ? 'true' : 'false');
    if (state === 'loading') {
      btn.setAttribute('aria-busy', 'true');
    } else {
      btn.removeAttribute('aria-busy');
    }
    btn.dataset.ttsState = state;

    if (state === 'playing') {
      btn.classList.remove('btn-outline-secondary');
      btn.classList.add('btn-primary');
    } else {
      btn.classList.remove('btn-primary');
      if (!btn.classList.contains('btn-outline-secondary')) {
        btn.classList.add('btn-outline-secondary');
      }
    }

    updateTooltip(btn, srLabel);
  }

  function setCardState(key, state) {
    const card = getCardForKey(key);
    if (!card) {
      return;
    }
    card.classList.remove(CARD_CLASSES.loading, CARD_CLASSES.playing);
    if (state === 'loading') {
      card.classList.add(CARD_CLASSES.loading);
    } else if (state === 'playing') {
      card.classList.add(CARD_CLASSES.playing);
    }
  }

  function updateButtonStates(key) {
    const state = STATE.status.get(key) || 'idle';
    const buttons = STATE.buttons.get(key);
    if (buttons) {
      buttons.forEach((btn) => updateButtonAppearance(btn, key, state));
    }
    setCardState(key, state);
  }

  function setStatus(key, state) {
    STATE.status.set(key, state);
    updateButtonStates(key);
  }

  function clearActive() {
    if (STATE.activeKey) {
      const key = STATE.activeKey;
      STATE.activeKey = null;
      STATE.pendingKey = null;
      setStatus(key, 'idle');
    }
    STATE.awaitingPlaybackEnd = null;
  }

  function stopPlayback() {
    if (window.TTS && typeof window.TTS.stop === 'function') {
      try {
        window.TTS.stop();
      } catch (err) {
        console.warn('LobbyTTS: error stopping playback', err);
      }
    }
    clearActive();
    STATE.awaitingPlaybackEnd = null;
  }

  function bindSocket() {
    if (STATE.socketBound) {
      return;
    }
    if (!window.TTS || typeof window.TTS.ensureSocket !== 'function') {
      return;
    }
    try {
      const socket = window.TTS.ensureSocket();
      if (!socket) {
        return;
      }
      STATE.socketBound = true;
      socket.on('tts_audio_start', () => {
        if (STATE.pendingKey) {
          clearActive();
          STATE.activeKey = STATE.pendingKey;
          STATE.pendingKey = null;
          STATE.awaitingPlaybackEnd = null;
          if (STATE.activeKey) {
            setStatus(STATE.activeKey, 'playing');
          }
        } else {
          clearActive();
        }
      });
      socket.on('tts_complete', () => {
        if (STATE.activeKey) {
          STATE.awaitingPlaybackEnd = STATE.activeKey;
        } else {
          clearActive();
        }
      });
      socket.on('tts_error', () => {
        STATE.awaitingPlaybackEnd = null;
        clearActive();
      });
    } catch (err) {
      console.warn('LobbyTTS: failed to bind socket listeners', err);
    }
  }

  function handleButtonClick(event) {
    event.preventDefault();
    const btn = event.currentTarget;
    const key = btn.dataset.ttsKey;
    if (!key) {
      return;
    }
    const currentState = STATE.status.get(key) || 'idle';
    if (currentState === 'loading') {
      return;
    }
    if (currentState === 'playing') {
      stopPlayback();
      btn.blur();
      return;
    }

    const text = STATE.content[key];
    const label = btn.dataset.ttsLabel || key;
    if (!text) {
      toast('warning', `There isn't any narration for ${label} yet.`);
      setStatus(key, 'idle');
      btn.blur();
      return;
    }

    if (!window.TTS || typeof window.TTS.play !== 'function') {
      toast('danger', 'Text-to-speech is unavailable right now.');
      setStatus(key, 'idle');
      btn.blur();
      return;
    }

    STATE.pendingKey = key;
    clearActive();
    setStatus(key, 'loading');
    btn.blur();
    bindSocket();

    try {
      window.TTS.play(text, {});
    } catch (err) {
      console.error('LobbyTTS: playback failed', err);
      toast('danger', 'Could not start playback. Please try again.');
      STATE.pendingKey = null;
      setStatus(key, 'idle');
    }
  }

  function registerButtons() {
    const btns = document.querySelectorAll('[data-tts-key]');
    btns.forEach((btn) => {
      const key = btn.dataset.ttsKey;
      if (!key) {
        return;
      }
      const set = getOrCreateButtonSet(key);
      if (!set.has(btn)) {
        btn.classList.add('tts-trigger');
        btn.addEventListener('click', handleButtonClick);
        set.add(btn);
          if (typeof bootstrap !== 'undefined' && btn.getAttribute('data-bs-toggle') === 'tooltip') {
            try {
              bootstrap.Tooltip.getOrCreateInstance(btn);
            } catch (err) {
              console.warn('LobbyTTS: failed to initialise tooltip', err);
            }
          }
      }
      if (!STATE.status.has(key)) {
        STATE.status.set(key, 'idle');
      }
      updateButtonStates(key);
    });
  }

  function init() {
    STATE.content = loadInitialContent();
    registerButtons();
    bindSocket();
  }

  injectStyles();

  if (!window.LobbyTTS) {
    window.LobbyTTS = {};
  }

  window.LobbyTTS.updateContent = function updateContent(key, text) {
    if (!key) {
      return;
    }
    STATE.content[key] = normalizeText(text);
    if (!STATE.status.has(key)) {
      STATE.status.set(key, 'idle');
      updateButtonStates(key);
    }
    if (!STATE.content[key] && STATE.activeKey === key) {
      stopPlayback();
    }
  };

  window.LobbyTTS.refreshButtons = function refreshButtons() {
    registerButtons();
    bindSocket();
  };

  window.LobbyTTS.stop = function stop() {
    stopPlayback();
  };

  window.addEventListener('tts-playback-start', () => {
    if (STATE.activeKey) {
      setStatus(STATE.activeKey, 'playing');
    }
  });

  window.addEventListener('tts-playback-ended', () => {
    if (STATE.activeKey) {
      clearActive();
    } else if (STATE.awaitingPlaybackEnd) {
      const key = STATE.awaitingPlaybackEnd;
      STATE.awaitingPlaybackEnd = null;
      setStatus(key, 'idle');
    }
  });

  const start = () => init();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
