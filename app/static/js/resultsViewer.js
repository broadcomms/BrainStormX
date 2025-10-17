// Generic Results PDF Viewer with Socket Sync
// Usage:
//   ResultsViewer.injectPdfLinks(containerEl, { pdfUrl, kind, openText })
//   ResultsViewer.mountViewer({
//     containerEl, pdfUrl, kind, taskId,
//     canControl, socket, roomName, workshopId, userId,
//     controlEvent: 'feasibility_control'|'prioritization_control'|'action_plan_control',
//     syncEvent: 'feasibility_sync'|'prioritization_sync'|'action_plan_sync',
//     roleNoteText
//   })

window.ResultsViewer = (function () {
  const mounts = new Set();

  async function ensurePdfJs() {
    if (window.pdfjsLib && window.pdfjsLib.getDocument) return true;
    try {
      const base = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174';
      const s = document.createElement('script'); s.src = `${base}/pdf.min.js`; s.async = true;
      document.head.appendChild(s);
      await new Promise((res, rej) => { s.onload = res; s.onerror = rej; });
      window.pdfjsLib.GlobalWorkerOptions.workerSrc = `${base}/pdf.worker.min.js`;
      return true;
    } catch (_) { return false; }
  }

  function injectPdfLinks(containerEl, { pdfUrl, kind, openText }) {
    if (!containerEl || !pdfUrl) return null;
    const pdfControls = document.createElement('div');
    pdfControls.className = 'mt-2 d-flex align-items-center gap-2 pdf-link-block';
    pdfControls.setAttribute('data-rv-owned', '1');
    if (kind) pdfControls.setAttribute('data-kind', String(kind));
    pdfControls.innerHTML = `
      <a class="btn btn-sm btn-outline-primary" href="${pdfUrl}" target="_blank" rel="noopener">${openText || 'Open PDF'}</a>
      <a class="btn btn-sm btn-primary" href="${pdfUrl}" download>Download</a>
      <button type="button" class="btn btn-sm btn-secondary rv-print-btn">Print</button>
      <span class="badge text-bg-light ms-1 pdf-pages-badge d-none" ${kind ? `data-kind="${kind}"` : ''} data-rv-owned="1" title="Total pages">—</span>
    `;
    containerEl.appendChild(pdfControls);
    const printBtn = pdfControls.querySelector('.rv-print-btn');
    if (printBtn) printBtn.addEventListener('click', () => window.open(pdfUrl, '_blank'));
    return pdfControls;
  }

  function mountViewer({
    containerEl, pdfUrl, kind, taskId,
    canControl, socket, roomName, workshopId, userId,
    controlEvent, syncEvent, roleNoteText
  }) {
    if (!containerEl || !pdfUrl) return null;
    const box = document.createElement('div');
    box.className = 'presentation-viewer-box mt-2';
    box.setAttribute('data-rv-owned', '1');
    if (kind) box.setAttribute('data-rv-kind', String(kind));
    box.innerHTML = `
      <div class="presentation-controls d-flex align-items-center gap-2 mb-2">
        <div class="btn-group btn-group-sm" role="group" aria-label="PDF controls">
          <button class="btn btn-outline-secondary pres-prev" title="Previous page"><i class="bi bi-caret-left-fill"></i></button>
          <button class="btn btn-outline-secondary pres-next" title="Next page"><i class="bi bi-caret-right-fill"></i></button>
        </div>
        <div class="btn-group btn-group-sm ms-1" role="group" aria-label="Zoom">
          <button class="btn btn-outline-secondary pres-zoom-out" title="Zoom out"><i class="bi bi-zoom-out"></i></button>
          <button class="btn btn-outline-secondary pres-zoom-in" title="Zoom in"><i class="bi bi-zoom-in"></i></button>
          <button class="btn btn-outline-secondary pres-fit" data-fit="width" title="Fit width"><i class="bi bi-arrows-fullscreen"></i></button>
          <button class="btn btn-outline-secondary pres-fit" data-fit="page" title="Fit page"><i class="bi bi-aspect-ratio"></i></button>
        </div>
        <div class="ms-2 small text-body-secondary"><span class="pres-page-label">Page</span> <input type="number" class="form-control form-control-sm d-inline-block pres-page-input" style="width:80px" min="1" value="1"> <span class="pres-pages-total"></span></div>
        
      </div>
      <div class="presentation-viewer-wrap position-relative border rounded bg-body-tertiary" style="min-height:240px;">
        <div class="presentation-placeholder small text-body-secondary p-3">Loading PDF…</div>
      </div>`;

    containerEl.insertBefore(box, containerEl.firstChild);
  mounts.add(box);

    const wrap = box.querySelector('.presentation-viewer-wrap');
    const pageInput = box.querySelector('.pres-page-input');
    const pagesTotal = box.querySelector('.pres-pages-total');
    const btnPrev = box.querySelector('.pres-prev');
    const btnNext = box.querySelector('.pres-next');
    const btnZoomIn = box.querySelector('.pres-zoom-in');
    const btnZoomOut = box.querySelector('.pres-zoom-out');
    const btnFits = box.querySelectorAll('.pres-fit');
  const badge = kind ? document.querySelector(`.pdf-pages-badge[data-kind="${kind}"]`) : null;
  const ctrls = box.querySelector('.presentation-controls');
  if (!canControl && ctrls) { ctrls.querySelectorAll('button,input').forEach(el => el.disabled = true); }

  const canvas = document.createElement('canvas');
  canvas.className = 'rounded bg-white';
  const ctx = canvas.getContext('2d', { alpha: false });
    const ph = box.querySelector('.presentation-placeholder');
    wrap.appendChild(canvas);

    const storageKey = `ws:${workshopId}:${kind || 'pdf'}:${taskId || 't'}`;
    function persistLocal(page, zoom, fit) {
      try { localStorage.setItem(storageKey, JSON.stringify({ page, zoom, fit })); } catch (_) {}
    }
    function restoreLocal() {
      try { return JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch (_) { return {}; }
    }
    function updatePageLabelLocal() {
      if (pageInput) pageInput.value = String(currentPage);
      if (pagesTotal) pagesTotal.textContent = totalPages > 0 ? `/ ${totalPages}` : '';
    }
    function computeScaleForFit(viewport) {
      try {
        const availW = wrap.clientWidth || canvas.parentElement.clientWidth || viewport.width;
        const availH = Math.max(320, Math.round(window.innerHeight * 0.52));
        const scaleW = availW / viewport.width;
        const scaleH = availH / viewport.height;
        if (currentFit === 'page') return Math.min(scaleW, scaleH);
        if (currentFit === 'width') return scaleW;
        return currentZoom;
      } catch (_) { return currentZoom; }
    }

    let pdfDoc = null; let totalPages = 0; let pending = false; let renderTask = null;
  const saved = restoreLocal();
  let currentPage = Math.max(1, parseInt(saved.page || 1, 10) || 1);
  let currentZoom = Math.max(0.25, Math.min(5.0, parseFloat(saved.zoom || 1.0)));
  // Default to fit width unless user explicitly switched to none or page
  let currentFit = (saved.fit && (saved.fit === 'none' || saved.fit === 'page' || saved.fit === 'width')) ? saved.fit : 'width';

    async function renderPage() {
      if (!pdfDoc || !ctx) return;
      pending = true;
      try {
        if (renderTask && typeof renderTask.cancel === 'function') {
          try { renderTask.cancel(); } catch (_) {}
        }
        const page = await pdfDoc.getPage(currentPage);
        const rotation = (page.rotate || 0);
        // Base viewport at scale 1 with correct rotation to compute fit
        let baseViewport = page.getViewport({ scale: 1.0, rotation });
        const scale = computeScaleForFit(baseViewport);
        const dpr = (window.devicePixelRatio || 1);
        const viewport = page.getViewport({ scale: scale * dpr, rotation });
        // Set canvas pixel size and CSS size for crisp rendering
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        canvas.style.width = Math.floor(viewport.width / dpr) + 'px';
        canvas.style.height = Math.floor(viewport.height / dpr) + 'px';
        // Clear any previous drawings
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        if (ph) ph.style.display = 'none';
        renderTask = page.render({ canvasContext: ctx, viewport });
        await renderTask.promise;
        if (badge) { badge.classList.remove('d-none'); badge.textContent = `${totalPages} page${totalPages===1?'':'s'}`; }
      } catch (err) {
        // Ignore benign cancellations that happen when flipping pages fast
        if (!String(err && err.name).includes('RenderingCancelledException')) {
          // Optional: console.debug('Render error', err);
        }
      } finally { pending = false; }
    }

    (async () => {
      const ok = await ensurePdfJs();
      if (!ok) {
        // Fallback iframe
        wrap.innerHTML = '';
        const iframe = document.createElement('iframe');
        iframe.src = pdfUrl + '#toolbar=0&navpanes=0&scrollbar=1';
        iframe.title = 'PDF Viewer';
        iframe.style.width = '100%';
        iframe.style.height = '52vh';
        iframe.className = 'border-0 rounded presentation-iframe';
        wrap.appendChild(iframe);
        return;
      }
      try {
        const doc = await window.pdfjsLib.getDocument({ url: pdfUrl }).promise;
        pdfDoc = doc; totalPages = doc.numPages || 0;
        updatePageLabelLocal(); await renderPage();
      } catch (_) {
        wrap.innerHTML = '';
        const iframe = document.createElement('iframe');
        iframe.src = pdfUrl + '#toolbar=0&navpanes=0&scrollbar=1';
        iframe.title = 'PDF Viewer';
        iframe.style.width = '100%';
        iframe.style.height = '52vh';
        iframe.className = 'border-0 rounded presentation-iframe';
        wrap.appendChild(iframe);
      }
    })();

    function broadcast(action) {
      if (!canControl || !socket || !controlEvent) return;
      try {
        socket.emit(controlEvent, {
          room: roomName,
          workshop_id: workshopId,
          user_id: userId,
          task_id: taskId,
          action,
          page: currentPage,
          zoom: currentZoom,
          fit: currentFit,
        });
      } catch (_) {}
    }

    if (btnPrev) btnPrev.addEventListener('click', async () => {
      if (!canControl || pending) return;
      currentPage = Math.max(1, currentPage - 1);
      updatePageLabelLocal(); persistLocal(currentPage, currentZoom, currentFit); broadcast('goto'); await renderPage();
    });
    if (btnNext) btnNext.addEventListener('click', async () => {
      if (!canControl || pending) return;
      currentPage = Math.min(totalPages || (currentPage+1), currentPage + 1);
      updatePageLabelLocal(); persistLocal(currentPage, currentZoom, currentFit); broadcast('goto'); await renderPage();
    });
    if (btnZoomIn) btnZoomIn.addEventListener('click', async () => {
      if (!canControl || pending) return;
      currentZoom = Math.min(5.0, (currentZoom + 0.1)); currentFit = 'none';
      persistLocal(currentPage, currentZoom, currentFit); broadcast('zoom'); await renderPage();
    });
    if (btnZoomOut) btnZoomOut.addEventListener('click', async () => {
      if (!canControl || pending) return;
      currentZoom = Math.max(0.25, (currentZoom - 0.1)); currentFit = 'none';
      persistLocal(currentPage, currentZoom, currentFit); broadcast('zoom'); await renderPage();
    });
    btnFits.forEach(btn => btn.addEventListener('click', async (e) => {
      if (!canControl || pending) return;
      const t = e.currentTarget; currentFit = t?.getAttribute('data-fit') || 'page';
      persistLocal(currentPage, currentZoom, currentFit); broadcast('fit'); await renderPage();
    }));
    if (pageInput) pageInput.addEventListener('change', async () => {
      if (!canControl || pending) return;
      const n = parseInt(pageInput.value || '1', 10) || 1; currentPage = Math.max(1, Math.min(totalPages || n, n));
      updatePageLabelLocal(); persistLocal(currentPage, currentZoom, currentFit); broadcast('goto'); await renderPage();
    });

    // Listen for sync
    if (socket && syncEvent) {
      try { socket.off(syncEvent); } catch (_) {}
      socket.on(syncEvent, async (m) => {
        try {
          if (!m || m.workshop_id !== workshopId || m.task_id !== taskId) return;
          if (m.page) currentPage = Math.max(1, parseInt(m.page, 10) || 1);
          if (m.zoom) currentZoom = Math.max(0.25, Math.min(5.0, parseFloat(m.zoom)));
          if (m.fit) currentFit = String(m.fit);
          updatePageLabelLocal(); persistLocal(currentPage, currentZoom, currentFit); await renderPage();
        } catch (_) {}
      });
    }

    // Auto-resize when fitting
    try {
      let rid = null;
      const ro = new ResizeObserver(() => {
        if (currentFit === 'none') return;
        if (rid) cancelAnimationFrame(rid);
        rid = requestAnimationFrame(() => { renderPage(); });
      });
      ro.observe(wrap);
    } catch (_) {}

    return box;
  }

  function teardown(options) {
    const opts = options || {};
    const scope = (opts && typeof opts.kind === 'string') ? String(opts.kind) : null;
    const toRemove = [];
    mounts.forEach((node) => {
      if (!node) return;
      if (scope && node.getAttribute('data-rv-kind') && node.getAttribute('data-rv-kind') !== scope) return;
      toRemove.push(node);
    });
    toRemove.forEach((node) => {
      try {
        if (node.parentNode) node.parentNode.removeChild(node);
      } catch (_) { /* ignore */ }
      mounts.delete(node);
    });
    if (!scope) {
      mounts.clear();
    }
    try {
      const selector = scope ? `.pdf-link-block[data-kind="${scope}"][data-rv-owned]` : '.pdf-link-block[data-rv-owned]';
      document.querySelectorAll(selector).forEach((el) => {
        try { el.remove(); } catch (_) { /* ignore */ }
      });
    } catch (_) { /* ignore */ }
    try {
      const badgeSelector = scope ? `.pdf-pages-badge[data-kind="${scope}"][data-rv-owned]` : '.pdf-pages-badge[data-rv-owned]';
      document.querySelectorAll(badgeSelector).forEach((el) => {
        el.classList.add('d-none');
        el.textContent = '—';
      });
    } catch (_) { /* ignore */ }
  }

  return { ensurePdfJs, injectPdfLinks, mountViewer, teardown };
})();
