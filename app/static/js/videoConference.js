/*
 * app/static/js/videoConference.js
 * Scaffold for WebRTC mesh + controls. Non-breaking placeholder.
 */

(() => {
	if (!window || !window.io) {
		console.warn('[VideoConference] Socket.IO not loaded yet.');
	}

	// --- Load persisted media preferences (localStorage) ---
	function lsGetBool(key, fallback){
		try { const v = localStorage.getItem(key); if (v === null) return fallback; return v === 'true'; } catch { return fallback; }
	}
	function lsSetBool(key, val){
		try { localStorage.setItem(key, val ? 'true':'false'); } catch {}
	}

	const state = {
		started: false,
		micEnabled: lsGetBool('vc_pref_mic', true),
		camEnabled: lsGetBool('vc_pref_cam', true),
		ccEnabled: lsGetBool('vc_pref_cc', false), // persist CC preference
		screenSharing: false,
		screenStream: null,
		screenTrack: null,
		localStream: null,
		peers: {}, // peerUserId -> { pc, streams: [] }
		pendingCandidates: {}, // peerUserId -> ICE candidates queued until PC setRemoteDescription
		userId: typeof window !== 'undefined' ? window.userId : null,
		workshopId: typeof window !== 'undefined' ? window.workshopId : null,
		participantsById: {}, // userId -> { display_name, first_name, last_name }
		spotlightUserId: null, // desired spotlight target (user id); null clears
		inConference: new Set(), // userIds currently joined (from server events)
		flags: (typeof window !== 'undefined' && window.conferenceFlags) ? window.conferenceFlags : { conferenceActive: true, transcriptionEnabled: false },
		remoteMedia: { /* userId -> { mic, cam, screen } */ },
		remoteScreens: { /* userId -> MediaStream */ }
	};

	// Elements (may not exist yet if template changed)
	const el = {
		card: document.getElementById('video-conference-card'),
		grid: document.getElementById('video-grid'),
		placeholder: document.getElementById('video-placeholder'),
		status: document.getElementById('vc-status-line'),
		btnMic: document.getElementById('vc-btn-mic'),
		btnCam: document.getElementById('vc-btn-cam'),
		btnShare: document.getElementById('vc-btn-share'),
		btnCC: document.getElementById('vc-btn-cc'),
		btnLeave: document.getElementById('vc-btn-leave'),
		aiTile: null,
		stage: document.getElementById('screen-stage'),
		stageVideo: document.getElementById('screen-stage-video'),
		stageOwner: document.getElementById('screen-stage-owner'),
		stageHint: document.getElementById('screen-stage-hint'),
	};

	function setStatus(msg) {
		if (el.status) el.status.textContent = msg;
	}

	function showPermissionWarning(e) {
		try {
			const warn = document.getElementById('vc-permission-warning');
			const txt = document.getElementById('vc-permission-text');
			if (txt) txt.textContent = 'Camera/Microphone access was blocked. Please allow permissions and retry.';
			if (warn) warn.classList.remove('d-none');
		} catch (_) {}
	}

	function ensureAiFacilitatorTile() {
		try {
			if (!el.grid) return;
			let tile = document.getElementById('ai-facilitator-tile');
			if (!tile) {
				// Create a non-stream tile representing the AI facilitator
				tile = document.createElement('div');
				tile.id = 'ai-facilitator-tile';
				tile.className = 'position-relative rounded overflow-hidden video-tile flex-shrink-0 ai-facilitator-tile';
				tile.setAttribute('data-ai', 'facilitator');
				tile.innerHTML = `
					<div class="w-100 h-100 d-flex flex-column align-items-center justify-content-center text-center text-light">
						<div class="ai-avatar mb-2" aria-label="AI Facilitator" title="AI Facilitator" style="width:72px;height:72px;border-radius:50%;background:#0d6efd1a;display:flex;align-items:center;justify-content:center;">
							<i class="bi bi-robot" style="font-size:2rem;color:#0d6efd;"></i>
						</div>
						<div class="small">AI Facilitator</div>
						<div class="ai-wave"></div>
					</div>`;
				// Insert at the beginning so it’s always visible
				if (el.grid.firstChild) el.grid.insertBefore(tile, el.grid.firstChild); else el.grid.appendChild(tile);
				if (el.placeholder) el.placeholder.remove();
				scheduleLayoutRecalc();
			}
			// Bind speaking indicator to facilitator TTS if available
			if (window.FacilitatorTTS && typeof window.FacilitatorTTS.onSpeakingChange === 'function') {
				window.FacilitatorTTS.onSpeakingChange((speaking) => {
					try { tile.classList.toggle('speaking', !!speaking); } catch(_) {}
				});
				// Initialize state
				try {
					if (typeof window.FacilitatorTTS.isSpeaking === 'function') {
						tile.classList.toggle('speaking', !!window.FacilitatorTTS.isSpeaking());
					}
				} catch(_) {}
			}
		} catch(_) {}
	}

	function setupLocalAudioAnalysis() {
		// No-op placeholder for audio level metering; can be implemented later
		return;
	}

	function clearExistingSpotlights() {
		if (!el.grid) return;
		el.grid.querySelectorAll('.video-tile.spotlight').forEach(t => t.classList.remove('spotlight'));
	}

	function applySpotlight() {
		clearExistingSpotlights();
		if (!el.grid) return;
		const target = state.spotlightUserId;
		if (target == null) return;
		let tile = null;
		if (String(target) === String(state.userId)) {
			// Local user spotlight
			tile = el.grid.querySelector('[data-tile="local"]');
		} else {
			tile = el.grid.querySelector(`[data-remote="${target}"]`);
		}
		if (tile) {
			tile.classList.add('spotlight');
			try { tile.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' }); } catch {}
		}
	}

	function setSpotlight(userId) {
		state.spotlightUserId = (userId === null || userId === undefined || userId === '') ? null : Number(userId);
		applySpotlight();
	}
	function clearSpotlight() { setSpotlight(null); }

	function setParticipants(list) {
		try {
			const map = {};
			(list || []).forEach(p => {
				const id = p.user_id || p.id;
				if (id == null) return;
				const dn = (p.display_name && String(p.display_name).trim())
					? String(p.display_name).trim()
					: (((p.first_name || '') + (p.last_name ? (' ' + p.last_name) : '')).trim() || (p.email ? p.email.split('@')[0] : ('User ' + id)));
				map[id] = { display_name: dn, first_name: p.first_name || '', last_name: p.last_name || '' };
			});
			state.participantsById = map;
			// Update existing remote tile labels to nicer names
			if (el.grid) {
				el.grid.querySelectorAll('.video-tile[data-remote]').forEach(tile => {
					const uid = tile.getAttribute('data-remote');
					const badge = tile.querySelector('.badge');
					if (badge && uid && map[uid]) badge.textContent = map[uid].display_name;
				});
			}
		} catch {}
	}

	function ensureLocalVideoTile() {
		if (!el.grid) return;
		let tile = el.grid.querySelector('[data-tile="local"]');
		if (!tile) {
			tile = document.createElement('div');
			tile.className = 'position-relative bg-dark rounded overflow-hidden video-tile flex-shrink-0';
			tile.dataset.tile = 'local';
			tile.innerHTML = `
				<video id="vc-local-video" autoplay playsinline muted class="w-100 h-100 object-fit-cover"></video>
				<span class="badge bg-primary position-absolute top-0 start-0 m-1 small">You</span>
				<div class="position-absolute bottom-0 start-0 m-1 small text-bg-dark px-1 rounded media-badge" style="font-size:.6rem;">🎤 📷</div>
			`;
			el.grid.appendChild(tile);
			if (el.placeholder) el.placeholder.remove();
			scheduleLayoutRecalc();
			applySpotlight();
		}
		return tile.querySelector('video');
	}

	async function initLocalMedia() {
		try {
			state.localStream = await navigator.mediaDevices.getUserMedia({ audio: state.micEnabled, video: state.camEnabled });
			const v = ensureLocalVideoTile();
			if (v && !v.srcObject) v.srcObject = state.localStream;
			// If prefs disabled tracks, reflect that
			if (!state.micEnabled) toggleTrack('audio', false);
			if (!state.camEnabled) toggleTrack('video', false);
			setStatus('Local media ready');
		} catch (e) {
			console.error('[VideoConference] Failed to get user media', e);
			setStatus('Failed to access camera/mic');
			showPermissionWarning(e);
		}
	}

	function toggleTrack(kind, enabled) {
		if (!state.localStream) return;
		state.localStream.getTracks().filter(t => t.kind === kind).forEach(t => { t.enabled = enabled; });
	}

	function updateButtons() {
		if (el.btnMic) el.btnMic.classList.toggle('active', state.micEnabled);
		if (el.btnCam) el.btnCam.classList.toggle('active', state.camEnabled);
		if (el.btnCC) el.btnCC.classList.toggle('active', state.ccEnabled);
		if (el.btnShare) el.btnShare.classList.toggle('active', state.screenSharing);
	}

	function _ownerName(userId){
		if (String(userId) === String(state.userId)) return 'You';
		const p = state.participantsById[userId];
		return (p && p.display_name) ? p.display_name : `User ${userId}`;
	}

	function showScreenStage(ownerUserId, stream, { local=false }={}){
		try {
			if (!el.stage) return;
			el.stage.classList.remove('d-none');
			el.stage.classList.add('ready');
			if (el.stageOwner) el.stageOwner.textContent = _ownerName(ownerUserId);
			if (el.stageHint) el.stageHint.textContent = local ? 'You are sharing this screen.' : 'Viewing shared screen.';
			if (el.stageVideo) {
				if (!local) { try { el.stageVideo.muted = false; } catch(_){} }
				if (el.stageVideo.srcObject !== stream) el.stageVideo.srcObject = stream;
			}
			scheduleLayoutRecalc();
		} catch(_) {}
	}

	function clearScreenStageIfOwner(ownerUserId){
		try {
			if (!el.stage) return;
			const label = el.stageOwner ? el.stageOwner.textContent : '';
			if (!label) { hideScreenStage(); return; }
			if (String(ownerUserId) === String(state.userId) || _ownerName(ownerUserId) === label) {
				hideScreenStage();
			}
		} catch(_) {}
	}

	function hideScreenStage(){
		try {
			if (!el.stage) return;
			if (el.stageVideo) el.stageVideo.srcObject = null;
			el.stage.classList.remove('ready');
			el.stage.classList.add('d-none');
			scheduleLayoutRecalc();
		} catch(_) {}
	}

	function recalcLayout() {
		if (!el.grid) return;
		const tiles = el.grid.querySelectorAll('.video-tile');
		const count = tiles.length;
		el.grid.dataset.count = count;
		// Adjust min tile size to keep within viewport
		if (count >= 12) {
			el.grid.style.setProperty('--vc-min-tile', '140px');
		} else if (count >= 9) {
			el.grid.style.setProperty('--vc-min-tile', '160px');
		} else if (count >= 5) {
			el.grid.style.setProperty('--vc-min-tile', '180px');
		} else {
			el.grid.style.setProperty('--vc-min-tile', '220px');
		}
		// Enforce max height relative to viewport to stop overflow on smaller screens
		const vh = window.innerHeight;
		const cardRect = el.card ? el.card.getBoundingClientRect() : null;
		const available = vh - (cardRect ? cardRect.top : 120) - 120; // leave room for footer and margins
		const maxGridHeight = Math.max(220, Math.min(vh - 200, available));
		el.grid.style.maxHeight = maxGridHeight + 'px';
		el.grid.style.overflowY = 'auto';
	}

	// Throttled layout recalculation
	let layoutRaf = null;
	function scheduleLayoutRecalc(){
		if (layoutRaf) return;
		layoutRaf = requestAnimationFrame(() => {
			layoutRaf = null;
			recalcLayout();
		});
	}

	function emitMediaState() {
		const sock = ensureSocket();
		if (!sock || !state.workshopId) return;
		sock.emit('update_media_state', {
			workshop_id: state.workshopId,
			mic: state.micEnabled,
			cam: state.camEnabled,
			screen: state.screenSharing,
		});
	}

	function ensureSocket() {
		if (state.socket) return state.socket;
		// Prefer the primary workshop socket if available to avoid duplicate connections
		if (window._workshopSocket && typeof window._workshopSocket.emit === 'function') {
			state.socket = window._workshopSocket;
			return state.socket;
		}
		if (window.socket && typeof window.socket.emit === 'function') {
			state.socket = window.socket;
			return state.socket;
		}
		if (!window.io) return null;
		state.socket = window.io();
		return state.socket;
	}

	function _displayName(userId){
		const p = state.participantsById[userId];
		return (p && p.display_name) ? p.display_name : ('User ' + userId);
	}

	function addRemoteTile(userId) {
		if (!el.grid) return;
		let tile = el.grid.querySelector(`[data-remote="${userId}"]`);
		if (!tile) {
			tile = document.createElement('div');
			tile.className = 'position-relative bg-secondary-subtle rounded overflow-hidden video-tile flex-shrink-0';
			tile.dataset.remote = userId;
			tile.innerHTML = `
				<video autoplay playsinline class="w-100 h-100 object-fit-cover"></video>
				<span class="badge bg-dark bg-opacity-50 text-light position-absolute top-0 start-0 m-1 small">${_displayName(userId)}</span>
				<div class="position-absolute bottom-0 start-0 m-1 small text-bg-dark px-1 rounded media-badge" style="font-size:.6rem;">…</div>`;
			el.grid.appendChild(tile);
			if (el.placeholder) el.placeholder.remove();
			// Add audio level meter element
			const meter = document.createElement('div');
			meter.className = 'level-meter';
			tile.appendChild(meter);
			scheduleLayoutRecalc();
			applySpotlight();
		}
		return tile.querySelector('video');
	}

	function createPeer(userId) {
		if (state.peers[userId]) return state.peers[userId];
		const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
		state.peers[userId] = { pc, streams: [] };
		pc.onicecandidate = (e) => {
			if (e.candidate) {
				ensureSocket()?.emit('rtc_ice', { workshop_id: state.workshopId, to_user_id: userId, candidate: e.candidate });
			}
		};
		pc.ontrack = (e) => {
			const v = addRemoteTile(userId);
			if (v && (!v.srcObject || v.srcObject.getTracks().length < e.streams[0].getTracks().length)) {
				v.srcObject = e.streams[0];
			}
			// If this peer is screen sharing, route their stream to the Screen Stage
			try {
				const m = state.remoteMedia[String(userId)] || state.remoteMedia[userId];
				if (m && m.screen) {
					state.remoteScreens[userId] = e.streams[0];
					if (!state.screenSharing) showScreenStage(userId, e.streams[0], { local: false });
				}
			} catch(_) {}
		};
		// Trigger renegotiation when local tracks change (e.g., screen share)
		try { pc.onnegotiationneeded = () => { safeNegotiate(userId); }; } catch(_) {}
		// Add local tracks
		if (state.localStream) {
			state.localStream.getTracks().forEach(t => pc.addTrack(t, state.localStream));
		}
		// If we're currently sharing a screen, add that track too
		if (state.screenTrack && state.screenStream) {
			try { pc.addTrack(state.screenTrack, state.screenStream); } catch(_) {}
		}
		return state.peers[userId];
	}

	async function safeNegotiate(targetUserId){
		try {
			const entry = state.peers[targetUserId]; if (!entry) return;
			const offer = await entry.pc.createOffer();
			await entry.pc.setLocalDescription(offer);
			ensureSocket()?.emit('rtc_offer', { workshop_id: state.workshopId, to_user_id: targetUserId, sdp: offer });
		} catch(_) { /* no-op */ }
	}

	async function makeOffer(targetUserId) {
		const { pc } = createPeer(targetUserId);
		const offer = await pc.createOffer();
		await pc.setLocalDescription(offer);
		ensureSocket()?.emit('rtc_offer', { workshop_id: state.workshopId, to_user_id: targetUserId, sdp: offer });
	}

	async function handleOffer(fromUserId, sdp) {
		const { pc } = createPeer(fromUserId);
		await pc.setRemoteDescription(new RTCSessionDescription(sdp));
		const answer = await pc.createAnswer();
		await pc.setLocalDescription(answer);
		ensureSocket()?.emit('rtc_answer', { workshop_id: state.workshopId, to_user_id: fromUserId, sdp: answer });
		// Flush queued ICE for this peer
		flushPending(fromUserId);
	}

	async function handleAnswer(fromUserId, sdp) {
		const peer = state.peers[fromUserId];
		if (!peer) return;
		await peer.pc.setRemoteDescription(new RTCSessionDescription(sdp));
		flushPending(fromUserId);
	}

	function flushPending(userId) {
		const peer = state.peers[userId];
		if (!peer) return;
		const list = state.pendingCandidates[userId] || [];
		list.forEach(c => peer.pc.addIceCandidate(new RTCIceCandidate(c)).catch(()=>{}));
		state.pendingCandidates[userId] = [];
	}

	function handleIce(fromUserId, candidate) {
		const peer = state.peers[fromUserId];
		if (peer && peer.pc.remoteDescription) {
			peer.pc.addIceCandidate(new RTCIceCandidate(candidate)).catch(()=>{});
		} else {
			(state.pendingCandidates[fromUserId] = state.pendingCandidates[fromUserId] || []).push(candidate);
		}
	}

	function removePeer(userId) {
		try {
			const entry = state.peers[userId];
			if (entry && entry.pc) {
				try { entry.pc.getSenders().forEach(s => { try { entry.pc.removeTrack(s); } catch(_){} }); } catch(_){ }
				try { entry.pc.close(); } catch(_) {}
			}
			delete state.peers[userId];
			// Remove tile
			if (el.grid) {
				const t = el.grid.querySelector(`[data-remote="${userId}"]`);
				if (t) t.remove();
				scheduleLayoutRecalc();
			}
		} catch(_) {}
	}

	function setupSocketHandlers() {
		const sock = ensureSocket();
		if (!sock || setupSocketHandlers._installed) return;
		setupSocketHandlers._installed = true;
		sock.on('conference_error', (d) => { setStatus((d && d.message) ? `Error: ${d.message}` : 'Conference error'); });
		// Track remote media (mic/cam/screen) to know when someone shares
		sock.on('media_state_update', (d) => {
			try {
				if (!d || Number(d.workshop_id) !== Number(state.workshopId)) return;
				const uid = Number(d.user_id);
				state.remoteMedia[uid] = { mic: !!d.mic, cam: !!d.cam, screen: !!d.screen };
				if (uid === Number(state.userId)) return; // local handled by buttons
				if (d.screen) {
					// If their stream already attached, present it; else show placeholder until ontrack fires
					const stream = state.remoteScreens[uid];
					if (!state.screenSharing && stream) showScreenStage(uid, stream, { local: false });
				} else {
					// If they stopped and they own the stage, clear it
					clearScreenStageIfOwner(uid);
				}
			} catch(_) {}
		});
		sock.on('conference_participants', (payload) => {
			try {
				const list = Array.isArray(payload && payload.participants) ? payload.participants : [];
				// Track current membership
				state.inConference = new Set(list.map(p => Number(p.user_id || p.id)).filter(v => !isNaN(v)));
				// Always include local user to avoid false negatives for spotlight/self
				const myId = Number(state.userId);
				if (myId) state.inConference.add(myId);
				setParticipants(list);
				// Seed remote media snapshot if provided
				const ms = (payload && payload.media_states) || {};
				Object.keys(ms).forEach(uid => { try { state.remoteMedia[Number(uid)] = { mic: !!ms[uid].mic, cam: !!ms[uid].cam, screen: !!ms[uid].screen }; } catch(_){} });
				// If someone already sharing, stage when we have their stream
				const preSharer = Object.keys(state.remoteMedia).map(n=>Number(n)).find(uid => uid !== myId && state.remoteMedia[uid] && state.remoteMedia[uid].screen);
				if (preSharer && state.remoteScreens[preSharer] && !state.screenSharing) {
					showScreenStage(preSharer, state.remoteScreens[preSharer], { local:false });
				}
				setStatus(`Connected: ${state.inConference.size} participant${state.inConference.size===1?'':'s'}`);
				// Create offers to peers deterministically to avoid glare: lower ID offers first
				list.forEach(p => {
					const pid = Number(p.user_id || p.id);
					if (!pid || pid === myId) return;
					if (myId && myId < pid) {
						makeOffer(pid).catch(()=>{});
					}
				});
			} catch(_) {}
		});
		sock.on('participant_joined', (d) => {
			const uid = Number(d && d.user_id);
			if (!uid) return;
			state.inConference.add(uid);
			// Offer if our id is lower to reduce glare
			if (state.userId && Number(state.userId) < uid) {
				makeOffer(uid).catch(()=>{});
			}
		});
		sock.on('participant_left', (d) => {
			const uid = Number(d && d.user_id);
			if (!uid) return;
			state.inConference.delete(uid);
			removePeer(uid);
			delete state.remoteScreens[uid];
		});
		sock.on('rtc_offer', (d) => {
			if (!d || Number(d.workshop_id) !== Number(state.workshopId)) return;
			const from = Number(d.from_user_id);
			if (!from || from === Number(state.userId)) return;
			handleOffer(from, d.sdp).catch(()=>{});
		});
		sock.on('rtc_answer', (d) => {
			if (!d || Number(d.workshop_id) !== Number(state.workshopId)) return;
			const from = Number(d.from_user_id);
			if (!from || from === Number(state.userId)) return;
			handleAnswer(from, d.sdp).catch(()=>{});
		});
		sock.on('rtc_ice', (d) => {
			if (!d || Number(d.workshop_id) !== Number(state.workshopId)) return;
			const from = Number(d.from_user_id);
			if (!from || from === Number(state.userId)) return;
			if (d.candidate) handleIce(from, d.candidate);
		});
	}

	async function startConferenceIfNeeded() {
		if (state.started) return;
		state.started = true;
		await initLocalMedia();
		ensureAiFacilitatorTile();
		setupLocalAudioAnalysis();
		updateButtons();
		const sock = ensureSocket();
		if (sock && state.workshopId) {
			sock.emit('join_conference', { workshop_id: state.workshopId });
		}
		setStatus('Connecting to conference...');
		// Apply persisted CC preference only if transcription enabled
		if (state.flags.transcriptionEnabled && state.ccEnabled) {
			if (el.btnCC) el.btnCC.classList.add('active');
			if (window.startLocalTranscription) window.startLocalTranscription();
			const tStatus = document.getElementById('transcript-status');
			if (tStatus) tStatus.textContent = 'CC: On';
		}
		// Ensure layout is correct after media init
		scheduleLayoutRecalc();
	}

	// Wire up control bar buttons if present
	function bindControls() {
		try {
			if (el.btnMic) el.btnMic.onclick = () => { state.micEnabled = !state.micEnabled; lsSetBool('vc_pref_mic', state.micEnabled); toggleTrack('audio', state.micEnabled); updateButtons(); emitMediaState(); };
			if (el.btnCam) el.btnCam.onclick = () => { state.camEnabled = !state.camEnabled; lsSetBool('vc_pref_cam', state.camEnabled); toggleTrack('video', state.camEnabled); updateButtons(); emitMediaState(); };
			if (el.btnShare) el.btnShare.onclick = async () => {
				if (!state.screenSharing) { await startScreenShare(); } else { await stopScreenShare(); }
			};
			if (el.btnCC) el.btnCC.onclick = () => {
				state.ccEnabled = !state.ccEnabled; lsSetBool('vc_pref_cc', state.ccEnabled); updateButtons();
				try {
					const tStatus = document.getElementById('transcript-status');
					if (state.ccEnabled) { if (window.startLocalTranscription) window.startLocalTranscription(); if (tStatus) tStatus.textContent = 'CC: On'; }
					else { if (window.stopLocalTranscription) window.stopLocalTranscription(); if (tStatus) tStatus.textContent = 'CC: Off'; }
				} catch(_) {}
			};
			if (el.btnLeave) el.btnLeave.onclick = () => { const s = ensureSocket(); if (s && state.workshopId) s.emit('leave_conference', { workshop_id: state.workshopId }); setStatus('Left conference'); };
		} catch(_) {}
	}

	async function startScreenShare(){
		try {
			const constraints = { video: { frameRate: 15 }, audio: false };
			const stream = await navigator.mediaDevices.getDisplayMedia(constraints);
			const [track] = stream.getVideoTracks();
			if (!track) return;
			state.screenStream = stream; state.screenTrack = track; state.screenSharing = true; updateButtons();
			// When user stops from browser UI
			track.addEventListener('ended', () => { stopScreenShare().catch(()=>{}); });
			// Add to all peers
			Object.values(state.peers).forEach(({pc}) => { try { pc.addTrack(track, stream); } catch(_){} });
			showScreenStage(state.userId, stream, { local: true });
			emitMediaState();
		} catch(e) {
			console.error('[VideoConference] getDisplayMedia failed', e);
			state.screenSharing = false; updateButtons();
		}
	}

	async function stopScreenShare(){
		try {
			const track = state.screenTrack; const stream = state.screenStream;
			state.screenSharing = false; updateButtons();
			if (track) {
				// Remove from all peers
				Object.values(state.peers).forEach(({pc}) => {
					try { pc.getSenders().forEach(s => { if (s.track === track) { try { pc.removeTrack(s); } catch(_){} } }); } catch(_) {}
				});
				try { track.stop(); } catch(_) {}
			}
			if (stream) { try { stream.getTracks().forEach(t=>{ try { t.stop(); } catch(_){} }); } catch(_) {}
			}
			state.screenTrack = null; state.screenStream = null;
			clearScreenStageIfOwner(state.userId);
			emitMediaState();
		} catch(_) {}
	}

	// Expose minimal API for other modules (room UI)
	window.VideoConference = {
		start: () => { bindControls(); setupSocketHandlers(); startConferenceIfNeeded(); },
		setFlags: (flags) => { state.flags = { ...state.flags, ...(flags || {}) }; },
		getState: () => ({
			started: state.started,
			micEnabled: state.micEnabled,
			camEnabled: state.camEnabled,
			ccEnabled: state.ccEnabled,
			screenSharing: state.screenSharing,
			userId: state.userId,
			workshopId: state.workshopId,
			spotlightUserId: state.spotlightUserId,
		}),
		setSpotlight,
		clearSpotlight,
		setParticipants,
		isInConference: (uid) => { 
			try { 
				const n = Number(uid);
				if (n && n === Number(state.userId)) return true; // local user is always "in"
				return state.inConference.has(n); 
			} catch(_) { return false; } 
		}
	};
})();