(function () {
  const root = document.getElementById('assistant-root');
  if (!root) return;

  const panel = document.getElementById('assistant-panel');
  const toggle = document.getElementById('assistant-toggle');
  const closeBtn = document.getElementById('assistant-close');
  const messagesEl = document.getElementById('assistant-messages');
  const personaButtons = document.getElementById('assistant-persona-buttons');
  const chipsEl = document.getElementById('assistant-chips');
  const contextEl = document.getElementById('assistant-context');
  const inputEl = document.getElementById('assistant-input');
  const sendBtn = document.getElementById('assistant-send');
  const titleEl = document.getElementById('assistant-title');
  const subtitleEl = document.getElementById('assistant-subtitle');
  const phaseEl = document.getElementById('assistant-phase');
  const timerEl = document.getElementById('assistant-timer');
  const timeboxBadge = document.getElementById('assistant-badge-timebox');
  const rbacBadge = document.getElementById('assistant-badge-rbac');
  const memoryBadge = document.getElementById('assistant-badge-memory');
  const personaBadge = document.getElementById('assistant-badge-persona');
  const autoplayToggle = document.getElementById('assistant-autoplay-toggle');
  const phaseSidebarEl = document.getElementById('assistant-phase-snapshot');
  const sidebarActionsEl = document.getElementById('assistant-sidebar-actions');
  const sidebarThreadsEl = document.getElementById('assistant-sidebar-threads');

  function coerceNumber(value) {
    if (value === undefined || value === null) return null;
    const trimmed = String(value).trim();
    if (!trimmed) return null;
    const lowered = trimmed.toLowerCase();
    if (lowered === 'none' || lowered === 'null' || lowered === 'undefined' || lowered === 'nan') return null;
    const parsed = Number(trimmed);
    return Number.isNaN(parsed) ? null : parsed;
  }

  const workshopId = coerceNumber(root.dataset.workshopId);
  if (workshopId === null) {
    console.warn('[Assistant] Missing workshop id on root element, aborting init');
    return;
  }
  const userId = coerceNumber(root.dataset.userId);
  const participantId = coerceNumber(root.dataset.participantId);
  const canAddActionItems = root.dataset.canAddActions === '1';

  const participantsScript = document.getElementById('assistant-participants-json');
  let participantsCache = null;

  function getAssistantParticipants() {
    if (participantsCache) return participantsCache;
    let parsed = [];
    if (participantsScript) {
      try {
        parsed = JSON.parse(participantsScript.textContent || '[]');
      } catch (error) {
        console.warn('[Assistant] Failed to parse participants payload', error);
      }
    }
    participantsCache = Array.isArray(parsed) ? parsed : [];
    return participantsCache;
  }

  function displayNameForParticipant(entry) {
    if (!entry || typeof entry !== 'object') return 'Participant';
    return entry.display_name || entry.email || 'Participant';
  }

  function populateOwnerOptions(selectEl, selectedId) {
    if (!selectEl) return;
    const data = getAssistantParticipants();
    const current = Number.isFinite(selectedId) ? Number(selectedId) : null;
    const options = ['<option value="">Unassigned</option>'];
    data.forEach((entry) => {
      if (!entry || typeof entry !== 'object') return;
      const value = Number(entry.id);
      if (!Number.isFinite(value)) return;
      const label = displayNameForParticipant(entry);
      const selected = current !== null && value === current ? ' selected' : '';
      options.push(`<option value="${value}"${selected}>${label}</option>`);
    });
    selectEl.innerHTML = options.join('');
  }

  function resolveParticipantIdForUser(targetUserId) {
    if (!Number.isFinite(targetUserId)) return null;
    const entries = getAssistantParticipants();
    for (const entry of entries) {
      if (!entry || typeof entry !== 'object') continue;
      if (Number(entry.user_id) === Number(targetUserId)) {
        const pid = Number(entry.id);
        return Number.isFinite(pid) ? pid : null;
      }
    }
    return null;
  }

  const BADGE_TONES = {
    neutral: ['bg-secondary-subtle', 'text-secondary-emphasis', 'border-secondary-subtle'],
    success: ['bg-success-subtle', 'text-success-emphasis', 'border-success-subtle'],
    info: ['bg-primary-subtle', 'text-primary-emphasis', 'border-primary-subtle'],
    warning: ['bg-warning-subtle', 'text-warning-emphasis', 'border-warning-subtle'],
  };

  const state = {
    threadId: root.dataset.threadId || null,
    persona: 'guide',
    streamingCard: null,
    streamingMeta: null,
    streamingWrapper: null,
    streamingBuffer: '',
    messages: [],
    workshopTitle: 'BrainStormX Assistant',
    headerPhase: '—',
    rbacRole: 'guest',
  };

  const speechState = {
    cards: new Map(),
    activeId: null,
    autoPlayed: new Set(),
    bound: false,
    autoplay: true,
  };

  const sidebarState = {
    captured: [],
    threads: [],
    phase: [],
    phaseNodes: {},
    phaseTimer: null,
  };

  const capturedActionKeys = new Set();

  const timerState = {
    remaining: null,
    display: null,
    intervalId: null,
    lastUpdated: null,
    active: false,
    lastPayload: null,
    paused: false,
  };

  const proposedActionsMap = new WeakMap();

  // ---- Threads: persistence helpers ----
  function activeThreadStorageKey() {
    const uid = Number.isFinite(userId) ? String(userId) : 'anon';
    return `assistant_active_thread:${workshopId}:${uid}`;
  }
  function persistActiveThreadId(id) {
    try {
      const key = activeThreadStorageKey();
      if (id) localStorage.setItem(key, String(id));
      else localStorage.removeItem(key);
    } catch (_) {}
  }
  function restoreActiveThreadId() {
    try {
      const raw = localStorage.getItem(activeThreadStorageKey());
      const num = raw != null ? Number(raw) : NaN;
      if (!Number.isNaN(num) && num > 0) return String(num);
    } catch (_) {}
    return null;
  }

  // Preload stored active thread if not provided by template
  if (!state.threadId) {
    const stored = restoreActiveThreadId();
    if (stored) state.threadId = stored;
  }

  const actionModalEl = document.getElementById('assistant-action-modal');
  const actionModalForm = document.getElementById('assistant-action-form');
  const actionModalSaveBtn = document.getElementById('assistant-action-save');
  const actionModalError = document.getElementById('assistant-action-error');
  let actionModalInstance = null;
  let currentActionContext = null;

  function ensureActionModal() {
    if (!actionModalInstance && actionModalEl && window.bootstrap) {
      try {
        actionModalInstance = new bootstrap.Modal(actionModalEl);
      } catch (error) {
        console.warn('[Assistant] Failed to initialise action modal', error);
      }
    }
    return actionModalInstance;
  }

  function formatDateForInput(value) {
    if (!value) return '';
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return '';
    const iso = dt.toISOString();
    return iso.slice(0, 10);
  }

  function humanizeActionString(value) {
    if (!value) return '';
    const trimmed = String(value).trim();
    if (!trimmed) return '';
    if (/\s/.test(trimmed)) return trimmed;
    const spaced = trimmed
      .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
      .replace(/[_-]+/g, ' ');
    const words = spaced.split(/\s+/).filter(Boolean);
    if (!words.length) return trimmed;
    return words
      .map((word) => {
        if (word.toUpperCase() === word && word.length <= 4) {
          return word;
        }
        return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
      })
      .join(' ');
  }

  function deriveActionTitle(rawTitle, description, idx) {
    const baseIdx = typeof idx === 'number' && Number.isFinite(idx) ? idx : null;
    const fallback = baseIdx !== null ? `Action ${baseIdx + 1}` : 'Action';
    const trimmedTitle = humanizeActionString(rawTitle || '');
    if (trimmedTitle && !/^action\s*\d+$/i.test(trimmedTitle)) {
      return trimmedTitle;
    }
    const detail = (description || '').trim();
    if (detail) {
      const sentence = detail.split(/(?<=[.!?])\s+/)[0] || detail;
      return sentence.length > 120 ? `${sentence.slice(0, 117)}…` : sentence;
    }
    return trimmedTitle || fallback;
  }

  function normalizeActionKey(title, description) {
    const cleanTitle = (title || '').trim().toLowerCase();
    let cleanDesc = (description || '').trim().toLowerCase();
    if (!cleanDesc || cleanDesc === cleanTitle) {
      cleanDesc = cleanTitle;
    }
    return `${cleanTitle}::${cleanDesc}`;
  }

  function uniqueActionEntries(entries) {
    const map = new Map();
    (entries || []).forEach((entry) => {
      if (!entry || typeof entry !== 'object') return;
      const key = normalizeActionKey(entry.title, entry.summary || entry.description || '');
      if (!map.has(key)) {
        map.set(key, entry);
      }
    });
    return Array.from(map.values());
  }

  function normalizeActionEntry(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const actionTextRaw = typeof raw.action === 'string' ? raw.action.trim() : '';
    const descriptionSource = raw.description ?? raw.summary ?? raw.text ?? (actionTextRaw && /\s/.test(actionTextRaw) ? actionTextRaw : '');
    let summary = String(descriptionSource || '').trim();
    const status = String(raw.status ?? 'todo').trim().toLowerCase() || 'todo';
    const due = raw.due_date || raw.dueDate || null;
    const titleSource = raw.title ?? raw.text ?? (summary ? '' : actionTextRaw) ?? '';
    let title = humanizeActionString(titleSource || '') || humanizeActionString(actionTextRaw) || 'Action';
    if (summary && summary.toLowerCase() === status) summary = '';
    return {
      title,
      summary,
      status,
      due_date: due,
      type: raw.type != null ? String(raw.type).trim() : '',
    };
  }

  if (actionModalEl) {
    actionModalEl.addEventListener('hidden.bs.modal', () => {
      if (actionModalError) actionModalError.classList.add('d-none');
      currentActionContext = null;
    });
  }

  if (actionModalSaveBtn) {
    actionModalSaveBtn.addEventListener('click', async () => {
      if (!actionModalForm || !currentActionContext) return;
      const modal = ensureActionModal();
      const formData = new FormData(actionModalForm);
      const title = String(formData.get('title') || '').trim();
      const description = String(formData.get('description') || '').trim();
      const dueRaw = String(formData.get('due_date') || '').trim();
      const status = String(formData.get('status') || 'todo').trim() || 'todo';
      const ownerVal = formData.get('owner_participant_id');
      if (!title) {
        if (actionModalError) {
          actionModalError.textContent = 'Title is required.';
          actionModalError.classList.remove('d-none');
        }
        return;
      }
      if (actionModalError) actionModalError.classList.add('d-none');

      const payload = {
        title,
        description: description || null,
        owner_participant_id: ownerVal ? Number(ownerVal) : null,
        due_date: dueRaw || null,
        status,
      };

      const originalLabel = actionModalSaveBtn.innerHTML;
      actionModalSaveBtn.disabled = true;
      actionModalSaveBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';

      let succeeded = false;
      const ctxSnapshot = currentActionContext ? { ...currentActionContext } : null;
      try {
        const response = await fetch(`/workshop/${workshopId}/action_items`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          let message = response.statusText;
          try {
            const data = await response.json();
            if (data && data.error) message = data.error;
          } catch (_) {
            message = await response.text();
          }
          throw new Error(message || 'Failed to create action item');
        }
        succeeded = true;
        if (modal) modal.hide();
        if (ctxSnapshot && ctxSnapshot.button) {
          ctxSnapshot.button.disabled = true;
          ctxSnapshot.button.classList.remove('btn-outline-primary');
          ctxSnapshot.button.classList.add('btn-success');
          ctxSnapshot.button.innerHTML = '<i class="bi bi-check-circle-fill me-1"></i>Added';
        }
        if (ctxSnapshot && ctxSnapshot.listItem && !ctxSnapshot.listItem.querySelector('.assistant-action-success')) {
          ctxSnapshot.listItem.insertAdjacentHTML(
            'beforeend',
            '<div class="small text-success mt-2 assistant-action-success"><i class="bi bi-check-circle me-1"></i>Action item captured</div>'
          );
        }
        if (ctxSnapshot && ctxSnapshot.info) {
          const entryTitle = payload.title || deriveActionTitle(ctxSnapshot.info.title, ctxSnapshot.info.description, ctxSnapshot.info.idx);
          const entrySummary = payload.description || ctxSnapshot.info.description || ctxSnapshot.info.summary || '';
          upsertCapturedAction({
            title: entryTitle,
            summary: entrySummary,
            type: ctxSnapshot.info.type || '',
            status: payload.status || 'todo',
            due_date: payload.due_date || null,
          });
        }
      } catch (error) {
        console.error(error);
        if (actionModalError) {
          actionModalError.textContent = error.message || 'Failed to create action item.';
          actionModalError.classList.remove('d-none');
        }
      } finally {
        actionModalSaveBtn.disabled = false;
        actionModalSaveBtn.innerHTML = originalLabel;
        if (succeeded) {
          currentActionContext = null;
        }
      }
    });
  }

  const PERSONA_STYLES = {
    guide: {
      label: 'Guide',
      badgeClass: 'assistant-persona-guide',
      icon: 'bi bi-stars',
      cardClass: 'assistant-turn-guide',
    },
    scribe: {
      label: 'Scribe',
      badgeClass: 'assistant-persona-scribe',
      icon: 'bi bi-stars',
      cardClass: 'assistant-turn-scribe',
    },
    mediator: {
      label: 'Mediator',
      badgeClass: 'assistant-persona-mediator',
      icon: 'bi bi-stars',
      cardClass: 'assistant-turn-mediator',
    },
    devil: {
      label: 'Devil',
      badgeClass: 'assistant-persona-devil',
      icon: 'bi bi-stars',
      cardClass: 'assistant-turn-devil',
    },
  };

  const defaultChips = [
    'Explain shortlist',
    'Summarize decisions',
    "Draft facilitator recap",
    "Generate devil's advocate",
  ];

  const PHASE_NAME_MAP = {
    'framing': 'Briefing',
    'briefing': 'Briefing',
    'warm-up': 'Warm-up',
    'warm_up': 'Warm-up',
    'warmup': 'Warm-up',
    'brainstorming': 'Ideas',
    'ideas': 'Ideas',
    'clustering_voting': 'Clustering',
    'clustering': 'Clustering',
    'results_feasibility': 'Feasibility',
    'feasibility': 'Feasibility',
    'results_prioritization': 'Prioritization',
    'prioritization': 'Prioritization',
    'results_action_plan': 'Action Plan',
    'action_plan': 'Action Plan',
    'discussion': 'Discussion',
    'summary': 'Summary',
  };

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderMarkdown(text) {
    if (!text) return '';
    let escaped = escapeHtml(text);
    escaped = escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/__(.+?)__/g, '<strong>$1</strong>');
    escaped = escaped.replace(/\*(.+?)\*/g, '<em>$1</em>');
    escaped = escaped.replace(/_(.+?)_/g, '<em>$1</em>');
    escaped = escaped.replace(/`(.+?)`/g, '<code>$1</code>');
    escaped = escaped.replace(/\[(.+?)\]\((https?:[^\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    const lines = escaped.split(/\r?\n/);
    let html = '';
    let inList = false;
    lines.forEach((line) => {
      const trimmed = line.trim();
      if (!trimmed) {
        if (inList) {
          html += '</ul>';
          inList = false;
        }
        html += '<p class="mb-2"></p>';
        return;
      }
      const listMatch = trimmed.match(/^[*-]\s+(.+)/);
      if (listMatch) {
        if (!inList) {
          html += '<ul class="mb-2">';
          inList = true;
        }
        html += `<li>${listMatch[1]}</li>`;
        return;
      }
      if (inList) {
        html += '</ul>';
        inList = false;
      }
      html += `<p>${trimmed}</p>`;
    });
    if (inList) html += '</ul>';
    return html;
  }

  function applyBadgeTone(el, tone) {
    if (!el) return;
    const all = Object.values(BADGE_TONES).flat();
    el.classList.remove(...all);
    const palette = BADGE_TONES[tone] || BADGE_TONES.neutral;
    el.classList.add(...palette);
  }

  function capitalize(word) {
    if (!word) return '';
    return word.charAt(0).toUpperCase() + word.slice(1);
  }

  function formatRole(role) {
    if (!role) return 'Guest';
    const normalized = String(role).trim().toLowerCase();
    const roleMap = {
      organizer: 'Organizer',
      participant: 'Participant',
      facilitator: 'Facilitator',
      admin: 'Admin',
      guest: 'Guest',
    };
    if (roleMap[normalized]) return roleMap[normalized];
    return normalized.replace(/\b\w/g, (ch) => ch.toUpperCase());
  }

  function formatPhaseName(raw) {
    if (!raw) return '';
    const trimmed = String(raw).trim();
    if (!trimmed) return '';
    const canonical = trimmed.toLowerCase();
    if (PHASE_NAME_MAP[canonical]) return PHASE_NAME_MAP[canonical];
    const simplified = canonical.replace(/\s+/g, '_').replace(/-+/g, '_');
    if (PHASE_NAME_MAP[simplified]) return PHASE_NAME_MAP[simplified];
    if (trimmed.includes(':')) {
      const prefix = trimmed.split(':')[0].trim();
      const prefixKey = prefix.toLowerCase().replace(/\s+/g, '_').replace(/-+/g, '_');
      if (PHASE_NAME_MAP[prefixKey]) return PHASE_NAME_MAP[prefixKey];
      return prefix;
    }
    return trimmed.replace(/\b\w/g, (ch) => ch.toUpperCase());
  }

  function setPersonaBadge(personaName, personaLabel) {
    if (!personaBadge || (!personaName && !personaLabel)) return;
    applyBadgeTone(personaBadge, 'info');
    const label = personaBadge.querySelector('.label');
    const value = personaBadge.querySelector('.value');
    if (label) label.textContent = 'Persona';
    if (value) value.textContent = personaLabel || capitalize(personaName || state.persona);
  }

  function formatTimer(seconds, fallback) {
    if (seconds == null || Number.isNaN(Number(seconds))) return fallback || '—';
    const total = Math.max(0, Number(seconds));
    const minutes = Math.floor(total / 60);
    const secs = Math.floor(total % 60);
    return `${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
 
  function setHeaderInfo(payload) {
    const workshopTitle = payload?.workshop_title || state.workshopTitle || 'BrainStormX Assistant';
    state.workshopTitle = workshopTitle;
    const rawPhase = payload?.phase_label || payload?.phase;
    const formattedPhase = rawPhase ? formatPhaseName(rawPhase) : '';
    if (formattedPhase) {
      state.headerPhase = formattedPhase;
    } else if (!state.headerPhase) {
      state.headerPhase = '—';
    }
    if (titleEl) titleEl.textContent = workshopTitle;
    if (phaseEl) phaseEl.textContent = state.headerPhase && state.headerPhase !== '—' ? state.headerPhase : '—';
    updateTimerElements(timerState.display, payload);
  }

  function updateTimerElements(seconds, payload) {
    const basePayload = payload || timerState.lastPayload || {};
    let timerText = '—';
    let secondsValue = null;
    if (seconds != null && !Number.isNaN(Number(seconds))) {
      secondsValue = Number(seconds);
      timerText = formatTimer(secondsValue, '—');
    } else if (typeof basePayload.timer === 'string' && basePayload.timer.trim()) {
      timerText = basePayload.timer.trim();
    } else if (basePayload.timer_seconds != null && !Number.isNaN(Number(basePayload.timer_seconds))) {
      secondsValue = Number(basePayload.timer_seconds);
      timerText = formatTimer(secondsValue, '—');
    }
    if (timerEl) timerEl.textContent = timerText;
    renderSubtitle(timerText);
    if (timeboxBadge) {
      const badgeValue = timeboxBadge.querySelector('.value');
      if (badgeValue) {
        badgeValue.textContent = '';
        badgeValue.classList.add('visually-hidden');
      }
    }
    if (secondsValue != null) {
      timerState.display = secondsValue;
    } else if (!timerState.active) {
      timerState.display = null;
    }
    updatePhaseSnapshotTimer(timerText, secondsValue != null ? secondsValue : timerState.display);
  }

  function renderSubtitle(timerText) {
    if (!subtitleEl) return;
    const phasePart = state.headerPhase && state.headerPhase !== '—' ? state.headerPhase : '';
    const timerPart = timerText && timerText !== '—' ? `${timerText} left` : '';
    let text = 'Assistant ready';
    if (phasePart && timerPart) text = `${phasePart} – ${timerPart}`;
    else if (phasePart) text = phasePart;
    else if (timerPart) text = timerPart;
    subtitleEl.textContent = text;
  }

  function updatePhaseSnapshotTimer(timerText, seconds) {
    if (!sidebarState.phaseTimer || !sidebarState.phaseTimer.valueEl) return;
    sidebarState.phaseTimer.valueEl.textContent = timerText;
    if (typeof seconds === 'number' && !Number.isNaN(seconds)) {
      sidebarState.phaseTimer.seconds = seconds;
      sidebarState.phaseTimer.valueEl.dataset.seconds = String(seconds);
    }
  }

  function stopTimerCountdown(options = {}) {
    const { preserveDisplay = false, keepPausedState = false } = options;
    if (timerState.intervalId) {
      clearInterval(timerState.intervalId);
      timerState.intervalId = null;
    }
    timerState.active = false;
    if (!keepPausedState) timerState.paused = false;
    timerState.remaining = null;
    timerState.lastUpdated = null;
    if (!preserveDisplay) timerState.display = null;
  }

  function syncTimerState(payload) {
    timerState.lastPayload = payload || {};
    const rawSeconds = timerState.lastPayload.timer_seconds;
    const parsed = typeof rawSeconds === 'number' ? rawSeconds : rawSeconds != null ? Number(rawSeconds) : null;
    const validSeconds = parsed != null && !Number.isNaN(parsed);
    const paused = Boolean(timerState.lastPayload.timer_paused);
    timerState.paused = paused;

    if (!validSeconds) {
      stopTimerCountdown();
      updateTimerElements(null, timerState.lastPayload);
      return;
    }

    timerState.remaining = parsed;
    timerState.display = parsed;
    timerState.lastUpdated = Date.now();

    if (timerState.intervalId) {
      clearInterval(timerState.intervalId);
    }

    const shouldRun = Boolean(timerState.lastPayload.timebox_active) && !paused;
    if (!shouldRun) {
      stopTimerCountdown({ preserveDisplay: true, keepPausedState: paused });
      updateTimerElements(parsed, timerState.lastPayload);
      return;
    }

    timerState.active = true;
    timerState.intervalId = window.setInterval(() => {
      if (timerState.paused) {
        stopTimerCountdown({ preserveDisplay: true, keepPausedState: true });
        updateTimerElements(timerState.display, timerState.lastPayload);
        return;
      }
      const elapsed = Math.floor((Date.now() - timerState.lastUpdated) / 1000);
      const current = Math.max(Number(timerState.remaining) - elapsed, 0);
      timerState.display = current;
      updateTimerElements(current, timerState.lastPayload);
      if (current <= 0) {
        stopTimerCountdown();
      }
    }, 1000);

    updateTimerElements(parsed, timerState.lastPayload);
  }

  function renderPhaseSnapshot(items) {
    if (!phaseSidebarEl) return;
    sidebarState.phaseNodes = {};
    sidebarState.phaseTimer = null;
    if (!items || !items.length) {
      phaseSidebarEl.innerHTML = '<p class="text-body-secondary small mb-0">Assistant will surface key phase data here.</p>';
      return;
    }
    phaseSidebarEl.innerHTML = '';
    items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'd-flex justify-content-between align-items-start small';
      if (item.field) row.dataset.field = item.field;
      if (typeof item.remaining_seconds === 'number') {
        row.dataset.seconds = String(item.remaining_seconds);
      }

      const labelSpan = document.createElement('span');
      labelSpan.className = 'text-body-secondary';
      labelSpan.textContent = item.label || '';

      const valueText = document.createElement('span');
      valueText.className = 'fw-semibold';
      valueText.textContent = item.value || '';

      let valueContainer = valueText;
      if (item.badge) {
        const tone = item.tone || 'info';
        const badgeSpan = document.createElement('span');
        const toneClass = tone === 'danger'
          ? 'badge bg-danger-subtle text-danger-emphasis'
          : tone === 'success'
            ? 'badge bg-success-subtle text-success-emphasis'
            : 'badge bg-primary-subtle text-primary-emphasis';
        badgeSpan.className = `${toneClass} ms-2`;
        badgeSpan.textContent = item.badge;

        valueContainer = document.createElement('span');
        valueContainer.className = 'd-inline-flex align-items-center gap-2';
        valueContainer.appendChild(valueText);
        valueContainer.appendChild(badgeSpan);
      }

      const valueWrapper = document.createElement('span');
      valueWrapper.className = 'fw-semibold text-end';
      valueWrapper.appendChild(valueContainer);

      row.appendChild(labelSpan);
      row.appendChild(valueWrapper);
      phaseSidebarEl.appendChild(row);

      if (item.field) {
        sidebarState.phaseNodes[item.field] = {
          row,
          valueEl: valueText,
        };
      }
      if (item.field === 'time_remaining') {
        sidebarState.phaseTimer = {
          valueEl: valueText,
          seconds: typeof item.remaining_seconds === 'number' ? item.remaining_seconds : null,
        };
        if (typeof item.remaining_seconds === 'number') {
          valueText.dataset.seconds = String(item.remaining_seconds);
        }
      }
    });
  }

  function renderSidebarActions(actions) {
    if (!sidebarActionsEl) return;
    const list = Array.isArray(actions) ? actions : [];
    if (!list.length) {
      sidebarActionsEl.innerHTML = '<p class="text-body-secondary small mb-0">No assistant actions captured yet.</p>';
      return;
    }
    sidebarActionsEl.innerHTML = list
      .map((action) => {
        const titleString = action.title || 'Action';
        const title = escapeHtml(titleString);
        const detailRaw = action.summary || action.description || '';
        const detail = detailRaw && detailRaw.trim().toLowerCase() !== titleString.trim().toLowerCase()
          ? `<div class="text-body-secondary">${escapeHtml(detailRaw)}</div>`
          : '';
        const badges = [];
        if (action.status) badges.push(`<span class="badge bg-light text-uppercase">${escapeHtml(action.status)}</span>`);
        const due = action.due_date ? `<div class="text-body-secondary">Due: ${escapeHtml(action.due_date)}</div>` : '';
        return `<div class="border rounded-4 px-3 py-2 small d-flex flex-column gap-1">
          <div class="d-flex align-items-center gap-2 flex-wrap">
            <span class="fw-semibold">${title}</span>
            ${badges.join(' ')}
          </div>
          ${detail}
          ${due}
        </div>`;
      })
      .join('');
  }

  function renderCombinedSidebarActions() {
    renderSidebarActions(sidebarState.captured);
  }

  function renderSidebarThreads(threads) {
    if (!sidebarThreadsEl) return;
    const list = Array.isArray(threads) ? threads : [];
    const header = `
      <div class="d-flex justify-content-between align-items-center mb-2">
        <div class="small text-uppercase text-body-secondary">Threads</div>
        <div class="d-flex gap-2">
          <button class="btn btn-sm btn-outline-secondary" type="button" id="assistant-thread-new"><i class="bi bi-plus-lg me-1"></i>New</button>
        </div>
      </div>`;
    if (!list.length) {
      sidebarThreadsEl.innerHTML = `${header}<p class="text-body-secondary small mb-0">Your conversation history will surface here.</p>`;
      return;
    }
    const activeId = String(state.threadId || '');
    const items = list
      .map((thread) => {
        const tid = String(thread.id);
        const title = escapeHtml(thread.title || 'Assistant Thread');
        const updated = thread.updated_at ? `<span class="text-body-secondary">${escapeHtml(thread.updated_at)}</span>` : '';
        const isActive = activeId && tid === activeId;
        const activeClass = isActive ? 'border-primary' : 'border-light';
        const activeBg = isActive ? 'bg-primary-subtle' : 'bg-body';
        return `
          <div class="assistant-thread-item border rounded-4 px-3 py-2 small ${activeClass} ${activeBg} mb-2" data-thread-id="${tid}" role="button" tabindex="0" aria-pressed="${isActive ? 'true' : 'false'}">
            <div class="d-flex justify-content-between align-items-start gap-2">
              <div class="fw-semibold text-truncate">${title}</div>
              <div class="d-flex gap-1">
                <button type="button" class="btn btn-sm btn-link assistant-thread-rename" data-thread-id="${tid}"><i class="bi bi-pencil-square"></i><span class="visually-hidden">Rename</span></button>
                <button type="button" class="btn btn-sm btn-link text-danger assistant-thread-delete" data-thread-id="${tid}"><i class="bi bi-trash"></i><span class="visually-hidden">Delete</span></button>
              </div>
            </div>
            ${thread.last_author ? `<div class="text-body-secondary">Last: ${escapeHtml(thread.last_author)}</div>` : ''}
            ${updated}
          </div>`;
      })
      .join('');
    sidebarThreadsEl.innerHTML = `${header}${items}`;
  }

  // Simple toast utility (non-blocking)
  function showToast(message, variant = 'danger') {
    try {
      const host = document.createElement('div');
      host.className = `assistant-toast alert alert-${variant} position-fixed top-0 end-0 m-3 py-2 px-3 shadow`;
      host.style.zIndex = 1080;
      host.innerHTML = `<div class="d-flex align-items-center gap-2"><i class="bi bi-exclamation-triangle"></i><span class="small"></span></div>`;
      host.querySelector('span').textContent = String(message || 'Something went wrong');
      document.body.appendChild(host);
      setTimeout(() => { try { host.remove(); } catch(_) {} }, 3000);
    } catch (_) {
      console.warn('[Assistant] toast:', message);
    }
  }

  // Sidebar threads event delegation
  if (sidebarThreadsEl) {
    sidebarThreadsEl.addEventListener('click', async (event) => {
      const newBtn = event.target.closest('#assistant-thread-new');
      if (newBtn) {
        event.preventDefault();
        let title = window.prompt('Thread title', 'Assistant Thread');
        if (title == null) return;
        title = String(title).trim().slice(0, 200) || 'Assistant Thread';
        try {
          const res = await fetch('/assistant/threads', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ workshop_id: workshopId, user_id: userId, title }),
          });
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText || 'Failed to create thread');
          const data = await res.json();
          state.threadId = String(data.id);
          persistActiveThreadId(state.threadId);
          messagesEl.innerHTML = '';
          await loadHistory();
        } catch (err) {
          console.error('[Assistant] create thread failed', err);
          showToast(err.message || 'Failed to create thread', 'danger');
        }
        return;
      }

      const renameBtn = event.target.closest('.assistant-thread-rename');
      if (renameBtn) {
        event.preventDefault();
        const tid = renameBtn.dataset.threadId;
        const current = (sidebarState.threads.find((t) => String(t.id) === String(tid)) || {}).title || 'Assistant Thread';
        let title = window.prompt('Rename thread', current);
        if (title == null) return;
        title = String(title).trim().slice(0, 200);
        if (!title) return;
        try {
          const res = await fetch(`/assistant/threads/${encodeURIComponent(tid)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ title, user_id: userId }),
          });
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText || 'Failed to rename thread');
          // Refresh threads via history (keeps active selection)
          await loadHistory();
        } catch (err) {
          console.error('[Assistant] rename thread failed', err);
          showToast(err.message || 'Failed to rename thread', 'danger');
        }
        return;
      }

      const deleteBtn = event.target.closest('.assistant-thread-delete');
      if (deleteBtn) {
        event.preventDefault();
        const tid = deleteBtn.dataset.threadId;
        if (!window.confirm('Delete this thread? This cannot be undone.')) return;
        try {
          const res = await fetch(`/assistant/threads/${encodeURIComponent(tid)}?user_id=${encodeURIComponent(userId)}`, {
            method: 'DELETE',
            credentials: 'same-origin',
          });
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText || 'Failed to delete thread');
          // If deleting active, clear and let server pick latest on reload
          if (String(state.threadId || '') === String(tid)) {
            state.threadId = null;
            persistActiveThreadId(null);
            messagesEl.innerHTML = '';
          }
          await loadHistory();
        } catch (err) {
          console.error('[Assistant] delete thread failed', err);
          showToast(err.message || 'Failed to delete thread', 'danger');
        }
        return;
      }

      const item = event.target.closest('.assistant-thread-item');
      if (item) {
        event.preventDefault();
        const tid = item.dataset.threadId;
        if (!tid || String(state.threadId || '') === String(tid)) return;
        state.threadId = String(tid);
        persistActiveThreadId(state.threadId);
        messagesEl.innerHTML = '';
        await loadHistory();
      }
    });
  }

  function syncPersonaSelection(targetPersona) {
    if (!personaButtons) return;
    personaButtons.querySelectorAll('button[data-persona]').forEach((btn) => {
      const isActive = btn.dataset.persona === targetPersona;
      btn.classList.toggle('active', isActive);
      btn.classList.toggle('btn-outline-primary', isActive);
      btn.classList.toggle('btn-outline-secondary', !isActive);
    });
  }

  function togglePanel(open) {
    if (open) {
      panel.classList.add('open');
      toggle.classList.add('d-none');
      inputEl.focus();
    } else {
      panel.classList.remove('open');
      toggle.classList.remove('d-none');
    }
  }

  toggle.addEventListener('click', () => togglePanel(true));
  closeBtn.addEventListener('click', () => togglePanel(false));

  personaButtons.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-persona]');
    if (!btn) return;
    state.persona = btn.dataset.persona;
    syncPersonaSelection(state.persona);
  });

  function renderChips(chips) {
    const values = chips && chips.length ? chips : defaultChips;
    chipsEl.innerHTML = '';
    values.forEach((label) => {
      const btn = document.createElement('button');
      btn.className = 'btn btn-sm btn-outline-secondary rounded-pill';
      btn.textContent = label;
      btn.addEventListener('click', () => {
        inputEl.value = label;
        inputEl.focus();
      });
      chipsEl.appendChild(btn);
    });
  }

  renderChips(defaultChips);
  preloadCapturedActionItems();

  function renderContext(entries) {
    contextEl.innerHTML = '';
    if (!entries || !entries.length) {
      const empty = document.createElement('div');
      empty.className = 'small text-body-secondary';
      empty.textContent = 'Context updates will appear as the assistant works.';
      contextEl.appendChild(empty);
      return;
    }
    entries.forEach((entry) => {
      const card = document.createElement('div');
      card.className = 'border rounded p-2 bg-body-tertiary';
      card.innerHTML = `<div class="small fw-semibold mb-1">${escapeHtml(entry.title || 'Update')}</div>
        <div class="small text-body-secondary">${escapeHtml(entry.body || '')}</div>`;
      contextEl.appendChild(card);
    });
  }

  async function loadHistory() {
    try {
      const params = new URLSearchParams({ workshop_id: workshopId });
      if (typeof userId === 'number' && !Number.isNaN(userId)) {
        params.append('user_id', String(userId));
      }
      if (state.threadId) params.append('thread_id', state.threadId);
      const response = await fetch(`/assistant/history?${params.toString()}`, {
        credentials: 'same-origin',
      });
      if (!response.ok) {
        // If the selected thread is stale or forbidden, clear it and retry once without thread_id
        if ((response.status === 403 || response.status === 404) && state.threadId) {
          console.warn('[Assistant] Clearing stale/forbidden thread and retrying history');
          state.threadId = null;
          persistActiveThreadId(null);
          messagesEl.innerHTML = '';
          try {
            const retryParams = new URLSearchParams({ workshop_id: workshopId });
            if (typeof userId === 'number' && !Number.isNaN(userId)) retryParams.append('user_id', String(userId));
            const retryRes = await fetch(`/assistant/history?${retryParams.toString()}`, { credentials: 'same-origin' });
            if (!retryRes.ok) {
              console.error('[Assistant] History retry failed', retryRes.status, retryRes.statusText);
              showToast('Could not load assistant history.', 'danger');
              return;
            }
            const retryData = await retryRes.json();
            if (retryData?.thread_id) {
              state.threadId = retryData.thread_id;
              persistActiveThreadId(state.threadId);
            }
            if (Array.isArray(retryData?.messages)) retryData.messages.forEach(renderHistoricalTurn);
            updateStatusBadges(retryData || {});
            if (retryData?.persona) {
              state.persona = retryData.persona;
              syncPersonaSelection(state.persona);
              setPersonaBadge(retryData.persona, retryData.persona_label);
            }
            sidebarState.phase = retryData?.phase_snapshot || [];
            capturedActionKeys.clear();
            sidebarState.captured = [];
            const retryActions = Array.isArray(retryData?.sidebar?.actions) ? retryData.sidebar.actions : [];
            for (let i = retryActions.length - 1; i >= 0; i -= 1) {
              upsertCapturedAction(retryActions[i], { render: false });
            }
            sidebarState.threads = retryData?.sidebar?.threads || [];
            renderPhaseSnapshot(sidebarState.phase);
            renderCombinedSidebarActions();
            renderSidebarThreads(sidebarState.threads);
            showToast('Your previous thread was unavailable. Started a new one.', 'warning');
            return;
          } catch (e) {
            console.error('[Assistant] History retry exception', e);
            showToast('Could not load assistant history.', 'danger');
            return;
          }
        }
        // Non-recoverable
        console.error('[Assistant] History request failed', response.status, response.statusText);
        showToast('Failed to load assistant history.', 'danger');
        return;
      }
      const data = await response.json();
      if (data?.thread_id) state.threadId = data.thread_id;
      if (state.threadId) persistActiveThreadId(state.threadId);
      if (Array.isArray(data?.messages)) {
        data.messages.forEach(renderHistoricalTurn);
      }
      updateStatusBadges(data || {});
      if (data?.persona) {
        state.persona = data.persona;
        syncPersonaSelection(state.persona);
        setPersonaBadge(data.persona, data.persona_label);
      }
      sidebarState.phase = data?.phase_snapshot || [];
      capturedActionKeys.clear();
      sidebarState.captured = [];
      const sidebarActions = Array.isArray(data?.sidebar?.actions) ? data.sidebar.actions : [];
      for (let i = sidebarActions.length - 1; i >= 0; i -= 1) {
        upsertCapturedAction(sidebarActions[i], { render: false });
      }
      sidebarState.threads = data?.sidebar?.threads || [];
      renderPhaseSnapshot(sidebarState.phase);
      renderCombinedSidebarActions();
      renderSidebarThreads(sidebarState.threads);
    } catch (err) {
      console.error('[Assistant] Failed to load history', err);
      showToast('Failed to load assistant history.', 'danger');
    }
  }

  function renderHistoricalTurn(turn) {
    if (!turn) return;
    if (turn.role === 'user') {
      createMessageCard({
        role: 'user',
        author: turn.user_id === userId ? 'You' : 'Participant',
        html: renderMarkdown(turn.content || ''),
      });
      return;
    }
    if (turn.role === 'assistant') {
      const payload = turn.payload || {};
      const persona = payload.persona || turn.persona || 'assistant';
      const html = renderMarkdown(payload.text || turn.content || '');
      const turnView = createMessageCard({ role: 'assistant', persona, html });
      if (Array.isArray(payload.tool_results)) {
        payload.tool_results.forEach((result) => appendToolResult(result, turnView.card));
      }
      if (Array.isArray(payload.proposed_actions) && payload.proposed_actions.length) {
        renderProposedActions(payload.proposed_actions, turnView.card);
      }
      const buttons = extractActionButtons(payload.ui_hints || payload);
      if (buttons.length) {
        renderActionButtons(buttons, turnView.card);
      }
      if (Array.isArray(payload.citations) && payload.citations.length) {
        renderCitations(payload.citations, turnView.card);
      }
      if (turn.id) {
        turnView.card.dataset.turnId = turn.id;
        wireFeedbackHandlers(turnView.card, turn.id);
      }
      setupSpeechControls(payload, turnView.card, turn.id, { autoplay: false });
    }
  }

  function updateStatusBadges(ack) {
    setHeaderInfo(ack);
    syncTimerState(ack);
    if (timeboxBadge) {
      const active = Boolean(ack?.timebox_active);
      const paused = Boolean(ack?.timer_paused);
      applyBadgeTone(timeboxBadge, active ? 'success' : paused ? 'warning' : 'neutral');
      const label = timeboxBadge.querySelector('.label');
      const value = timeboxBadge.querySelector('.value');
      if (label) label.textContent = paused ? 'Timebox paused' : active ? 'Timebox active' : 'Timebox idle';
      if (value) {
        value.textContent = '';
        value.classList.add('visually-hidden');
      }
    }
    const roleFromPayload = ack?.rbac?.role;
    if (typeof roleFromPayload === 'string' && roleFromPayload.trim()) {
      state.rbacRole = roleFromPayload;
    }
    if (rbacBadge && state.rbacRole) {
      const roleLabel = formatRole(state.rbacRole);
      applyBadgeTone(rbacBadge, roleLabel === 'Organizer' || roleLabel === 'Admin' ? 'info' : 'neutral');
      const label = rbacBadge.querySelector('.label');
      const value = rbacBadge.querySelector('.value');
      if (label) label.textContent = 'RBAC';
      if (value) value.textContent = roleLabel;
    }
    if (memoryBadge) {
      const mem = ack?.meta?.memory || ack?.memory || ack?.memory_hits || {};
      const count = Number(mem.count || mem.total || 0);
      const namespaces = Array.isArray(mem.namespaces) ? mem.namespaces : [];
      const label = memoryBadge.querySelector('.label');
      const value = memoryBadge.querySelector('.value');
      if (label) label.textContent = 'Memory';
      if (value) value.textContent = String(count);
      const tone = count > 0 ? 'info' : 'neutral';
      applyBadgeTone(memoryBadge, tone);
      memoryBadge.title = namespaces.join(', ');
      memoryBadge.classList.toggle('d-none', false);
    }
    setPersonaBadge(ack?.persona, ack?.persona_label);
  }

  function setTurnStatus(card, options = {}) {
    const status = card?.querySelector('.assistant-turn-status');
    if (!status) return;
    const textEl = status.querySelector('.assistant-turn-status-text');
    const spinner = status.querySelector('.assistant-turn-status-spinner');
    const { text = '', tone = 'neutral', showSpinner = false } = options;
    const visible = Boolean(text);
    status.classList.toggle('d-none', !visible);
    status.classList.toggle('d-inline-flex', visible);
    status.classList.toggle('text-bg-light', tone === 'neutral');
    status.classList.toggle('text-bg-warning-subtle', tone === 'warning');
    status.classList.toggle('text-bg-danger-subtle', tone === 'danger');
    status.classList.toggle('text-bg-success-subtle', tone === 'success');
    if (textEl) textEl.textContent = text;
    if (spinner) spinner.classList.toggle('d-none', !showSpinner);
  }

  function createMessageCard({ role, persona, author, html }) {
    const wrapper = document.createElement('article');
    wrapper.className = `assistant-turn ${role === 'assistant' ? 'assistant-turn-assistant' : 'assistant-turn-user'} mb-3`;

    const personaInfo = PERSONA_STYLES[(persona || '').toLowerCase()] || PERSONA_STYLES.guide;
    const isAssistant = role === 'assistant';
    const badgeClass = isAssistant
      ? `assistant-persona-badge ${personaInfo.badgeClass}`
      : 'assistant-persona-badge assistant-badge-user';
    const badgeIcon = isAssistant ? personaInfo.icon : 'bi bi-person';
    const authorLabel = (author || 'Participant');
    const badgeLabel = isAssistant ? personaInfo.label : authorLabel;
    const showAuthorBadge = !isAssistant && authorLabel.trim().toLowerCase() !== 'you';
    const statusMarkup = role === 'assistant'
      ? '<span class="assistant-turn-status badge text-bg-light d-none align-items-center gap-2"><span class="assistant-turn-status-spinner spinner-border spinner-border-sm d-none" role="status" aria-hidden="true"></span><span class="assistant-turn-status-text">Thinking…</span></span>'
      : '';

    const card = document.createElement('div');
    const cardClasses = ['assistant-card', 'card'];
    if (role === 'assistant') {
      if (personaInfo.cardClass) cardClasses.push(personaInfo.cardClass);
      cardClasses.push('border-primary-subtle');
    } else {
      cardClasses.push('border-0', 'shadow-sm');
    }
    card.className = cardClasses.join(' ');
    card.innerHTML = `
      <div class="card-body p-4">
        <div class="assistant-turn-header d-flex justify-content-between align-items-start mb-3 ">
          <div class="d-flex align-items-center gap-2">
            <span class="${badgeClass}"><i class="${badgeIcon}"></i>${escapeHtml(badgeLabel)}</span>
            ${statusMarkup}
            ${showAuthorBadge ? `<span class="badge bg-light text-body-secondary text-uppercase">${escapeHtml(authorLabel)}</span>` : ''}
          </div>
          <div class="assistant-turn-header-extra d-flex align-items-center gap-2"></div>
        </div>
        <div class="assistant-turn-body small">${html || ''}</div>
        <div class="assistant-turn-meta mt-4"></div>
      </div>`;

    if (role === 'assistant') {
      const feedbackGroup = document.createElement('div');
      feedbackGroup.className = 'btn-group btn-group-sm assistant-feedback';
      feedbackGroup.innerHTML = `
        <button class="btn btn-outline-success" title="Helpful" data-rating="up" aria-label="Mark helpful"><i class="bi bi-hand-thumbs-up"></i></button>
        <button class="btn btn-outline-danger" title="Needs work" data-rating="down" aria-label="Mark needs work"><i class="bi bi-hand-thumbs-down"></i></button>
      `;
      card.querySelector('.assistant-turn-header-extra').appendChild(feedbackGroup);
    }

    wrapper.appendChild(card);
    messagesEl.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    return {
      wrapper,
      card,
      meta: card.querySelector('.assistant-turn-meta'),
      headerExtra: card.querySelector('.assistant-turn-header-extra'),
      status: card.querySelector('.assistant-turn-status'),
    };
  }

  function appendUserMessage(text) {
    createMessageCard({ role: 'user', author: 'You', html: renderMarkdown(text) });
  }

  function ensureAssistantCard(persona) {
    const turn = createMessageCard({ role: 'assistant', persona, html: '' });
    state.streamingCard = turn.card;
    state.streamingMeta = turn.meta;
    state.streamingWrapper = turn.wrapper;
    setTurnStatus(turn.card, { text: 'Thinking…', showSpinner: true, tone: 'neutral' });
    return turn;
  }

  function finalizeAssistantMessage(reply, meta) {
    if (!state.streamingCard) return;
    const body = state.streamingCard.querySelector('.assistant-turn-body');
    if (body) {
      const text = reply.text || '';
      body.innerHTML = renderMarkdown(text);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    if (reply.proposed_actions && reply.proposed_actions.length) {
      renderProposedActions(reply.proposed_actions, state.streamingCard);
    }
    if (reply.citations && reply.citations.length) {
      renderCitations(reply.citations, state.streamingCard);
    }
    const buttons = extractActionButtons(reply.ui_hints);
    if (buttons.length) {
      renderActionButtons(buttons, state.streamingCard);
    }
    const memoryMeta = meta?.meta?.memory || meta?.meta?.memory_hits;
    if (memoryMeta) {
      renderMemoryUsage(memoryMeta, state.streamingCard);
      // Update header badge immediately post-reply
      try { updateStatusBadges({ meta: { memory: memoryMeta } }); } catch (_) {}
    }
    let turnId = meta?.turn_id;
    if (turnId) {
      state.streamingCard.dataset.turnId = turnId;
      wireFeedbackHandlers(state.streamingCard, turnId);
    }
    setupSpeechControls(reply, state.streamingCard, turnId, { autoplay: true });
    setTurnStatus(state.streamingCard, { text: '', showSpinner: false });
  }

  function appendToolResult(result, targetCard = null) {
    const card = targetCard || state.streamingCard;
    if (!card) return;
    const meta = (targetCard && targetCard.querySelector('.assistant-turn-meta')) || state.streamingMeta || card.querySelector('.assistant-turn-meta');
    if (!meta) return;
    const row = document.createElement('div');
    row.className = 'small text-body-secondary d-flex justify-content-between border rounded p-2 bg-light mb-2';
    row.innerHTML = `<span><i class="bi bi-robot me-1"></i>${escapeHtml(result.name || result.tool)}</span>
      <span>${result.error ? '<span class="text-danger">error</span>' : '<span class="text-success">ok</span>'}${result.elapsed_ms ? ` · ${result.elapsed_ms}ms` : ''}</span>`;
    meta.appendChild(row);
  }
  
  // ---- Structured artifacts rendering (documents, etc.) ----
  function humanFileSize(bytes) {
    const num = Number(bytes);
    if (!Number.isFinite(num) || num < 0) return '';
    const thresh = 1024;
    if (num < thresh) return `${num} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let u = -1;
    let val = num;
    do {
      val /= thresh;
      u++;
    } while (val >= thresh && u < units.length - 1);
    return `${val.toFixed(val < 10 ? 1 : 0)} ${units[u]}`;
  }
  
  function formatUploadedAt(value) {
    if (!value) return '';
    try {
      const dt = new Date(value);
      if (Number.isNaN(dt.getTime())) return '';
      return dt.toLocaleString();
    } catch (_) {
      return '';
    }
  }
  
  function collectReportsFromResults(results) {
    const out = [];
    const seen = new Set();
    (Array.isArray(results) ? results : []).forEach((res) => {
      if (!res || typeof res !== 'object') return;
      const candidates = Array.isArray(res?.output?.reports)
        ? res.output.reports
        : Array.isArray(res?.data?.reports)
          ? res.data.reports
          : null;
      if (!Array.isArray(candidates) || !candidates.length) return;
      candidates.forEach((r) => {
        if (!r || typeof r !== 'object') return;
        const key = r.document_id != null ? `id:${r.document_id}` : r.url ? `url:${r.url}` : JSON.stringify(r);
        if (seen.has(key)) return;
        seen.add(key);
        out.push(r);
      });
    });
    return out;
  }
  
  function renderDocumentsSection(reports, targetCard = null) {
    const card = targetCard || state.streamingCard;
    if (!card) return;
    const meta = (targetCard && targetCard.querySelector('.assistant-turn-meta')) || state.streamingMeta || card.querySelector('.assistant-turn-meta');
    if (!meta) return;
    let section = meta.querySelector('.assistant-documents');
    const list = Array.isArray(reports) ? reports.slice() : [];
    if (!list.length) {
      if (section) section.remove();
      return;
    }
    // Sort by uploaded_at desc when available
    list.sort((a, b) => {
      const ta = a && a.uploaded_at ? Date.parse(a.uploaded_at) : 0;
      const tb = b && b.uploaded_at ? Date.parse(b.uploaded_at) : 0;
      return tb - ta;
    });
    if (!section) {
      section = document.createElement('div');
      section.className = 'assistant-documents card border-0 bg-body-secondary-subtle';
      meta.appendChild(section);
    }
    const itemsHtml = list.map((doc) => {
      const title = escapeHtml(doc.title || doc.file_name || 'Document');
      const href = escapeHtml(doc.url || '#');
      const phase = doc.phase ? formatPhaseName(doc.phase) : '';
      const size = humanFileSize(doc.file_size);
      const uploaded = formatUploadedAt(doc.uploaded_at);
      const desc = doc.description ? `<div class="small text-body-secondary">${escapeHtml(doc.description)}</div>` : '';
      const metaLineBits = [];
      if (size) metaLineBits.push(size);
      if (uploaded) metaLineBits.push(uploaded);
      const metaLine = metaLineBits.length ? `<div class="small text-body-secondary">${metaLineBits.join(' · ')}</div>` : '';
      const phaseBadge = phase ? `<span class="badge text-bg-light text-uppercase">${escapeHtml(phase)}</span>` : '';
      return `<li class="list-group-item px-0 border-0 bg-transparent">
        <div class="d-flex justify-content-between align-items-start gap-2">
          <a href="${href}" target="_blank" rel="noopener" class="fw-semibold">${title}</a>
          ${phaseBadge}
        </div>
        ${desc}
        ${metaLine}
      </li>`;
    }).join('');
    section.innerHTML = `
      <div class="card-body p-3">
        <div class="d-flex align-items-center gap-2 mb-2">
          <span class="badge text-bg-secondary"><i class="bi bi-file-earmark-pdf me-1"></i>Documents</span>
          <span class="small text-body-secondary">${list.length} item${list.length === 1 ? '' : 's'}</span>
        </div>
        <ul class="list-group list-group-flush">${itemsHtml}</ul>
      </div>`;
  }
  
  function renderDocumentsFromPayloadMeta(payloadOrMeta, targetCard = null) {
    if (!payloadOrMeta || typeof payloadOrMeta !== 'object') return;
    // Prefer structured tool_results if present
    const metaResults = Array.isArray(payloadOrMeta?.meta?.tool_results) ? payloadOrMeta.meta.tool_results : null;
    const directResults = Array.isArray(payloadOrMeta?.tool_results) ? payloadOrMeta.tool_results : null;
    const gatewayResults = Array.isArray(payloadOrMeta?.meta?.tool_gateway)
      ? payloadOrMeta.meta.tool_gateway.map((g) => ({ output: g }))
      : null;
    const combined = (metaResults || []).concat(directResults || []).concat(gatewayResults || []);
    const reports = collectReportsFromResults(combined);
    if (reports.length) renderDocumentsSection(reports, targetCard);
  }

  function renderProposedActions(actions, targetCard = null) {
    const card = targetCard || state.streamingCard;
    if (!card) return;
    const meta = (targetCard && targetCard.querySelector('.assistant-turn-meta')) || state.streamingMeta || card.querySelector('.assistant-turn-meta');
    if (!meta) return;
    let section = meta.querySelector('.assistant-actions');
    if (!section) {
      section = document.createElement('div');
      section.className = 'assistant-actions card border-0 bg-body-secondary-subtle';
      meta.appendChild(section);
    }

    const normalizedRaw = (Array.isArray(actions) ? actions : []).map((action, idx) => {
      if (!action || typeof action !== 'object') return null;
      const actionTextRaw = typeof action.action === 'string' ? action.action.trim() : '';
      const humanizedActionText = humanizeActionString(actionTextRaw);
      const descriptionSource = action.summary || action.description || action.text || (actionTextRaw && /\s/.test(actionTextRaw) ? actionTextRaw : '');
      const description = descriptionSource || '';
      const titleSeed = action.title || action.text || (!description ? (humanizedActionText || actionTextRaw) : '');
      const title = deriveActionTitle(titleSeed || humanizedActionText, description, idx);
      const metaBits = [];
      const ownerLabel = action.owner_name || action.owner || '';
      if (ownerLabel) metaBits.push(`Owner: ${ownerLabel}`);
      if (action.owner_user_id) metaBits.push(`Owner ID: ${action.owner_user_id}`);
      if (action.due_date) metaBits.push(`Due ${action.due_date}`);
      if (action.metric) metaBits.push(`Metric: ${action.metric}`);
      const showDescription = description && description.trim().toLowerCase() !== title.trim().toLowerCase();
      return {
        idx,
        title,
        description: showDescription ? description : '',
        metaBits,
        type: action.type || '',
        owner_participant_id: action.owner_participant_id != null ? Number(action.owner_participant_id) : null,
        owner_user_id: action.owner_user_id != null ? Number(action.owner_user_id) : null,
        due_date: action.due_date || null,
        status: action.status || 'todo',
        metric: action.metric || null,
        action_text: actionTextRaw,
        key: normalizeActionKey(title, description),
      };
    }).filter(Boolean);

    const normalized = uniqueActionEntries(normalizedRaw);

    const itemsHtml = normalized
      .map((item) => {
        const safeTitle = escapeHtml(item.title);
        const safeDescription = item.description ? escapeHtml(item.description) : '';
        const metaLine = item.metaBits.length
          ? `<div class="mt-1 small text-body-secondary">${item.metaBits.map((bit) => escapeHtml(bit)).join(' · ')}</div>`
          : '';
        let actionButton = '';
        if (canAddActionItems) {
          const alreadyCaptured = capturedActionKeys.has(item.key);
          actionButton = alreadyCaptured
            ? '<div class="mt-2"><span class="badge text-bg-success">Added</span></div>'
            : `<div class="mt-2"><button type="button" class="btn btn-sm btn-outline-primary assistant-add-action" data-action-idx="${item.idx}" data-action-key="${escapeHtml(item.key)}"><i class="bi bi-plus-circle me-1"></i>Add as Action Item</button></div>`;
        }
        return `<li class="list-group-item px-0 border-0 bg-transparent">
            <div class="d-flex justify-content-between align-items-start gap-2">
              <span class="fw-semibold">${safeTitle}</span>
              ${item.type ? `<span class="badge text-bg-light text-uppercase">${escapeHtml(item.type)}</span>` : ''}
            </div>
            ${safeDescription ? `<p class="mb-0 small text-body-secondary">${safeDescription}</p>` : ''}
            ${metaLine}
            ${actionButton}
          </li>`;
      })
      .join('');

    section.innerHTML = `
      <div class="card-body p-3">
        <div class="d-flex align-items-center gap-2 mb-2">
          <span class="badge text-bg-secondary"><i class="bi bi-list-check me-1"></i>Proposed actions</span>
          <span class="small text-body-secondary">${normalized.length} suggestion${normalized.length === 1 ? '' : 's'}</span>
        </div>
        <ul class="list-group list-group-flush">
          ${itemsHtml || '<li class="list-group-item px-0 border-0 bg-transparent small text-body-secondary">No specific actions provided.</li>'}
        </ul>
      </div>`;

    proposedActionsMap.set(section, normalized);

    markCapturedActionButtons();
  }

  function openAssistantActionModal(actionInfo, context) {
    const modal = ensureActionModal();
    if (!modal || !actionModalForm) return;
    currentActionContext = {
      info: actionInfo,
      button: context?.button || null,
      listItem: context?.listItem || null,
      section: context?.section || null,
    };

    if (actionModalError) actionModalError.classList.add('d-none');
    actionModalForm.reset();

    const titleInput = actionModalForm.querySelector('#assistant-action-title');
    const descInput = actionModalForm.querySelector('#assistant-action-description');
    const ownerSelect = actionModalForm.querySelector('#assistant-action-owner');
    const dueInput = actionModalForm.querySelector('#assistant-action-due');
    const statusSelect = actionModalForm.querySelector('#assistant-action-status');

    const baseDescription = actionInfo.description || actionInfo.summary || '';
    const computedTitle = deriveActionTitle(actionInfo.title, baseDescription, actionInfo.idx);
    if (titleInput) titleInput.value = computedTitle;
    if (descInput) {
      const descValue = baseDescription && baseDescription.trim() !== computedTitle.trim() ? baseDescription : '';
      descInput.value = descValue;
    }

    let defaultOwnerId = null;
    if (Number.isFinite(actionInfo.owner_participant_id)) {
      defaultOwnerId = Number(actionInfo.owner_participant_id);
    } else if (Number.isFinite(actionInfo.owner_user_id)) {
      defaultOwnerId = resolveParticipantIdForUser(Number(actionInfo.owner_user_id));
    }
    if (defaultOwnerId === null && Number.isFinite(participantId)) {
      defaultOwnerId = Number(participantId);
    }
    populateOwnerOptions(ownerSelect, defaultOwnerId);

    if (dueInput) {
      dueInput.value = formatDateForInput(actionInfo.due_date || new Date());
    }
    if (statusSelect) statusSelect.value = actionInfo.status || 'todo';

    modal.show();
  }

  if (messagesEl) {
    messagesEl.addEventListener('click', (event) => {
      const button = event.target.closest('.assistant-add-action');
      if (!button) return;
      event.preventDefault();
      if (!canAddActionItems) return;
      const section = button.closest('.assistant-actions');
      if (!section) return;
      const list = proposedActionsMap.get(section);
      if (!Array.isArray(list)) return;
      const idx = Number(button.dataset.actionIdx);
      if (Number.isNaN(idx) || !list[idx]) return;
      const listItem = button.closest('li');
      openAssistantActionModal(list[idx], { button, listItem, section });
    });
  }

  function extractActionButtons(hints) {
    if (!hints) return [];
    const candidates = hints.action_buttons || hints.buttons || hints.actions;
    if (!candidates) return [];
    const arr = Array.isArray(candidates) ? candidates : [candidates];
    return arr
      .map((item) => {
        if (!item) return null;
        if (typeof item === 'string') {
          return { label: item, action: item, variant: 'secondary' };
        }
        if (typeof item === 'object') {
          const label = item.label || item.title || item.text;
          const action = item.action || item.command || item.value || item.url || (item.onClick && typeof item.onClick === 'string' ? item.onClick : '');
          if (!label || !action) return null;
          return {
            label,
            action,
            variant: item.variant || item.style || 'primary',
            tooltip: item.tooltip || item.description || '',
            icon: item.icon || null,
            url: item.url || undefined,
            target: item.target || undefined,
          };
        }
        return null;
      })
      .filter(Boolean);
  }

  function renderActionButtons(buttons, targetCard = null) {
    const card = targetCard || state.streamingCard;
    if (!card) return;
    const meta = (targetCard && targetCard.querySelector('.assistant-turn-meta')) || state.streamingMeta || card.querySelector('.assistant-turn-meta');
    if (!meta) return;
    let section = meta.querySelector('.assistant-action-buttons');
    if (!buttons || !buttons.length) {
      if (section) section.remove();
      return;
    }
    if (!section) {
      section = document.createElement('div');
      section.className = 'assistant-action-buttons d-flex flex-wrap gap-2';
      meta.appendChild(section);
    } else {
      section.innerHTML = '';
    }
    buttons.forEach((info) => {
      const btn = document.createElement('button');
      const variant = info.variant === 'secondary' ? 'btn-outline-secondary' : info.variant === 'danger' ? 'btn-outline-danger' : info.variant === 'success' ? 'btn-outline-success' : 'btn-primary';
      btn.className = `btn btn-sm ${variant}`;
      btn.type = 'button';
      btn.textContent = info.label;
      if (info.tooltip) btn.title = info.tooltip;
      btn.dataset.action = info.action;
      const key = normalizeActionKey(info.label || info.text || info.title || '', info.summary || info.description || '');
      btn.dataset.actionKey = key;
      if (capturedActionKeys.has(key)) {
        btn.disabled = true;
        btn.classList.remove('btn-outline-secondary', 'btn-outline-danger', 'btn-outline-success', 'btn-primary');
        btn.classList.add('btn-success', 'text-white');
        btn.innerHTML = '<i class="bi bi-check-circle me-1"></i>Added';
      }
      btn.addEventListener('click', () => handleActionButton(info));
      section.appendChild(btn);
    });
  }

  function handleActionButton(info) {
    console.debug('[Assistant] action button clicked', info);
    // If the action or url is a URL (absolute or relative), open it in a new tab and return
    try {
      const directTarget = (info && (info.target || info.url)) || '';
      const action = (info && (info.action || info.value || info.url || directTarget || '')) || '';
      const trimmed = typeof action === 'string' ? action.trim() : '';
      if (trimmed && (/^https?:\/\//i.test(trimmed) || trimmed.startsWith('/'))) {
        window.open(trimmed, '_blank', 'noopener');
        return;
      }
      // Special-case: navigate intent with target path
      if (typeof info === 'object' && info && info.action === 'navigate' && typeof info.target === 'string' && info.target.trim()) {
        const t = info.target.trim();
        if (/^https?:\/\//i.test(t) || t.startsWith('/')) {
          window.open(t, '_blank', 'noopener');
          return;
        }
      }
    } catch (_) {}

    const messageText = info && (info.text || info.prompt || info.label || '').trim();
    if (messageText) {
      inputEl.value = messageText;
      sendMessage(messageText);
      inputEl.value = '';
    }

    const event = new CustomEvent('assistant-action', {
      detail: {
        action: info.action,
        label: info.label,
        workshopId,
        threadId: state.threadId,
      },
    });
    window.dispatchEvent(event);
  }

  function markCapturedActionButtons() {
    document.querySelectorAll('.assistant-add-action').forEach((btn) => {
      const key = btn.dataset.actionKey;
      if (key && capturedActionKeys.has(key)) {
        const wrapper = btn.parentElement;
        btn.remove();
        if (wrapper && !wrapper.querySelector('.badge')) {
          const badge = document.createElement('span');
          badge.className = 'badge text-bg-success';
          badge.innerHTML = '<i class="bi bi-check-circle me-1"></i>Added';
          wrapper.appendChild(badge);
        }
      }
    });
  }

  function renderCombinedSidebarActions() {
    renderSidebarActions(uniqueActionEntries(sidebarState.captured));
  }

  function upsertCapturedAction(rawEntry, options = {}) {
    const entry = normalizeActionEntry(rawEntry);
    if (!entry) return;
    const key = normalizeActionKey(entry.title, entry.summary);
    capturedActionKeys.add(key);
    const existingIndex = sidebarState.captured.findIndex((item) => normalizeActionKey(item.title, item.summary) === key);
    if (existingIndex !== -1) {
      sidebarState.captured.splice(existingIndex, 1);
    }
    sidebarState.captured.unshift(entry);
    if (options.render !== false) {
      renderCombinedSidebarActions();
      markCapturedActionButtons();
    }
  }

  async function preloadCapturedActionItems() {
    try {
      const res = await fetch(`/workshop/${workshopId}/action_items`, { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      const items = Array.isArray(data?.items) ? data.items : [];
      capturedActionKeys.clear();
      sidebarState.captured = [];
      for (let i = items.length - 1; i >= 0; i -= 1) {
        upsertCapturedAction(items[i], { render: false });
      }
      renderCombinedSidebarActions();
      markCapturedActionButtons();
    } catch (error) {
      console.warn('[Assistant] Failed to preload captured action items', error);
    }
  }

  function ensureSpeechEvents() {
    if (speechState.bound) return;
    speechState.bound = true;
    // Load persisted autoplay preference (default to true if not set)
    try {
      const raw = localStorage.getItem('assistant:autoplay');
      if (raw !== null) {
        speechState.autoplay = raw === '1';
      } else {
        // Default to true for first-time users
        speechState.autoplay = true;
      }
    } catch (_) {
      // Fallback to true if localStorage is unavailable
      speechState.autoplay = true;
    }
    if (autoplayToggle) {
      autoplayToggle.checked = !!speechState.autoplay;
      autoplayToggle.addEventListener('change', () => {
        speechState.autoplay = !!autoplayToggle.checked;
        try { localStorage.setItem('assistant:autoplay', speechState.autoplay ? '1' : '0'); } catch (_) {}
        // Update badges on all visible cards to reflect reason
        speechState.cards.forEach((_, key) => updateSpeechUI(key, speechState.cards.get(key)?.state || 'idle'));
      });
    }
    // Keep mute/blocked badge in sync with facilitator lifecycle
    const refreshAll = () => { speechState.cards.forEach((_, key) => updateSpeechUI(key, speechState.cards.get(key)?.state || 'idle')); };
    window.addEventListener('facilitator-tts-play', refreshAll);
    window.addEventListener('facilitator-tts-paused', refreshAll);
    window.addEventListener('facilitator-tts-stopped', refreshAll);
    window.addEventListener('facilitator-tts-ended', refreshAll);
    window.addEventListener('tts-playback-start', (event) => {
      const detail = event.detail || {};
      const source = detail.source;
      const turnId = detail.turnId != null ? String(detail.turnId) : null;
      if (source !== 'assistant') {
        speechState.activeId = null;
        speechState.cards.forEach((_, key) => updateSpeechUI(key, 'idle'));
        return;
      }
      if (turnId && speechState.cards.has(turnId)) {
        speechState.activeId = turnId;
        updateSpeechUI(turnId, 'playing');
        speechState.cards.forEach((_, key) => {
          if (key !== turnId) updateSpeechUI(key, 'idle');
        });
      }
    });
    window.addEventListener('tts-playback-ended', (event) => {
      const detail = event.detail || {};
      const source = detail.source;
      if (source && source !== 'assistant') {
        speechState.cards.forEach((_, key) => updateSpeechUI(key, 'idle'));
        speechState.activeId = null;
        return;
      }
      const turnId = detail.turnId != null ? String(detail.turnId) : speechState.activeId;
      if (turnId) updateSpeechUI(turnId, 'idle');
      speechState.activeId = null;
    });
  }

  function stripMarkdownForSpeech(input) {
    if (!input) return '';
    let text = String(input);
    text = text.replace(/```[\s\S]*?```/g, ' ');
    text = text.replace(/`([^`]*)`/g, '$1');
    text = text.replace(/\*\*([^*]+)\*\*/g, '$1');
    text = text.replace(/\*([^*]+)\*/g, '$1');
    text = text.replace(/__([^_]+)__|_([^_]+)_/g, (match, p1, p2) => p1 || p2 || '');
    text = text.replace(/~~([^~]+)~~/g, '$1');
    text = text.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
    text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
    text = text.replace(/^\s{0,3}[-*+]\s+/gm, '');
  text = text.replace(/^\s{0,3}(\d+)\.\s+/gm, '$1. ');
    text = text.replace(/^#{1,6}\s+/gm, '');
    text = text.replace(/^>\s?/gm, '');
    text = text.replace(/\n{3,}/g, '\n\n');
    return text.trim();
  }

  function getSpeechScript(data) {
    if (!data) return '';
    const speech = data.speech;
    if (speech && typeof speech === 'object') {
      const script = speech.tts_script || speech.script || speech.text || speech.ttsScript;
      if (script) return String(script);
    }
    if (typeof data.tts_script === 'string') return data.tts_script;
    if (typeof data.ttsScript === 'string') return data.ttsScript;
    if (typeof data.text === 'string') {
      return data.text;
    }
    return '';
  }

  function setupSpeechControls(data, targetCard, turnId, options = {}) {
  const script = stripMarkdownForSpeech((getSpeechScript(data) || '').trim());
    if (!script || !targetCard) return;
    const meta = targetCard.querySelector('.assistant-turn-meta') || state.streamingMeta;
    if (!meta) return;

    ensureSpeechEvents();

  const key = String(turnId || targetCard.dataset.turnId || `${Date.now()}-${Math.random().toString(16).slice(2)}`);

    let section = targetCard.querySelector('.assistant-tts');
    if (!section) {
      section = document.createElement('div');
      section.className = 'assistant-tts d-flex align-items-center gap-2';
      section.innerHTML = `
        <button type="button" class="btn btn-sm btn-outline-secondary assistant-tts-button" aria-pressed="false">
          <i class="bi bi-volume-up"></i>
          <span class="visually-hidden">Play narration</span>
        </button>
        <span class="assistant-tts-label small text-body-secondary">Narration ready</span>
        <span class="assistant-tts-mute badge text-bg-warning-subtle text-warning-emphasis d-none"></span>
      `;
      meta.prepend(section);
    }
    section.dataset.speechId = key;
    const button = section.querySelector('.assistant-tts-button');
    const status = section.querySelector('.assistant-tts-label');
    if (!button) return;

    const entry = {
      id: key,
      card: targetCard,
      section,
      button,
      status,
      script,
      state: speechState.cards.get(key)?.state || 'idle',
    };
    speechState.cards.set(key, entry);

    button.onclick = () => {
      if (entry.state === 'playing' && speechState.activeId === key) {
        stopSpeech(key);
      } else {
        speechState.autoPlayed.add(key);
        playSpeech(key);
      }
    };

    updateSpeechUI(key, entry.state);
    // Honor explicit suppression hint from server-side tools: do not autoplay and show mute reason
    try {
      const hints = data && data.ui_hints ? data.ui_hints : (data || {});
      if (hints && hints.suppress_narration) {
        if (entry.section) {
          const muteBadge = entry.section.querySelector('.assistant-tts-mute');
          if (muteBadge) {
            muteBadge.textContent = 'muted: facilitator narration suppressed';
            muteBadge.classList.remove('d-none');
          }
        }
      } else if (options.autoplay && speechState.autoplay && !speechState.autoPlayed.has(key)) {
        const tryAuto = () => {
          // If facilitator is speaking, postpone autoplay until it stops
          try {
            if (window.FacilitatorTTS && typeof window.FacilitatorTTS.isSpeaking === 'function' && window.FacilitatorTTS.isSpeaking()) {
              const once = () => {
                try { window.removeEventListener('facilitator-tts-ended', once); window.removeEventListener('facilitator-tts-stopped', once); } catch(_) {}
                setTimeout(() => playSpeech(key), 120);
              };
              window.addEventListener('facilitator-tts-ended', once, { once: true });
              window.addEventListener('facilitator-tts-stopped', once, { once: true });
              return;
            }
          } catch(_) {}
          setTimeout(() => playSpeech(key), 160);
        };
        speechState.autoPlayed.add(key);
        tryAuto();
      }
    } catch (_) {
      // If anything goes wrong, fall back to existing autoplay behavior
      if (options.autoplay && speechState.autoplay && !speechState.autoPlayed.has(key)) {
        speechState.autoPlayed.add(key);
        try { setTimeout(() => playSpeech(key), 160); } catch(_) {}
      }
    }
  }

  function playSpeech(turnId) {
    const key = String(turnId);
    const entry = speechState.cards.get(key);
    if (!entry) return;
    const script = entry.script;
    if (!script) return;
    if (!window.TTS || typeof window.TTS.play !== 'function') {
      console.warn('[Assistant] TTS playback unavailable');
      updateSpeechUI(key, 'idle');
      return;
    }
    speechState.cards.forEach((_, id) => {
      if (id !== key) updateSpeechUI(id, 'idle');
    });
    speechState.activeId = key;
    updateSpeechUI(key, 'loading');
    try {
      window.TTS.play(script, { meta: { source: 'assistant', turnId: key } });
    } catch (err) {
      console.error('[Assistant] Narration play error', err);
      updateSpeechUI(key, 'idle');
      speechState.activeId = null;
    }
  }

  function stopSpeech(turnId) {
    const key = turnId ? String(turnId) : speechState.activeId;
    if (!key) return;
    try {
      if (window.TTS && typeof window.TTS.stop === 'function') window.TTS.stop();
    } catch (err) {
      console.warn('[Assistant] Failed to stop narration', err);
    }
    updateSpeechUI(key, 'idle');
    if (speechState.activeId === key) speechState.activeId = null;
  }

  function updateSpeechUI(turnId, state) {
    const key = String(turnId);
    const entry = speechState.cards.get(key);
    if (!entry) return;
    entry.state = state || 'idle';
    const btn = entry.button;
    const status = entry.status;
  const muteBadge = entry.section.querySelector('.assistant-tts-mute');
    if (!btn) return;

    let iconHtml = '<i class="bi bi-volume-up"></i>';
    let srLabel = 'Play narration';
    if (entry.state === 'loading') {
      iconHtml = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
      srLabel = 'Preparing narration';
      btn.disabled = true;
    } else if (entry.state === 'playing') {
      iconHtml = '<i class="bi bi-stop-fill"></i>';
      srLabel = 'Stop narration';
      btn.disabled = false;
      btn.classList.remove('btn-outline-secondary');
      btn.classList.add('btn-primary');
    } else {
      btn.disabled = false;
      btn.classList.remove('btn-primary');
      if (!btn.classList.contains('btn-outline-secondary')) btn.classList.add('btn-outline-secondary');
    }

    btn.innerHTML = `${iconHtml}<span class="visually-hidden">${srLabel}</span>`;
    btn.setAttribute('aria-pressed', entry.state === 'playing' ? 'true' : 'false');
    if (entry.state === 'loading') {
      btn.setAttribute('aria-busy', 'true');
    } else {
      btn.removeAttribute('aria-busy');
    }
    if (status) {
      if (entry.state === 'playing') status.textContent = 'Speaking…';
      else if (entry.state === 'loading') status.textContent = 'Preparing narration…';
      else status.textContent = 'Narration ready';
    }

    // Show debug mute/blocked reason if facilitator is active or autoplay disabled
    let reason = '';
    try {
      if (window.FacilitatorTTS && typeof window.FacilitatorTTS.isSpeaking === 'function' && window.FacilitatorTTS.isSpeaking()) {
        reason = 'muted: facilitator speaking';
      } else if (!speechState.autoplay) {
        reason = 'autoplay off';
      }
    } catch (_) {}
    if (muteBadge) {
      if (reason) {
        muteBadge.textContent = reason;
        muteBadge.classList.remove('d-none');
      } else {
        muteBadge.textContent = '';
        muteBadge.classList.add('d-none');
      }
    }
  }

  function renderCitations(citations, targetCard = null) {
    const card = targetCard || state.streamingCard;
    if (!card) return;
    const meta = (targetCard && targetCard.querySelector('.assistant-turn-meta')) || state.streamingMeta || card.querySelector('.assistant-turn-meta');
    if (!meta) return;
    let section = meta.querySelector('.assistant-citations');
    if (!section) {
      section = document.createElement('div');
      section.className = 'assistant-citations alert alert-secondary mt-3 py-2 px-3';
      meta.appendChild(section);
    }
    const listItems = citations
      .map((citation, idx) => {
        if (!citation || typeof citation !== 'object') return '';
        const rawLabel = citation.display_label || citation.source_ref || (citation.document_id ? `Document ${citation.document_id}` : `Source ${idx + 1}`);
        const label = escapeHtml(rawLabel);
        const type = citation.source_type ? `<span class="badge text-bg-light ms-2 text-uppercase">${escapeHtml(citation.source_type)}</span>` : '';
        const href = resolveCitationHref(citation);
        const labelHtml = href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${label}</a>` : label;
        return `<li class="mb-1">${labelHtml}${type}</li>`;
      })
      .filter(Boolean)
      .join('');
    section.innerHTML = `
      <div class="d-flex align-items-center gap-2 mb-1">
        <i class="bi bi-link-45deg"></i>
        <span class="small fw-semibold text-uppercase">Citations</span>
      </div>
      <ul class="list-unstyled mb-0 small">${listItems || '<li class="text-body-secondary">No citations returned.</li>'}</ul>
    `;
  }

  function renderMemoryUsage(memoryInfo, targetCard = null) {
    if (!memoryInfo || typeof memoryInfo !== 'object') return;
    const recalled = Number(memoryInfo.count || memoryInfo.total || 0);
    if (!Number.isFinite(recalled) || recalled <= 0) return;
    const card = targetCard || state.streamingCard;
    if (!card) return;
    const meta = (targetCard && targetCard.querySelector('.assistant-turn-meta')) || state.streamingMeta || card.querySelector('.assistant-turn-meta');
    if (!meta) return;

    let section = meta.querySelector('.assistant-memory');
    if (!section) {
      section = document.createElement('div');
      section.className = 'assistant-memory d-flex align-items-center gap-2 text-body-secondary small mb-2';
      meta.prepend(section);
    }

    const namespaces = Array.isArray(memoryInfo.namespaces)
      ? memoryInfo.namespaces.filter((ns) => typeof ns === 'string' && ns.trim())
      : [];
    const displayNamespaces = namespaces.slice(0, 3).map((ns) => `<code class="assistant-memory-namespace">${escapeHtml(ns.trim())}</code>`);
    const remaining = Math.max(0, namespaces.length - displayNamespaces.length);
    const namespaceHtml = displayNamespaces.length ? ` · ${displayNamespaces.join(', ')}${remaining ? `, +${remaining} more` : ''}` : '';

    section.innerHTML = `
      <i class="bi bi-database-gear"></i>
      <span>Memory recall · ${recalled} ${recalled === 1 ? 'item' : 'items'}${namespaceHtml}</span>
    `;

    if (namespaces.length) {
      section.title = namespaces.join(', ');
    }
  }

  function resolveCitationHref(citation) {
    try {
      const explicit = citation.url || citation.href || citation.link || citation.document_url;
      if (explicit && typeof explicit === 'string') return explicit;
      const ref = citation.source_ref;
      if (typeof ref === 'string' && /^https?:\/\//i.test(ref.trim())) {
        return ref.trim();
      }
      const docId = citation.document_id;
      if (docId != null && docId !== '') {
        const numeric = Number(docId);
        if (!Number.isNaN(numeric) && numeric > 0) {
          return `/document/file/${numeric}`;
        }
      }
    } catch (err) {
      console.warn('[Assistant] Failed to resolve citation href', err, citation);
    }
    return null;
  }

  function applyUiHints(reply) {
    if (reply?.ui_hints?.chips) renderChips(reply.ui_hints.chips);
    if (reply?.ui_hints?.followups) {
      const contextEntries = reply.ui_hints.followups.map((item) => ({ title: 'Follow-up', body: item }));
      renderContext(contextEntries);
    }
    const buttons = extractActionButtons(reply?.ui_hints);
    if (buttons.length) {
      renderActionButtons(buttons);
    }
  }

  function sendMessage(text) {
    const trimmed = (text || '').trim();
    if (!trimmed) return;
    appendUserMessage(trimmed);
    state.streamingBuffer = '';
    ensureAssistantCard(state.persona);
    const payload = {
      workshop_id: workshopId,
      text: trimmed,
      persona: state.persona,
    };
    if (state.threadId) payload.thread_id = state.threadId;
    if (typeof userId === 'number' && !Number.isNaN(userId)) {
      payload.user_id = userId;
    }
    socket.emit('ask', payload);
  }

  sendBtn.addEventListener('click', () => {
    sendMessage(inputEl.value);
    inputEl.value = '';
  });

  inputEl.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      sendBtn.click();
    }
  });

  const socket = io('/assistant', { transports: ['websocket', 'polling'] });
  window.assistantSocket = socket;

  socket.on('connect', () => {
    const joinPayload = { workshop_id: workshopId };
    if (typeof userId === 'number' && !Number.isNaN(userId)) {
      joinPayload.user_id = userId;
    }
    socket.emit('join', joinPayload);
  });

  socket.on('assistant:ack', (payload) => {
    if (payload?.thread_id) state.threadId = payload.thread_id;
    if (state.threadId) persistActiveThreadId(state.threadId);
    updateStatusBadges(payload || {});
    if (payload?.persona) {
      state.persona = payload.persona;
      syncPersonaSelection(payload.persona);
      setPersonaBadge(payload.persona, payload.persona_label);
    }
    if (Array.isArray(payload?.phase_snapshot)) {
      sidebarState.phase = payload.phase_snapshot;
      renderPhaseSnapshot(sidebarState.phase);
    }
    if (Array.isArray(payload?.sidebar?.actions)) {
      capturedActionKeys.clear();
      sidebarState.captured = [];
      const raw = payload.sidebar.actions || [];
      for (let i = raw.length - 1; i >= 0; i -= 1) {
        upsertCapturedAction(raw[i], { render: false });
      }
      renderCombinedSidebarActions();
      markCapturedActionButtons();
    }
    if (Array.isArray(payload?.sidebar?.threads)) {
      sidebarState.threads = payload.sidebar.threads;
      renderSidebarThreads(sidebarState.threads);
    }
    state.streamingBuffer = '';
    if (state.streamingCard) {
      const body = state.streamingCard.querySelector('.assistant-turn-body');
      if (body) body.innerHTML = '';
      const meta = state.streamingCard.querySelector('.assistant-turn-meta');
      if (meta) meta.innerHTML = '';
      const feedbackGroup = state.streamingCard.querySelector('.assistant-feedback');
      if (feedbackGroup) {
        delete feedbackGroup.dataset.feedback;
        feedbackGroup.querySelectorAll('button[data-rating]').forEach((btn) => {
          if (btn.dataset.rating === 'up') {
            btn.classList.add('btn-outline-success');
            btn.classList.remove('btn-success');
          } else {
            btn.classList.add('btn-outline-danger');
            btn.classList.remove('btn-danger');
          }
          btn.disabled = false;
        });
      }
    }
  });

  socket.on('assistant:state', (payload) => {
    if (!payload || typeof payload !== 'object') return;
    updateStatusBadges(payload || {});
    if (Array.isArray(payload.phase_snapshot)) {
      sidebarState.phase = payload.phase_snapshot;
      renderPhaseSnapshot(sidebarState.phase);
    }
    if (payload.sidebar && Array.isArray(payload.sidebar.actions)) {
      capturedActionKeys.clear();
      sidebarState.captured = [];
      const raw = payload.sidebar.actions || [];
      for (let i = raw.length - 1; i >= 0; i -= 1) {
        upsertCapturedAction(raw[i], { render: false });
      }
      renderCombinedSidebarActions();
      markCapturedActionButtons();
    }
  });

  socket.on('assistant:token', (payload) => {
    const delta = typeof payload?.delta === 'string' ? payload.delta : typeof payload?.text === 'string' ? payload.text : '';
    if (!delta) return;
    state.streamingBuffer += delta;
    let turn = null;
    if (!state.streamingCard) {
      turn = ensureAssistantCard(state.persona);
    } else {
      turn = { card: state.streamingCard, meta: state.streamingMeta, wrapper: state.streamingWrapper };
    }
    state.streamingCard = turn.card;
    state.streamingMeta = turn.meta || state.streamingMeta;
    state.streamingWrapper = turn.wrapper || state.streamingWrapper;
    setTurnStatus(state.streamingCard, { text: 'Thinking…', showSpinner: true, tone: 'neutral' });
    const body = turn.card?.querySelector('.assistant-turn-body');
    if (body) {
      body.innerHTML = renderMarkdown(state.streamingBuffer);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  });

  socket.on('assistant:tool_result', (toolResult) => {
    // Render structured documents from this tool result (live reply)
    try { renderDocumentsFromPayloadMeta({ tool_results: [toolResult] }, state.streamingCard); } catch (err) { console.warn('[Assistant] documents render failed', err); }
    appendToolResult(toolResult);
  });

  socket.on('assistant:reply', (payload) => {
    if (!payload?.reply) return;
    const reply = payload.reply;
    state.streamingBuffer = reply.text || state.streamingBuffer;
    finalizeAssistantMessage(reply, payload);
    applyUiHints(reply);
    state.threadId = payload.thread_id || state.threadId;
    if (state.threadId) persistActiveThreadId(state.threadId);
    if (payload?.meta?.persona) {
      state.persona = payload.meta.persona;
      syncPersonaSelection(payload.meta.persona);
      setPersonaBadge(payload.meta.persona, payload.meta.persona_label);
    }
    if (Array.isArray(payload?.meta?.phase_snapshot)) {
      sidebarState.phase = payload.meta.phase_snapshot;
      renderPhaseSnapshot(sidebarState.phase);
    }
    if (payload?.meta?.sidebar) {
      if (Array.isArray(payload.meta.sidebar.actions)) {
        capturedActionKeys.clear();
        sidebarState.captured = [];
        const raw = payload.meta.sidebar.actions || [];
        for (let i = raw.length - 1; i >= 0; i -= 1) {
          upsertCapturedAction(raw[i], { render: false });
        }
        renderCombinedSidebarActions();
        markCapturedActionButtons();
      }
      if (Array.isArray(payload.meta.sidebar.threads)) {
        sidebarState.threads = payload.meta.sidebar.threads;
        renderSidebarThreads(sidebarState.threads);
      }
    }
  });

  socket.on('assistant:error', (payload) => {
    if (!state.streamingCard) {
      ensureAssistantCard(state.persona);
    }
    if (!state.streamingCard) return;
    setTurnStatus(state.streamingCard, { text: 'Assistant unavailable', showSpinner: false, tone: 'danger' });
    const body = state.streamingCard.querySelector('.assistant-turn-body');
    if (body) {
      body.innerHTML = `<span class="text-danger">${escapeHtml(payload?.error || 'Assistant failed')}</span>`;
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // If backend indicates thread issues, clear stale thread id and reload history to recover
    try {
      const message = String(payload?.error || '').toLowerCase();
      if (message.includes('thread') || message.includes('forbidden')) {
        if (state.threadId) {
          state.threadId = null;
          persistActiveThreadId(null);
        }
        messagesEl.innerHTML = '';
        showToast('Your previous thread is unavailable. Starting a new one…', 'warning');
        loadHistory();
      }
    } catch (_) {}
  });

  function wireFeedbackHandlers(card, turnId) {
    if (!card || !turnId) return;
    const group = card.querySelector('.assistant-feedback');
    if (!group) return;
    const updateButtons = (rating) => {
      group.dataset.feedback = rating || '';
      group.querySelectorAll('button[data-rating]').forEach((btn) => {
        const desired = btn.dataset.rating;
        const isActive = rating === desired;
        if (desired === 'up') {
          btn.classList.toggle('btn-success', isActive);
          btn.classList.toggle('btn-outline-success', !isActive);
        } else {
          btn.classList.toggle('btn-danger', isActive);
          btn.classList.toggle('btn-outline-danger', !isActive);
        }
        btn.disabled = false;
      });
    };
    if (group.dataset.bound === '1') {
      updateButtons(group.dataset.feedback || '');
      return;
    }
    group.dataset.bound = '1';
    updateButtons(group.dataset.feedback || '');
    group.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-rating]');
      if (!button) return;
      event.preventDefault();
      const rating = button.dataset.rating;
      if (!rating) return;
      if (group.dataset.pending === '1') return;
      if (group.dataset.feedback === rating) return;
      group.dataset.pending = '1';
      group.querySelectorAll('button[data-rating]').forEach((btn) => {
        btn.disabled = true;
      });
      try {
        const response = await fetch('/assistant/feedback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ turn_id: turnId, rating }),
        });
        if (!response.ok) throw new Error('Failed to submit feedback');
        const data = await response.json().catch(() => ({}));
        updateButtons(data.rating || rating);
      } catch (err) {
        console.error(err);
        updateButtons(group.dataset.feedback || '');
      } finally {
        delete group.dataset.pending;
      }
    });
  }

  window.addEventListener('beforeunload', stopTimerCountdown);

  loadHistory();
})();
