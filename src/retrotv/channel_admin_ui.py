CHANNEL_ADMIN_HTML = """
<details class="panel" id="channel-folder-review"><summary>Channel folders and queued deletions <span id="channel-folder-count">Checking…</span></summary>
<div style="padding:16px"><p>Missing folders require your choice. Recreate makes empty template folders; it cannot restore deleted videos. Deletion requires review and confirmation.</p>
<button type="button" id="refresh-channel-admin">Refresh channel status</button>
<p id="channel-folder-message" role="status"></p><div id="channel-folder-items"></div>
<h3>Deletion jobs</h3><div id="channel-deletion-jobs" aria-live="polite"></div></div></details>
"""

CHANNEL_ADMIN_JS = r"""
(() => {
  const el = id => document.getElementById(id);
  let loading = false, foldersAnnounced = false, jobsAnnounced = false;
  async function request(url, payload) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 15000);
    try {
      const response = await fetch(url, {signal: abort.signal, ...(payload ? {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)
      } : {})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Channel status request failed');
      return data;
    } finally { clearTimeout(timer); }
  }
  function line(parent, text) {
    const p = document.createElement('p'); p.textContent = text; parent.append(p); return p;
  }
  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      const results = await Promise.allSettled([
        request('/api/channels/missing-folders'), request('/api/channels/deletions')
      ]);
      const folders = results[0], jobs = results[1];
      if (folders.status === 'fulfilled') {
        const data = folders.value;
        el('channel-folder-message').textContent = data.available ? '' : data.detail;
        el('channel-folder-count').textContent = data.available ? String(data.items.length) : 'Unavailable';
        const list = el('channel-folder-items'); list.replaceChildren();
        if (data.items.length && !foldersAnnounced) el('channel-folder-review').open = true;
        foldersAnnounced = data.items.length > 0;
        for (const item of data.items) {
          const p = line(list, `${item.display_name} — missing folder: ${item.path} `);
          const recreate = document.createElement('button'); recreate.textContent = 'Recreate folders';
          recreate.onclick = async () => {
            recreate.disabled = true;
            try {
              const result = await request(`/api/channels/${encodeURIComponent(item.slug)}/recreate-folders`, {channel_identity:item.management_identity});
              el('channel-folder-message').textContent = result.detail;
              p.remove();
              await refresh();
            } catch (e) { el('channel-folder-message').textContent = e.message; }
            finally { recreate.disabled = false; }
          };
          const remove = document.createElement('a'); remove.textContent = 'Review deletion';
          remove.href = `/internet-media?delete=${encodeURIComponent(item.slug)}`;
          remove.style.marginLeft = '12px'; p.append(recreate, remove);
        }
        if (data.available && !data.items.length) line(list, 'No missing channel folders need review.');
      } else {
        el('channel-folder-message').textContent = `Channel folder status unavailable: ${folders.reason.message}. Use Refresh channel status to retry.`;
      }
      if (jobs.status === 'fulfilled') {
        const list = el('channel-deletion-jobs'); list.replaceChildren();
        const data = jobs.value;
        if (data.error) line(list, data.error);
        for (const job of data.jobs.slice(-20).reverse()) {
          const p = line(list, `${job.display_name}: ${job.state} — ${job.error || job.detail}`);
          if (['queued', 'waiting'].includes(job.state)) {
            const cancel = document.createElement('button'); cancel.textContent = 'Cancel deletion';
            cancel.onclick = async () => {
              cancel.disabled = true;
              try { await request(`/api/channels/deletions/${encodeURIComponent(job.id)}/cancel`, {}); await refresh(); }
              catch (e) { el('channel-folder-message').textContent = e.message; }
              finally { cancel.disabled = false; }
            };
            p.append(' ', cancel);
          }
          if (job.state === 'failed') {
            const retry = document.createElement('a'); retry.textContent = 'Review and retry';
            retry.href = `/internet-media?delete=${encodeURIComponent(job.slug)}`; p.append(' ', retry);
          }
        }
        const activeJobs = data.jobs.some(job => ['queued','waiting','running'].includes(job.state));
        if (activeJobs && !jobsAnnounced) el('channel-folder-review').open = true;
        jobsAnnounced = activeJobs;
        if (!data.jobs.length && !data.error) line(list, 'No deletion jobs.');
      } else {
        el('channel-deletion-jobs').textContent = `Deletion status unavailable: ${jobs.reason.message}`;
      }
    } finally { loading = false; }
  }
  el('refresh-channel-admin').onclick = refresh;
  window.refreshChannelAdministration = refresh;
  refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 5000);
})();
"""
