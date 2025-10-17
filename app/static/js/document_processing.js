// app/static/js/document_processing.js
// Real-time document processing UI wiring for BrainStormX Document AI experience.

(() => {
	const root = document.getElementById('document-processing-root');
	if (!root) {
		return;
	}

	const dataset = root.dataset;
	const parseNumber = (value) => {
		if (value === undefined || value === null || value === '') {
			return null;
		}
		const number = Number(value);
		return Number.isNaN(number) ? null : number;
	};

	const state = {
		documentId: parseNumber(dataset.documentId),
		workspaceId: parseNumber(dataset.workspaceId),
		userId: parseNumber(dataset.userId),
		processUrl: dataset.processUrl,
		chunksUrl: dataset.chunksUrl,
		logsUrl: dataset.logsUrl,
		detailUrl: dataset.detailUrl,
		generateAudioUrl: dataset.generateAudioUrl,
		audioUrl: dataset.audioUrl || '',
		audioDuration: parseNumber(dataset.audioDuration),
		status: dataset.processingStatus || 'pending',
		lastProcessed: dataset.lastProcessed || '',
		socketAttached: false,
	};

	const STATUS_CONFIG = {
		completed: { label: 'Completed', classes: ['text-bg-success'], icon: 'bi bi-check-circle' },
		processing: { label: 'Processing', classes: ['text-bg-warning', 'text-dark'], icon: 'bi bi-arrow-repeat' },
		queued: { label: 'Queued', classes: ['text-bg-info', 'text-dark'], icon: 'bi bi-hourglass-split' },
		failed: { label: 'Failed', classes: ['text-bg-danger'], icon: 'bi bi-exclamation-octagon' },
		pending: { label: 'Pending', classes: ['text-bg-secondary'], icon: 'bi bi-pause-circle' },
	};

	const STAGE_PROGRESS = {
		queued: { percent: 10, label: 'Queued…' },
		extract: { percent: 25, label: 'Extracting text…' },
		normalize: { percent: 40, label: 'Normalizing structure…' },
		llm_enrichment: { percent: 55, label: 'Enriching with LLM…' },
		chunk: { percent: 70, label: 'Chunking content…' },
		embed: { percent: 85, label: 'Embedding semantics…' },
		persist: { percent: 95, label: 'Persisting insights…' },
		completed: { percent: 100, label: 'Completed' },
		failed: { percent: 100, label: 'Failed' },
	};

	const elements = {
		processButton: document.getElementById('process-document-btn'),
		processSpinner: document.getElementById('process-spinner'),
		progressBar: document.getElementById('processing-progress'),
		progressBarInner: document.querySelector('#processing-progress .progress-bar'),
		statusBadge: document.getElementById('document-status-badge'),
		statusMeta: document.getElementById('document-status-meta'),
		feedback: document.getElementById('processing-feedback'),
		chunksContainer: document.getElementById('document-chunks'),
		logsContainer: document.getElementById('processing-log'),
		summaryContainer: document.getElementById('document-summary'),
		markdownContainer: document.getElementById('document-markdown'),
		shaMeta: document.getElementById('document-summary-meta'),
		ttsScript: document.getElementById('tts-script-text'),
		audio: document.getElementById('document-audio'),
		audioSource: document.getElementById('document-audio-source'),
		audioDuration: document.getElementById('audio-duration'),
		audioStatus: document.getElementById('audio-status-text'),
		refreshAudioBtn: document.getElementById('refresh-audio-btn'),
		forceMenu: root.querySelector('[data-action="force-process"]'),
	};

	const escapeHtml = (value) => {
		if (value === undefined || value === null) return '';
		return String(value)
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;')
			.replace(/'/g, '&#39;');
	};

	const setButtonBusy = (busy) => {
		if (!elements.processButton || !elements.processSpinner) return;
		if (busy) {
			elements.processButton.setAttribute('disabled', 'disabled');
			elements.processSpinner.classList.remove('d-none');
		} else {
			elements.processButton.removeAttribute('disabled');
			elements.processSpinner.classList.add('d-none');
		}
	};

	const showFeedback = (message, tone = 'info', { persist = false } = {}) => {
		if (!elements.feedback) return;
		const iconMap = {
			success: 'bi bi-check-circle',
			info: 'bi bi-info-circle',
			warning: 'bi bi-exclamation-triangle',
			danger: 'bi bi-exclamation-octagon',
		};
		const toneClass = ['success', 'info', 'warning', 'danger'].includes(tone) ? tone : 'info';
		elements.feedback.innerHTML = `
			<div class="alert alert-${toneClass} d-flex align-items-center py-2 px-3 mb-3" role="alert">
				<i class="${iconMap[toneClass] || iconMap.info} me-2"></i>
				<div class="flex-grow-1">${escapeHtml(message)}</div>
				${persist ? '' : '<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>'}
			</div>
		`;
	};

	const clearFeedback = () => {
		if (elements.feedback) {
			elements.feedback.innerHTML = '';
		}
	};

	const setAudioBusy = (busy) => {
		if (!elements.refreshAudioBtn) return;
		if (busy) {
			elements.refreshAudioBtn.setAttribute('disabled', 'disabled');
			elements.refreshAudioBtn.dataset.originalLabel = elements.refreshAudioBtn.dataset.originalLabel || elements.refreshAudioBtn.innerHTML;
			elements.refreshAudioBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>Generating…';
		} else {
			elements.refreshAudioBtn.removeAttribute('disabled');
			const original = elements.refreshAudioBtn.dataset.originalLabel;
			if (original && elements.refreshAudioBtn.innerHTML.includes('spinner-border')) {
				elements.refreshAudioBtn.innerHTML = original;
			}
			delete elements.refreshAudioBtn.dataset.originalLabel;
		}
	};

	const updateStatusBadge = (status) => {
		if (!elements.statusBadge) return;
		const normalized = status || 'pending';
		const config = STATUS_CONFIG[normalized] || { label: normalized, classes: ['text-bg-secondary'], icon: 'bi bi-activity' };
		const classList = elements.statusBadge.classList;
		Object.values(STATUS_CONFIG).forEach((value) => {
			value.classes.forEach((cls) => classList.remove(cls));
		});
		config.classes.forEach((cls) => classList.add(cls));
		elements.statusBadge.innerHTML = `<i class="${config.icon} me-1"></i>${escapeHtml(config.label)}`;
	};

	const updateStatusMeta = ({ lastProcessed, contentSha } = {}) => {
		if (elements.statusMeta) {
			if (lastProcessed) {
				let display = lastProcessed;
				try {
					const parsed = new Date(lastProcessed);
					if (!Number.isNaN(parsed.getTime())) {
						display = `${parsed.toISOString().replace('T', ' ').split('.')[0]} UTC`;
					}
				} catch (error) {
					// keep original string if parsing fails
				}
				elements.statusMeta.textContent = `Last processed ${display}`;
			} else {
				elements.statusMeta.textContent = 'Not processed yet';
			}
		}
		if (elements.shaMeta && contentSha) {
			elements.shaMeta.innerHTML = `<i class="bi bi-hash me-1"></i>SHA: ${escapeHtml(contentSha)}`;
			root.dataset.contentSha = contentSha;
		}
	};

	const ensureProgressVisible = (visible) => {
		if (!elements.progressBar) return;
		elements.progressBar.classList.toggle('d-none', !visible);
	};

	const updateProgress = (stage, status) => {
		if (!elements.progressBarInner) return;
		const normalized = status === 'failed' ? 'failed' : stage;
		const config = STAGE_PROGRESS[normalized] || { percent: 15, label: 'Processing…' };
		elements.progressBarInner.style.width = `${config.percent}%`;
		elements.progressBarInner.setAttribute('aria-valuenow', String(config.percent));
		elements.progressBarInner.textContent = config.label;
		elements.progressBarInner.classList.remove('bg-success', 'bg-danger');
		if (status === 'failed') {
			elements.progressBarInner.classList.add('bg-danger');
		} else if (config.percent >= 100) {
			elements.progressBarInner.classList.add('bg-success');
		}
		ensureProgressVisible(true);
	};

	const renderChunks = (chunks) => {
		if (!elements.chunksContainer) return;
		if (!chunks || chunks.length === 0) {
			elements.chunksContainer.innerHTML = '<div class="text-body-secondary">No chunks generated yet.</div>';
			return;
		}
		const html = chunks
			.map((chunk, index) => {
				const order = chunk.metadata && chunk.metadata.order !== undefined ? chunk.metadata.order : index + 1;
				const headingList = Array.isArray(chunk.metadata && chunk.metadata.headings)
					? `<div class="text-body-secondary">Headings: ${chunk.metadata.headings.map(escapeHtml).join(', ')}</div>`
					: '';
				const preview = chunk.content && chunk.content.length > 400
					? `${escapeHtml(chunk.content.slice(0, 400))}…`
					: escapeHtml(chunk.content || '');
				return `
					<div class="list-group-item" data-chunk-id="${chunk.id}">
						<div class="d-flex justify-content-between align-items-center">
							<strong>Chunk ${index + 1}</strong>
							<span class="badge text-bg-light">Order ${escapeHtml(order)}</span>
						</div>
						<p class="mb-1 text-body">${preview}</p>
						${headingList}
					</div>
				`;
			})
			.join('');
		elements.chunksContainer.innerHTML = html;
	};

	const renderLogs = (logs) => {
		if (!elements.logsContainer) return;
		if (!logs || logs.length === 0) {
			elements.logsContainer.innerHTML = '<p class="text-body-secondary">Stage progression will appear after processing begins.</p>';
			return;
		}
		const badgeForStatus = (status) => {
			switch (status) {
				case 'completed':
					return 'text-bg-success';
				case 'processing':
					return 'text-bg-warning text-dark';
				case 'failed':
					return 'text-bg-danger';
				default:
					return 'text-bg-secondary';
			}
		};
		const rows = logs
			.map((log) => {
				const status = escapeHtml(log.status || 'pending');
				const stage = escapeHtml(log.stage || '—');
				const started = escapeHtml(log.startedAt || '—');
				const completed = escapeHtml(log.completedAt || '—');
				const note = log.error
					? `<span class="text-danger">${escapeHtml(log.error)}</span>`
					: `<span class="text-body-tertiary">processed ${escapeHtml(log.processedPages ?? '—')} / ${escapeHtml(log.totalPages ?? '—')} units</span>`;
				return `
					<tr>
						<td class="text-capitalize">${stage}</td>
						<td><span class="badge ${badgeForStatus(log.status)}">${status}</span></td>
						<td>${started}</td>
						<td>${completed}</td>
						<td>${note}</td>
					</tr>
				`;
			})
			.join('');
		elements.logsContainer.innerHTML = `
			<div class="table-responsive">
				<table class="table table-sm align-middle">
					<thead class="table-light">
						<tr>
							<th scope="col">Stage</th>
							<th scope="col">Status</th>
							<th scope="col">Started</th>
							<th scope="col">Completed</th>
							<th scope="col">Notes</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`;
	};

	const updateSummary = ({ summary, description, ttsScript }) => {
		if (elements.summaryContainer) {
			const summaryTextEl = document.getElementById('document-summary-text');
			const summaryFallbackEl = document.getElementById('document-summary-fallback');
			if (summary) {
				const safeSummary = escapeHtml(summary);
				if (summaryTextEl) {
					summaryTextEl.innerHTML = safeSummary;
				} else {
					const lead = document.createElement('p');
					lead.className = 'lead fs-6';
					lead.id = 'document-summary-text';
					lead.innerHTML = safeSummary;
					if (summaryFallbackEl) {
						elements.summaryContainer.replaceChild(lead, summaryFallbackEl);
					} else {
						elements.summaryContainer.insertAdjacentElement('afterbegin', lead);
					}
				}
				if (summaryFallbackEl) {
					summaryFallbackEl.remove();
				}
			} else {
				if (summaryTextEl) {
					summaryTextEl.remove();
				}
				if (!summaryFallbackEl) {
					const fallback = document.createElement('p');
					fallback.className = 'text-body-secondary';
					fallback.id = 'document-summary-fallback';
					fallback.textContent = 'No AI summary yet. Run processing to generate insights.';
					elements.summaryContainer.insertAdjacentElement('afterbegin', fallback);
				}
			}
		}
		const descriptionEl = document.getElementById('document-description-text');
		if (descriptionEl) {
			if (description) {
				descriptionEl.innerHTML = escapeHtml(description);
				descriptionEl.classList.add('text-body');
				descriptionEl.classList.remove('text-body-secondary');
			} else {
				descriptionEl.textContent = 'No description provided.';
				descriptionEl.classList.add('text-body-secondary');
				descriptionEl.classList.remove('text-body');
			}
		}
		if (elements.ttsScript) {
			if (ttsScript) {
				elements.ttsScript.textContent = ttsScript;
			} else {
				elements.ttsScript.textContent = '';
			}
		}
	};

	const updateMarkdown = (markdown) => {
		if (!elements.markdownContainer) return;
		if (!markdown) {
			elements.markdownContainer.innerHTML = '<p class="text-body-secondary">Markdown representation will be available after processing.</p>';
			return;
		}
		try {
			if (window.marked && typeof window.marked.parse === 'function') {
				elements.markdownContainer.innerHTML = window.marked.parse(markdown);
			} else {
				elements.markdownContainer.textContent = markdown;
			}
		} catch (error) {
			elements.markdownContainer.textContent = markdown;
		}
	};

	const updateAudioUI = ({ url, durationSeconds }) => {
		if (!elements.audio || !elements.audioSource || !elements.audioDuration || !elements.audioStatus) return;
		if (url) {
			elements.audio.removeAttribute('data-disabled');
			elements.audioSource.setAttribute('src', url);
			try {
				elements.audio.load();
			} catch (error) {
				// ignore load issues
			}
			elements.audioStatus.textContent = 'Latest narration ready for playback.';
			elements.audioDuration.textContent = `Duration: ${durationSeconds ? `${durationSeconds}s` : '—'}`;
			elements.refreshAudioBtn && (elements.refreshAudioBtn.innerHTML = '<i class="bi bi-broadcast me-1"></i>Regenerate audio');
			state.audioUrl = url;
			state.audioDuration = durationSeconds || null;
		} else {
			elements.audioSource.setAttribute('src', '');
			elements.audio.setAttribute('data-disabled', 'true');
			elements.audioStatus.textContent = 'Audio playback will be available once narration is generated.';
			elements.audioDuration.textContent = 'Duration: —';
			elements.refreshAudioBtn && (elements.refreshAudioBtn.innerHTML = '<i class="bi bi-broadcast me-1"></i>Generate audio');
			state.audioUrl = '';
			state.audioDuration = null;
		}
	};

	const fetchJSON = async (url, options = {}) => {
		const headers = options.headers ? { ...options.headers } : {};
		if (!headers['Accept']) headers['Accept'] = 'application/json';
		const response = await fetch(url, { credentials: 'same-origin', ...options, headers });
		let payload = null;
		try {
			payload = await response.json();
		} catch (error) {
			payload = null;
		}
		return { response, payload };
	};

	const refreshChunks = async () => {
		if (!state.chunksUrl) return;
		const { response, payload } = await fetchJSON(state.chunksUrl);
		if (!response.ok || !payload) {
			showFeedback('Unable to refresh document chunks.', 'warning');
			return;
		}
		renderChunks(payload.chunks || []);
	};

	const refreshLogs = async () => {
		if (!state.logsUrl) return;
		const { response, payload } = await fetchJSON(state.logsUrl);
		if (!response.ok || !payload) {
			showFeedback('Unable to refresh processing logs.', 'warning');
			return;
		}
		renderLogs(payload.logs || []);
	};

	const refreshDocumentDetails = async () => {
		if (!state.detailUrl) return;
		const { response, payload } = await fetchJSON(state.detailUrl);
		if (!response.ok || !payload) {
			showFeedback('Unable to refresh document details.', 'warning');
			return;
		}
		updateSummary({ summary: payload.summary, description: payload.description, ttsScript: payload.ttsScript });
		updateMarkdown(payload.markdown);
		updateStatusBadge(payload.status);
		updateStatusMeta({ lastProcessed: payload.lastProcessedAt, contentSha: payload.contentSha });
		if (payload.audio) {
			updateAudioUI({ url: payload.audio.url, durationSeconds: payload.audio.durationSeconds });
		} else {
			updateAudioUI({ url: '', durationSeconds: null });
		}
	};

	const triggerProcessing = async (force = false) => {
		if (!state.processUrl) return;
		clearFeedback();
		setButtonBusy(true);
		showFeedback(force ? 'Force reprocessing requested…' : 'Document processing requested…', 'info', { persist: true });
		try {
			const { response, payload } = await fetchJSON(state.processUrl, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
				body: JSON.stringify({ force }),
			});
			if (!response.ok) {
				const detail = (payload && (payload.error || payload.message)) || response.statusText;
				showFeedback(`Unable to start processing: ${detail}`, 'danger', { persist: true });
				setButtonBusy(false);
				return;
			}
			state.status = 'queued';
			updateStatusBadge('queued');
			updateProgress('queued', 'queued');
			ensureProgressVisible(true);
		} catch (error) {
			showFeedback(`Processing start failed: ${error.message || error}`, 'danger', { persist: true });
			setButtonBusy(false);
		}
	};

	const triggerAudioGeneration = async (force = false) => {
		if (!elements.refreshAudioBtn) return;
		if (!state.generateAudioUrl) {
			// Fallback to force processing if dedicated endpoint unavailable
			triggerProcessing(true);
			return;
		}
		clearFeedback();
		const previousAudio = { url: state.audioUrl, durationSeconds: state.audioDuration };
		setAudioBusy(true);
		showFeedback(force ? 'Regenerating narration audio…' : 'Generating narration audio…', 'info', { persist: true });
		try {
			const { response, payload } = await fetchJSON(state.generateAudioUrl, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
				body: JSON.stringify({ force }),
			});
			if (!response.ok || !payload) {
				const detail = (payload && (payload.error || payload.message)) || response.statusText;
				throw new Error(detail);
			}
			if (payload.audio) {
				updateAudioUI({ url: payload.audio.url, durationSeconds: payload.audio.durationSeconds });
				showFeedback('Narration audio ready for playback.', 'success');
			} else {
				updateAudioUI({ url: '', durationSeconds: null });
				showFeedback('Narration script saved, but no audio file was generated.', 'warning');
			}
			await refreshDocumentDetails();
		} catch (error) {
			updateAudioUI(previousAudio);
			showFeedback(`Audio generation failed: ${error.message || error}`, 'danger', { persist: true });
		} finally {
			setAudioBusy(false);
		}
	};

	const handleProgressEvent = (payload) => {
		if (!payload || payload.documentId !== state.documentId) return;
		state.status = payload.status || 'processing';
		updateStatusBadge('processing');
		updateProgress(payload.stage, payload.status);
	};

	const handleDoneEvent = async (payload) => {
		if (!payload || payload.documentId !== state.documentId) return;
		state.status = 'completed';
		setButtonBusy(false);
		updateStatusBadge('completed');
		updateProgress('completed', 'completed');
		updateStatusMeta({ lastProcessed: new Date().toISOString().replace('T', ' ').split('.')[0] + ' UTC', contentSha: payload.contentSha });
		showFeedback('Document processing completed.', 'success');
		await Promise.all([refreshDocumentDetails(), refreshChunks(), refreshLogs()]);
	};

	const handleFailedEvent = (payload) => {
		if (!payload || payload.documentId !== state.documentId) return;
		state.status = 'failed';
		setButtonBusy(false);
		updateStatusBadge('failed');
		updateProgress('failed', 'failed');
		const message = payload.error ? `Processing failed: ${payload.error}` : 'Document processing failed.';
		showFeedback(message, 'danger', { persist: true });
	};

	const ensureSocket = () => {
		if (window.socket && typeof window.socket.on === 'function') {
			return window.socket;
		}
		if (typeof window.io === 'function') {
			window.socket = window.io();
			return window.socket;
		}
		console.warn('[DocumentProcessing] Socket.IO client not available.');
		return null;
	};

	const attachSocketListeners = () => {
		const socket = ensureSocket();
		if (!socket || state.socketAttached) return;
		socket.off?.('doc_processing_progress', handleProgressEvent);
		socket.off?.('doc_processing_done', handleDoneEvent);
		socket.off?.('doc_processing_failed', handleFailedEvent);
		socket.on('doc_processing_progress', handleProgressEvent);
		socket.on('doc_processing_done', handleDoneEvent);
		socket.on('doc_processing_failed', handleFailedEvent);
		const subscribe = () => {
			try {
				socket.emit('document_subscribe', { documentId: state.documentId });
			} catch (error) {
				console.warn('[DocumentProcessing] Failed to emit document_subscribe', error);
			}
		};
		if (socket.connected) {
			subscribe();
		} else {
			socket.once('connect', subscribe);
		}
		state.socketAttached = true;
	};

	const attachEventListeners = () => {
		if (elements.processButton) {
			elements.processButton.addEventListener('click', (event) => {
				event.preventDefault();
				triggerProcessing(false);
			});
		}
		if (elements.forceMenu) {
			elements.forceMenu.addEventListener('click', (event) => {
				event.preventDefault();
				triggerProcessing(true);
			});
		}
		if (elements.refreshAudioBtn) {
			elements.refreshAudioBtn.addEventListener('click', (event) => {
				event.preventDefault();
				const shouldForce = Boolean(state.audioUrl);
				triggerAudioGeneration(shouldForce);
			});
		}
	};

	// Initial UI state sync
	updateStatusBadge(state.status);
	updateStatusMeta({ lastProcessed: state.lastProcessed, contentSha: root.dataset.contentSha });
	if (state.audioUrl) {
		updateAudioUI({ url: state.audioUrl, durationSeconds: state.audioDuration });
	}
	attachSocketListeners();
	attachEventListeners();
	refreshLogs();

	if (state.status === 'processing' || state.status === 'queued') {
		ensureProgressVisible(true);
	} else {
		ensureProgressVisible(false);
	}
})();

// app/static/js/document_processing.js