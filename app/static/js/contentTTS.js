// Legacy shim retained for backward compatibility.
// The previous ContentTTS implementation auto-scraped DOM content, which lead to
// inaccurate narration in the workshop lobby. The new lobbyTts.js module drives
// playback using clean, LLM-provided text. This shim keeps the global symbol to
// avoid runtime errors if other bundles still reference `ContentTTS`.

(function () {
  if (window.ContentTTS) {
    return;
  }

  function warn(method) {
    console.warn(`ContentTTS.${method} is deprecated. Please migrate to LobbyTTS.`);
  }

  const noop = function () { warn('enable'); };
  const api = {
    enable() { warn('enable'); },
    enableAll() { warn('enableAll'); },
    disable() { warn('disable'); },
    stopAll() { warn('stopAll'); if (window.LobbyTTS && typeof window.LobbyTTS.stop === 'function') { window.LobbyTTS.stop(); } },
    getInstance() { warn('getInstance'); return null; },
    config: {}
  };

  window.ContentTTS = api;
})();