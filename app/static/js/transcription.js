/*
 * app/static/js/transcription.js
 * Scaffold for streaming mic PCM16 chunks to server via Socket.IO for STT.
 * Non-breaking placeholder; real AudioWorklet + server integration to follow.
 */

STT_DEBUG_PANEL = false; // set to true to show debug panel

(() => {
	const CHUNK_MS = 40; // target frame size
	const SAMPLE_RATE = 16000; // required by AWS Transcribe config
	const participantDeleteEnabledGlobal = (typeof window !== 'undefined' && Object.prototype.hasOwnProperty.call(window, 'participantDeleteEnabled'))
		? !!window.participantDeleteEnabled
		: true;
	
	// Polish button and toggle handlers
	document.addEventListener('click', async (e) => {
	  const btn = e.target.closest ? e.target.closest('.polish-btn') : null;
	  if (!btn) return;
	  const row = btn.closest('.transcript-final');
	  if (!row) return;
	  const workshopId = row.dataset.workshopId;
	  const transcriptId = row.dataset.transcriptId;
	  if (!workshopId || !transcriptId) return;
	  const spinner = row.querySelector('.spinner-border');
	  const toggles = row.querySelector('.toggle-group');
	  const txtProcessed = row.querySelector('.text-processed');
	  btn.disabled = true; if (spinner) spinner.classList.remove('d-none');
	  try {
	    const res = await fetch(`/api/workshops/${workshopId}/transcripts/${transcriptId}/polish`, {
	      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }, body: JSON.stringify({})
	    });
	    const data = await res.json();
	    if (!res.ok) throw new Error(data.error || 'Failed');
	    if (txtProcessed) txtProcessed.textContent = data.processed_text || '';
	    if (toggles) toggles.classList.remove('d-none');
	    const showProc = row.querySelector('.show-processed');
	    if (showProc) showProc.click();
	    btn.textContent = 'Re-polish'; btn.disabled = false;
	  } catch (err) {
	    console.error(err); alert('Could not polish this line. Please try again.'); btn.disabled = false;
	  } finally { if (spinner) spinner.classList.add('d-none'); }
	});
	
	document.addEventListener('click', (e) => {
	  const el = e.target;
	  if (!el || !el.classList) return;
	  if (!(el.classList.contains('show-original') || el.classList.contains('show-processed'))) return;
	  const group = el.closest('.toggle-group'); const row = el.closest('.transcript-final');
	  if (!group || !row) return;
	  const orig = row.querySelector('.text-original');
	  const proc = row.querySelector('.text-processed');
	  group.querySelectorAll('button').forEach(b => b.classList.remove('active'));
	  el.classList.add('active');
	  if (el.classList.contains('show-original')) { orig?.classList.remove('d-none'); proc?.classList.add('d-none'); }
	  else { orig?.classList.add('d-none'); proc?.classList.remove('d-none'); }
	});
	document.addEventListener('click', async (e) => {
		const btn = e.target.closest ? e.target.closest('.delete-transcript-btn') : null;
		if (!btn) return;
		const row = btn.closest('.transcript-final');
		if (!row) return;
		const transcriptId = row.dataset.transcriptId;
		const workshopId = row.dataset.workshopId || (window.workshopId ? String(window.workshopId) : null);
		if (!transcriptId || !workshopId) return;
		const entryType = row.dataset.entryType || '';
		const rowUserId = row.dataset.userId ? Number(row.dataset.userId) : null;
		const currentUserId = (typeof window !== 'undefined' && typeof window.userId !== 'undefined') ? Number(window.userId) : null;
		const isOrganizerUser = (typeof window !== 'undefined' && window.isOrganizer === true);
		const isOwner = rowUserId !== null && currentUserId !== null && rowUserId === currentUserId;
		const participantDeleteEnabled = participantDeleteEnabledGlobal;
		const ownerCanDelete = participantDeleteEnabled && entryType !== 'facilitator' && isOwner;
		if (!(isOrganizerUser || ownerCanDelete)) return;
		if (!window.confirm('Delete this transcript line? This cannot be undone.')) return;
		const originalHtml = btn.innerHTML;
		let removed = false;
		btn.disabled = true;
		btn.classList.add('disabled');
		btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Deleting…';
		try {
			const res = await fetch(`/api/workshops/${workshopId}/transcripts/${transcriptId}`, {
				method: 'DELETE',
				headers: { 'X-Requested-With': 'XMLHttpRequest' }
			});
			const data = await res.json().catch(() => ({}));
			if (!res.ok) throw new Error((data && data.error) || 'Failed');
			row.remove();
			removed = true;
			if (window.__loadedTranscriptIds instanceof Set) {
				window.__loadedTranscriptIds.delete(Number(transcriptId));
			}
			ensureTranscriptPlaceholder();
			updateTranscriptCountLabel();
		} catch (err) {
			console.error(err);
			window.alert('Could not delete this transcript line. Please try again.');
		} finally {
			if (!removed) {
				btn.disabled = false;
				btn.classList.remove('disabled');
				btn.innerHTML = originalHtml;
			}
		}
	});
	let mediaStream = null;
	let audioContext = null;
	let processor = null;
	let seq = 0;
	let active = false;
	let wasActiveBeforeFacilitator = false; // track STT state to auto-resume after facilitator playback
	let workletNode = null; // NEW: reference to AudioWorkletNode
	let usingWorklet = false;
	let ready = false; // provider ready flag
	let selectedProvider = null; // NEW: user-selected provider override
	// De-dup + throttle state
	const lastPartialByUser = new Map();
	let pendingPartialUpdate = null; // { userId, text }
	let lastPartialFlushTs = 0;
	const PARTIAL_FLUSH_INTERVAL = 90; // ms (target ~11 fps)
	let partialAnimationScheduled = false;
	let showPartials = true; // verbosity toggle
	// Auto-scroll state for transcript panel
	let autoScrollEnabled = true;
	const SCROLL_LOCK_THRESHOLD = 24; // px from bottom treated as "at bottom"
	// Waveform / level meter state
	let analyser = null; let waveformRAF = null; let waveformCanvas = null; let waveformCtx = null;
	// Silence detection (frontend heuristic)
	let silenceStartTs = null; const SILENCE_THRESHOLD = 0.012; // RMS approx
	const SILENCE_SECS_TO_PROMPT = 3.0; // show prompt after 3s
	let silencePromptShown = false;
	// WPM metric
	let wordsFinal = 0; let wpmTimer = null; let sessionStartMs = null;

	// UI utility references (lazy resolved)
	function q(id){ return document.getElementById(id); }
	function setTranscriptStatus(txt){ const el = q('transcript-status'); if (el) el.textContent = txt; }
	function getRecordingBadgeEls(){
		// Support multiple badge placements (inline footer + banner)
		return [ 'transcript-recording-badge-inline', 'transcript-recording-badge', 'transcript-recording-badge-banner' ]
			.map(id => q(id)).filter(Boolean);
	}
	function updateRecordingBadge(on){
		getRecordingBadgeEls().forEach(el=>{
			if (on) el.classList.remove('d-none'); else el.classList.add('d-none');
		});
		if (on) setTranscriptStatus('CC: Starting…'); else setTranscriptStatus('CC: Off');
	}

	function log(...a) { console.log('[Transcription]', ...a); }

	function ensureSocket() {
		if (!window.io) {
			console.warn('[Transcription] Socket.IO not available.');
			return null;
		}
		// Reuse existing global page socket if present to receive room broadcasts
		if (!window.transcriptionSocket) {
			if (window.socket) {
				window.transcriptionSocket = window.socket; // attach listeners to primary connection
				console.log('[Transcription] Reusing existing global socket connection (SID may already be in workshop room).');
			} else {
				window.transcriptionSocket = window.io();
				console.log('[Transcription] Created dedicated transcription socket connection.');
			}
		}
		return window.transcriptionSocket;
	}

	async function startLocalTranscription() {
		if (active) {
			console.log('[Transcription] Already active, ignoring start request');
			return;
		}
		active = true;
		updateRecordingBadge(true);
		// Reset session metrics
		wordsFinal = 0; sessionStartMs = Date.now();
		if (wpmTimer) { clearInterval(wpmTimer); wpmTimer = null; }
		wpmTimer = setInterval(()=>{
			if (!active) return;
			const wpmEl = document.getElementById('stt-debug-wpm');
			if (wpmEl && sessionStartMs) {
				const mins = (Date.now() - sessionStartMs)/60000;
				const wpm = mins > 0 ? Math.round(wordsFinal / mins) : 0;
				wpmEl.textContent = wpm.toString();
			}
		}, 1000);
		// Record start timestamp for premature stop diagnostics
		window.__sttStartEpoch = Date.now();
		window.__sttStopInvocations = window.__sttStopInvocations || [];
		const socket = ensureSocket();
		if (!socket) return;
		log('Starting local transcription scaffold');
		console.log('[Transcription] Setting active=true, sending stt_start');
		
		// CRITICAL: Ensure we're in the workshop room to receive events.
		// Some templates define workshopId / userId as block‑scoped const (not on window), so fall back.
		const resolvedWorkshopId = window.workshopId || (typeof workshopId !== 'undefined' ? workshopId : null);
		const resolvedUserId = window.userId || (typeof userId !== 'undefined' ? userId : null);
		if (resolvedWorkshopId) {
			// Persist back to window for later emissions
			if (!window.workshopId) window.workshopId = resolvedWorkshopId;
			if (!window.userId && resolvedUserId) window.userId = resolvedUserId;
			const roomName = `workshop_room_${resolvedWorkshopId}`;
			const joinPayload = { room: roomName, workshop_id: resolvedWorkshopId, user_id: resolvedUserId };
			log(`Joining workshop room '${roomName}' for transcription events (user_id=${joinPayload.user_id})`);
			console.log('[Transcription] Emitting join_room payload:', joinPayload);
			socket.emit('join_room', joinPayload);
		} else {
			console.warn('[Transcription] No workshopId found (neither window.workshopId nor global workshopId). STT events will not be received.');
		}
		
		try {
			mediaStream = mediaStream || await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
			
			// Create AudioContext without forcing sample rate - use browser default
			if (!audioContext) {
				audioContext = new (window.AudioContext || window.webkitAudioContext)();
				log('AudioContext created with sampleRate:', audioContext.sampleRate);
				
				// If the AudioContext sample rate differs from our target, we'll handle resampling in the worklet
				if (audioContext.sampleRate !== SAMPLE_RATE) {
					log(`AudioContext sampleRate (${audioContext.sampleRate}) differs from target (${SAMPLE_RATE}), will resample in worklet`);
				}
			}
			
			const source = audioContext.createMediaStreamSource(mediaStream);

			// Setup analyser for waveform + silence detection
			if (!analyser) {
				analyser = audioContext.createAnalyser();
				analyser.fftSize = 2048;
				analyser.smoothingTimeConstant = 0.8;
			}
			source.connect(analyser);
			startWaveformLoop();

			// Attempt AudioWorklet path first
			if (audioContext.audioWorklet) {
				try {
					if (!window.__transcriptionWorkletLoaded) {
							await audioContext.audioWorklet.addModule('/static/js/transcription-worklet.js?v=2');
						window.__transcriptionWorkletLoaded = true;
						console.log('[Transcription] AudioWorklet module loaded');
					}
					workletNode = new AudioWorkletNode(audioContext, 'transcription-pcm-processor', {
						processorOptions: { 
							sampleRate: SAMPLE_RATE, // target rate (16000)
							frameSize: 640 // 40ms at 16kHz
						}
					});
					usingWorklet = true;
					workletNode.port.onmessage = (ev) => {
						if (!active || !ready) return; // don't stream until provider ready
						const msg = ev.data || {};
						if (msg.type === 'chunk' && msg.pcm) {
							// Convert transferred ArrayBuffer (PCM16) to base64 on main thread
							const b64 = pcmBytesToB64(new Uint8Array(msg.pcm));
							socket.emit('stt_audio_chunk', {
								workshop_id: window.workshopId || (typeof workshopId !== 'undefined' ? workshopId : null),
								user_id: window.userId || (typeof userId !== 'undefined' ? userId : null),
								seq: msg.seq,
								codec: 'pcm16le',
								sampleRate: msg.sampleRate || SAMPLE_RATE,
								payloadBase64: b64
							});
						}
					};
					source.connect(workletNode);
					// Worklet nodes must connect somewhere; connect to destination at 0 gain or directly skip if allowed
					try { workletNode.connect(audioContext.destination); } catch {}
					console.log('[Transcription] Using AudioWorkletNode streaming path');
				} catch (ew) {
					console.warn('[Transcription] AudioWorklet failed, falling back to ScriptProcessor:', ew);
					usingWorklet = false;
				}
			}

			if (!usingWorklet) {
				// Fallback original ScriptProcessor approach
				const bufferSize = 1024;
				processor = audioContext.createScriptProcessor(bufferSize, 1, 1);
				processor.onaudioprocess = (e) => {
					if (!active || !ready) return;
					const input = e.inputBuffer.getChannelData(0);
					const pcm16 = new Int16Array(input.length);
					for (let i = 0; i < input.length; i++) {
						const s = Math.max(-1, Math.min(1, input[i]));
						pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
					}
					const b64 = pcmBytesToB64(new Uint8Array(pcm16.buffer));
					socket.emit('stt_audio_chunk', {
						workshop_id: window.workshopId || null,
						user_id: window.userId || null,
						seq: seq++,
						codec: 'pcm16le',
						sampleRate: SAMPLE_RATE,
						payloadBase64: b64
					});
				};
				source.connect(processor);
				processor.connect(audioContext.destination);
				console.log('[Transcription] Using ScriptProcessor fallback path');
			}

			const startPayload = { 
				workshop_id: window.workshopId || (typeof workshopId !== 'undefined' ? workshopId : null), 
				user_id: window.userId || (typeof userId !== 'undefined' ? userId : null), 
				language: 'en-US', 
				sampleRate: SAMPLE_RATE 
			};
			if (selectedProvider) {
				startPayload.provider = selectedProvider;
			}
			console.log('[Transcription] Sending stt_start event:', startPayload);
			socket.emit('stt_start', startPayload);
			// Fallback: if stt_ready not received in 1200ms, assume ready (local Vosk) so we don't block streaming
			(function readinessFallback(){
				const startedAt = Date.now();
				setTimeout(()=>{
					if (!ready && active) {
						ready = true;
						console.warn('[Transcription] stt_ready not received; forcing ready (fallback)');
						const statusEl = document.getElementById('stt-debug-status');
						if (statusEl) statusEl.textContent = 'ready?';
					}
				}, 1200);
			})();

			// If we don't see any partials within 5s after ready => likely socket mismatch; surface a warning.
			setTimeout(()=>{
				if (active && ready && (typeof window.__sttPartialCount === 'number' ? window.__sttPartialCount : 0) === 0) {
					console.warn('[Transcription][DIAG] No stt_partial events received within 5s of start. Likely socket mismatch or room join failure.');
					console.warn('[Transcription][DIAG] Inspect window.__sttStopInvocations and network tab -> WS frames for emitted events.');
				}
			}, 5000);
		} catch (e) {
			console.error('[Transcription] Failed to start:', e);
			active = false;
		}
	}

	function stopLocalTranscription() {
		const now = Date.now();
		const sinceStart = typeof window.__sttStartEpoch === 'number' ? (now - window.__sttStartEpoch) : null;
		// Capture stack for diagnostics
		const trace = (new Error('stopLocalTranscription trace')).stack;
		window.__sttStopInvocations.push({ t: now, sinceStart, trace });
		// Suppress unintended auto-stop occurring within first 1500ms of start unless explicitly forced
		if ((!active) && sinceStart !== null && sinceStart < 1500) {
			console.log('[Transcription][DIAG] stopLocalTranscription called while already inactive within first 1.5s; ignoring. sinceStart=', sinceStart);
			return;
		}
		if (sinceStart !== null && sinceStart < 1500) {
			console.warn(`[Transcription][DIAG] Suppressing premature stop (${sinceStart}ms after start). Trace below:`);
			console.warn(trace);
			return; // do not actually stop yet
		}
		if (!active) {
			console.log('[Transcription] Already inactive, ignoring stop request');
			return;
		}
		active = false;
		console.log('[Transcription] Setting active=false, sending stt_stop (sinceStart='+sinceStart+'ms)');
		const socket = ensureSocket();
		if (socket) {
			const stopPayload = { workshop_id: window.workshopId || null, user_id: window.userId || null };
			console.log('[Transcription] Sending stt_stop event:', stopPayload);
			socket.emit('stt_stop', stopPayload);
		}
			if (workletNode) {
			try { workletNode.disconnect(); } catch {}
			workletNode = null;
		}
		if (processor) { try { processor.disconnect(); } catch {}; processor = null; }
			if (waveformRAF) { cancelAnimationFrame(waveformRAF); waveformRAF = null; }
			if (wpmTimer) { clearInterval(wpmTimer); wpmTimer = null; }
			analyser = null;
			waveformCanvas = null; waveformCtx = null;
		// Keep mediaStream + audioContext for reuse; optionally suspend context
		seq = 0;
		ready = false;
			lastPartialByUser.clear(); pendingPartialUpdate = null; partialAnimationScheduled = false;
			silenceStartTs = null; silencePromptShown = false;
		log('Stopped local transcription');
		updateRecordingBadge(false);
	}

	// Expose helper API for facilitator playback to pause/resume STT safely
	function isTranscriptionActive(){ return !!active; }
	function pauseTranscriptionForFacilitator(){
		try {
			if (active) { wasActiveBeforeFacilitator = true; stopLocalTranscription(); }
			else { wasActiveBeforeFacilitator = false; }
		} catch(_) {}
	}
	function resumeTranscriptionAfterFacilitator(){
		try {
			if (wasActiveBeforeFacilitator && !active) { startLocalTranscription(); }
		} catch(_) {}
		finally { wasActiveBeforeFacilitator = false; }
	}

	// --- Standalone Transcription Mode Support ---
	function maybeInitStandaloneTranscription(){
		// Conditions: transcription enabled flag present AND either no conference flags object OR conferenceActive === false
		const flags = (window.conferenceFlags || {});
		const transcriptionEnabled = !!flags.transcriptionEnabled;
		const conferenceActive = (typeof flags.conferenceActive === 'boolean') ? flags.conferenceActive : true;
		if (!transcriptionEnabled) return; // nothing to do
		// If conference is disabled, auto-start (respect persisted CC preference if any?)
		if (!conferenceActive) {
			// Attach toggle button if present
			const toggleBtn = document.getElementById('standalone-cc-toggle');
			if (toggleBtn) {
				const updateLabel = () => { toggleBtn.innerHTML = active ? '<i class="bi bi-stop-circle"></i> Stop Captions' : '<i class="bi bi-cc-square"></i> Start Captions'; };
				updateLabel();
				toggleBtn.addEventListener('click', () => { if (active) { stopLocalTranscription(); } else { startLocalTranscription(); } updateLabel(); });
			}
			// Auto-start preference logic
			const PREF_KEY = 'brainstormx.standalone_stt_autostart';
			const prefCheckbox = document.getElementById('stt-auto-start-pref');
			if (prefCheckbox) {
				// Load saved preference
				let saved = null;
				try { saved = localStorage.getItem(PREF_KEY); } catch {}
				const shouldAuto = saved === '1';
				prefCheckbox.checked = shouldAuto;
				prefCheckbox.addEventListener('change', ()=>{
					try { localStorage.setItem(PREF_KEY, prefCheckbox.checked ? '1' : '0'); } catch {}
				});
				// Only auto-start if preference is enabled
				if (shouldAuto) startLocalTranscription();
			} else {
				// Fallback to always auto-start if checkbox not present
				startLocalTranscription();
			}
		}
	}

	document.addEventListener('DOMContentLoaded', () => { try { maybeInitStandaloneTranscription(); } catch(e){ console.warn('[Transcription] Standalone init failed', e); } });

	// Utility: efficient base64 conversion for Uint8Array
	function pcmBytesToB64(bytes){
		let binary = '';
		const CHUNK = 0x8000;
		for (let i=0;i<bytes.length;i+=CHUNK){
			binary += String.fromCharCode.apply(null, bytes.subarray(i,i+CHUNK));
		}
		return btoa(binary);
	}

	// Transcript UI helpers
	function isAtBottom(el){ return (el.scrollHeight - el.scrollTop - el.clientHeight) < SCROLL_LOCK_THRESHOLD; }
	function maybeScrollToBottom(){
		const list = document.getElementById('transcript-list');
		if (!list) return;
		const scrollBtn = document.getElementById('transcript-scroll-bottom');
		if (autoScrollEnabled) {
			// Keep pinned to bottom while user is at/near the bottom
			list.scrollTop = list.scrollHeight;
			if (scrollBtn) scrollBtn.classList.add('d-none');
		} else {
			// User scrolled away; show affordance
			if (scrollBtn) scrollBtn.classList.remove('d-none');
		}
	}
	function updateTranscriptCountLabel(){
		const statusEl = document.getElementById('transcript-status');
		const list = document.getElementById('transcript-list');
		if (!statusEl || !list) return;
		const finals = list.querySelectorAll('.transcript-final').length;
		statusEl.textContent = finals > 0 ? `CC: ${finals} line${finals === 1 ? '' : 's'}` : 'CC: Off';
	}
	function ensureTranscriptPlaceholder(){
		const list = document.getElementById('transcript-list');
		if (!list) return;
		const hasEntries = list.querySelector('.transcript-final') || list.querySelector('.transcript-partial');
		if (hasEntries) return;
		if (document.getElementById('transcript-empty-placeholder')) return;
		const placeholder = document.createElement('p');
		placeholder.id = 'transcript-empty-placeholder';
		placeholder.className = 'text-muted fst-italic mb-0';
		placeholder.textContent = 'Live transcripts will appear here...';
		list.appendChild(placeholder);
	}
	function renderPartial(userId, text) {
		const list = document.getElementById('transcript-list');
		if (!list) return;
		const id = `partial-${userId}`;
		let row = document.getElementById(id);
		if (!row) {
			row = document.createElement('div');
			row.id = id;
			row.className = 'transcript-partial small text-muted';
			list.appendChild(row);
		}
		row.textContent = text;
		const placeholder = document.getElementById('transcript-empty-placeholder');
		if (placeholder) placeholder.remove();
		// Auto-scroll only if user is at the bottom
		maybeScrollToBottom();
	}

	function appendFinal(payload) {
		const list = document.getElementById('transcript-list');
		if (!list) return;
		const placeholder = document.getElementById('transcript-empty-placeholder');
		if (placeholder) placeholder.remove();
		// If we already rendered this persisted transcript_id, update existing row text and return to avoid duplicates
		if (payload.transcript_id) {
			const existing = list.querySelector(`.transcript-final[data-transcript-id="${payload.transcript_id}"]`);
			if (existing) {
				const txtEl = existing.querySelector('.text-original');
				if (txtEl) txtEl.textContent = (payload.text || '');
				return;
			}
		}
		// Remove partial for this user if exists
		try {
			const pid = `partial-${payload.user_id}`;
			const partial = document.getElementById(pid);
			if (partial) partial.remove();
			// If this is a facilitator final, also clear any lingering facilitator-tagged partials (safety)
			if (payload.entry_type === 'facilitator') {
				[...document.querySelectorAll('.transcript-partial')].forEach(el=>{
					// No guaranteed class on partials; rely on lack of transcript_id + facilitator entry_type to cleanly reset the panel
					el.remove();
				});
				lastPartialByUser.clear();
			}
		} catch(_) {}
		const wrapper = document.createElement('div');
		const isFac = (payload.entry_type === 'facilitator');
		wrapper.className = 'transcript-final mb-2' + (isFac ? ' facilitator' : '');
		if (payload.transcript_id) wrapper.dataset.transcriptId = String(payload.transcript_id);
		if (payload.workshop_id) wrapper.dataset.workshopId = String(payload.workshop_id);
		if (typeof payload.user_id !== 'undefined' && payload.user_id !== null) {
			wrapper.dataset.userId = String(payload.user_id);
		}
		wrapper.dataset.entryType = payload.entry_type || (isFac ? 'facilitator' : 'human');
		const ts = payload.startTs ? new Date(payload.startTs) : new Date();
		const timeStr = ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
		const speakerName = isFac ? 'AI Facilitator' : (((payload.first_name || '') + ' ' + (payload.last_name || '')).trim() || 'Speaker');
		const safeText = (payload.text || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
		const windowObj = (typeof window !== 'undefined') ? window : {};
		const currentUserId = typeof windowObj.userId !== 'undefined' ? Number(windowObj.userId) : null;
		const payloadUserId = typeof payload.user_id !== 'undefined' && payload.user_id !== null ? Number(payload.user_id) : null;
		const isOrganizerUser = windowObj.isOrganizer === true;
		const participantDeleteEnabled = participantDeleteEnabledGlobal;
		const ownerCanDelete = participantDeleteEnabled && !isFac && payloadUserId !== null && currentUserId !== null && payloadUserId === currentUserId;
		const canDelete = !!payload.transcript_id && (isOrganizerUser || ownerCanDelete);
		wrapper.innerHTML = `
			<div class="d-flex align-items-start gap-2">
				<div class="flex-grow-1">
					<div class="d-flex justify-content-between align-items-center">
						<strong class="small">${isFac ? '<i class="bi bi-robot me-1 text-primary"></i>' : ''}${speakerName}</strong>
						<span class="text-muted small">${timeStr}</span>
					</div>
					<div class="small transcript-text">
						<span class="text-original">${safeText}</span>
						<span class="text-processed d-none"></span>
					</div>
					<div class="d-flex align-items-center gap-2 mt-1">
						<button class="btn btn-sm btn-outline-secondary polish-btn"><i class="bi bi-magic"></i> Polish</button>
						<div class="btn-group btn-group-sm toggle-group d-none">
							<button class="btn btn-outline-secondary show-original active">Original</button>
							<button class="btn btn-outline-secondary show-processed">Corrected</button>
						</div>
						<span class="spinner-border spinner-border-sm text-secondary ms-1 d-none" role="status"></span>
						${canDelete ? '<button type="button" class="btn btn-sm btn-outline-danger delete-transcript-btn ms-auto"><i class="bi bi-trash"></i> Delete</button>' : ''}
					</div>
				</div>
			</div>`;
		list.appendChild(wrapper);
		if (payload.transcript_id) {
			window.__loadedTranscriptIds = window.__loadedTranscriptIds || new Set();
			window.__loadedTranscriptIds.add(Number(payload.transcript_id));
		}
		// Disable polish for facilitator/non-persisted entries
		try {
			if (isFac || !payload.transcript_id) {
				const pb = wrapper.querySelector('.polish-btn');
				if (pb) { pb.disabled = true; pb.title = 'Not editable'; }
			}
		} catch(_) {}
		// Highlight newest final & remove highlight from previous
		try {
			[...list.querySelectorAll('.transcript-final.highlighting')].forEach(el=>el.classList.remove('highlighting'));
			wrapper.classList.add('highlighting');
			setTimeout(()=>{ wrapper.classList.remove('highlighting'); }, 4000);
		} catch(e){ /* noop */ }
		// Auto-scroll only if user is at the bottom; otherwise show scroll affordance
		maybeScrollToBottom();
		// If this is first meaningful transcript after starting, move status to On
		if (active) setTranscriptStatus('CC: On');
		else updateTranscriptCountLabel();
	}

	// Utility to attach transcription listeners to a given socket exactly once
	function attachTranscriptionListeners(sock){
		if (!sock || sock.__transcriptionListenersAttached) return;
		sock.__transcriptionListenersAttached = true;
		const socket = sock; // local alias
		// DEBUG: Log workshop context
		console.log('[Transcription] Initialized with workshopId:', window.workshopId, 'userId:', window.userId, '(socket SID:', socket.id, ')');

		const debugState = { provider:null, model:null, partials:0, finals:0, started:false };
		function setText(id, value){ const el = document.getElementById(id); if (el) el.textContent = value; }
		function updateCounters(){ setText('stt-debug-partials', debugState.partials); setText('stt-debug-finals', debugState.finals); }
		function setStatus(s){ setText('stt-debug-status', s); }

		socket.on('stt_ready', (d)=>{
			if (!d || d.workshop_id !== window.workshopId) return;
			ready = true; // allow audio frames to flow
			debugState.provider = d.provider || '-';
			debugState.model = d.model_path || '-';
			debugState.partials = 0; debugState.finals = 0;
			updateCounters();
			setText('stt-debug-provider', debugState.provider);
			setText('stt-debug-model', debugState.model);
			setStatus('ready');
			console.log('[Transcription] Received stt_ready (via socket '+socket.id+'):', d);
			if (active) setTranscriptStatus('CC: On');
		});
		socket.on('stt_partial', (d)=>{
			if (!d || d.workshop_id !== window.workshopId) return;
			if (!showPartials) return; // verbosity filter
			const text = d.text || '';
			// If facilitator sent an empty partial, clear any existing partial rows
			if ((d.entry_type === 'facilitator') && text.length === 0) {
				try {
					[...document.querySelectorAll('.transcript-partial')].forEach(el=>el.remove());
					lastPartialByUser.clear();
				} catch(_) {}
				return;
			}
			// De-dup: ignore if unchanged for that user
			const prev = lastPartialByUser.get(d.user_id);
			if (prev === text) return;
			lastPartialByUser.set(d.user_id, text);
			debugState.partials++; updateCounters(); setStatus('streaming');
			window.__sttPartialCount = (window.__sttPartialCount||0)+1;
			// Throttle UI updates
			pendingPartialUpdate = { userId: d.user_id, text };
			if (!partialAnimationScheduled) {
				partialAnimationScheduled = true;
				requestAnimationFrame(partialFlushLoop);
			}
		});
		socket.on('transcript_final', (d)=>{
			if (!d || d.workshop_id !== window.workshopId) return;
			debugState.finals++; updateCounters(); setStatus('streaming');
			console.log('[Transcription] Received transcript_final (via socket '+socket.id+'):', d.text);
			// Update WPM stats
			if (d.text) {
				const wc = d.text.trim().split(/\s+/).filter(Boolean).length;
				wordsFinal += wc;
			}
			appendFinal(d);
		});
		socket.on('transcript_corrected', (msg)=>{
			if (!msg || msg.workshop_id !== window.workshopId) return;
			const row = document.querySelector(`.transcript-final[data-transcript-id="${msg.transcript_id}"]`);
			if (!row) return;
			const txtProcessed = row.querySelector('.text-processed');
			const toggles = row.querySelector('.toggle-group');
			if (txtProcessed) txtProcessed.textContent = msg.processed_text || '';
			if (toggles) toggles.classList.remove('d-none');
		});
		socket.on('transcript_deleted', (msg)=>{
			if (!msg || msg.workshop_id !== window.workshopId) return;
			const row = document.querySelector(`.transcript-final[data-transcript-id="${msg.transcript_id}"]`);
			if (row) row.remove();
			if (window.__loadedTranscriptIds instanceof Set) {
				window.__loadedTranscriptIds.delete(Number(msg.transcript_id));
			}
			ensureTranscriptPlaceholder();
			updateTranscriptCountLabel();
		});
		socket.on('stt_stopped', (d)=>{
			if (!d || d.workshop_id !== window.workshopId) return;
			if (typeof d.partials === 'number') debugState.partials = d.partials;
			if (typeof d.finals === 'number') debugState.finals = d.finals;
			updateCounters(); setStatus('stopped');
			ready = false;
			const sel = document.getElementById('stt-provider-select');
			if (sel) sel.disabled = false;
			updateRecordingBadge(false);
		});
		socket.on('stt_stop_ack', (d)=>{
			console.log('[Transcription] Stop acknowledgement received (socket '+socket.id+'):', d);
		});
		socket.on('stt_error', (d)=>{
			if (!d || d.workshop_id !== window.workshopId) return;
			setStatus('error');
			console.error('[Transcription] stt_error (socket '+socket.id+'):', d);
			if (active) { // auto-stop
				try { active = false; } catch {}
				try { if (processor) processor.disconnect(); } catch {}
				processor = null; ready = false;
				const sel = document.getElementById('stt-provider-select');
				if (sel) sel.disabled = false;
			}
			updateRecordingBadge(false);
		});
	}

	// Socket listeners (attach once DOM ready). We may not yet have the main global socket; if not, we create a dedicated one, but later we will rebind to the primary when it appears.
	document.addEventListener('DOMContentLoaded', () => {
		let socket = ensureSocket();
		if (!socket) return;
		
        // Initialize debug panel first (so counters exist before listener events fire)
		(function initDebugPanel(){
			if (window.__sttDebugPanelInit) return; // singleton
			window.__sttDebugPanelInit = true;
			// Debug panel
			const panel = document.createElement('div');
			panel.id = 'stt-debug-panel';
			panel.style.cssText = [
				'position:fixed','bottom:0','right:0','z-index:1040','font-family:system-ui,Arial,sans-serif',
				'background:rgba(20,20,28,0.92)','color:#eee','min-width:260px','max-width:320px','padding:8px 10px',
				'box-shadow:0 0 8px rgba(0,0,0,0.4)','border-top-left-radius:6px',
				'font-size:12px','line-height:1.3','backdrop-filter:blur(4px)'
			].join(';');
 			panel.innerHTML = `
<div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none;">
	<strong style="font-size:12px;">STT Debug</strong>
	<span id="stt-debug-toggle" style="opacity:.7;">▼</span>
</div>
<div id="stt-debug-body" style="margin-top:6px;">
	<div>Status: <span id="stt-debug-status" class="text-info">idle</span></div>
	<div style="margin:4px 0;display:flex;align-items:center;gap:4px;">
		<canvas id="stt-debug-waveform" width="180" height="10" style="background:#111;border:1px solid #333;border-radius:2px;flex-shrink:0;"></canvas>
		<label style="font-size:10px;display:flex;align-items:center;gap:2px;color:#9ab;">
			<input type="checkbox" id="stt-toggle-partials" checked style="margin:0;" />partials
		</label>
	</div>
	<div style="margin-top:4px;">Select Provider:<br/>
		<select id="stt-provider-select" style="width:100%;background:#222;color:#eee;border:1px solid #555;border-radius:4px;font-size:11px;padding:2px 4px;">
			<option value="">(default)</option>
			<option value="vosk">Vosk (offline)</option>
			<option value="aws_transcribe">AWS Transcribe</option>
		</select>
	</div>
	<div style="margin-top:4px;">Active Provider: <span id="stt-debug-provider">-</span></div>
	<div>Model: <span id="stt-debug-model" style="word-break:break-all;opacity:.8;">-</span></div>
	<div>Partials: <span id="stt-debug-partials">0</span></div>
	<div>Finals: <span id="stt-debug-finals">0</span></div>
	<div>WPM: <span id="stt-debug-wpm">0</span></div>
	<div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap;">
		<button id="stt-debug-start" style="background:#2d5;border:0;color:#eee;padding:2px 6px;border-radius:4px;font-size:11px;">Start</button>
		<button id="stt-debug-stop" style="background:#a52;border:0;color:#eee;padding:2px 6px;border-radius:4px;font-size:11px;">Stop</button>
		<button id="stt-debug-reset" style="background:#444;border:0;color:#eee;padding:2px 6px;border-radius:4px;font-size:11px;">Reset</button>
		<button id="stt-debug-close" style="background:#642;border:0;color:#eee;padding:2px 6px;border-radius:4px;font-size:11px;">Close</button>
	</div>
</div>`;

		if (STT_DEBUG_PANEL === true) {
				panel.style.display = 'block';
			} else {
				panel.style.display = 'none';
			}
			// Create the debug panel
			(document.body || document.documentElement).appendChild(panel);
			const body = panel.querySelector('#stt-debug-body');
			panel.addEventListener('click', (e)=>{
				if (e.target && (e.target.id === 'stt-debug-close' || e.target.id === 'stt-debug-reset' || e.target.id === 'stt-debug-start' || e.target.id === 'stt-debug-stop' || e.target.id === 'stt-provider-select')) return; // handled below
				if (!body) return;
				const hidden = body.style.display === 'none';
				body.style.display = hidden ? 'block' : 'none';
				panel.querySelector('#stt-debug-toggle').textContent = hidden ? '▼' : '▲';
			});
			panel.querySelector('#stt-debug-reset').addEventListener('click', (ev)=>{
				ev.stopPropagation();
				window.__sttPartialCount = 0;
				const pEl = document.getElementById('stt-debug-partials'); if (pEl) pEl.textContent = '0';
				const fEl = document.getElementById('stt-debug-finals'); if (fEl) fEl.textContent = '0';
			});
			panel.querySelector('#stt-debug-close').addEventListener('click', (ev)=>{
				ev.stopPropagation(); panel.remove();
			});
			panel.querySelector('#stt-debug-start').addEventListener('click', (ev)=>{
				ev.stopPropagation();
				const sel = document.getElementById('stt-provider-select');
				selectedProvider = sel && sel.value ? sel.value : null;
				startLocalTranscription();
				if (sel) sel.disabled = true;
			});
			panel.querySelector('#stt-debug-stop').addEventListener('click', (ev)=>{
				ev.stopPropagation(); stopLocalTranscription();
				const sel = document.getElementById('stt-provider-select'); if (sel) sel.disabled = false;
			});
			panel.querySelector('#stt-provider-select').addEventListener('change', (ev)=>{
				ev.stopPropagation(); const sel = ev.target; selectedProvider = sel && sel.value ? sel.value : null;
			});
			const togglePartials = panel.querySelector('#stt-toggle-partials');
			if (togglePartials) {
				togglePartials.addEventListener('change', (e)=>{
					e.stopPropagation(); showPartials = !!togglePartials.checked;
					if (!showPartials) { // hide any existing partial rows
						[...document.querySelectorAll('.transcript-partial')].forEach(el=>el.remove());
					}
				});
			}
		})();

		// Attach listeners now
		attachTranscriptionListeners(socket);

		// If later a primary global socket appears and differs, migrate
		let rebindAttempts = 0;
		const rebindTimer = setInterval(()=>{
			rebindAttempts++;
			if (window.socket && window.socket !== socket) {
				console.log('[Transcription][Rebind] Detected primary global socket after init; attaching listeners to it.');
				attachTranscriptionListeners(window.socket);
				// Optionally close the dedicated socket if it is separate
				try { if (socket && !socket.__isGlobal && socket.disconnect) socket.disconnect(); } catch {}
				window.transcriptionSocket = window.socket;
				clearInterval(rebindTimer);
			}
			if (rebindAttempts > 50) { // ~5s
				clearInterval(rebindTimer);
			}
		}, 100);

		// Scroll button + user scroll pause logic
		const scrollBtn = document.getElementById('transcript-scroll-bottom');
		const listEl = document.getElementById('transcript-list');
		if (scrollBtn && listEl) {
			// Initialize state based on current scroll position
			autoScrollEnabled = isAtBottom(listEl);
			if (autoScrollEnabled) scrollBtn.classList.add('d-none');
			// Affordance to jump back and re-enable auto-scroll
			scrollBtn.addEventListener('click', () => {
				autoScrollEnabled = true;
				try {
					listEl.scrollTo({ top: listEl.scrollHeight, behavior: 'smooth' });
				} catch(_){
					// Fallback for older browsers
					listEl.scrollTop = listEl.scrollHeight;
				}
				scrollBtn.classList.add('d-none');
			});
			// Disable auto-scroll if user scrolls away; re-enable when they return to bottom
			listEl.addEventListener('scroll', () => {
				const atBottom = isAtBottom(listEl);
				autoScrollEnabled = atBottom;
				if (atBottom) scrollBtn.classList.add('d-none'); else scrollBtn.classList.remove('d-none');
			});
		}
	});
	
  // If this script loads after DOMContentLoaded, our bootstrap above may not run.
  // Proactively trigger the same initialization to ensure listeners are attached without requiring a manual refresh.
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    try {
      let socket = ensureSocket();
      if (socket) {
        attachTranscriptionListeners(socket);
      }
    } catch(e) { console.warn('[Transcription] Late bootstrap failed', e); }
  }

	function partialFlushLoop(){
		if (!pendingPartialUpdate) { partialAnimationScheduled = false; return; }
		const now = performance.now();
		if (now - lastPartialFlushTs >= PARTIAL_FLUSH_INTERVAL) {
			const upd = pendingPartialUpdate; pendingPartialUpdate = null; lastPartialFlushTs = now;
			if (upd) renderPartial(upd.userId, upd.text || '…');
		}
		if (pendingPartialUpdate) {
			requestAnimationFrame(partialFlushLoop);
		} else {
			partialAnimationScheduled = false;
		}
	}

	function startWaveformLoop(){
		if (!analyser) return;
		if (!waveformCanvas) {
			waveformCanvas = document.getElementById('stt-debug-waveform');
			if (!waveformCanvas) { return; }
			waveformCtx = waveformCanvas.getContext('2d');
		}
		const data = new Uint8Array(analyser.fftSize);
		function draw(){
			if (!active || !analyser || !waveformCtx) { waveformRAF = null; return; }
			analyser.getByteTimeDomainData(data);
			let sum = 0; for (let i=0;i<data.length;i++){ const v = (data[i]-128)/128; sum += v*v; }
			const rms = Math.sqrt(sum / data.length);
			const peak = Math.max(...data.map(v=>Math.abs((v-128)/128)));
			if (waveformCanvas) {
				waveformCtx.clearRect(0,0,waveformCanvas.width,waveformCanvas.height);
				// background bar
				waveformCtx.fillStyle = '#222'; waveformCtx.fillRect(0,0,waveformCanvas.width,waveformCanvas.height);
				const level = Math.min(1, rms * 4);
				const grd = waveformCtx.createLinearGradient(0,0,waveformCanvas.width,0);
				const hue = Math.round(120 - 120*level); // green -> red
				grd.addColorStop(0, `hsl(${hue},70%,45%)`);
				grd.addColorStop(1, `hsl(${hue},80%,60%)`);
				waveformCtx.fillStyle = grd;
				waveformCtx.fillRect(0,0,Math.max(2, level*waveformCanvas.width), waveformCanvas.height);
			}
			// Silence detection
			if (rms < SILENCE_THRESHOLD) {
				if (silenceStartTs == null) silenceStartTs = performance.now();
				else if (!silencePromptShown && performance.now() - silenceStartTs > SILENCE_SECS_TO_PROMPT*1000) {
					silencePromptShown = true;
					const statusEl = document.getElementById('stt-debug-status');
					if (statusEl) statusEl.textContent = 'silence…';
				}
			} else {
				silenceStartTs = null; silencePromptShown = false;
			}
			waveformRAF = requestAnimationFrame(draw);
		}
		waveformRAF = requestAnimationFrame(draw);
	}

	// Expose start/stop globally, plus facilitator pause/resume helpers
	window.startLocalTranscription = startLocalTranscription;
	window.stopLocalTranscription = stopLocalTranscription;
	window.isTranscriptionActive = isTranscriptionActive;
	window.pauseTranscriptionForFacilitator = pauseTranscriptionForFacilitator;
	window.resumeTranscriptionAfterFacilitator = resumeTranscriptionAfterFacilitator;
})();

