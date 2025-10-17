(function (global) {
	const WarmupUI = global.WarmupUI || {};
	const state = {
		modalEl: null,
		modalInstance: null,
	};

	const escapeHtml = global.escapeHtml || function (value) {
		if (value === null || value === undefined) return '';
		return String(value)
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;')
			.replace(/'/g, '&#39;');
	};

	function dismissModal() {
		try {
			if (state.modalInstance && typeof state.modalInstance.hide === 'function') {
				state.modalInstance.hide();
			}
		} catch (_) { /* no-op */ }
		if (state.modalEl) {
			try {
				state.modalEl.remove();
			} catch (_) { /* no-op */ }
		}
		state.modalEl = null;
		state.modalInstance = null;
	}

	WarmupUI.dismissModal = dismissModal;

	WarmupUI.showOptions = function showOptions() {
		const options = global.warmupOptions;
		const isOrganizer = !!global.isOrganizer;
		if (!isOrganizer || !Array.isArray(options) || !options.length) {
			return;
		}

		dismissModal();

		const modalEl = document.createElement('div');
		modalEl.className = 'modal fade';
		modalEl.innerHTML = `
			<div class="modal-dialog modal-lg">
				<div class="modal-content">
					<div class="modal-header">
						<h5 class="modal-title">Warm-Up Options</h5>
						<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
					</div>
					<div class="modal-body">
						${options.map((opt, idx) => {
							const isActive = idx === (global.selectedWarmupIndex ?? 0);
							const mode = (opt && opt.mode) || 'solo';
							const modeIcon = mode === 'pairs' ? 'people' : (mode === 'groups' ? 'people-fill' : 'person');
							const duration = typeof opt?.timer_sec === 'number' ? `${opt.timer_sec}s` : '90s';
							const energy = opt?.energy_level || 'medium';
							return `
								<div class="card mb-2 ${isActive ? 'border-primary shadow-sm' : ''}">
									<div class="card-body">
										<div class="d-flex justify-content-between align-items-start gap-3">
											<div>
												<h6 class="card-title mb-1">${escapeHtml(opt?.title || `Option ${idx + 1}`)}</h6>
												<p class="card-text mb-2">${escapeHtml(opt?.prompt || '')}</p>
												<small class="text-muted d-flex align-items-center gap-3 flex-wrap">
													<span><i class="bi bi-clock"></i> ${duration}</span>
													<span><i class="bi bi-${modeIcon}"></i> ${escapeHtml(mode)}</span>
													<span><i class="bi bi-lightning"></i> ${escapeHtml(energy)} energy</span>
												</small>
											</div>
											${isActive
												? '<span class="badge bg-primary">Active</span>'
												: `<button class="btn btn-sm btn-outline-primary" data-warmup-index="${idx}">Select</button>`}
										</div>
									</div>
								</div>`;
						}).join('')}
					</div>
				</div>
			</div>`;

		document.body.appendChild(modalEl);
		try {
			state.modalInstance = new bootstrap.Modal(modalEl, { backdrop: 'static', keyboard: true });
		} catch (err) {
			console.warn('[WarmUp] Unable to open modal:', err);
			try { modalEl.remove(); } catch (_) { /* no-op */ }
			state.modalInstance = null;
			state.modalEl = null;
			return;
		}
		state.modalEl = modalEl;
		modalEl.addEventListener('click', (evt) => {
			const target = evt.target.closest('button[data-warmup-index]');
			if (target) {
				const idx = Number(target.getAttribute('data-warmup-index'));
				WarmupUI.switchOption(idx);
			}
		});
		modalEl.addEventListener('hidden.bs.modal', () => {
			if (modalEl === state.modalEl) {
				state.modalInstance = null;
				state.modalEl = null;
			}
			setTimeout(() => {
				try { modalEl.remove(); } catch (_) { /* no-op */ }
			}, 200);
		});
		state.modalInstance.show();
	};

	WarmupUI.switchOption = async function switchOption(index) {
		const options = global.warmupOptions;
		const isOrganizer = !!global.isOrganizer;
		if (!isOrganizer || !Array.isArray(options) || !options.length) {
			return;
		}
		if (index === (global.selectedWarmupIndex ?? 0)) {
			return;
		}
		const workshopId = global.workshopId;
		if (!workshopId) {
			console.warn('[WarmUp] Cannot switch option without workshopId');
			return;
		}
		try {
			const response = await fetch(`/workshop/${workshopId}/switch_warmup`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					'X-Requested-With': 'XMLHttpRequest',
				},
				body: JSON.stringify({ option_index: index }),
			});
			if (!response.ok) {
				console.warn('[WarmUp] Failed to switch option:', await response.text());
				return;
			}
			const result = await response.json().catch(() => ({}));
			if (result && result.success) {
				global.selectedWarmupIndex = index;
				dismissModal();
			}
		} catch (error) {
			console.error('[WarmUp] Error switching option:', error);
		}
	};

	WarmupUI.hydrateFromCache = function hydrateFromCache(config) {
		const cfg = config || {};
		const workshopId = cfg.workshopId ?? global.workshopId;
		if (!workshopId) {
			return Promise.resolve({ ok: false, reason: 'missing-workshop' });
		}
		return fetch(`/workshop/${workshopId}/warmup_state`, {
			headers: {
				'X-Requested-With': 'XMLHttpRequest',
			},
		})
			.then((resp) => {
				if (!resp.ok) {
					return resp.text().then((text) => { throw new Error(text || `HTTP ${resp.status}`); });
				}
				return resp.json();
			})
			.then((data) => {
				if (!data || !data.active || !data.payload) {
					return { ok: true, applied: false };
				}
				const payload = data.payload;
				if (Array.isArray(payload.options)) {
					global.warmupOptions = payload.options;
				}
				if (typeof payload.selected_index === 'number') {
					global.selectedWarmupIndex = payload.selected_index;
				}
				if (typeof cfg.applyPayload === 'function') {
					cfg.applyPayload(payload);
				}
				return { ok: true, applied: true, payload };
			});
	};

	global.WarmupUI = WarmupUI;
	global.showWarmupOptions = WarmupUI.showOptions;
	global.switchWarmupOption = WarmupUI.switchOption;
})(window);