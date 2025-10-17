(() => {
	const STORAGE_KEY = 'brainstormxVideoState';
	const DEFAULT_SPEED_STEPS = [0.5, 1, 1.25, 1.5, 1.75, 2];

	const defaultVideos = [
		{
			id: 1,
			title: 'BrainStormX Overview',
			subtitle: 'Guided tour of the AI-powered innovation workspace',
			src: '/static/videos/brainstormx-overview.mp4',
			poster: '/static/images/video-poster.jpg',
			transcriptEndpoint: '/api/transcripts/1',
			captions: '',
			duration: 0,
			views: 0,
			chapters: [
				{ time: 0, title: 'Welcome' },
				{ time: 32, title: 'Workshop Intelligence' },
				{ time: 78, title: 'Action Orchestration' },
				{ time: 135, title: 'Next Steps' }
			]
		}
	];

	class VideoPlayerManager {
		constructor(root, playlist) {
			this.root = root;
			this.videos = Array.isArray(playlist) && playlist.length ? playlist : defaultVideos;
			this.currentVideoIndex = 0;
			this.state = {
				autoplay: true,
				autoScroll: true,
				playbackSpeed: 1,
				videoIndex: 0,
				progress: {},
				transcriptLanguage: {}
			};

			this.speedSteps = DEFAULT_SPEED_STEPS;
			this.transcriptWords = [];
			this.transcriptBlocks = [];
			this.pendingChapters = [];
			this.saveTimeout = null;

			this.mapElements();
			this.loadState();
			this.renderPlaylist();
			this.wireEvents();
			this.loadVideo(this.state.videoIndex);
		}

		mapElements() {
			this.videoEl = this.root.querySelector('#mainVideo');
			this.videoSourceEl = this.root.querySelector('#videoSource');
			this.captionTrackEl = this.root.querySelector('#captionTrack');
			this.titleEl = this.root.querySelector('#currentVideoTitle');
			this.progressLabelEl = this.root.querySelector('#videoProgress');
			this.progressBarEl = this.root.querySelector('#progressBar');
			this.chapterTrackEl = this.root.querySelector('#chapterMarkers');
			this.playlistContainer = document.getElementById('playlistContainer');
			this.transcriptContainer = document.getElementById('transcriptContent');
			this.ccOverlay = this.root.querySelector('#ccOverlay');
			this.ccText = this.root.querySelector('#ccText');
			this.speedControlLabel = this.root.querySelector('#speedControlLabel');
			this.autoplayToggle = document.getElementById('autoplayToggle');
			this.autoplayStatus = document.getElementById('autoplayStatus');
			this.downloadTranscriptsBtn = document.getElementById('downloadTranscripts');
			this.prevBtn = this.root.querySelector('[data-video-action="prev"]');
			this.nextBtn = this.root.querySelector('[data-video-action="next"]');
			this.captionToggleBtn = this.root.querySelector('[data-video-action="captions"]');
			this.speedBtn = this.root.querySelector('[data-video-action="speed"]');
			this.fullscreenBtn = this.root.querySelector('[data-video-action="fullscreen"]');
			this.tabsEl = document.getElementById('videoTabs');
			this.transcriptTabBtn = document.getElementById('transcript-tab');
			this.playlistTabBtn = document.getElementById('playlist-tab');
			this.searchTranscriptBtn = document.getElementById('transcriptSearch');
			this.copyTranscriptBtn = document.getElementById('transcriptCopy');
			this.transcriptSyncBtn = document.getElementById('transcriptSync');
			this.transcriptLangSelect = document.getElementById('transcriptLang');
			this.videoViewsEl = this.root.querySelector('[data-video-views]');
		}

		loadState() {
			try {
				const stored = localStorage.getItem(STORAGE_KEY);
				if (!stored) {
					this.reflectState();
					return;
				}

				const parsed = JSON.parse(stored);
				this.state = {
					...this.state,
					...parsed,
					progress: parsed.progress || {}
				};
				this.reflectState();
			} catch (error) {
				console.warn('[VideoPlayer] Unable to parse video state', error);
				this.state = { ...this.state };
				this.reflectState();
			}
		}

		reflectState() {
			if (this.autoplayStatus) {
				this.autoplayStatus.textContent = this.state.autoplay ? 'On' : 'Off';
			}

			if (this.autoplayToggle) {
				this.autoplayToggle.classList.toggle('active', this.state.autoplay);
			}

			if (this.transcriptSyncBtn) {
				this.transcriptSyncBtn.classList.toggle('active', this.state.autoScroll);
				this.transcriptSyncBtn.setAttribute('aria-pressed', this.state.autoScroll ? 'true' : 'false');
			}

			if (this.speedControlLabel) {
				this.speedControlLabel.textContent = `${this.state.playbackSpeed.toFixed(2).replace(/\.00$/, '')}x`;
			}
		}

		saveState() {
			clearTimeout(this.saveTimeout);
			this.saveTimeout = setTimeout(() => {
				localStorage.setItem(STORAGE_KEY, JSON.stringify(this.state));
			}, 300);
		}

		wireEvents() {
			if (this.prevBtn) {
				this.prevBtn.addEventListener('click', () => this.loadVideo(this.getPrevIndex()));
			}

			if (this.nextBtn) {
				this.nextBtn.addEventListener('click', () => this.loadVideo(this.getNextIndex()));
			}

			if (this.autoplayToggle) {
				this.autoplayToggle.addEventListener('click', () => {
					this.state.autoplay = !this.state.autoplay;
					this.reflectState();
					this.saveState();
				});
			}

			if (this.downloadTranscriptsBtn) {
				this.downloadTranscriptsBtn.addEventListener('click', () => this.handleTranscriptDownload());
			}

			if (this.captionToggleBtn) {
				this.captionToggleBtn.addEventListener('click', () => this.toggleCaptions());
			}

			if (this.speedBtn) {
				this.speedBtn.addEventListener('click', () => this.cycleSpeed());
			}

			if (this.fullscreenBtn) {
				this.fullscreenBtn.addEventListener('click', () => this.toggleFullscreen());
			}

			if (this.searchTranscriptBtn) {
				this.searchTranscriptBtn.addEventListener('click', () => this.promptTranscriptSearch());
			}

			if (this.copyTranscriptBtn) {
				this.copyTranscriptBtn.addEventListener('click', () => this.copyTranscript());
			}

			if (this.transcriptSyncBtn) {
				this.transcriptSyncBtn.addEventListener('click', () => {
					this.state.autoScroll = !this.state.autoScroll;
					this.reflectState();
					this.saveState();
				});
			}

			if (this.transcriptLangSelect) {
				this.transcriptLangSelect.addEventListener('change', () => {
					const video = this.videos[this.currentVideoIndex];
					if (!video || !video.transcriptEndpoint) return;
					const selectedLang = (this.transcriptLangSelect.value || 'en').toLowerCase();
					this.state.transcriptLanguage = this.state.transcriptLanguage || {};
					this.state.transcriptLanguage[video.id] = selectedLang;
					this.saveState();
					this.loadTranscript(video.id, selectedLang);
				});
			}

			if (this.tabsEl) {
				this.tabsEl.addEventListener('shown.bs.tab', (event) => {
					const targetId = event.target.id;
					this.state.activeTab = targetId;
					this.saveState();
				});
			}

			if (this.videoEl) {
				this.videoEl.addEventListener('timeupdate', () => this.handleTimeUpdate());
				this.videoEl.addEventListener('ended', () => this.handleEnded());
				this.videoEl.addEventListener('loadedmetadata', () => this.handleLoadedMetadata());
				this.videoEl.addEventListener('durationchange', () => this.handleLoadedMetadata());
				this.videoEl.addEventListener('play', () => {
					if (this.videoEl) {
						this.videoEl.playbackRate = this.state.playbackSpeed;
					}
				});
			}

			document.addEventListener('keydown', (event) => this.handleKeydown(event));
		}

		getPrevIndex() {
			if (!this.videos.length) return 0;
			return (this.currentVideoIndex - 1 + this.videos.length) % this.videos.length;
		}

		getNextIndex() {
			if (!this.videos.length) return 0;
			return (this.currentVideoIndex + 1) % this.videos.length;
		}

		loadVideo(index) {
			if (!this.videos[index]) return;

			this.currentVideoIndex = index;
			this.state.videoIndex = index;
			this.saveState();

			const video = this.videos[index];

			if (this.titleEl) {
				this.titleEl.textContent = video.title;
			}

			if (this.videoViewsEl) {
				this.videoViewsEl.textContent = video.views ?? 0;
			}

			if (this.videoSourceEl) {
				this.videoSourceEl.src = video.src;
			}

			if (this.videoEl) {
				if (video.poster) {
					this.videoEl.poster = video.poster;
				}

				this.videoEl.load();

				const resumeTime = this.state.progress?.[video.id];
				if (typeof resumeTime === 'number' && resumeTime > 1) {
					this.videoEl.addEventListener(
						'loadedmetadata',
						() => {
							try {
								if (resumeTime < this.videoEl.duration) {
									this.videoEl.currentTime = resumeTime;
								}
							} catch (error) {
								console.debug('[VideoPlayer] Unable to resume video position', error);
							}
						},
						{ once: true }
					);
				}

				this.videoEl.playbackRate = this.state.playbackSpeed;
			}

			if (this.captionTrackEl) {
				this.captionTrackEl.src = video.captions || '';
				this.captionTrackEl.track && (this.captionTrackEl.track.mode = 'disabled');
				if (this.captionToggleBtn) {
					this.captionToggleBtn.classList.remove('active');
					this.captionToggleBtn.setAttribute('aria-pressed', 'false');
				}
			}

			const selectedLanguage = this.updateTranscriptLanguageOptions(
				video,
				(this.state.transcriptLanguage && this.state.transcriptLanguage[video.id]) || 'en'
			);
			this.state.transcriptLanguage = this.state.transcriptLanguage || {};
			this.state.transcriptLanguage[video.id] = selectedLanguage;
			this.saveState();

			this.highlightPlaylistActive();
			this.pendingChapters = Array.isArray(video.chapters) ? [...video.chapters] : [];
			this.renderChapters();
			this.loadTranscript(video.id, selectedLanguage);
		}

		renderPlaylist() {
			if (!this.playlistContainer) return;

			const fragment = document.createDocumentFragment();
			this.playlistContainer.innerHTML = '';
			const playlistCountBadge = document.getElementById('playlistCount');
			if (playlistCountBadge) {
				playlistCountBadge.textContent = String(this.videos.length || 0);
			}

			this.videos.forEach((video, index) => {
				const item = document.createElement('button');
				item.type = 'button';
				item.className = 'list-group-item list-group-item-action text-start';
				item.dataset.videoIndex = String(index);
				const thumbSrc = video.thumbnail || video.poster || '';
				const safeSubtitle = video.subtitle ? `<p class="mb-1 small text-muted">${video.subtitle}</p>` : '';
				item.innerHTML = `
							<div class="d-flex align-items-start">
								<div class="position-relative me-3 flex-shrink-0 video-playlist-thumb">
									<div class="ratio ratio-16x9 rounded overflow-hidden bg-body-secondary">
							${thumbSrc ? `<img src="${thumbSrc}" class="img-fluid" alt="${video.title} poster">` : ''}
							</div>
						</div>
						<div class="flex-grow-1">
							<div class="d-flex justify-content-between align-items-start">
								<h6 class="mb-1 fw-semibold">${index + 1}. ${video.title}</h6>
								${video.isNew ? '<span class="badge bg-primary rounded-pill">New</span>' : ''}
							</div>
						${safeSubtitle}
							<div class="progress progress-thin" role="progressbar" aria-label="Video progress">
								<div class="progress-bar bg-success" data-playlist-progress="${video.id}"></div>
							</div>
							<small class="text-muted" data-playlist-progress-label="${video.id}">0% watched</small>
						</div>
					</div>
				`;

				item.addEventListener('click', () => this.loadVideo(index));
				fragment.appendChild(item);
			});

			this.playlistContainer.appendChild(fragment);
			this.playlistContainer.removeAttribute('data-playlist-empty');
			this.highlightPlaylistActive();
		}

			highlightPlaylistActive() {
				if (!this.playlistContainer) return;
				this.playlistContainer.querySelectorAll('button.list-group-item').forEach((item) => {
					const index = Number(item.dataset.videoIndex);
					item.classList.toggle('active', index === this.currentVideoIndex);
				});

				this.updatePlaylistProgress();
			}

			renderChapters(chapters) {
				if (!this.chapterTrackEl) return;

				const chapterList = Array.isArray(chapters) && chapters.length ? chapters : this.pendingChapters;
				this.chapterTrackEl.innerHTML = '';

				const duration = this.videoEl?.duration || this.videos[this.currentVideoIndex]?.duration || 0;
				if (!chapterList.length || !duration) {
					return;
				}

				chapterList.forEach((chapter) => {
					const marker = document.createElement('span');
					marker.className = 'chapter-marker';
					marker.dataset.title = chapter.title;
					const clampedTime = Math.max(0, Math.min(chapter.time, duration));
					marker.style.left = `${(clampedTime / duration) * 100}%`;
					marker.tabIndex = 0;
					marker.setAttribute('role', 'button');
					marker.setAttribute('aria-label', `Jump to ${chapter.title}`);
					marker.addEventListener('click', () => {
						if (!this.videoEl) return;
						this.videoEl.currentTime = chapter.time;
						this.videoEl.focus();
					});
					marker.addEventListener('keyup', (event) => {
						if (event.key === 'Enter' || event.key === ' ') {
							event.preventDefault();
							marker.click();
						}
					});

					this.chapterTrackEl.appendChild(marker);
				});
			}

		async loadTranscript(videoId, languageCode) {
			if (!this.transcriptContainer) return;

			const emptyState = this.transcriptContainer.querySelector('[data-transcript-empty]');
			if (emptyState) {
				emptyState.innerHTML = `
					<div class="spinner-border text-primary mb-3" role="status">
						<span class="visually-hidden">Loading transcript...</span>
					</div>
					<p class="mb-0">Fetching transcript&hellip;</p>
				`;
			}

			try {
				const url = languageCode ? `/api/transcripts/${videoId}?lang=${languageCode}` : `/api/transcripts/${videoId}`;
				const response = await fetch(url, { credentials: 'same-origin' });
				if (!response.ok) {
					throw new Error(`Transcript request failed with status ${response.status}`);
				}

				const transcript = await response.json();
				this.renderTranscript(transcript);
			} catch (error) {
				console.error('[VideoPlayer] Transcript fetch failed', error);
				this.renderTranscriptError();
			}
		}

		updateTranscriptLanguageOptions(video, preferredLanguage) {
			if (!this.transcriptLangSelect) return 'en';
			const languages = Array.isArray(video.languages) && video.languages.length ? video.languages : ['en'];
			const normalized = languages.map((code) => (typeof code === 'string' ? code.toLowerCase() : 'en'));
			const preferred = (preferredLanguage || '').toLowerCase();
			let selected = normalized.includes(preferred) ? preferred : normalized[0];
			this.transcriptLangSelect.innerHTML = '';
			normalized.forEach((code) => {
				const option = document.createElement('option');
				option.value = code;
				option.textContent = this.getLanguageLabel(code);
				if (code === selected) {
					option.selected = true;
				}
				this.transcriptLangSelect.appendChild(option);
			});
			return selected;
		}

		getLanguageLabel(code) {
			switch (code) {
				case 'en':
					return 'English';
				case 'es':
					return 'Spanish';
				case 'fr':
					return 'French';
				case 'de':
					return 'German';
				case 'pt':
					return 'Portuguese';
				default:
					return code.toUpperCase();
			}
		}

		renderTranscript(transcript) {
			if (!this.transcriptContainer) return;

			this.transcriptContainer.innerHTML = '';
			this.transcriptWords = [];
			this.transcriptBlocks = [];

			if (!transcript?.blocks?.length) {
				this.transcriptContainer.innerHTML = '<p class="text-center text-muted mb-0">Transcript unavailable.</p>';
				return;
			}

			const fragment = document.createDocumentFragment();

			transcript.blocks.forEach((block) => {
				const blockEl = document.createElement('div');
				blockEl.className = 'transcript-block mb-3';
				blockEl.dataset.start = block.start;
				blockEl.dataset.end = block.end;

				const timeEl = document.createElement('p');
				timeEl.className = 'mb-2';
				timeEl.innerHTML = `<small class="text-muted fw-semibold">${this.formatTime(block.start)}</small>`;

				const textEl = document.createElement('p');
				textEl.className = 'lh-lg mb-0';

				(block.words || []).forEach((word) => {
					const wordEl = document.createElement('span');
					wordEl.className = 'transcript-word';
					wordEl.dataset.start = word.start;
					wordEl.dataset.end = word.end;
					wordEl.textContent = `${word.text} `;
					wordEl.tabIndex = 0;
					wordEl.addEventListener('click', () => this.seekTo(word.start));
					wordEl.addEventListener('keydown', (event) => {
						if (event.key === 'Enter' || event.key === ' ') {
							event.preventDefault();
							this.seekTo(word.start);
						}
					});
					textEl.appendChild(wordEl);
					this.transcriptWords.push(wordEl);
				});

				blockEl.appendChild(timeEl);
				blockEl.appendChild(textEl);
				fragment.appendChild(blockEl);
				this.transcriptBlocks.push(blockEl);
			});

			this.transcriptContainer.appendChild(fragment);
		}

		renderTranscriptError() {
			if (!this.transcriptContainer) return;
			this.transcriptContainer.innerHTML = `
				<div class="alert alert-warning" role="status">
					<i class="bi bi-exclamation-triangle me-1"></i>
					Unable to load transcript right now. Please try again later.
				</div>
			`;
		}

		seekTo(time) {
			if (!this.videoEl) return;
			this.videoEl.currentTime = time;
			this.videoEl.play().catch(() => {
				/* ignore play interruption */
			});
		}

		handleTimeUpdate() {
			if (!this.videoEl) return;

			const { currentTime, duration } = this.videoEl;
			this.state.progress = {
				...this.state.progress,
				[this.videos[this.currentVideoIndex].id]: currentTime
			};
			this.saveState();
			this.updateProgressLabel(currentTime, duration);
			this.updateTranscriptHighlight(currentTime);
			this.updatePlaylistProgress();
		}

		updateProgressLabel(currentTime, duration) {
			if (!this.progressLabelEl || !this.progressBarEl || !duration) return;

			const percent = Math.min(100, (currentTime / duration) * 100);
			this.progressBarEl.style.width = `${percent}%`;
			this.progressLabelEl.textContent = `${this.formatTime(currentTime)} / ${this.formatTime(duration)}`;
		}

		updateTranscriptHighlight(currentTime) {
			if (!this.transcriptWords.length) return;

			this.transcriptWords.forEach((word) => {
				const start = parseFloat(word.dataset.start);
				const end = parseFloat(word.dataset.end);
				if (currentTime >= start && currentTime <= end) {
					word.classList.add('active');
					if (this.state.autoScroll) {
						word.scrollIntoView({ behavior: 'smooth', block: 'center' });
					}
				} else {
					word.classList.remove('active');
				}
			});

			this.transcriptBlocks.forEach((block) => {
				const start = parseFloat(block.dataset.start);
				const end = parseFloat(block.dataset.end);
				block.classList.toggle('active', currentTime >= start && currentTime <= end);
			});
		}

		updatePlaylistProgress() {
			if (!this.playlistContainer) return;

			this.videos.forEach((video) => {
				const progressBar = this.playlistContainer.querySelector(`[data-playlist-progress="${video.id}"]`);
				const progressLabel = this.playlistContainer.querySelector(`[data-playlist-progress-label="${video.id}"]`);

				if (!progressBar || !progressLabel) return;

				const progressSeconds = this.state.progress?.[video.id] ?? 0;
				const duration = video.duration || this.videoEl?.duration || 0;
				const percent = duration ? Math.min(100, Math.round((progressSeconds / duration) * 100)) : 0;

				progressBar.style.width = `${percent}%`;
				progressLabel.textContent = percent ? `${percent}% watched` : 'Not started';
			});
		}

		handleLoadedMetadata() {
			if (!this.videoEl) return;
			const video = this.videos[this.currentVideoIndex];
			video.duration = this.videoEl.duration;
			this.updatePlaylistProgress();
			this.renderChapters(video.chapters || []);
		}

		handleEnded() {
			if (this.state.autoplay) {
				this.loadVideo(this.getNextIndex());
				if (this.videoEl) {
					this.videoEl.play().catch(() => {/* autoplay might be blocked */});
				}
			}
		}

		handleKeydown(event) {
			if (!this.videoEl) return;
			const activeElement = document.activeElement;
			if (activeElement && ['INPUT', 'TEXTAREA'].includes(activeElement.tagName)) {
				return;
			}

			switch (event.key) {
				case ' ':
					event.preventDefault();
					if (this.videoEl.paused) {
						this.videoEl.play();
					} else {
						this.videoEl.pause();
					}
					break;
				case 'ArrowRight':
					this.videoEl.currentTime = Math.min(this.videoEl.currentTime + 5, this.videoEl.duration);
					break;
				case 'ArrowLeft':
					this.videoEl.currentTime = Math.max(this.videoEl.currentTime - 5, 0);
					break;
				case 'ArrowUp':
					this.videoEl.volume = Math.min(1, this.videoEl.volume + 0.1);
					break;
				case 'ArrowDown':
					this.videoEl.volume = Math.max(0, this.videoEl.volume - 0.1);
					break;
				case 'c':
					this.toggleCaptions();
					break;
				case 'f':
					this.toggleFullscreen();
					break;
				case 'n':
					this.loadVideo(this.getNextIndex());
					break;
				case 'p':
					this.loadVideo(this.getPrevIndex());
					break;
				default:
					break;
			}
		}

		toggleCaptions() {
			if (!this.captionToggleBtn || !this.videoEl) return;
			const textTrack = this.videoEl.textTracks && this.videoEl.textTracks[0];
			const isActive = this.captionToggleBtn.classList.toggle('active');
			this.captionToggleBtn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
			if (textTrack) {
				textTrack.mode = isActive ? 'showing' : 'hidden';
			}

			if (!isActive && this.ccOverlay) {
				this.ccOverlay.classList.add('d-none');
			}
		}

		cycleSpeed() {
			const idx = this.speedSteps.indexOf(this.state.playbackSpeed);
			const nextIndex = idx >= 0 ? (idx + 1) % this.speedSteps.length : 1;
			this.state.playbackSpeed = this.speedSteps[nextIndex];
			this.reflectState();
			this.saveState();
			if (this.videoEl) {
				this.videoEl.playbackRate = this.state.playbackSpeed;
			}
		}

		toggleFullscreen() {
			if (!document.fullscreenElement) {
				this.root.requestFullscreen?.().catch(() => this.videoEl?.requestFullscreen?.());
			} else {
				document.exitFullscreen?.();
			}
		}

		async handleTranscriptDownload() {
			try {
				const transcripts = await Promise.all(
					this.videos.map((video) => fetch(video.transcriptEndpoint).then((res) => res.json()))
				);

				const blob = new Blob([JSON.stringify(transcripts, null, 2)], { type: 'application/json' });
				const url = URL.createObjectURL(blob);
				const anchor = document.createElement('a');
				anchor.href = url;
				anchor.download = 'brainstormx-transcripts.json';
				anchor.click();
				URL.revokeObjectURL(url);
			} catch (error) {
				console.error('[VideoPlayer] Transcript download failed', error);
				alert('Unable to download transcripts right now. Please try again later.');
			}
		}

		async copyTranscript() {
			if (!navigator.clipboard || !this.transcriptContainer) return;
			try {
				const text = this.transcriptContainer.innerText.trim();
				await navigator.clipboard.writeText(text);
				this.showToast('Transcript copied to clipboard');
			} catch (error) {
				console.error('[VideoPlayer] Clipboard copy failed', error);
			}
		}

		promptTranscriptSearch() {
			if (!this.transcriptWords.length) return;
			const term = window.prompt('Search transcript for:');
			if (!term) return;

			const match = this.transcriptWords.find((word) => word.textContent.toLowerCase().includes(term.toLowerCase()));
			if (match) {
				match.scrollIntoView({ behavior: 'smooth', block: 'center' });
				match.classList.add('active');
			} else {
				alert('No matches found in transcript.');
			}
		}

		showToast(message) {
			const toastContainer = document.getElementById('notification-area');
			if (!toastContainer) {
				console.info(message);
				return;
			}

			const toast = document.createElement('div');
			toast.className = 'toast align-items-center text-bg-dark border-0 flash-toast';
			toast.role = 'status';
			toast.innerHTML = `
				<div class="d-flex">
					<div class="toast-body">${message}</div>
					<button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
				</div>
			`;

			toastContainer.appendChild(toast);
			if (window.bootstrap?.Toast) {
				const toastInstance = new window.bootstrap.Toast(toast, { delay: 2500 });
				toastInstance.show();
				toast.addEventListener('hidden.bs.toast', () => toast.remove());
			} else {
				setTimeout(() => toast.remove(), 3000);
			}
		}

		formatTime(totalSeconds) {
			if (!Number.isFinite(totalSeconds)) return '0:00';
			const minutes = Math.floor(totalSeconds / 60);
			const seconds = Math.floor(totalSeconds % 60);
			return `${minutes}:${seconds.toString().padStart(2, '0')}`;
		}
	}

	document.addEventListener('DOMContentLoaded', () => {
		const playerRoot = document.querySelector('[data-video-player]');
		if (!playerRoot) return;

		const datasetScript = document.getElementById('videoPlayerData');
		let playlist = [];
		if (datasetScript?.textContent) {
			try {
				const parsed = JSON.parse(datasetScript.textContent);
				playlist = parsed?.videos ?? [];
			} catch (error) {
				console.warn('[VideoPlayer] Failed to parse embedded playlist data', error);
			}
		}

		window.videoPlayer = new VideoPlayerManager(playerRoot, playlist);
	});
})();
