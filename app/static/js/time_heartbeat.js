(function () {
  const root = document.getElementById('assistant-root');
  if (!root) return;
  const workshopIdRaw = root.dataset.workshopId || window.workshopId;
  if (!workshopIdRaw) return;
  const workshopId = Number(workshopIdRaw);
  if (!Number.isFinite(workshopId)) return;

  const HEARTBEAT_INTERVAL = (window.HEARTBEAT_INTERVAL_SECONDS || 60) * 1000;
  const socket = window.assistantSocket || (window.io ? window.io('/assistant') : null);
  const workshopSocket = window.workshopSocket || (window.io ? window.io('/workshop') : null);
  if (!workshopSocket && !socket) return;

  const emitHeartbeat = () => {
    const payload = {
      workshop_id: workshopId,
      timestamp: Date.now(),
    };
    if (workshopSocket) {
      workshopSocket.emit('heartbeat', payload);
    } else if (socket) {
      socket.emit('heartbeat', payload);
    }
  };

  emitHeartbeat();
  const intervalId = window.setInterval(emitHeartbeat, HEARTBEAT_INTERVAL);
  window.addEventListener('beforeunload', () => window.clearInterval(intervalId));
})();
