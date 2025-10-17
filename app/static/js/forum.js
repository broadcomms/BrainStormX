// app/static/js/forum.js
// Minimal forum UI controller for Discussion phase. Renders categories, topics, posts, and handles create post/reply.
(function(){
  const state = {
    workshopId: null,
    categoryId: null,
    topicId: null,
    topicMeta: null,
    categories: [],
    topics: [],
    posts: [],
    typingUsers: new Set(),
    isOrganizer: false,
    _socketBound: false,
    // paging state
    topicsLimit: 5,
    topicsOffset: 0,
    postsLimit: 5,
    postsOffset: 0,
    repliesLimit: 5,
  };

  // Per-post reply size persistence helpers
  function _replyLimitKey(postId) {
    return `forum:repliesLimit:${state.workshopId || ''}:${postId}`;
  }
  function getReplyLimit(postId) {
    try {
      const v = sessionStorage.getItem(_replyLimitKey(postId));
      const n = v ? parseInt(v, 10) : NaN;
      if (!isNaN(n) && [5,10,20].includes(n)) return n;
    } catch(_) {}
    return state.repliesLimit;
  }
  function setReplyLimit(postId, limit) {
    try { sessionStorage.setItem(_replyLimitKey(postId), String(limit)); } catch(_) {}
  }

  function dedupePosts(posts) {
    try {
      const seen = new Map(); // id -> post
      (posts || []).forEach(p => {
        const pid = Number(p && p.id);
        if (!pid) return;
        // Dedupe replies inside the post first
        const rSeen = new Map();
        const uniqueReplies = [];
        (p.replies || []).forEach(r => {
          const rid = Number(r && r.id);
          if (!rid || rSeen.has(rid)) return;
          rSeen.set(rid, true);
          uniqueReplies.push(r);
        });
        p.replies = uniqueReplies;
        // Now dedupe the post itself
        if (!seen.has(pid)) {
          seen.set(pid, p);
        } else {
          // Merge basic fields preferring non-optimistic values
          const existing = seen.get(pid);
          const prefer = (!existing.__optimistic && p.__optimistic) ? existing : p;
          // Keep whichever is non-optimistic if available
          const merged = Object.assign({}, existing, prefer);
          // Merge replies (union by id)
          const byId = new Map();
          (existing.replies || []).forEach(r => byId.set(Number(r.id), r));
          (p.replies || []).forEach(r => {
            const rid = Number(r.id);
            if (!byId.has(rid)) byId.set(rid, r);
          });
          merged.replies = Array.from(byId.values());
          seen.set(pid, merged);
        }
      });
      return Array.from(seen.values()).sort((a,b) => {
        // Prefer non-optimistic entries first so optimistic items sort after server-confirmed posts
        const ao = a.__optimistic ? 1 : 0;
        const bo = b.__optimistic ? 1 : 0;
        if (ao !== bo) return ao - bo; // non-optimistic (0) before optimistic (1)
        const at = a.created_at ? new Date(a.created_at).getTime() : Number.MAX_SAFE_INTEGER;
        const bt = b.created_at ? new Date(b.created_at).getTime() : Number.MAX_SAFE_INTEGER;
        if (at !== bt) return at - bt; // asc by time
        const ai = Number(a.id) || 0;
        const bi = Number(b.id) || 0;
        return ai - bi; // deterministic tie-breaker
      });
    } catch(_) {
      return posts || [];
    }
  }

  function h(tag, attrs = {}, children = []) {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k,v]) => {
      if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;
      else el.setAttribute(k, v);
    });
    (children || []).forEach(ch => {
      if (typeof ch === 'string') el.appendChild(document.createTextNode(ch));
      else if (ch) el.appendChild(ch);
    });
    return el;
  }

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json' }}, opts || {}));
    const data = await r.json().catch(()=>({}));
    if (!r.ok) throw new Error(data.error || data.message || 'Request failed');
    return data;
  }

  async function loadCategories() {
    const res = await api(`/api/workshops/${state.workshopId}/forum/categories`);
    state.categories = res.categories || [];
    renderCategories();
  }

  function parseForumHash() {
    try {
      const h = (window.location.hash || '').replace(/^#/, '');
      if (!h.startsWith('forum:')) return {};
      const qs = new URLSearchParams(h.slice('forum:'.length));
      const cat = parseInt(qs.get('cat') || '0', 10) || null;
      const topic = parseInt(qs.get('topic') || '0', 10) || null;
      const topicsLimit = parseInt(qs.get('tLimit') || '0', 10) || null;
      const topicsOffset = parseInt(qs.get('tOffset') || '0', 10) || null;
      const postsLimit = parseInt(qs.get('pLimit') || '0', 10) || null;
      const postsOffset = parseInt(qs.get('pOffset') || '0', 10) || null;
      return { categoryId: cat, topicId: topic, topicsLimit, topicsOffset, postsLimit, postsOffset };
    } catch(_) { return {}; }
  }

  function setForumHash(catId, topicId) {
    try {
      const qs = new URLSearchParams();
      if (catId) qs.set('cat', String(catId));
      if (topicId) qs.set('topic', String(topicId));
      if (state.topicsLimit) qs.set('tLimit', String(state.topicsLimit));
      if (state.topicsOffset) qs.set('tOffset', String(state.topicsOffset));
      if (state.postsLimit) qs.set('pLimit', String(state.postsLimit));
      if (state.postsOffset) qs.set('pOffset', String(state.postsOffset));
      const hash = 'forum:' + qs.toString();
      if (window.location.hash !== '#' + hash) {
        window.location.hash = hash;
      }
    } catch(_) {}
  }

  async function loadTopics(categoryId, link) {
    state.categoryId = categoryId;
    let url = link || `/api/workshops/${state.workshopId}/forum/topics?category_id=${encodeURIComponent(categoryId)}&limit=${encodeURIComponent(state.topicsLimit)}&offset=${encodeURIComponent(state.topicsOffset)}`;
    const res = await api(url);
    state.topics = res.topics || [];
    // Sync pagination state
    if (res && res.pagination) {
      state.topicsLimit = res.pagination.limit ?? state.topicsLimit;
      state.topicsOffset = res.pagination.offset ?? state.topicsOffset;
    }
    renderTopics(res.category, res.links, res.pagination);
    // Update hash
    setForumHash(categoryId, null);
  }

  async function loadPosts(topicId, link) {
    state.topicId = topicId;
    let url = link || `/api/workshops/${state.workshopId}/forum/topics/${topicId}/posts?limit=${encodeURIComponent(state.postsLimit)}&offset=${encodeURIComponent(state.postsOffset)}`;
    const res = await api(url);
    state.posts = res.posts || [];
    state.topicMeta = res.topic || { id: topicId };
    // Sync pagination state
    if (res && res.pagination) {
      state.postsLimit = res.pagination.limit ?? state.postsLimit;
      state.postsOffset = res.pagination.offset ?? state.postsOffset;
    }
    renderPosts(res.topic, res.links, res.pagination);
    // Update hash
    setForumHash(state.categoryId, topicId);
  }

  async function createPost(body) {
    const res = await api(`/api/workshops/${state.workshopId}/forum/posts`, {
      method: 'POST',
      body: JSON.stringify({ topic_id: state.topicId, body })
    });
    // Upsert to avoid duplicates if socket event already added it
    if (res && res.post) {
      const pid = Number(res.post.id);
      const existing = state.posts.find(p => Number(p.id) === pid);
      if (existing) {
        existing.user_id = res.post.user_id;
        existing.user_name = res.post.user_name;
        existing.body = res.post.body;
        existing.created_at = res.post.created_at;
        existing.replies = existing.replies || [];
        delete existing.__optimistic;
      } else {
        state.posts.push({ id: res.post.id, user_name: res.post.user_name, user_id: res.post.user_id, body: res.post.body, created_at: res.post.created_at, replies: [] });
      }
      // Preserve topic metadata so header doesn't flash as Untitled
      renderPosts(state.topicMeta || { id: state.topicId });
    }
  }

  async function createReply(postId, body) {
    const res = await api(`/api/workshops/${state.workshopId}/forum/replies`, {
      method: 'POST',
      body: JSON.stringify({ post_id: postId, body })
    });
    // Upsert within the right post to avoid duplicates if socket beat us
    const p = state.posts.find(p => Number(p.id) === Number(postId));
    if (p && res && res.reply) {
      p.replies = p.replies || [];
      const rid = Number(res.reply.id);
      const existing = p.replies.find(r => Number(r.id) === rid);
      if (existing) {
        existing.user_id = res.reply.user_id;
        existing.user_name = res.reply.user_name;
        existing.body = res.reply.body;
        existing.created_at = res.reply.created_at;
      } else {
        p.replies.push({ id: res.reply.id, user_name: res.reply.user_name, user_id: res.reply.user_id, body: res.reply.body, created_at: res.reply.created_at });
      }
      // Preserve topic metadata so header doesn't flash as Untitled
      renderPosts(state.topicMeta || { id: state.topicId });
    }
  }

  function renderCategories() {
    const root = document.getElementById('discussion-forum-root');
    if (!root) return;
    root.innerHTML = '';
    const list = h('div', { class: 'list-group list-group-flush small' });
    if (!state.categories.length) list.appendChild(h('div', { class: 'p-3 text-muted' }, ['No categories yet. Organizer can seed from results.']));
    state.categories.forEach(c => {
      const a = h('a', { class: 'list-group-item list-group-item-action d-flex justify-content-between align-items-center', href: '#'} , [
        h('span', {}, [c.title || 'Untitled']),
        h('span', { class: 'badge text-bg-secondary' }, [String(c.topic_count || 0)])
      ]);
      a.addEventListener('click', (e) => { e.preventDefault(); loadTopics(c.id); });
      list.appendChild(a);
    });
    root.appendChild(h('div', { class: 'mb-2 fw-semibold' }, ['Categories']));
    root.appendChild(list);
    // Clear right pane
    const right = document.getElementById('discussion-forum-right');
    if (right) right.innerHTML = '<div class="text-muted small p-3">Select a category</div>';
  }

  function renderTopics(category, links, pagination) {
    const root = document.getElementById('discussion-forum-right');
    if (!root) return;
    root.innerHTML = '';
    // Breadcrumbs
    const bc = h('div', { class: 'small text-body-secondary mb-2 d-flex align-items-center justify-content-between' }, [
      h('span', {}, [ 'Category: ', category?.title || '' ]),
    ]);
    root.appendChild(bc);
    const list = h('div', { class: 'list-group list-group-flush small' });
    if (!state.topics.length) list.appendChild(h('div', { class: 'p-3 text-muted' }, ['No topics in this category.']));
    state.topics.forEach(t => {
      const metaBadges = h('span', { class: 'ms-2' });
      if (t.pinned) metaBadges.appendChild(h('span', { class: 'badge rounded-pill text-bg-warning ms-1' }, ['Pinned']));
      if (t.locked) metaBadges.appendChild(h('span', { class: 'badge rounded-pill text-bg-secondary ms-1' }, ['Locked']));
      const a = h('a', { class: 'list-group-item list-group-item-action d-flex justify-content-between align-items-center', href: '#' }, [
        h('span', {}, [ t.title || 'Untitled topic' ]),
        metaBadges
      ]);
      a.addEventListener('click', (e) => { e.preventDefault(); loadPosts(t.id); });
      list.appendChild(a);
    });
    // Header with page position and size selector
    const headerRow = h('div', { class: 'd-flex justify-content-between align-items-center mb-2' });
    const headerTitle = h('div', { class: 'fw-semibold' }, ['Topics']);
    const rightHeader = h('div', { class: 'd-flex align-items-center gap-2' });
    const pageInfo = (pagination && pagination.total != null)
      ? h('div', { class: 'small text-body-secondary' }, [
          `Showing ${Math.min(pagination.total, pagination.offset + 1)}–${Math.min(pagination.total, pagination.offset + pagination.returned)} of ${pagination.total}`
        ])
      : h('div');
    const sizeSel = h('select', { class: 'form-select form-select-sm', style: 'width: auto;' });
    [5,10,20].forEach(n => {
      const opt = h('option', { value: String(n) }, [String(n)]);
      if (Number(n) === Number(state.topicsLimit)) opt.selected = true;
      sizeSel.appendChild(opt);
    });
    sizeSel.addEventListener('change', () => {
      state.topicsLimit = Number(sizeSel.value) || state.topicsLimit;
      state.topicsOffset = 0; // reset to first page when size changes
      loadTopics(category.id);
    });
    rightHeader.appendChild(pageInfo);
    rightHeader.appendChild(sizeSel);
    headerRow.appendChild(headerTitle);
    headerRow.appendChild(rightHeader);
    root.appendChild(headerRow);
    root.appendChild(list);
    // Pagination controls
    const pager = h('div', { class: 'd-flex justify-content-between align-items-center mt-2' });
    const prev = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Prev']);
    const next = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Next']);
    const last = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Last']);
    prev.disabled = !(links && links.prev);
    next.disabled = !(links && links.next);
    last.disabled = !(links && links.last);
  prev.addEventListener('click', () => { state.topicsOffset = Math.max(0, (pagination?.offset||0) - (pagination?.limit||state.topicsLimit)); links && links.prev && loadTopics(category.id, links.prev); });
  next.addEventListener('click', () => { state.topicsOffset = (pagination?.offset||0) + (pagination?.limit||state.topicsLimit); links && links.next && loadTopics(category.id, links.next); });
    last.addEventListener('click', () => links && links.last && loadTopics(category.id, links.last));
    pager.appendChild(prev); pager.appendChild(next); pager.appendChild(last);
    root.appendChild(pager);
    // Post pane
    const postsPane = h('div', { id: 'forum-posts-pane', class: 'mt-3' });
    root.appendChild(postsPane);
  }

  // Helper to merge provided topic with cached metadata
  function _mergeTopicMeta(topic) {
    const base = state.topicMeta || {};
    const t = Object.assign({}, base, topic || {});
    if (!t.id) t.id = state.topicId;
    return t;
  }

  function renderPosts(topic, links, pagination) {
    // Always merge with cached metadata to keep title/flags consistent
    topic = _mergeTopicMeta(topic);
    const pane = document.getElementById('forum-posts-pane');
    if (!pane) return;
    pane.innerHTML = '';
    // Ensure we display unique posts/replies only
    const unique = dedupePosts(state.posts);
    if (unique.length !== state.posts.length) state.posts = unique;
    // Header with breadcrumbs and enrichment toggle
    const header = h('div', { class: 'd-flex align-items-center justify-content-between mb-2' });
    const left = h('div', { class: 'd-flex align-items-center gap-2' }, [
      h('span', { class: 'small text-body-secondary' }, ['Topic: ']),
      h('strong', {}, [topic?.title || 'Untitled'])
    ]);
    if (topic && topic.pinned) left.appendChild(h('span', { class: 'badge rounded-pill text-bg-warning' }, ['Pinned']));
    if (topic && topic.locked) left.appendChild(h('span', { class: 'badge rounded-pill text-bg-secondary' }, ['Locked']));
    const right = h('div', { class: 'd-flex align-items-center gap-2' });
    const enrichBtn = h('button', { class: 'btn btn-sm btn-outline-info', type: 'button' }, ['Related']);
    enrichBtn.addEventListener('click', () => loadEnrichment(topic?.id));
    right.appendChild(enrichBtn);
    // Organizer moderation controls
    if (state.isOrganizer && topic && topic.id) {
      const modGroup = h('div', { class: 'btn-group' });
      const editBtn = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Edit']);
      const pinBtn = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, [topic.pinned ? 'Unpin' : 'Pin']);
      const lockBtn = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, [topic.locked ? 'Unlock' : 'Lock']);
      const delBtn = h('button', { class: 'btn btn-sm btn-outline-danger', type: 'button' }, ['Delete']);
      editBtn.addEventListener('click', async () => {
        const newTitle = prompt('Edit topic title', topic.title || '');
        if (newTitle == null) return;
        const newDesc = prompt('Edit topic description (optional)', topic.description || '');
        try {
          await api(`/api/workshops/${state.workshopId}/forum/topics/${topic.id}`, { method: 'PATCH', body: JSON.stringify({ title: newTitle, description: newDesc || null }) });
          await loadPosts(topic.id);
        } catch(e) { alert(e.message || 'Failed to update topic'); }
      });
      pinBtn.addEventListener('click', async () => {
        try {
          await api(`/api/workshops/${state.workshopId}/forum/topics/${topic.id}`, { method: 'PATCH', body: JSON.stringify({ pinned: !topic.pinned }) });
          await loadTopics(state.categoryId);
          await loadPosts(topic.id);
        } catch(e) { alert(e.message || 'Failed to toggle pin'); }
      });
      lockBtn.addEventListener('click', async () => {
        try {
          await api(`/api/workshops/${state.workshopId}/forum/topics/${topic.id}`, { method: 'PATCH', body: JSON.stringify({ locked: !topic.locked }) });
          await loadPosts(topic.id);
        } catch(e) { alert(e.message || 'Failed to toggle lock'); }
      });
      delBtn.addEventListener('click', async () => {
        if (!confirm('Delete this topic and all its posts?')) return;
        try { await api(`/api/workshops/${state.workshopId}/forum/topics/${topic.id}`, { method: 'DELETE' });
          // Refresh category topics and clear posts pane
          await loadTopics(state.categoryId);
          const postsPane = document.getElementById('forum-posts-pane'); if (postsPane) postsPane.innerHTML = '<div class="text-muted small">Topic deleted.</div>';
        } catch(e) { alert(e.message || 'Failed to delete topic'); }
      });
      modGroup.appendChild(editBtn); modGroup.appendChild(pinBtn); modGroup.appendChild(lockBtn); modGroup.appendChild(delBtn);
      right.appendChild(modGroup);
    }
    header.appendChild(left); header.appendChild(right);
    pane.appendChild(header);

    // Typing indicator area
    const typing = h('div', { id: 'forum-typing', class: 'small text-muted mb-2' });
    pane.appendChild(typing);

  const list = h('div', { class: 'list-group list-group-flush small' });
  if (!state.posts.length) list.appendChild(h('div', { class: 'p-3 text-muted' }, ['No posts yet. Be the first to start the discussion.']));
  state.posts.forEach(p => {
      const item = h('div', { class: 'list-group-item' });
      item.appendChild(h('div', { class: 'mb-1' }, [ h('strong', {}, [p.user_name || 'User']), ' ', h('span', { class: 'text-muted' }, [ p.created_at ? new Date(p.created_at).toLocaleString() : '' ]) ]));
      const postBody = h('div', {} , [ p.body || '' ]);
      if (p.edited_at) {
        postBody.appendChild(h('span', { class: 'ms-2 badge text-bg-light' }, ['edited']));
      }
      item.appendChild(postBody);
      // Post moderation (organizer or author)
      if (state.isOrganizer || (p.user_id && window.userId && Number(p.user_id) === Number(window.userId))) {
        const mods = h('div', { class: 'mt-1 d-flex gap-2' });
        const ebtn = h('button', { class: 'btn btn-xs btn-outline-secondary', type: 'button' }, ['Edit']);
        const dbtn = h('button', { class: 'btn btn-xs btn-outline-danger', type: 'button' }, ['Delete']);
        ebtn.addEventListener('click', async () => {
          // Inline edit: replace body with textarea + Save/Cancel controls
          const existing = item.querySelector('textarea.__edit_area');
          if (existing) return; // already editing
          const original = p.body || '';
          const area = h('textarea', { class: 'form-control form-control-sm __edit_area', rows: '3' });
          area.value = original;
          const ctrl = h('div', { class: 'mt-1 d-flex gap-2' });
          const save = h('button', { class: 'btn btn-xs btn-primary', type: 'button' }, ['Save']);
          const cancel = h('button', { class: 'btn btn-xs btn-outline-secondary', type: 'button' }, ['Cancel']);
          ctrl.appendChild(save); ctrl.appendChild(cancel);
          // Swap body view
          postBody.replaceWith(area);
          item.insertBefore(ctrl, reactBar);
          cancel.addEventListener('click', () => {
            try { area.replaceWith(postBody); ctrl.remove(); } catch(_) {}
          });
          save.addEventListener('click', async () => {
            const body = (area.value || '').trim(); if (!body) return;
            save.disabled = true; save.textContent = 'Saving…';
            try {
              await api(`/api/workshops/${state.workshopId}/forum/posts/${p.id}`, { method: 'PATCH', body: JSON.stringify({ body }) });
              await loadPosts(state.topicId);
            } catch(e) { alert(e.message || 'Failed to update post'); }
          });
        });
        dbtn.addEventListener('click', async () => {
          if (!confirm('Delete this post?')) return;
          try { await api(`/api/workshops/${state.workshopId}/forum/posts/${p.id}`, { method: 'DELETE' }); await loadPosts(state.topicId); }
          catch(e) { alert(e.message || 'Failed to delete post'); }
        });
        item.appendChild(mods);
        mods.appendChild(ebtn); mods.appendChild(dbtn);
      }

      // Reactions bar
      const reactBar = h('div', { class: 'mt-2 d-flex align-items-center gap-2' });
      const renderPostReactionButton = (kind) => {
        const info = (p.reactions && p.reactions[kind]) || { count: 0, users: [], user_ids: [] };
        const countLabel = info.count ? ` (${info.count})` : '';
        const names = Array.isArray(info.users) ? info.users.slice(0,5) : [];
        const extra = (info.users && info.users.length > 5) ? `, +${info.users.length-5} more` : '';
        const title = names.length ? `${kind} by ${names.join(', ')}${extra}` : kind;
        const isMine = Array.isArray(info.user_ids) && window.userId != null && info.user_ids.includes(Number(window.userId));
        const cls = isMine ? 'btn btn-sm btn-primary' : 'btn btn-sm btn-outline-secondary';
        const b = h('button', { class: cls, type: 'button', title }, [kind + countLabel]);
        b.addEventListener('click', async () => {
          try { await api(`/api/workshops/${state.workshopId}/forum/reactions`, { method: 'POST', body: JSON.stringify({ post_id: p.id, reaction: kind }) }); }
          catch (e) { alert(e.message || 'Failed'); }
        });
        return b;
      };
      ['like','love','clap'].forEach(kind => reactBar.appendChild(renderPostReactionButton(kind)));
      item.appendChild(reactBar);

      // Replies (lazy pagination)
      const replies = h('div', { class: 'mt-2 ms-3 border-start ps-3' });
      const totalRepliesCount = Number((p.reply_count != null ? p.reply_count : (p.replies ? p.replies.length : 0)));
      const headerRow = h('div', { class: 'd-flex justify-content-between align-items-center mb-1' });
      const repliesLabel = h('div', { class: 'small text-body-secondary' }, [ `Replies (${isFinite(totalRepliesCount) ? totalRepliesCount : (p.replies ? p.replies.length : 0)})` ]);
      headerRow.appendChild(repliesLabel);
      replies.appendChild(headerRow);
      // Dedicated list host so pager/header remain intact across page loads
      const repliesListHost = h('div');
      replies.appendChild(repliesListHost);
      // Render a slice if reply_count is large and initial payload may be partial in the future
      const renderRepliesList = (list) => {
        repliesListHost.innerHTML = '';
        (list || []).forEach(r => {
        const rdiv = h('div', { class: 'mb-1' });
        rdiv.appendChild(h('strong', {}, [r.user_name || 'User']));
        rdiv.appendChild(h('span', { class: 'text-muted ms-1' }, [ r.created_at ? new Date(r.created_at).toLocaleString() : '' ]));
        const rBody = h('div', {}, [ r.body || '' ]);
        if (r.edited_at) {
          rBody.appendChild(h('span', { class: 'ms-2 badge text-bg-light' }, ['edited']));
        }
        rdiv.appendChild(rBody);
        if (state.isOrganizer || (r.user_id && window.userId && Number(r.user_id) === Number(window.userId))) {
          const mods = h('span', { class: 'ms-2 d-inline-flex gap-2' });
          const ebtn = h('button', { class: 'btn btn-xs btn-outline-secondary', type: 'button' }, ['Edit']);
          const dbtn = h('button', { class: 'btn btn-xs btn-outline-danger', type: 'button' }, ['Delete']);
          ebtn.addEventListener('click', async () => {
            const existing = rdiv.querySelector('textarea.__edit_area');
            if (existing) return;
            const original = r.body || '';
            const area = h('textarea', { class: 'form-control form-control-sm __edit_area', rows: '3' });
            area.value = original;
            const ctrl = h('div', { class: 'mt-1 d-flex gap-2' });
            const save = h('button', { class: 'btn btn-xs btn-primary', type: 'button' }, ['Save']);
            const cancel = h('button', { class: 'btn btn-xs btn-outline-secondary', type: 'button' }, ['Cancel']);
            ctrl.appendChild(save); ctrl.appendChild(cancel);
            rBody.replaceWith(area);
            rdiv.insertBefore(ctrl, rbar);
            cancel.addEventListener('click', () => {
              try { area.replaceWith(rBody); ctrl.remove(); } catch(_) {}
            });
            save.addEventListener('click', async () => {
              const body = (area.value || '').trim(); if (!body) return;
              save.disabled = true; save.textContent = 'Saving…';
              try {
                await api(`/api/workshops/${state.workshopId}/forum/replies/${r.id}`, { method: 'PATCH', body: JSON.stringify({ body }) });
                await loadPosts(state.topicId);
              } catch(e) { alert(e.message || 'Failed to update reply'); }
            });
          });
          dbtn.addEventListener('click', async () => {
            if (!confirm('Delete this reply?')) return;
            try { await api(`/api/workshops/${state.workshopId}/forum/replies/${r.id}`, { method: 'DELETE' }); await loadPosts(state.topicId); }
            catch(e) { alert(e.message || 'Failed to delete reply'); }
          });
          rdiv.appendChild(mods); mods.appendChild(ebtn); mods.appendChild(dbtn);
        }
        // Reply reactions
        const rbar = h('div', { class: 'mt-1 d-flex align-items-center gap-2' });
        const renderReplyReactionButton = (kind) => {
          const info = (r.reactions && r.reactions[kind]) || { count: 0, users: [], user_ids: [] };
          const countLabel = info.count ? ` (${info.count})` : '';
          const names = Array.isArray(info.users) ? info.users.slice(0,5) : [];
          const extra = (info.users && info.users.length > 5) ? `, +${info.users.length-5} more` : '';
          const title = names.length ? `${kind} by ${names.join(', ')}${extra}` : kind;
          const isMine = Array.isArray(info.user_ids) && window.userId != null && info.user_ids.includes(Number(window.userId));
          const cls = isMine ? 'btn btn-xs btn-primary' : 'btn btn-xs btn-outline-secondary';
          const b = h('button', { class: cls, type: 'button', title }, [kind + countLabel]);
          b.addEventListener('click', async () => {
            try { await api(`/api/workshops/${state.workshopId}/forum/reactions`, { method: 'POST', body: JSON.stringify({ reply_id: r.id, reaction: kind }) }); }
            catch (e) { alert(e.message || 'Failed'); }
          });
          return b;
        };
        ['like','love','clap'].forEach(kind => rbar.appendChild(renderReplyReactionButton(kind)));
        rdiv.appendChild(rbar);
        repliesListHost.appendChild(rdiv);
        });
      };
  // If server returned all replies but count is large, show the first page initially
  const initialList = (Array.isArray(p.replies) ? p.replies.slice(0, getReplyLimit(p.id)) : []);
  renderRepliesList(initialList);
      // If there are many replies, show pager controls under the replies with page size selector
  const totalReplies = totalRepliesCount;
      // Use per-post persisted limit
      let rLimit = getReplyLimit(p.id);
      const currentListLen = Array.isArray(p.replies) ? p.replies.length : 0;
      if ((Number.isFinite(totalReplies) && totalReplies >= rLimit) || (!Number.isFinite(totalReplies) && currentListLen > rLimit)) {
        const rPager = h('div', { class: 'd-flex justify-content-between align-items-center mt-2' });
        const rPrev = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Prev']);
        const rNext = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Next']);
        const rLast = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Last']);
        const rInfo = h('div', { class: 'small text-body-secondary' });
        const rSize = h('select', { class: 'form-select form-select-sm', style: 'width: auto;' });
        [5,10,20].forEach(n => {
          const opt = h('option', { value: String(n) }, [String(n)]);
          if (Number(n) === Number(rLimit)) opt.selected = true;
          rSize.appendChild(opt);
        });
        let rOffset = 0;
        async function loadRepliesPage(off) {
          const url = `/api/workshops/${state.workshopId}/forum/posts/${p.id}/replies?limit=${encodeURIComponent(rLimit)}&offset=${encodeURIComponent(off)}`;
          const res = await api(url);
          const list = res.replies || [];
          renderRepliesList(list);
          rOffset = res.pagination?.offset || off;
          const limit = res.pagination?.limit || rLimit;
          const total = res.pagination?.total || totalReplies;
          const returned = res.pagination?.returned || list.length;
          const start = Math.min(total, rOffset + 1);
          const end = Math.min(total, rOffset + returned);
          rInfo.textContent = `Showing ${start}–${end} of ${total}`;
          const nextOff = rOffset + limit;
          const prevOff = Math.max(0, rOffset - limit);
          const lastOff = total > 0 ? Math.floor((total - 1) / limit) * limit : 0;
          rPrev.disabled = !(rOffset > 0);
          rNext.disabled = !(nextOff < total);
          rLast.disabled = !(total > limit);
          rPrev.onclick = () => loadRepliesPage(prevOff);
          rNext.onclick = () => loadRepliesPage(nextOff);
          rLast.onclick = () => loadRepliesPage(lastOff);
        }
        rSize.addEventListener('change', () => {
          const newLimit = Number(rSize.value) || rLimit;
          rLimit = newLimit;
          setReplyLimit(p.id, rLimit);
          loadRepliesPage(0);
        });
        const left = h('div', { class: 'd-flex align-items-center gap-2' });
        left.appendChild(rPrev); left.appendChild(rNext); left.appendChild(rLast);
        const right = h('div', { class: 'd-flex align-items-center gap-2' });
        right.appendChild(rInfo); right.appendChild(rSize);
        rPager.appendChild(left); rPager.appendChild(right);
        replies.appendChild(rPager);
        // Initial page
        loadRepliesPage(0);
      }
      // Reply form
      const frm = h('form', { class: 'mt-2' });
      const ta = h('textarea', { class: 'form-control form-control-sm', rows: '2', placeholder: 'Write a reply…' });
      const btn = h('button', { class: 'btn btn-sm btn-outline-primary mt-2', type: 'submit' }, ['Reply']);
      frm.appendChild(ta); frm.appendChild(btn);
      // typing emitters for replies
      ta.addEventListener('input', () => emitTyping());
      frm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const txt = (ta.value || '').trim();
        if (!txt) return;
        if (state.topicMeta && state.topicMeta.locked) { alert('Topic is locked.'); return; }
        btn.disabled = true; btn.innerHTML = 'Posting…';
        try {
          await createReply(p.id, txt);
          ta.value = '';
        } catch (err) {
          alert(err.message || 'Failed to reply');
        } finally {
          btn.disabled = false; btn.innerHTML = 'Reply';
        }
      });

      item.appendChild(replies);
      item.appendChild(frm);
      list.appendChild(item);
    });
    pane.appendChild(list);

  // New post form
    const form = h('form', { class: 'mt-3' });
    const ta = h('textarea', { id: 'forum-new-post', class: 'form-control', rows: '3', placeholder: 'Start a new post…' });
    const btn = h('button', { class: 'btn btn-primary mt-2', type: 'submit' }, ['Post']);
    form.appendChild(ta); form.appendChild(btn);
    ta.addEventListener('input', () => emitTyping());
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const txt = (ta.value || '').trim();
      if (!txt) return;
      if (state.topicMeta && state.topicMeta.locked) { alert('Topic is locked.'); return; }
      btn.disabled = true; btn.innerHTML = 'Posting…';
      try {
        await createPost(txt);
        ta.value = '';
      } catch (err) {
        alert(err.message || 'Failed to post');
      } finally {
        btn.disabled = false; btn.innerHTML = 'Post';
      }
    });
    pane.appendChild(form);

    // Pagination controls for posts + page position and page size
    const pager = h('div', { class: 'd-flex justify-content-between align-items-center mt-2' });
    const prev = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Prev']);
    const next = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Next']);
    const last = h('button', { class: 'btn btn-sm btn-outline-secondary', type: 'button' }, ['Last']);
    const info = (pagination && pagination.total != null)
      ? h('div', { class: 'small text-body-secondary' }, [
          `Showing ${Math.min(pagination.total, pagination.offset + 1)}–${Math.min(pagination.total, pagination.offset + pagination.returned)} of ${pagination.total}`
        ])
      : h('div');
    // Page size selector
    const sizeSel = h('select', { class: 'form-select form-select-sm', style: 'width: auto;' });
    [5,10,20].forEach(n => {
      const opt = h('option', { value: String(n) }, [String(n)]);
      if (Number(n) === Number(state.postsLimit)) opt.selected = true;
      sizeSel.appendChild(opt);
    });
    sizeSel.addEventListener('change', () => {
      state.postsLimit = Number(sizeSel.value) || state.postsLimit;
      state.postsOffset = 0; // reset to first page on size change
      loadPosts(topic.id);
    });
    prev.disabled = !(links && links.prev);
    next.disabled = !(links && links.next);
    last.disabled = !(links && links.last);
    prev.addEventListener('click', () => { state.postsOffset = Math.max(0, (pagination?.offset||0) - (pagination?.limit||state.postsLimit)); links && links.prev && loadPosts(topic.id, links.prev); });
    next.addEventListener('click', () => { state.postsOffset = (pagination?.offset||0) + (pagination?.limit||state.postsLimit); links && links.next && loadPosts(topic.id, links.next); });
    last.addEventListener('click', () => links && links.last && loadPosts(topic.id, links.last));
    const leftGrp = h('div', { class: 'd-flex align-items-center gap-2' });
    leftGrp.appendChild(prev); leftGrp.appendChild(next); leftGrp.appendChild(last);
    const rightGrp = h('div', { class: 'd-flex align-items-center gap-2' });
    rightGrp.appendChild(info); rightGrp.appendChild(sizeSel);
    pager.appendChild(leftGrp); pager.appendChild(rightGrp);
    pane.appendChild(pager);
  }

  async function loadEnrichment(topicId) {
    try {
      const res = await api(`/api/workshops/${state.workshopId}/forum/topics/${topicId}/enrichment`);
      const right = document.getElementById('discussion-forum-right');
      const pane = right ? right.querySelector('#forum-posts-pane') : null;
      const host = pane || right || document.body;
      const box = h('div', { class: 'alert alert-info small mt-2' });
      const items = [];
      (res.enrichment?.related_ideas || []).forEach(ri => items.push(`Idea #${ri.id}: ${ri.content}`));
      (res.enrichment?.tags || []).forEach(t => items.push(`#${t}`));
      (res.enrichment?.insights || []).forEach(ins => items.push(`Insight: ${ins}`));
      box.innerHTML = items.length ? items.map(x => `<div>${x}</div>`).join('') : 'No related items yet.';
      host.appendChild(box);
      setTimeout(() => { try { box.remove(); } catch(_){} }, 8000);
    } catch (e) { alert(e.message || 'Failed to load enrichment'); }
  }

  function attachSocketListeners(socket, workshopId) {
    if (!socket) return;
    if (state._socketBound) return; // avoid duplicate bindings
    state._socketBound = true;
    try {
      if (window.__forumSocketListenersBound) return;
      window.__forumSocketListenersBound = true;
    } catch(_) {}
    socket.on('forum_post_created', (d) => {
      if (!d || d.workshop_id !== workshopId) return;
      if (state.topicId && d.topic_id === state.topicId) {
        // Avoid duplicates if we already appended optimistically
        const existing = state.posts.find(p => Number(p.id) === Number(d.id));
        if (existing) {
          // Upgrade optimistic entry with authoritative data
          existing.user_id = d.user_id;
          existing.user_name = d.user_name;
          existing.body = d.body;
          existing.created_at = d.created_at;
          delete existing.__optimistic;
        } else {
          state.posts.push({ id: d.id, user_id: d.user_id, user_name: d.user_name, body: d.body, created_at: d.created_at, replies: [] });
        }
        renderPosts(state.topicMeta || { id: state.topicId });
      }
    });
    socket.on('forum_reply_created', (d) => {
      if (!d || d.workshop_id !== workshopId) return;
      const post = state.posts.find(p => p.id === d.post_id);
      if (post) {
        post.replies = post.replies || [];
        const rexisting = post.replies.find(r => Number(r.id) === Number(d.id));
        if (rexisting) {
          rexisting.user_id = d.user_id;
          rexisting.user_name = d.user_name;
          rexisting.body = d.body;
          rexisting.created_at = d.created_at;
        } else {
          post.replies.push({ id: d.id, user_id: d.user_id, user_name: d.user_name, body: d.body, created_at: d.created_at });
        }
        renderPosts(state.topicMeta || { id: state.topicId });
      }
    });
    // Live reaction updates
    socket.on('forum_reaction_updated', (d) => {
      if (!d || d.workshop_id !== workshopId) return;
      if (!state.posts || !state.posts.length) return;
      const targetKind = d.reaction || 'like';
      const uid = Number(d.user_id);
      const applyUpdate = (obj) => {
        obj.reactions = obj.reactions || {};
        const info = obj.reactions[targetKind] || { count: 0, users: [], user_ids: [] };
        // Maintain user_ids set
        const set = new Set(Array.isArray(info.user_ids) ? info.user_ids.map(Number) : []);
        const had = set.has(uid);
        if (d.toggled === 'on') {
          if (!had) {
            set.add(uid);
            info.count = (info.count || 0) + 1;
          }
        } else {
          if (had) {
            set.delete(uid);
            info.count = Math.max(0, (info.count || 0) - 1);
          }
        }
        info.user_ids = Array.from(set);
        // We can’t know the display name without a map; keep users list as-is or rebuild lightly
        obj.reactions[targetKind] = info;
      };
      if (d.post_id) {
        const post = state.posts.find(p => p.id === d.post_id);
        if (post) applyUpdate(post);
      }
      if (d.reply_id) {
        state.posts.forEach(p => {
          const rep = (p.replies || []).find(r => r.id === d.reply_id);
          if (rep) applyUpdate(rep);
        });
      }
      // Re-render to refresh counts and button styles
      if (state.topicId) renderPosts(state.topicMeta || { id: state.topicId });
    });
    socket.on('forum_seed_done', (d) => {
      if (!d || d.workshop_id !== workshopId) return;
      // Refresh categories to show new content
      loadCategories();
    });
    // Typing indicators
    socket.on('forum_typing', (d) => {
      if (!d || d.workshop_id !== workshopId) return;
      if (state.topicId !== d.topic_id) return;
      const key = String(d.user_id);
      if (d.is_typing) state.typingUsers.add(key); else state.typingUsers.delete(key);
      const el = document.getElementById('forum-typing');
      if (el) {
        const n = state.typingUsers.size;
        el.textContent = n ? `${n} user${n>1?'s':''} typing…` : '';
      }
    });
  }

  function emitTyping() {
    try {
      if (!window.socket || !state.topicId) return;
      window.socket.emit('forum_typing', {
        room: `workshop_room_${state.workshopId}`,
        workshop_id: state.workshopId,
        user_id: (window.userId || null),
        topic_id: state.topicId,
        is_typing: true,
      });
      // Debounce a stop event after inactivity
      clearTimeout(state._typingTimer);
      state._typingTimer = setTimeout(() => {
        try {
          window.socket.emit('forum_typing', {
            room: `workshop_room_${state.workshopId}`,
            workshop_id: state.workshopId,
            user_id: (window.userId || null),
            topic_id: state.topicId,
            is_typing: false,
          });
        } catch(_) {}
      }, 1200);
    } catch(_) {}
  }

  // Public entrypoint for workshop room
  window.DiscussionForum = {
    mount: async function(workshopId) {
      state.workshopId = workshopId;
      state.isOrganizer = !!(window.isOrganizer);
      const root = document.getElementById('discussion-forum-root');
      const right = document.getElementById('discussion-forum-right');
      if (!root || !right) return;
      // Prime socket listeners
      try { attachSocketListeners(window.socket, workshopId); } catch(_) {}
      // Load base or deep-link target
      await loadCategories();
      const hash = parseForumHash();
      if (hash.topicsLimit) state.topicsLimit = hash.topicsLimit;
      if (hash.topicsOffset) state.topicsOffset = hash.topicsOffset;
      if (hash.postsLimit) state.postsLimit = hash.postsLimit;
      if (hash.postsOffset) state.postsOffset = hash.postsOffset;
      if (hash.categoryId) {
        await loadTopics(hash.categoryId);
        if (hash.topicId) await loadPosts(hash.topicId);
      }
    }
  };
})();
