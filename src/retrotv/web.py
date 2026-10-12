from __future__ import annotations

import json
from contextlib import closing
import base64
import binascii
import os
import posixpath
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from retrotv.asset_paths import resolve_asset_path, is_external
from retrotv.external_media import ExternalMediaService
from retrotv.config import AppConfig
from retrotv import __version__
from retrotv.commercial_review import CommercialReviewService
from retrotv.holiday_review import HolidayReviewService
from retrotv.credentials import (configured_breeze_api_key, save_breeze_key, remove_breeze_key,
    configured_openai_api_key, save_openai_key, remove_openai_key)
from retrotv.tts import BreezeTTSProvider, provider_for_profile
from types import SimpleNamespace
from retrotv.channel_management import ChannelDeletionService
from retrotv.blocks import BlockService
from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.bumper_cues import bumper_review_catalog, save_bumper_override
from retrotv.audio_normalization import AudioNormalizationService
from retrotv.guide import GuideService
from retrotv.db import connect_database, utc_now
from retrotv.duplicates import DuplicateService
from retrotv.normalization import Crop, MODES, NormalizationService
from retrotv.playback import PlaybackError
from retrotv.promotion import PromotionService
from retrotv.scheduling import ScheduleBuilder
from retrotv.settings import SettingsService
from retrotv.graphics import AUDIO_EXTENSIONS, GRAPHIC_EXTENSIONS, GraphicsService
from retrotv.graphics_lab import GraphicsLab
from retrotv.internet_media import InternetMediaService, PreparationService
from retrotv.restore_point import RestorePointService
from retrotv.ai_announcements import (
    AnnouncementProfileService,
    OpenAIHTTPClient,
    OpenAIScriptProvider,
    OpenAITTSProvider,
    configured_openai_api_key,
)
from retrotv.shadows import ShadowService
from retrotv.synthetic import weather_channel_definition


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>CRTV Sim Dashboard</title>
  <style>
    :root { color-scheme: dark; --bg:#0d1118; --panel:#19212d; --line:#334154;
      --text:#f3f6fa; --muted:#9eacbd; --accent:#67d6a0; --warn:#f1bd64; --bad:#ff858d; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text);
      font:15px/1.45 system-ui,sans-serif; }
    header { padding:22px clamp(18px,4vw,48px); background:#121923;
      border-bottom:1px solid var(--line); display:flex; align-items:center;
      justify-content:space-between; gap:20px; }
    h1,h2,p { margin-top:0; } h1 { margin-bottom:3px; }
    nav { display:flex; flex-wrap:wrap; gap:14px; }
    a { color:var(--accent); }
    main { padding:24px clamp(18px,4vw,48px); max-width:1200px; margin:auto; }
    .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
      gap:14px; margin-bottom:20px; }
    .card,.panel { background:var(--panel); border:1px solid var(--line);
      border-radius:12px; padding:18px; }
    .label { color:var(--muted); font-size:12px; text-transform:uppercase;
      letter-spacing:.08em; }
    .value { font-size:25px; font-weight:700; margin-top:5px; overflow-wrap:anywhere; }
    .good { color:var(--accent); } .warn { color:var(--warn); } .bad { color:var(--bad); }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
      gap:16px; }
    .controls { display:flex; flex-wrap:wrap; gap:9px; align-items:center; }
    button,select,input { color:var(--text); background:#0d141d; border:1px solid var(--line);
      border-radius:7px; padding:9px 11px; font:inherit; }
    button { cursor:pointer; } button:hover { border-color:var(--accent); }
    button.primary { background:#174b38; border-color:#348965; }
    button.danger { border-color:#784046; }
    select,input { min-width:150px; }
    dl { display:grid; grid-template-columns:max-content 1fr; gap:7px 14px; margin-bottom:0; }
    dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
    .message { min-height:24px; margin:12px 0 0; color:var(--muted); }
    .message.waiting { color:var(--warn); font-weight:700; }
    .message.safe { color:var(--accent); font-weight:700; }
    .keys { color:var(--muted); font-size:13px; }
  </style>
</head>
<body>
  <header>
    <div><h1>CRTV Sim</h1><p>Windows Application Dashboard</p></div>
    <nav><a href="/guide">Live Guide</a><a href="/library">Library &amp; Schedules</a><a href="/internet-media">Internet Media</a><a href="/settings">Settings</a></nav>
  </header>
  <main>
    <section id="weather-setup-required" class="panel" role="alert" hidden style="margin-bottom:20px;border-color:var(--warn)">
      <h2>Finish RetroWeather setup</h2>
      <p id="weather-setup-message">Choose your location, then fetch weather and create the channel. Weather stays out of channel navigation until its first playable video is ready.</p>
      <a id="weather-setup-link" href="http://127.0.0.1:8775/#setup" target="_blank" rel="noopener">Set up RetroWeather</a>
      <p class="keys">The Guide and other channels remain available. You can disable Weather in Settings if you do not want to use it.</p>
    </section>
    <section class="cards">
      <div class="card"><div class="label">Television</div><div id="tv-state" class="value">Loading…</div></div>
      <div class="card"><div class="label">Channel</div><div id="channel" class="value">—</div></div>
      <div class="card"><div class="label">Now Playing</div><div id="now" class="value">—</div>
        <button id="re-review-normalization" disabled>Re-review normalization</button>
        <div id="re-review-status" class="label"></div>
        <div id="missing-media-status" class="label" style="overflow-wrap:anywhere"></div></div>
      <div class="card"><div class="label">Maintenance</div><div id="maintenance-state" class="value">—</div></div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Television controls</h2>
        <div class="controls">
          <select id="channel-select" aria-label="Channel"></select>
          <button id="start" class="primary">Start TV</button>
          <button id="tune">Tune</button>
          <button id="guide">Guide</button>
          <button id="stop" class="danger">Stop TV</button>
          <button id="exit" class="danger">Shut down safely</button>
        </div>
        <div class="controls" style="margin-top:10px">
          <input id="channel-number" inputmode="numeric" placeholder="Channel number">
          <button id="tune-number">Enter channel</button>
        </div>
        <p class="keys">While mpv has focus: type a channel number and Enter.
          G opens the Guide, ↑/↓ changes channels, L returns to the last channel,
          R retunes, and Q stops television playback.</p>
        <p class="keys"><strong>External drive:</strong> use <em>Shut down safely</em>
          and wait for the green confirmation before turning off the computer or
          using Windows Eject/Safely Remove Hardware.</p>
        <p id="message" class="message"></p>
      </div>
      <div class="panel">
        <h2>Automatic maintenance</h2>
        <dl>
          <dt>Readiness pipeline</dt><dd id="readiness-state">—</dd>
          <dt>Pipeline stage</dt><dd id="readiness-stage">—</dd>
          <dt>Stage</dt><dd id="maintenance-stage">—</dd>
          <dt>Last scan</dt><dd id="scan-result">—</dd>
          <dt>Commercial black tails</dt><dd id="commercial-tail-result">—</dd>
          <dt>Exact hashes</dt><dd id="duplicate-maintenance-result">—</dd>
          <dt>New analysis</dt><dd id="normalization-result">—</dd>
          <dt>Act breaks</dt><dd id="breakpoint-result">—</dd>
          <dt>Audio loudness</dt><dd id="audio-maintenance-result">—</dd>
          <dt>Schedules created</dt><dd id="schedules-created">—</dd>
          <dt>Guide rendered</dt><dd id="guide-rendered">—</dd>
          <dt>File cleanup</dt><dd id="cleanup-result">—</dd>
          <dt>Database backup</dt><dd id="database-backup-result">—</dd>
          <dt>Database housekeeping</dt><dd id="database-housekeeping-result">—</dd>
          <dt>Maintenance window</dt><dd id="maintenance-window-state">—</dd>
          <dt>Window stage</dt><dd id="maintenance-window-stage">—</dd>
          <dt>Next window</dt><dd id="maintenance-window-next">—</dd>
          <dt>Completed</dt><dd id="maintenance-completed">—</dd>
          <dt>Error</dt><dd id="maintenance-error">—</dd>
        </dl>
        <div class="controls" style="margin-top:16px">
          <button id="maintain">Scan &amp; update now</button>
          <button id="refresh-guide">Refresh Guide now</button>
          <button id="cleanup-generated">Clean files and expired backups now</button>
          <button id="backup-database">Back up database now</button>
          <button id="housekeeping-database">Purge stale database records now</button>
          <button id="empty-internet-cache">Empty Internet Cache</button>
          <button id="compact-database">Compact Database</button>
          <button id="skip-maintenance-window" hidden>Start TV and skip this window</button>
        </div>
      </div>
      <div class="panel">
        <h2>System readiness</h2>
        <dl>
          <dt>Profile</dt><dd id="profile">—</dd>
          <dt>Media root</dt><dd id="media-root">—</dd>
          <dt>FFmpeg</dt><dd id="ffmpeg-ready">—</dd>
          <dt>FFprobe</dt><dd id="ffprobe-ready">—</dd>
          <dt>mpv</dt><dd id="mpv-ready">—</dd>
        </dl>
      </div>
      <div class="panel">
        <h2>Weather channel</h2>
        <dl>
          <dt>Companion</dt><dd id="weather-companion">--</dd>
          <dt>Process ownership</dt><dd id="weather-ownership">--</dd>
          <dt>Weather update</dt><dd id="weather-update">--</dd>
          <dt>Enabled</dt><dd id="weather-enabled">—</dd>
          <dt>Readiness</dt><dd id="weather-ready">—</dd>
          <dt>Current segment</dt><dd id="weather-segment">—</dd>
          <dt>Generation</dt><dd id="weather-generation">—</dd>
          <dt>Publication age</dt><dd id="weather-age">—</dd>
          <dt>Valid until</dt><dd id="weather-valid-until">—</dd>
          <dt>Last-known-good</dt><dd id="weather-last-good">—</dd>
          <dt>Warning</dt><dd id="weather-warning">—</dd>
        </dl>
        <div class="controls" style="margin-top:14px">
          <button id="weather-restart">Restart RetroWeather</button>
          <a id="weather-dashboard" href="http://127.0.0.1:8775/" target="_blank" rel="noopener">Open RetroWeather</a>
        </div>
      </div>
      <div class="panel">
        <h2>Demand-driven announcer</h2>
        <dl>
          <dt>Demand poller</dt><dd id="announce-poller">--</dd>
          <dt>Last demand check</dt><dd id="announce-last-poll">--</dd>
          <dt>Tuned since</dt><dd id="announce-tuned">—</dd>
          <dt>Last human input</dt><dd id="announce-activity">—</dd>
          <dt>Next opportunity</dt><dd id="announce-next">—</dd>
          <dt>Eligibility</dt><dd id="announce-reason">—</dd>
          <dt>Generation</dt><dd id="announce-state">—</dd>
          <dt>Error</dt><dd id="announce-error">—</dd>
        </dl>
      </div>
      <div class="panel">
        <h2>Approved media normalization</h2>
        <p>Convert approved files that require cropping, unstretching, a custom
          correction, or downscaling to the configured output ceiling. Native
          and preserved files at or below that ceiling are not re-encoded.
          Pending work here, Library review, or Aspect review causes the Guide's
          yellow [!] indicator.</p>
        <dl>
          <dt>Pending corrections</dt><dd id="batch-pending">0</dd>
          <dt>Policy</dt><dd id="batch-policy">—</dd>
          <dt>State</dt><dd id="batch-state">—</dd>
          <dt>Progress</dt><dd id="batch-progress">—</dd>
          <dt>Converted</dt><dd id="batch-converted">—</dd>
          <dt>Reused duplicate result</dt><dd id="batch-reused">—</dd>
          <dt>Already done</dt><dd id="batch-skipped">—</dd>
          <dt>No conversion needed</dt><dd id="batch-not-needed">—</dd>
          <dt>Failed</dt><dd id="batch-failed">—</dd>
          <dt>Current file</dt><dd id="batch-current">—</dd>
          <dt>Completed</dt><dd id="batch-completed">—</dd>
          <dt>Error</dt><dd id="batch-error">—</dd>
        </dl>
        <div class="controls" style="margin-top:16px">
          <button id="convert-approved" class="primary">Convert approved media</button>
        </div>
      </div>
      <div class="panel">
        <h2>Audio loudness normalization</h2>
        <p>Measure perceived loudness and normalize files to the configured
          target. Video is copied unchanged; only the audio stream is encoded.
          The immediate pre-audio working file receives a verified backup.</p>
        <dl>
          <dt>Not yet analyzed</dt><dd id="audio-analysis-pending">0</dd>
          <dt>Needs review</dt><dd id="audio-review-pending">0</dd>
          <dt>Ready to normalize</dt><dd id="audio-correction-pending">0</dd>
          <dt>Analysis state</dt><dd id="audio-analysis-state">—</dd>
          <dt>Analysis progress</dt><dd id="audio-analysis-progress">—</dd>
          <dt>Corrections found</dt><dd id="audio-analysis-corrections">—</dd>
          <dt>Normalization state</dt><dd id="audio-normalization-state">—</dd>
          <dt>Normalization progress</dt><dd id="audio-normalization-progress">—</dd>
          <dt>Normalized</dt><dd id="audio-normalized">—</dd>
          <dt>Failed</dt><dd id="audio-failed">—</dd>
          <dt>Current file</dt><dd id="audio-current">—</dd>
          <dt>Error</dt><dd id="audio-error">—</dd>
        </dl>
        <div class="controls" style="margin-top:16px">
          <button id="analyze-audio">Analyze pending audio</button>
          <button id="normalize-audio" class="primary">Normalize approved audio</button>
        </div>
      </div>
    </section>
  </main>
  <script>
    const $ = id => document.getElementById(id);
    let lastChannels = "";
    let playingAssetId = null;
    let dashboardRefreshTimer = null;
    let safeShutdownRequested = false;
    async function request(path, options) {
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
      return data;
    }
    async function command(action, extra={}) {
      try {
        const data = await request(`/api/runtime/${action}`, {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify(extra)
        });
        $("message").textContent = data.message || "Command accepted.";
        await refresh();
      } catch (error) {
        if (!safeShutdownRequested) $("message").textContent = error.message;
      }
    }

    function channelLabel(item) {
      return `${item.channel_number ?? "—"} · ${item.display_name}`;
    }
    function fileSize(bytes) {
      if (!bytes) return "0 MB";
      return `${(bytes / 1048576).toFixed(bytes >= 10485760 ? 0 : 1)} MB`;
    }
    function shortAge(seconds) {
      if (seconds == null) return "—";
      if (seconds < 120) return `${Math.round(seconds)} seconds`;
      if (seconds < 7200) return `${Math.round(seconds / 60)} minutes`;
      return `${(seconds / 3600).toFixed(1)} hours`;
    }
    async function refresh() {
      try {
        const data = await request("/api/runtime/status");
        $("tv-state").textContent = data.tv_state;
        $("tv-state").className = `value ${data.tv_state === "playing" ? "good" :
          data.tv_state === "error" ? "bad" : "warn"}`;
        const selected = data.channels.find(item => item.slug === data.requested_channel);
        $("channel").textContent = selected ? channelLabel(selected) : "—";
        $("now").textContent = data.now_playing?.title || "—";
        playingAssetId = data.now_playing?.asset_id || null;
        const review = data.normalization_review || {};
        $("re-review-normalization").disabled = !playingAssetId || data.now_playing?.external || review.state === "running";
        $("re-review-status").textContent = review.error || review.message ||
          (review.state === "running" ? "Stopping playback and preparing a fresh review…" : "");
        const missing = data.now_playing?.missing_media;
        $("missing-media-status").textContent = missing ?
          `Missing asset ${missing.asset_id}: ${missing.path}. Continuity is filling this slot. Recovery: ${data.playback_recovery?.state || "queued"}` : "";
        const readiness = data.readiness || {state: "idle", stage: null};
        $("maintenance-state").textContent = readiness.state;
        $("maintenance-state").className = `value ${readiness.state === "error" ? "bad" :
          readiness.state === "running" ? "warn" : "good"}`;
        const signature = JSON.stringify(data.channels);
        if (signature !== lastChannels) {
          $("channel-select").replaceChildren(...data.channels.map(item => {
            const option = document.createElement("option");
            option.value = item.slug; option.textContent = channelLabel(item); return option;
          }));
          lastChannels = signature;
        }
        if (data.requested_channel) $("channel-select").value = data.requested_channel;
        const maintenance = data.maintenance;
        $("readiness-state").textContent = readiness.state || "Idle";
        $("readiness-state").className = readiness.state === "error" ? "bad" :
          readiness.state === "running" ? "warn" : "good";
        $("readiness-stage").textContent = readiness.stage ||
          (readiness.state === "error" && readiness.failed_stage ? `Failed during ${readiness.failed_stage}` :
          (readiness.state === "deferred" ? "Waiting for TV to stop" : "Idle"));
        $("maintenance-stage").textContent = maintenance.stage || "Idle";
        $("scan-result").textContent = maintenance.scan ?
          `${maintenance.scan.discovered} found; ${maintenance.scan.new_or_changed_ready} changed; ${maintenance.scan.review} review; ${maintenance.scan.missing} removed` : "—";
        $("commercial-tail-result").textContent = maintenance.commercial_tails ?
          `${maintenance.commercial_tails.analyzed} analyzed; ${maintenance.commercial_tails.trimmed} trimmed; ${maintenance.commercial_tails.skipped} left unchanged; ${maintenance.commercial_tails.failed} failed` : "—";
        $("duplicate-maintenance-result").textContent = maintenance.duplicates ?
          `${maintenance.duplicates.hashed} hashed; ${maintenance.duplicates.reused_analysis} analyses reused; ${maintenance.duplicates.errors} errors` : "—";
        $("normalization-result").textContent = maintenance.normalization ?
          `${maintenance.normalization.processed} processed; ${maintenance.normalization.review} review; ${maintenance.normalization.errors} errors` : "—";
        $("breakpoint-result").textContent = maintenance.breakpoints ?
          `${maintenance.breakpoints.processed} processed; ${maintenance.breakpoints.natural} natural; ${maintenance.breakpoints.midpoint} midpoint; ${maintenance.breakpoints.errors} errors` : "—";
        $("audio-maintenance-result").textContent = maintenance.audio ?
          `${maintenance.audio.processed} processed; ${maintenance.audio.correction_needed} correction; ${maintenance.audio.review} review; ${maintenance.audio.errors} errors` : "—";
        $("schedules-created").textContent = maintenance.schedules_created ?? "—";
        $("guide-rendered").textContent = maintenance.guide_rendered ? "Yes" : "No";
        const cleanup = data.generated_cleanup;
        $("cleanup-result").textContent = cleanup.state === "running" ? "Running…" :
          cleanup.skipped ? (cleanup.reason || "Not due") :
          cleanup.completed_at ? `${cleanup.files_removed} files; ${fileSize(cleanup.bytes_removed)} recovered` : "—";
        const databaseBackup = data.database_backup || {};
        $("database-backup-result").textContent = databaseBackup.state === "running" ? "Running…" :
          databaseBackup.error ? `Failed: ${databaseBackup.error}` :
          databaseBackup.latest_path ?
            `${databaseBackup.retained_count} retained; latest ${databaseBackup.latest_modified_at || databaseBackup.completed_at || "available"}` :
            (databaseBackup.enabled === false ? "Disabled" : "No backup yet");
        const housekeeping = data.database_housekeeping || {};
        const removedRows = Object.values(housekeeping.rows_removed || {}).reduce((a,b)=>a+Number(b||0),0);
        $("database-housekeeping-result").textContent = housekeeping.state === "running" ? "Running…" :
          housekeeping.error ? `Failed: ${housekeeping.error}` :
          housekeeping.state === "skipped" ? (housekeeping.reason || "Not due") :
          housekeeping.completed_at ? `${removedRows} stale rows removed; ${fileSize(housekeeping.reclaimable_bytes)} reclaimable` : "—";
        const windowState = data.maintenance_window || {};
        const remaining = windowState.seconds_remaining == null ? "" : ` · ${shortAge(windowState.seconds_remaining)} remaining`;
        $("maintenance-window-state").textContent = windowState.enabled ? `${windowState.state}${remaining}` : "Disabled";
        $("maintenance-window-stage").textContent = windowState.stage || windowState.reason || "—";
        $("maintenance-window-next").textContent = windowState.next_starts_at || "—";
        $("skip-maintenance-window").hidden = !["active","ending"].includes(windowState.state);
        $("maintenance-completed").textContent = maintenance.completed_at || "—";
        $("maintenance-error").textContent = readiness.error || maintenance.error || "—";
        const diagnostics = data.diagnostics;
        $("profile").textContent = diagnostics.profile;
        $("media-root").textContent = `${diagnostics.media_root} (${diagnostics.media_root_available ? "ready" : "missing"})`;
        $("ffmpeg-ready").textContent = diagnostics.ffmpeg_available ? "Ready" : "Missing";
        $("ffprobe-ready").textContent = diagnostics.ffprobe_available ? "Ready" : "Missing";
        $("mpv-ready").textContent = diagnostics.mpv_available ? "Ready" : "Missing";
        const weather = data.synthetic_channels?.weather;
        if (weather) {
          $("weather-setup-required").hidden = !weather.setup_required;
          $("weather-setup-message").textContent = weather.setup_message || "";
          if (weather.dashboard_url) $("weather-setup-link").href = weather.dashboard_url + "#setup";
          const companion = weather.companion || {};
          $("weather-companion").textContent = companion.state || "Unavailable";
          $("weather-companion").className = companion.healthy ? "good" :
            (companion.state === "starting" || companion.state === "updating" ? "warn" : "");
          $("weather-ownership").textContent = companion.owned ?
            `Managed by CRTV Sim (PID ${companion.pid || "--"})` :
            (companion.healthy ? "Running independently" : "No process detected");
          $("weather-update").textContent = companion.job?.state === "running" ?
            `${companion.job.stage || "Updating"} (${companion.job.progress || 0}%)` :
            (companion.last_update_request || companion.scheduler?.state || "--");
          $("weather-enabled").textContent = weather.enabled ? "Yes" : "No";
          $("weather-ready").textContent = weather.ready ?
            (weather.stale ? "Ready, manifest stale" : "Ready") :
            (weather.enabled ? "Setup or first video required" : "Disabled");
          $("weather-ready").className = weather.warning ? "warn" : "good";
          $("weather-segment").textContent = weather.current_segment ?
            `${weather.current_segment.title} (+${weather.current_segment.offset_seconds.toFixed(1)}s)` : "—";
          $("weather-generation").textContent = weather.generation_id || "—";
          $("weather-age").textContent = shortAge(weather.age_seconds);
          $("weather-valid-until").textContent = weather.valid_until || "—";
          $("weather-last-good").textContent = weather.using_last_known_good ? "In use" : "No";
          $("weather-warning").textContent = weather.warning ?
            (weather.error || (weather.stale ? "Published forecast is stale" : "Publication is older than the warning threshold")) : "—";
          $("weather-warning").className = weather.warning ? "warn" : "";
          if (weather.dashboard_url) $("weather-dashboard").href = weather.dashboard_url;
          $("weather-restart").disabled = !weather.enabled || companion.state === "starting";
        }
        const announcements = data.announcements;
        $("announce-poller").textContent = announcements.automation_thread_alive ? "Running" : "Stopped";
        $("announce-last-poll").textContent = announcements.last_poll_at || "--";
        $("announce-tuned").textContent = announcements.tuned_since || "—";
        $("announce-activity").textContent = announcements.last_qualifying_user_activity || "—";
        $("announce-next").textContent = announcements.next_opportunity ?
          `${announcements.next_opportunity.starts_at} (${announcements.next_opportunity.seconds_away}s)` : "—";
        $("announce-reason").textContent = announcements.eligibility_reason || "—";
        $("announce-state").textContent = announcements.generation_state || "idle";
        $("announce-error").textContent = announcements.error || "—";
        const batch = data.normalization_batch;
        const pending = data.pending_actions || {};
        $("batch-pending").textContent = `${pending.conversion_ready || 0}` +
          ((pending.library_review || pending.shadow_review || pending.aspect_review || pending.duplicate_review) ?
            ` (${pending.library_review || 0} library review; ${pending.shadow_review || 0} shadow review; ${pending.aspect_review || 0} aspect review; ${pending.duplicate_review || 0} duplicate review)` : "");
        $("batch-policy").textContent = batch.policy === "replace_with_backup" ?
          "Replace working copy + verified backup" : "Legacy read-only correction copies";
        $("batch-state").textContent = batch.state;
        $("batch-progress").textContent = `${batch.processed} / ${batch.total}`;
        $("batch-converted").textContent = batch.converted;
        $("batch-reused").textContent = batch.reused || 0;
        $("batch-skipped").textContent = batch.skipped;
        $("batch-not-needed").textContent = batch.not_needed;
        $("batch-failed").textContent = batch.failed;
        $("batch-current").textContent = batch.current_path ||
          (batch.current_asset_id ? `Asset ${batch.current_asset_id}` : "—");
        $("batch-completed").textContent = batch.completed_at || "—";
        const recentFailure = batch.failures?.at(-1);
        $("batch-error").textContent = batch.error ||
          (recentFailure ? `Asset ${recentFailure.asset_id}: ${recentFailure.error}` : "—");
        $("convert-approved").disabled = batch.state === "running";
        const audioAnalysis = data.audio_analysis_batch;
        const audioNormalization = data.audio_normalization_batch;
        $("audio-analysis-pending").textContent = pending.audio_analysis_pending || 0;
        $("audio-review-pending").textContent = pending.audio_review || 0;
        $("audio-correction-pending").textContent = pending.audio_conversion_ready || 0;
        $("audio-analysis-state").textContent = audioAnalysis.state;
        $("audio-analysis-progress").textContent = `${audioAnalysis.processed} / ${audioAnalysis.total}`;
        $("audio-analysis-corrections").textContent = audioAnalysis.correction_needed;
        $("audio-normalization-state").textContent = audioNormalization.state;
        $("audio-normalization-progress").textContent = `${audioNormalization.processed} / ${audioNormalization.total}`;
        $("audio-normalized").textContent = audioNormalization.normalized;
        $("audio-failed").textContent = audioAnalysis.failed + audioNormalization.failed;
        $("audio-current").textContent = audioNormalization.current_path ||
          audioAnalysis.current_path || "—";
        const audioFailure = audioNormalization.failures?.at(-1) ||
          audioAnalysis.failures?.at(-1);
        $("audio-error").textContent = audioNormalization.error ||
          audioAnalysis.error || (audioFailure ?
            `Asset ${audioFailure.asset_id}: ${audioFailure.error}` : "—");
        $("analyze-audio").disabled = audioAnalysis.state === "running";
        $("normalize-audio").disabled = audioNormalization.state === "running";
        $("cleanup-generated").disabled = cleanup.state === "running";
        $("housekeeping-database").disabled = housekeeping.state === "running" || data.tv_state !== "stopped";
        $("compact-database").disabled = data.database_compaction?.state === "running" || data.tv_state !== "stopped";
        $("empty-internet-cache").disabled = data.cache_cleanup?.state === "running" || data.internet_media?.state === "working";
        if (data.tv_error) $("message").textContent = data.tv_error;
      } catch (error) {
        if (!safeShutdownRequested) $("message").textContent = error.message;
      }
    }
    $("start").onclick = () => command("start", {channel:$("channel-select").value});
    $("re-review-normalization").onclick = () => {
      if (playingAssetId && confirm("Re-review the playing file? Playback will briefly stop. CRTV Sim restores a verified original backup when available; otherwise it reviews the current file. The file must be approved again in Library aspect review.")) {
        command("normalization-review", {asset_id:playingAssetId});
      }
    };
    $("tune").onclick = () => command("tune", {channel:$("channel-select").value});
    $("guide").onclick = () => command("tune", {channel:"__guide__"});
    $("stop").onclick = () => command("stop");
    $("exit").onclick = async () => {
      if (!confirm(
        "Safely shut down CRTV Sim? The application will stop playback, finish its " +
        "current file operation, flush the database, and create a verified backup " +
        "only if none was made in the last 24 hours. " +
        "Do not unplug the media drive until the Dashboard confirms completion."
      )) return;
      safeShutdownRequested = true;
      if (dashboardRefreshTimer !== null) {
        clearInterval(dashboardRefreshTimer);
        dashboardRefreshTimer = null;
      }
      $("exit").disabled = true;
      $("message").className = "message waiting";
      $("message").textContent =
        "SHUTTING DOWN — do not remove the drive or turn off the computer. " +
        "CRTV Sim is waiting for active work to finish…";
      let shutdownPending = true;
      let pollingShutdown = false;
      async function pollShutdown() {
        if (!shutdownPending || pollingShutdown) return;
        pollingShutdown = true;
        try {
          const progress = await request("/api/runtime/shutdown-status");
          if (shutdownPending && progress.state === "stopping") {
            const jobs = progress.active_jobs?.length ?
              ` Waiting for: ${progress.active_jobs.join(", ")}.` : "";
            const elapsed = progress.elapsed_seconds != null ?
              ` (${shortAge(progress.elapsed_seconds)})` : "";
            $("message").textContent =
              "SHUTTING DOWN — do not remove the drive or turn off the computer. " +
              `${progress.stage}${elapsed}.${jobs}`;
          }
        } catch (_) {
          // Shutdown may close the server before the last poll responds.
          // Only the shutdown request itself can confirm success or failure.
        } finally { pollingShutdown = false; }
      }
      const shutdownProgressTimer = setInterval(pollShutdown, 1000);
      try {
        const data = await request("/api/runtime/shutdown", {
          method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"
        });
        shutdownPending = false;
        $("message").className = "message safe";
        $("message").textContent = data.message;
      } catch (error) {
        shutdownPending = false;
        $("message").className = "message waiting";
        $("message").textContent =
          `SHUTDOWN CONFIRMATION FAILED: ${error.message}. Do not disconnect the drive. ` +
          "Use CRTV-Sim-Safe-Stop.exe or restart CRTV Sim and try again.";
      } finally {
        clearInterval(shutdownProgressTimer);
      }
    };
    $("maintain").onclick = () => command("maintenance");
    $("refresh-guide").onclick = () => command("guide-refresh", {force:true});
    $("cleanup-generated").onclick = () => command("generated-cleanup");
    $("backup-database").onclick = () => command("database-backup");
    $("housekeeping-database").onclick = () => {
      if (confirm("Purge stale database history now? CRTV Sim will create and verify a database backup first.")) command("database-housekeeping");
    };
    $("empty-internet-cache").onclick = async () => {
      try {
        const preview = await request("/api/runtime/internet-cache-preview");
        if (confirm(`Delete ${preview.files} managed cache files (${fileSize(preview.bytes)})? Active, incomplete, retained, and user-owned media are protected.`)) command("internet-cache-empty");
      } catch (error) { $("message").textContent = error.message; }
    };
    $("compact-database").onclick = async () => {
      try {
        const preview = await request("/api/runtime/database-compaction-preview");
        if (confirm(`Compact the database now? About ${fileSize(preview.reclaimable_bytes)} can be reclaimed. Playback and background writers must be stopped.`)) command("database-compact");
      } catch (error) { $("message").textContent = error.message; }
    };
    $("skip-maintenance-window").onclick = () => command("maintenance-window-skip");
    $("weather-restart").onclick = () => command("weather-restart");
    $("convert-approved").onclick = () => {
      if (!confirm(
        "Convert all approved media that requires aspect correction or exceeds " +
        "the output-size ceiling? Native and preserved files at or below the " +
        "ceiling will be skipped. With replace_with_backup, each working file " +
        "is replaced only after validation and an original backup."
      )) return;
      command("normalization-batch");
    };
    $("analyze-audio").onclick = () => {
      if (!confirm(
        "Analyze every pending media file from beginning to end? This is read-only, " +
        "but a large library can take several hours. Automatic maintenance also " +
        "analyzes two files at a time in the background."
      )) return;
      command("audio-analysis");
    };
    $("normalize-audio").onclick = () => {
      if (!confirm(
        "Normalize all analyzed audio outside the configured loudness tolerance? " +
        "Video streams will not be re-encoded. Each working file is replaced " +
        "only after a verified pre-audio backup and output validation. Stop TV " +
        "playback first so Windows does not hold the current media file open."
      )) return;
      command("audio-normalization");
    };
    $("tune-number").onclick = () => command("tune", {channel:$("channel-number").value});
    $("channel-number").onkeydown = event => {
      if (event.key === "Enter") $("tune-number").click();
    };
    refresh(); dashboardRefreshTimer = setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>CRTV Sim Library</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #11141a;
      --panel: #1b2029;
      --line: #343d4c;
      --text: #edf1f7;
      --muted: #9ca8b8;
      --accent: #70d6a3;
      --warn: #f0bd64;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 15px/1.45 system-ui, sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    header {
      padding: 24px clamp(16px, 4vw, 48px);
      border-bottom: 1px solid var(--line);
      background: #151922;
    }
    h1 { margin: 0 0 4px; font-size: 26px; }
    header p { margin: 0; color: var(--muted); }
    main { padding: 24px clamp(16px, 4vw, 48px); }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    .card, .toolbar, .table-wrap {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
    }
    .card { padding: 16px; }
    .card strong { display: block; font-size: 24px; color: var(--accent); }
    .card span { color: var(--muted); }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      padding: 12px;
      margin-bottom: 12px;
    }
    input, select, button {
      border: 1px solid var(--line);
      background: #10141b;
      color: var(--text);
      border-radius: 6px;
      padding: 8px 10px;
    }
    input { min-width: 240px; flex: 1; }
    textarea { border:1px solid var(--line); background:#10141b; color:var(--text); border-radius:6px;
      padding:8px 10px; font:inherit; }
    .graphic-art-direction { display:grid; gap:4px; flex:1 1 360px; color:var(--muted); }
    .graphic-art-direction textarea { min-height:68px; resize:vertical; width:100%; }
    button { cursor: pointer; }
    button:hover { border-color: var(--accent); }
    .table-wrap { overflow: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 1250px; }
    th, td {
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    td.path { max-width: 400px; overflow-wrap: anywhere; }
    .status-ready { color: var(--accent); }
    .status-open { color: var(--warn); }
    .actions { display: flex; flex-wrap: wrap; gap: 6px; min-width: 210px; }
    .mode { min-width: 145px; }
    .normalization-detail { color: var(--muted); font-size: 12px; max-width: 260px; }
    .crop-controls {
      display: flex; flex-wrap: wrap; gap: 5px; align-items: center;
      margin-top: 6px;
    }
    .crop-controls[hidden] { display: none; }
    .crop-controls input {
      flex: 0 0 58px; min-width: 0; width: 58px; padding: 6px;
    }
    .crop-controls button { padding: 6px 8px; }
    .preview-links { display: flex; gap: 8px; margin-top: 5px; }
    .aspect-review-row.active-review { outline: 2px solid var(--accent); outline-offset: -2px; }
    .aspect-preview-cell { min-width: 430px; width: 430px; }
    .aspect-comparison { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .aspect-preview-panel { min-width:0; }
    .aspect-preview-panel strong { display:block; margin-bottom:5px; font-size:12px; color:var(--muted); }
    .aspect-preview-panel a { display:block; }
    .aspect-preview-panel img {
      display:block; width:100%; height:150px; object-fit:contain;
      background:#050608; border:1px solid var(--line); border-radius:6px;
    }
    .aspect-preview-panel img.stale { opacity:.3; }
    .aspect-preview-panel.expanded a {
      aspect-ratio:4 / 3; margin-bottom:6px; overflow:hidden;
      background:#050608; border:1px solid var(--line); border-radius:6px;
    }
    .aspect-preview-panel.expanded img {
      width:100%; height:100%; margin:0; border:0; border-radius:0;
    }
    .preview-placeholder {
      display:flex; align-items:center; justify-content:center; min-height:150px;
      padding:12px; text-align:center; color:var(--muted); background:#0b0e13;
      border:1px dashed var(--line); border-radius:6px;
    }
    .preview-status { margin-top:6px; color:var(--muted); font-size:12px; }
    button.primary { background:#174b38; border-color:#348965; }
    button:disabled { cursor:not-allowed; opacity:.48; }
    a { color: var(--accent); }
    .inventory-title-link { font-weight: 600; }
    .inventory-shadow-row td { background:rgba(103,150,214,.10); }
    .inventory-shadow-row td:first-child { border-left:4px solid #6796d6; }
    .shadow-badge { display:inline-block; margin-left:8px; padding:2px 7px;
      border:1px solid #6796d6; border-radius:999px; color:#b8d8ff;
      font-size:11px; font-weight:700; letter-spacing:.05em; text-transform:uppercase; }
    .notice {
      display: none;
      margin-bottom: 12px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .notice.error { display: block; border-color: #a84f55; color: #ffb8bd; }
    .notice.success { display: block; border-color: var(--accent); color: var(--accent); }
    .empty { color: var(--muted); padding: 24px; text-align: center; }
    h2 { margin: 0; font-size: 20px; }
    .section-controls {
      display: flex; gap: 8px; justify-content: flex-end; margin-bottom: 12px;
    }
    details.collapsible {
      margin-bottom: 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #171c24;
      overflow: hidden;
    }
    details.collapsible > summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      cursor: pointer;
      user-select: none;
      list-style: none;
    }
    details.collapsible > summary::-webkit-details-marker { display: none; }
    details.collapsible > summary::after {
      content: "▸";
      color: var(--accent);
      font-size: 18px;
      transform: rotate(0deg);
      transition: transform .15s ease;
    }
    details.collapsible[open] > summary {
      border-bottom: 1px solid var(--line);
    }
    details.collapsible[open] > summary::after { transform: rotate(90deg); }
    .section-body { padding: 14px; }
    .section-body > :first-child { margin-top: 0; }
    .section-body > :last-child { margin-bottom: 0; }
    .schedule-time { white-space: nowrap; font-variant-numeric: tabular-nums; }
    .schedule-toolbar {
      display: grid;
      grid-template-columns: minmax(190px, .75fr) minmax(460px, 1.7fr) minmax(390px, 1.2fr);
      gap: 14px;
      padding: 12px;
      margin-bottom: 12px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      align-items: stretch;
    }
    .schedule-context, .schedule-actions {
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
    }
    .schedule-field { display: block; min-width: 0; }
    .schedule-field > span {
      display: block;
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    .schedule-field select, .schedule-field input[type="date"] {
      width: 100%;
      min-width: 0;
    }
    .schedule-view-filter {
      padding-top: 10px;
      border-top: 1px solid var(--line);
    }
    .schedule-group-title {
      display: block;
      margin-bottom: 7px;
      color: var(--warn);
      font-weight: 700;
    }
    .schedule-check {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: fit-content;
      cursor: pointer;
    }
    .schedule-check input[type="checkbox"] {
      flex: 0 0 auto;
      width: 16px;
      height: 16px;
      min-width: 0;
      margin: 0;
      padding: 0;
    }
    .schedule-check.muted { cursor: default; }
    .schedule-help {
      display: block;
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .schedule-settings {
      min-width: 0;
      margin: 0;
      padding: 10px 14px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .schedule-settings legend {
      margin: 0 auto;
      padding: 0 8px;
      color: var(--warn);
      font-weight: 700;
      text-align: center;
    }
    .schedule-settings-grid {
      display: grid;
      grid-template-columns: minmax(145px, 1.1fr) minmax(140px, 1fr) minmax(135px, 1fr);
      gap: 14px;
      align-items: start;
    }
    .schedule-lineup-overview {
      grid-column: 1 / -1;
      padding: 10px 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--panel) 78%, transparent);
    }
    .schedule-lineup-overview h3, .schedule-lineup-overview h4 { margin: 0 0 7px; }
    .schedule-lineup-overview h4 { color: var(--warn); font-size: 13px; }
    .schedule-lineup-overview section { margin-top: 10px; }
    .schedule-lineup-overview p { margin: 4px 0; color: var(--muted); font-size: 12px; }
    .schedule-setting { min-width: 0; }
    .schedule-setting select { width: 100%; min-width: 0; }
    .schedule-actions-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .schedule-actions-buttons button { flex: 1 1 auto; }
    @media (max-width: 1250px) {
      .schedule-toolbar { grid-template-columns: minmax(190px, .75fr) minmax(460px, 1.7fr); }
      .schedule-actions { grid-column: 1 / -1; }
      .schedule-actions-buttons button { flex: 0 1 auto; }
    }
    @media (max-width: 760px) {
      .schedule-toolbar { grid-template-columns: 1fr; }
      .schedule-actions { grid-column: auto; }
      .schedule-settings-grid { grid-template-columns: 1fr; }
    }
    .announcer-profile { padding:20px 0; border-bottom:1px solid var(--line); }
    .announcer-profile:last-child { border-bottom:0; }
    .announcer-profile > h3 { margin:0 0 14px; }
    .announcer-group { max-width:960px; margin:0 0 16px; padding:16px; border:1px solid var(--line); border-radius:10px; background:#171c25; }
    .announcer-group > h4 { margin:0 0 14px; font-size:15px; }
    .announcer-field { display:grid; grid-template-columns:minmax(180px,230px) minmax(0,1fr); gap:8px 16px; align-items:start; margin:0 0 12px; }
    .announcer-field:last-child { margin-bottom:0; }
    .announcer-field > label { color:var(--muted); padding-top:7px; }
    .announcer-field input[type=text], .announcer-field input[type=number], .announcer-field textarea { width:100%; min-width:0; }
    .announcer-field textarea { min-height:76px; resize:vertical; color:var(--text); background:#111720; border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; }
    .announcer-check { display:flex; align-items:center; gap:9px; margin:0 0 14px; color:var(--text); cursor:pointer; }
    .announcer-check:last-child { margin-bottom:0; }
    .announcer-check input[type=checkbox] { width:16px; height:16px; min-width:16px; margin:0; }
    .announcer-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
    .announcer-actions .preview-status { flex-basis:100%; }
    @media (max-width:720px) {
      .announcer-field { grid-template-columns:1fr; gap:4px; }
      .announcer-field > label { padding-top:0; }
    }
    dialog { color:var(--text); background:var(--panel); border:1px solid var(--line);
      border-radius:12px; width:min(620px,calc(100vw - 32px)); }
    dialog::backdrop { background:rgba(0,0,0,.72); }
    .dialog-grid { display:grid; grid-template-columns:150px 1fr; gap:12px; align-items:center; }
    .dialog-grid label { color:var(--muted); }
    .dialog-grid select { width:100%; }
    .dialog-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:18px; }
  </style>
</head>
<body>
  <header>
    <h1>CRTV Sim Library</h1>
    <p>Media index, normalization, seasonal rules, and linear schedules</p>
    <p><a href="/">Dashboard</a> &nbsp; <a href="/settings">Settings</a> &nbsp; <a href="/guide">Live Guide</a></p>
  </header>
  <main>
    <div class="section-controls">
      <button id="expand-sections">Expand all</button>
      <button id="collapse-sections">Collapse all</button>
    </div>
    <details class="collapsible" data-section="external-media" id="external-media-panel">
      <summary><h2>External Media Sources</h2></summary>
      <div class="section-body">
        <p>Use an existing series folder without copying or changing its files. External media is always read-only. Scheduled channels only; At Your Pace support is planned for Phase 1B.</p>
        <div class="toolbar">
          <label>Series folder on this computer<input id="external-path" placeholder="E:/TV Series/Doug or a network share"></label>
          <label>Series title<input id="external-title" placeholder="Doug"></label>
          <label>Scheduled channel<select id="external-channel"></select></label>
          <label>Daypart scope<select id="external-scope"></select></label>
        </div>
        <div class="toolbar">
          <label>New channel name (optional)<input id="external-new-channel" placeholder="Leave blank to use selected channel"></label>
          <label>New channel number (optional)<input id="external-number" type="number" min="1"></label>
          <button id="external-add">Add &amp; Scan</button><button id="external-refresh">Refresh sources</button>
        </div>
        <p class="muted">Folder paths are read by the Windows account running CRTV Sim, including when you open this Dashboard from another computer. Choose the series folder, not an entire multi-series library.</p>
        <div id="external-message" role="status"></div>
        <div id="external-sources"></div>
      </div>
    </details>
    <details class="collapsible" data-section="media-library" open>
      <summary><h2>Media Library</h2></summary>
      <div class="section-body">
        <section id="stats" class="stats"></section>
        <div id="notice" class="notice" role="status"></div>
        <section class="toolbar">
          <input id="search" placeholder="Search title, filename, path, or Asset ID">
          <select id="review">
            <option value="">All review queues</option>
            <option value="library:open">Library review</option>
            <option value="normalization:review">Aspect review</option>
            <option value="audio:review">Audio review</option>
            <option value="commercial:pending">Long commercials</option>
            <option value="library:ready">Library ready</option>
            <option value="library:ignored">Library ignored</option>
          </select>
          <button id="refresh">Refresh</button>
        </section>
        <p id="aspect-review-help" class="muted" hidden>
          Aspect Review: select draft settings, preview the correction, then approve
          exactly what is shown. Keyboard: J/K or arrow keys move between rows, P
          renders the selected correction, and A approves a current preview.
        </p>
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Title</th><th>Channel</th><th>Daypart</th>
                <th>Type</th><th>Duration</th><th>Status</th><th>Normalization</th>
                <th>Preview comparison</th><th>Path</th><th>Actions</th>
              </tr>
            </thead>
            <tbody id="assets"></tbody>
          </table>
          <div id="empty" class="empty" hidden>No matching assets.</div>
        </section>
      </div>
    </details>
    <details class="collapsible" data-section="bumper-cue-review" open>
      <summary><h2>Bumper Placement Review <span id="bumper-review-count" class="pill">0</span></h2></summary>
      <div class="section-body">
        <p class="muted">Bumpers needing review are held out of scheduling when their detected show cannot be matched. Mark a mistaken detection as General, or choose Break start/Break end and link the correct show. An empty show means the bumper can play with any show at that break boundary. Decisions affect future schedules and can be changed under All classified and saved.</p>
        <select id="bumper-review-filter"><option value="pending">Needs review</option><option value="all">All classified and saved</option></select>
        <button id="refresh-bumper-review">Refresh bumpers</button>
        <section class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Filename</th><th>Detected</th><th>Placement</th><th>Show to match</th><th>Path</th><th>Actions</th></tr></thead>
            <tbody id="bumper-review"></tbody>
          </table>
          <div id="bumper-review-empty" class="empty" hidden>No bumpers in this view.</div>
        </section>
        <datalist id="bumper-series-options"></datalist>
      </div>
    </details>
    <details class="collapsible" data-section="programming-inventory" open>
      <summary><h2>Programming Inventory</h2></summary>
      <div class="section-body">
        <p class="muted">See which series, movies, blocks, and interstitial pools
          are available to each channel and scheduling collection. Episode and short
          counts use the series folder title.</p>
        <section id="inventory-stats" class="stats"></section>
        <section class="toolbar">
          <input id="inventory-search" placeholder="Search series, movie, channel, or collection">
          <select id="inventory-type"><option value="">All media types</option></select>
          <select id="inventory-channel"><option value="">All channels</option></select>
          <select id="inventory-count">
            <option value="">Any collection size</option>
            <option value="thin">Fewer than 5 items</option>
            <option value="review">Has review items</option>
            <option value="unplaced">Unplaced recurring series</option>
          </select>
          <button id="refresh-inventory">Refresh inventory</button>
        </section>
        <section class="table-wrap">
          <table>
            <thead><tr><th>Series / Title</th><th>Type</th><th>Items</th><th>Shadows</th>
              <th>Ready</th><th>Review</th><th>Channel</th>
              <th>Daypart or Collection</th><th>Upcoming</th><th>Total Runtime</th><th>Actions</th></tr></thead>
            <tbody id="inventory"></tbody>
          </table>
          <div id="inventory-empty" class="empty" hidden>No matching media inventory.</div>
        </section>
      </div>
    </details>
    <details class="collapsible" data-section="shadow-files" open>
      <summary><h2>Shadow Files</h2></summary>
      <div class="section-body">
        <p class="muted">Create lightweight scheduling placements that reuse an
          existing video's conversion, audio analysis, and act breaks. Folder batches
          recurse through every indexed media file and preserve its organization.</p>
        <section id="shadow-stats" class="stats"></section>
        <section class="toolbar">
          <input id="shadow-folder-source" list="shadow-folder-list"
            placeholder="Choose an indexed source folder">
          <datalist id="shadow-folder-list"></datalist>
          <button id="shadow-folder-create">Create shadows from folder</button>
          <button id="refresh-shadows">Refresh shadows</button>
        </section>
        <section class="table-wrap">
          <table>
            <thead><tr><th>Status</th><th>Shadow placement</th><th>Real media</th>
              <th>Channel</th><th>Daypart or Collection</th><th>Detail</th></tr></thead>
            <tbody id="shadows"></tbody>
          </table>
          <div id="shadows-empty" class="empty" hidden>No shadow files have been created.</div>
        </section>
      </div>
    </details>
    <details class="collapsible" data-section="duplicate-media" open>
      <summary><h2>Duplicate Media Review</h2></summary>
      <div class="section-body">
        <p class="muted">Exact SHA-256 matches remain separate scheduling assets.
          CRTV Sim reuses compatible analysis and corrected output so identical files
          are not needlessly re-encoded.</p>
        <section id="duplicate-stats" class="stats"></section>
        <section class="toolbar">
          <select id="duplicate-filter">
            <option value="pending">Pending review</option>
            <option value="all">All exact duplicate sets</option>
            <option value="intentional">Marked intentional</option>
          </select>
          <button id="refresh-duplicates">Refresh duplicates</button>
        </section>
        <section class="table-wrap">
          <table>
            <thead><tr><th>Status</th><th>Copies</th><th>Duplicated space</th>
              <th>Locations and scheduling assignments</th><th>Actions</th></tr></thead>
            <tbody id="duplicates"></tbody>
          </table>
          <div id="duplicates-empty" class="empty" hidden>No matching exact duplicates.</div>
        </section>
      </div>
    </details>
    <dialog id="shadow-dialog">
      <h2>Create shadow files</h2>
      <p id="shadow-source-label" class="muted"></p>
      <div class="dialog-grid">
        <label for="shadow-channel">Destination channel</label>
        <select id="shadow-channel"></select>
        <label for="shadow-scope">Placement type</label>
        <select id="shadow-scope">
          <option value="daypart">Daypart</option>
          <option value="weekly">Recurring weekly block</option>
          <option value="holiday">Holiday collection</option>
        </select>
        <label for="shadow-value">Destination</label>
        <select id="shadow-value"></select>
      </div>
      <p id="shadow-destination-preview" class="muted"></p>
      <div class="dialog-actions">
        <button id="shadow-cancel">Cancel</button>
        <button id="shadow-confirm">Create shadow files</button>
      </div>
    </dialog>
    <dialog id="shadow-delete-dialog">
      <h2>Delete shadow files?</h2>
      <p id="shadow-delete-summary"></p>
      <p class="warn"><strong>Original media will remain untouched.</strong>
        These scheduling sidecars can be recreated later.</p>
      <div class="dialog-actions">
        <button id="shadow-delete-cancel">Cancel</button>
        <button id="shadow-delete-confirm" class="danger">Delete Shadows</button>
      </div>
    </dialog>
    <details class="collapsible" data-section="holiday-filename-review" open>
      <summary><h2>Holiday Filename Review</h2></summary>
      <div class="section-body">
        <p class="muted">Filename clues are suggestions only. Files remain eligible for scheduling. Choose the destination folder yourself; CRTV Sim never moves a file automatically.</p>
        <section class="toolbar"><button id="refresh-holiday-reviews">Refresh suggestions</button></section>
        <section class="table-wrap"><table><thead><tr><th>Suggested holiday</th><th>Filename</th><th>Current location</th><th>Type</th><th>Matched clue</th><th>Actions</th></tr></thead>
          <tbody id="holiday-reviews"></tbody></table>
          <div id="holiday-reviews-empty" class="empty" hidden>No holiday filename suggestions.</div>
        </section>
      </div>
    </details>
    <dialog id="holiday-move-dialog">
      <h2>Move holiday media</h2>
      <p id="holiday-move-source" class="muted"></p>
      <label for="holiday-move-folder">Destination folder inside the media library</label>
      <input id="holiday-move-folder" list="holiday-folders" placeholder="channel/daypart/episodes/Show" style="width:100%">
      <datalist id="holiday-folders"></datalist>
      <p id="holiday-move-preview" class="muted"></p>
      <p class="muted">You may choose an existing folder or name a new one. Files placed outside CRTV Sim's recognized folder layout may need Library review.</p>
      <div class="dialog-actions"><button id="holiday-move-cancel">Cancel</button><button id="holiday-move-confirm">Move file</button></div>
    </dialog>
    <details class="collapsible" data-section="act-breaks" open>
      <summary><h2>Act Breaks</h2></summary>
      <div class="section-body">
        <p class="muted">Eligible episodes and movies are analyzed throughout their runtime.
          Each proposed break shows its detected cue or a recorded fallback. These
          timestamps do not modify or split the source files.</p>
        <section class="toolbar">
          <select id="breakpoint-filter">
            <option value="">All eligible programs</option>
            <option value="pending">Pending analysis</option>
            <option value="analyzed">Analyzed</option>
            <option value="midpoint">Generic halfway fallback</option>
            <option value="manual">Manual overrides</option>
          </select>
          <button id="refresh-breakpoints">Refresh act breaks</button>
        </section>
        <section class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Episode</th><th>Duration</th><th>Status</th>
              <th>Selected break</th><th>Method</th><th>Confidence</th><th>Actions</th></tr></thead>
            <tbody id="breakpoints"></tbody>
          </table>
          <div id="breakpoints-empty" class="empty" hidden>No matching eligible programs.</div>
        </section>
      </div>
    </details>
    <details class="collapsible" data-section="seasonal-rules" open>
      <summary><h2>Seasonal and Recurring Rules</h2></summary>
      <div class="section-body">
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Rule</th><th>Type</th><th>Channel</th><th>When</th>
                <th>Mode</th><th>Priority</th><th>Enabled</th><th>Ready assets</th>
              </tr>
            </thead>
            <tbody id="programming-rules"></tbody>
          </table>
        </section>
      </div>
    </details>
    <details class="collapsible" data-section="linear-schedule" open>
      <summary><h2>Linear Schedule</h2></summary>
      <div class="section-body">
        <section class="schedule-toolbar" aria-label="Linear schedule controls">
          <div class="schedule-context">
            <label class="schedule-field">
              <span>Channel</span>
              <select id="schedule-channel" aria-label="Channel"></select>
            </label>
            <div class="schedule-view-filter">
              <span class="schedule-group-title">Schedule view filter</span>
              <label class="schedule-check"><input id="programs-only" type="checkbox"> <span>Programs only</span></label>
              <small class="schedule-help">Filters the table below only. It does not change what airs.</small>
            </div>
          </div>
          <fieldset class="schedule-settings">
            <legend>Schedule settings</legend>
            <div class="schedule-settings-grid">
              <div class="schedule-setting">
                <label class="schedule-field">
                  <span>Presentation</span>
                  <select id="schedule-presentation-format" aria-label="Channel presentation format" aria-describedby="schedule-presentation-help"><option value="standard">Standard</option><option value="content_only">Content Only</option></select>
                </label>
                <small id="schedule-presentation-help" class="schedule-help">Uses clock-aligned blocks with station interruptions.</small>
              </div>
              <div class="schedule-setting">
                <label class="schedule-check" title="Reuse this channel's ordinary series placement instead of re-randomizing every day."><input id="schedule-recurring" type="checkbox"> <span>Recurring schedule</span></label>
                <small class="schedule-help">Reuses established series placement on later dates.</small>
              </div>
              <div class="schedule-setting">
                <label class="schedule-check" id="schedule-unique-weekends-label" title="Keep Saturday and Sunday as their own recurring patterns."><input id="schedule-unique-weekends" type="checkbox"> <span>Unique weekends</span></label>
                <small class="schedule-help">Gives weekdays, Saturdays, and Sundays separate patterns.</small>
              </div>
            </div>
          </fieldset>
          <section id="recurring-lineup-overview" class="schedule-lineup-overview" hidden>
            <h3>Recurring lineup and waiting list</h3>
            <p class="schedule-help">Series advance when their scheduled episodes air, whether watched or not. Fixed weekly specials keep their reserved windows.</p>
            <div id="recurring-lineup-patterns"></div>
            <div id="recurring-lineup-waiting"></div>
          </section>
          <div class="schedule-actions">
            <label class="schedule-field">
              <span>Schedule date</span>
              <input id="schedule-date" type="date" aria-label="Schedule date">
            </label>
            <div class="schedule-actions-buttons">
              <button id="generate-schedule">Generate / Replace</button>
              <button id="reroll-schedule" title="Replace this channel and date using a fresh random seed">Re-roll schedule</button>
              <button id="refresh-schedule">Refresh schedule</button>
            </div>
            <small class="schedule-help">Generate follows the normal plan. Re-roll creates a different valid arrangement. Refresh only reloads this table.</small>
          </div>
        </section>
        <section class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Start</th><th>End</th><th>Daypart</th><th>Type</th>
                <th>Title</th><th>Programming owner</th><th>Rules</th><th>Source</th>
              </tr>
            </thead>
            <tbody id="schedule"></tbody>
          </table>
          <div id="schedule-empty" class="empty" hidden>No active schedule for this date.</div>
        </section>
      </div>
    </details>
    <details class="collapsible" data-section="block-cues" open>
      <summary><h2>Complete Block Cue Sheets</h2></summary>
      <div class="section-body">
        <section class="toolbar">
          <select id="block-asset" aria-label="Complete block"></select>
          <input id="block-title" placeholder="General guide title" aria-label="General guide title">
          <select id="block-boundary" aria-label="Boundary policy">
            <option value="may_overrun">May overrun daypart</option>
            <option value="must_fit">Must fit</option>
            <option value="soft">Soft boundary</option>
            <option value="hard">Hard boundary</option>
            <option value="fill_short">Fill short only</option>
          </select>
          <input id="block-probability" type="number" min="0" max="1" step="0.05"
                 placeholder="Default probability" aria-label="Probability override">
          <button id="save-block-profile">Save block settings</button>
        </section>
        <p class="muted">Use HH:MM:SS times. The entire sheet is validated before replacement.</p>
        <section class="table-wrap">
          <table>
            <thead>
              <tr><th>Start</th><th>End</th><th>Type</th><th>Title</th><th>Description</th><th></th></tr>
            </thead>
            <tbody id="block-cue-rows"></tbody>
          </table>
        </section>
        <section class="toolbar">
          <button id="add-block-cue">Add cue</button>
          <button id="save-block-cues">Validate / Save cue sheet</button>
        </section>
      </div>
    </details>
    <details class="collapsible" data-section="weather-graphics">
      <summary><h2>Weather Graphics</h2></summary>
      <div class="section-body">
        <p class="muted">Upload backgrounds or generate artwork for each weather condition and time of day. Holiday layers follow the holidays and date ranges in Settings. Artwork is used automatically on the next Weather render; existing files can rotate.</p>
        <label for="weather-art-direction">Weather artwork creative direction</label>
        <textarea id="weather-art-direction" rows="3" maxlength="1000" placeholder="Optional style direction for the next generation"></textarea>
        <p class="muted">AI uses your saved OpenAI key and the channel graphic image model. Holiday layers need a transparent center; opaque layers are rejected. Generated images are resized to 640×480 without cropping.</p>
        <button id="refresh-weather-graphics">Refresh Weather graphics</button>
        <p id="weather-graphics-status" class="muted" role="status"></p>
        <section class="table-wrap"><table><thead><tr><th>Weather / holiday</th><th>Artwork</th><th>Actions</th></tr></thead><tbody id="weather-graphics-rows"></tbody></table></section>
      </div>
    </details>
    <details class="collapsible" data-section="channel-graphics" open>
      <summary><h2>Channel Graphics</h2></summary>
      <div class="section-body">
        <p class="muted">Up-next backgrounds and ten-second station cards are kept
          outside the program library. Holiday and active weekly-special folders take precedence automatically.
          Up-next artwork should keep important art outside the standard text-safe area.</p>
        <p class="muted">
          <a href="/api/templates/up-next-background" target="_blank">Open 640×480 background template</a>
          · <a href="/api/templates/up-next-layout" target="_blank">Open labeled layout guide</a>
        </p>
        <section class="toolbar">
          <select id="graphic-channel" aria-label="Graphic channel"></select>
          <select id="graphic-purpose">
            <option value="up-next">Up-next background</option>
            <option value="interstitials">Standalone interstitial</option>
            <option value="channel-music">Music for all channel graphics</option>
          </select>
          <select id="graphic-daypart">
            <option value="">Default channel branding</option>
          </select>
          <select id="graphic-holiday">
            <option value="">Default / year-round</option>
            <option value="halloween">Halloween</option>
            <option value="christmas">Christmas</option>
          </select>
          <input id="graphic-file" type="file" accept=".png,.jpg,.jpeg,.webp,.mp4,.mkv,.webm,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus">
          <button id="upload-graphic">Add graphic</button>
          <button id="regenerate-placeholder">Regenerate placeholder</button>
        </section>
        <section class="toolbar">
          <label class="graphic-art-direction">AI art direction
            <textarea id="graphic-art-direction" placeholder="Optional: describe the era, mood, palette, motifs, or seasonal direction."></textarea>
          </label>
          <button id="generate-graphic">Generate and use AI graphic</button>
        </section>
        <h3>Graphics To Do <span id="graphics-todo-count" class="pill">0</span></h3>
        <p class="muted">Generated placeholders that do not yet have a replacement in the same channel, weekly special, daypart, holiday, and purpose. AI graphics are installed immediately and can be regenerated, replaced, or deleted here.</p>
        <section class="table-wrap">
          <table>
            <thead><tr><th>Channel</th><th>Daypart</th><th>Weekly special</th><th>Purpose</th><th>Holiday</th><th>Replacement folder</th><th>Placeholder</th><th>Action</th></tr></thead>
            <tbody id="graphics-todo"></tbody>
          </table>
          <div id="graphics-todo-empty" class="empty" hidden>All placeholder graphics have replacements.</div>
        </section>
        <section class="table-wrap">
          <table>
            <thead><tr><th>Channel</th><th>Daypart</th><th>Weekly special</th><th>Purpose</th><th>Holiday</th><th>Kind</th><th>Source</th><th>Path</th><th>Preview</th><th>Actions</th></tr></thead>
            <tbody id="graphics"></tbody>
          </table>
          <div id="graphics-empty" class="empty" hidden>No channel graphics cataloged.</div>
        </section>
      </div>
    </details>
    <details id="station-announcers" class="collapsible" data-section="station-announcers" open>
      <summary><h2>Station Announcer Profiles</h2></summary>
      <div class="section-body">
        <p class="muted">Dynamic announcements use the upcoming schedule and are generated only for the actively watched channel inside the demand window. Generate each generic voice once so it can be reused whenever dynamic generation is unavailable.</p>
        <p id="announcer-key-status" class="muted"></p>
        <div id="announcer-profiles" class="table-wrap"></div>
        <section class="weekly-announcer-settings">
          <h3>Weekly Specials</h3>
          <p class="muted">Configure each weekly special's promos and announcer. Promo narration also follows the normal Up Next AI eligibility rules. Promo placement changes take effect when that channel's schedule is refreshed.</p>
          <div id="weekly-announcer-profiles" class="table-wrap"></div>
          <div id="weekly-announcer-empty" class="empty" hidden>No enabled weekly specials are configured.</div>
        </section>
      </div>
    </details>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const escText = (value) => value == null ? "" : String(value);
    const sectionStorageKey = "retrotv.library.collapsed-sections.v1";

    function saveSectionState() {
      const state = {};
      document.querySelectorAll("details.collapsible").forEach(section => {
        state[section.dataset.section] = section.open;
      });
      try {
        localStorage.setItem(sectionStorageKey, JSON.stringify(state));
      } catch (_) {}
    }

    function initializeCollapsibleSections() {
      let state = {};
      try {
        state = JSON.parse(localStorage.getItem(sectionStorageKey) || "{}");
      } catch (_) {}
      document.querySelectorAll("details.collapsible").forEach(section => {
        if (typeof state[section.dataset.section] === "boolean") {
          section.open = state[section.dataset.section];
        }
        section.addEventListener("toggle", saveSectionState);
      });
      $("expand-sections").addEventListener("click", () => {
        document.querySelectorAll("details.collapsible").forEach(section => {
          section.open = true;
        });
        saveSectionState();
      });
      $("collapse-sections").addEventListener("click", () => {
        document.querySelectorAll("details.collapsible").forEach(section => {
          section.open = false;
        });
        saveSectionState();
      });
    }

    async function api(path, options) {
      const response = await fetch(path, options);
      if (!response.ok) {
        let message = await response.text();
        try { message = JSON.parse(message).error || message; } catch (_) {}
        throw new Error(message);
      }
      return response.json();
    }

    function notice(message, kind = "success") {
      const node = $("notice");
      node.textContent = message;
      node.className = `notice ${kind}`;
    }

    function card(label, value) {
      const node = document.createElement("div");
      node.className = "card";
      const strong = document.createElement("strong");
      strong.textContent = value;
      const span = document.createElement("span");
      span.textContent = label;
      node.append(strong, span);
      return node;
    }

    async function loadStats() {
      const data = await api("/api/health");
      $("stats").replaceChildren(
        card("Assets", data.assets),
        card("Ready", data.ready),
        card("Library review", data.needs_review),
        card("Long commercials", data.long_commercial_review || 0),
        card("Enabled", data.enabled),
        card("Geometry analyzed", data.normalization_analyzed),
        card("Aspect review", data.normalization_review),
        card("Corrections pending", data.conversion_ready),
        card("Audio analyzed", data.audio_analyzed),
        card("Audio review", data.audio_review),
        card("Audio corrections", data.audio_conversion_ready),
        card("Active schedules", data.active_schedules)
      );
    }

    let externalData = null;
    async function externalPost(path, payload) {
      return api(path, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    }
    function externalAssignmentEditor(container, assignments) {
      container.replaceChildren();
      function addRow(assignment={}) {
        const row=document.createElement("div"); row.className="toolbar external-assignment";
        const channel=document.createElement("select"); channel.setAttribute("aria-label","Scheduled channel");
        channel.replaceChildren(...externalData.channels.map(c=>new Option(`${c.channel_number || ""} · ${c.display_name}`,c.slug)));
        if(assignment.channel_slug) channel.value=assignment.channel_slug;
        const scope=document.createElement("select"); scope.setAttribute("aria-label","Daypart scope");
        scope.replaceChildren(...externalData.dayparts.map(d=>new Option(d,d)));
        scope.value=assignment.scope || "all-day";
        const remove=document.createElement("button"); remove.textContent="Remove assignment"; remove.onclick=()=>row.remove();
        row.append(channel,scope,remove); container.append(row);
      }
      assignments.forEach(addRow);
      const add=document.createElement("button"); add.textContent="Add assignment";
      add.onclick=()=>addRow(); container.append(add);
    }
    async function loadExternalSources() {
      try {
        externalData=await api("/api/external-media");
        for(const [id,options] of [["external-channel",externalData.channels.map(c=>new Option(`${c.channel_number || ""} · ${c.display_name}`,c.slug))],["external-scope",externalData.dayparts.map(d=>new Option(d,d))]]) {
          const select=$(id), previous=select.value; select.replaceChildren(...options);
          if([...select.options].some(o=>o.value===previous)) select.value=previous;
          else if(id==="external-scope") select.value="all-day";
        }
        const host=$("external-sources"); host.replaceChildren();
        for(const source of externalData.sources) {
          const card=document.createElement("section"); card.className="panel";
          const title=document.createElement("h3"); title.textContent=`${source.series_title} · External / read-only`;
          const path=document.createElement("p"); path.textContent=source.absolute_path; path.className="path";
          const info=document.createElement("p"); info.textContent=`${source.enabled ? "Enabled" : "Disconnected"} · ${source.availability} · ${source.episode_count} episodes · scan ${source.scan_status} (${source.processed_count} processed) · last scan ${source.last_scan_at || "not yet"} · periodic rescan every ${externalData.rescan_interval_hours} hours`;
          const placements=document.createElement("p"); placements.textContent=source.assignments.map(a=>`${a.channel_slug} / ${a.scope}`).join("; ") || "No assignments";
          card.append(title,path,placements,info);
          if(source.unsupported_assignments?.length){const warning=document.createElement("p");warning.textContent="Weather is a synthetic channel and cannot receive programming. Add a scheduled-channel assignment to this source.";warning.className="warn";card.append(warning);}
          if(source.scan_error){const error=document.createElement("p");error.textContent=source.scan_error;error.className="bad";card.append(error);}
          const actions=document.createElement("div"); actions.className="toolbar";
          function button(label,action){const b=document.createElement("button");b.textContent=label;b.onclick=async()=>{b.disabled=true;try{const result=await action();await loadExternalSources();$("external-message").textContent=result?.message||"Saved. Affected schedule updates use the maintenance pipeline.";}catch(e){$("external-message").textContent=e.message;}finally{b.disabled=false;}};actions.append(b);return b;}
          const rescan=button("Rescan",()=>externalPost(`/api/external-media/${source.id}/rescan`,{})); rescan.disabled=!source.enabled;
          const editor=document.createElement("div"); editor.hidden=true;
          const edit=document.createElement("button"); edit.textContent="Edit title & assignments";
          edit.onclick=()=>{editor.hidden=!editor.hidden;card.classList.toggle("external-editing",!editor.hidden);};actions.append(edit);
          const name=document.createElement("input"); name.value=source.series_title;name.setAttribute("aria-label","Series title");
          const rows=document.createElement("div");externalAssignmentEditor(rows,source.assignments);
          const save=document.createElement("button");save.textContent="Save assignments";
          save.onclick=async()=>{try{const assignments=[...rows.querySelectorAll(".external-assignment")].map(r=>({channel:r.querySelectorAll("select")[0].value,scope:r.querySelectorAll("select")[1].value}));await externalPost(`/api/external-media/${source.id}`,{series_title:name.value,assignments});await loadExternalSources();}catch(e){$("external-message").textContent=e.message;}};
          editor.append(name,rows,save);card.append(actions,editor);
          button(source.enabled ? "Disconnect" : "Reconnect",async()=>{
            if(source.enabled&&!confirm("Disconnect this source? Unlocked future schedules will change; locked schedules may show continuity for these files. Its files and catalog history will be retained."))return;
            await externalPost(`/api/external-media/${source.id}`,{enabled:!source.enabled});
          });
          host.append(card);
        }
        if(!externalData.sources.length) host.textContent="No external series registered.";
      }catch(e){$("external-message").textContent=e.message;}
    }
    $("external-add").onclick=async()=>{
      const button=$("external-add");button.disabled=true;
      try{const result=await externalPost("/api/external-media",{path:$("external-path").value,series_title:$("external-title").value,new_channel:$("external-new-channel").value,channel_number:$("external-number").value,scope:$("external-scope").value,assignments:[{channel:$("external-channel").value,scope:$("external-scope").value}]});$("external-message").textContent=result.message||"Source registered. Scan queued through maintenance; refresh sources to see progress.";await loadExternalSources();}
      catch(e){$("external-message").textContent=e.message;}finally{button.disabled=false;}
    };
    $("external-refresh").onclick=loadExternalSources;
    loadExternalSources();
    setInterval(()=>{if(externalData?.sources.some(s=>s.enabled&&["running","pending"].includes(s.scan_status))&&!document.querySelector(".external-editing")&&!$("external-media-panel").contains(document.activeElement))loadExternalSources();},5000);

    let inventoryData = null;
    async function loadBumperReview() {
      const data = await api("/api/bumper-cues/review");
      $("bumper-review-count").textContent = data.pending_count;
      $("bumper-series-options").replaceChildren(
        ...data.series.map(title => new Option(title, title))
      );
      const items = $("bumper-review-filter").value === "pending"
        ? data.items.filter(item => item.review_required) : data.items;
      const body = $("bumper-review"); body.replaceChildren();
      $("bumper-review-empty").hidden = items.length > 0;
      for (const item of items) {
        const row = document.createElement("tr");
        addCell(row, item.id); addCell(row, item.original_filename);
        addCell(row,
          `${item.automatic_position.replaceAll("_", " ")}` +
          (item.automatic_series_hint ? ` / ${item.automatic_series_hint}` : ""));
        const placement = addCell(row, "");
        const position = document.createElement("select");
        for (const [value, label] of [
          ["auto", "Use filename detection"], ["general", "General bumper"],
          ["break_start", "Before commercials"], ["break_end", "After commercials"],
          ["exclude", "Do not schedule"]
        ]) position.add(new Option(label, value));
        position.value = item.override_position || "auto";
        placement.append(position);
        const showCell = addCell(row, "");
        const show = document.createElement("input");
        show.setAttribute("list", "bumper-series-options");
        show.placeholder = "Any show";
        show.value = item.override_series || "";
        const updateShow = () => {
          show.disabled = !["break_start", "break_end"].includes(position.value);
          if (show.disabled) show.value = "";
        };
        position.addEventListener("change", updateShow);
        updateShow(); showCell.append(show);
        addCell(row, item.source_relative_path, "path");
        const action = addCell(row, "", "actions");
        const save = document.createElement("button");
        save.textContent = "Save";
        save.onclick = async () => {
          save.disabled = true;
          try {
            await api(`/api/bumper-cues/${item.id}/override`, {
              method:"POST", headers:{"Content-Type":"application/json"},
              body:JSON.stringify({position:position.value, series_title:show.value.trim()})
            });
            notice(`Saved bumper ${item.id}. Future schedules will use this placement.`);
            await loadBumperReview();
          } catch (error) { notice(error.message, "error"); save.disabled = false; }
        };
        const general = document.createElement("button");
        general.textContent = "Mark general";
        general.onclick = () => { position.value = "general"; updateShow(); save.click(); };
        action.append(save, general); body.append(row);
      }
    }
    function inventoryTypeLabel(value) {
      return String(value || "unknown").replaceAll("_", " ").replace(/\\b\\w/g, letter => letter.toUpperCase());
    }
    function inventoryDuration(seconds) {
      const total = Number(seconds || 0);
      if (total < 60) return `${Math.round(total)}s`;
      if (total < 3600) return `${Math.round(total / 60)}m`;
      const hours = Math.floor(total / 3600);
      const minutes = Math.round((total % 3600) / 60);
      return `${hours}h ${minutes}m`;
    }
    function renderInventory() {
      if (!inventoryData) return;
      const search = $("inventory-search").value.trim().toLowerCase();
      const mediaType = $("inventory-type").value;
      const channel = $("inventory-channel").value;
      const countFilter = $("inventory-count").value;
      const items = inventoryData.items.filter(item => {
        const searchable = `${item.title} ${item.channel_slug} ${item.location} ${item.media_type} ${item.row_kind}`.toLowerCase();
        return (!search || searchable.includes(search))
          && (!mediaType || item.media_type === mediaType)
           && (!channel || item.channel_slug === channel)
           && (countFilter !== "thin" || Number(item.item_count) < 5)
           && (countFilter !== "review" || Number(item.review_count) > 0)
           && (countFilter !== "unplaced" || Boolean(item.recurring_unplaced));
      });
      const body = $("inventory"); body.replaceChildren();
      $("inventory-empty").hidden = items.length > 0;
      for (const item of items) {
        const row = document.createElement("tr");
        if (item.row_kind === "shadow") {
          row.className = "inventory-shadow-row";
          row.title = "This row contains shadow scheduling references to original media.";
        }
        const titleCell = addCell(row, "");
        const titleLink = document.createElement("a");
        titleLink.href = "#";
        titleLink.className = "inventory-title-link";
        titleLink.textContent = item.title;
        titleLink.title = `Open ${item.folder_relative_path} in the file browser`;
        titleLink.addEventListener("click", async event => {
          event.preventDefault();
          try {
            await api("/api/inventory/open-folder", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify(item.row_kind === "external" ? {asset_id:item.preview_asset_id} : {path: item.folder_relative_path})
            });
            notice(`Opened the folder for ${item.title}.`);
          } catch (error) { notice(error.message, "error"); }
        });
        titleCell.append(titleLink);
        if (item.row_kind === "shadow" || item.row_kind === "external") {
          const badge = document.createElement("span");
          badge.className = "shadow-badge";
          badge.textContent = item.row_kind === "external" ? "External · read-only" : "Shadow";
          titleCell.append(badge);
        }
        addCell(row, inventoryTypeLabel(item.media_type));
        addCell(row, item.item_count);
        addCell(row, item.shadow_count || 0, Number(item.shadow_count) ? "status-open" : "");
        addCell(row, item.ready_count, Number(item.ready_count) ? "status-ready" : "");
        addCell(row, item.review_count, Number(item.review_count) ? "status-open" : "");
        addCell(row, String(item.channel_slug).toUpperCase());
        addCell(row, item.location);
        addCell(
          row,
          item.recurring_displaced
            ? `Holiday rotation; regular slot saved${item.holiday_upcoming_airings?.length
                ? ` · next ${item.holiday_upcoming_airings[0].target_date}` : ""}`
            : item.recurring_unplaced
              ? "No upcoming placement"
              : (item.upcoming_appearances ? `${item.upcoming_appearances} scheduled`
                : item.recurring_assigned ? "Recurring slot assigned" : "—"),
          item.recurring_unplaced ? "status-open" : ""
        );
        addCell(row, inventoryDuration(item.duration_seconds));
        const actions = addCell(row, "", "actions");
        const shadowAction = document.createElement("button");
        if (item.row_kind === "shadow") {
          shadowAction.textContent = "Delete Shadows";
          shadowAction.className = "danger";
          shadowAction.onclick = () => previewShadowDeletion(item);
        } else {
          shadowAction.textContent = "Create shadows";
          shadowAction.onclick = () => openShadowDialog({
            folder: item.folder_relative_path,
            label: `${item.title} folder (${item.item_count} indexed items)`
          });
        }
        if (item.row_kind !== "external") actions.append(shadowAction);
        else actions.textContent = "Manage assignments in External Media Sources";
        body.append(row);
      }
    }
    async function loadInventory() {
      inventoryData = await api("/api/inventory");
      const summary = inventoryData.summary;
      $("inventory-stats").replaceChildren(
        card("Series", summary.series), card("Movies", summary.movies),
        card("Complete blocks", summary.blocks), card("Current assets", summary.assets),
        card("Scheduling placements", summary.placements),
        card("Unplaced recurring series", summary.unplaced_recurring || 0),
        card("Items needing review", summary.review)
      );
      const type = $("inventory-type"); const selectedType = type.value;
      type.replaceChildren(new Option("All media types", ""),
        ...inventoryData.media_types.map(value => new Option(inventoryTypeLabel(value), value)));
      type.value = selectedType;
      const channel = $("inventory-channel"); const selectedChannel = channel.value;
      channel.replaceChildren(new Option("All channels", ""),
        ...inventoryData.channels.map(value => new Option(value.toUpperCase(), value)));
      channel.value = selectedChannel;
      renderInventory();
    }

    let shadowData = null;
    let shadowSource = null;
    let shadowDeletePreview = null;
    async function previewShadowDeletion(item) {
      try {
        shadowDeletePreview = await api("/api/shadows/deletion-preview", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({shadow_ids:item.shadow_ids})
        });
        const count = Number(shadowDeletePreview.count || 0);
        $("shadow-delete-summary").textContent =
          `${shadowDeletePreview.title} · ${shadowDeletePreview.channel} · ${shadowDeletePreview.location}. ` +
          `${count} shadow sidecar file${count === 1 ? "" : "s"} will be deleted.`;
        $("shadow-delete-dialog").showModal();
      } catch (error) { notice(error.message, "error"); }
    }
    async function deleteShadows() {
      if (!shadowDeletePreview) return;
      $("shadow-delete-confirm").disabled = true;
      try {
        const result = await api("/api/shadows/delete", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({
            shadow_ids:shadowDeletePreview.shadow_ids,
            token:shadowDeletePreview.token
          })
        });
        $("shadow-delete-dialog").close();
        const removed = result.deleted.length + result.already_missing.length;
        const failures = result.failed.length;
        notice(failures
          ? `Removed ${removed} shadow files; ${failures} could not be deleted. Original media was untouched.`
          : `Deleted ${removed} shadow files. Original media was untouched; future schedules will rebuild during maintenance.`,
          failures ? "error" : "");
        await Promise.all([loadShadows(), loadInventory()]);
      } catch (error) { notice(error.message, "error"); }
      finally { $("shadow-delete-confirm").disabled = false; }
    }
    function updateShadowDestination() {
      if (!shadowData) return;
      const scope = $("shadow-scope").value;
      const channel = $("shadow-channel").value;
      let values = [];
      if (scope === "daypart") {
        values = shadowData.options.dayparts.map(value => ({value, label:value}));
      } else if (scope === "weekly") {
        values = shadowData.options.weekly
          .filter(item => item.channel === channel)
          .map(item => ({value:item.slug, label:`${item.label} (${item.slug})`}));
      } else {
        values = shadowData.options.holidays.map(value => ({value, label:value}));
      }
      const select = $("shadow-value"); const previous = select.value;
      select.replaceChildren(...values.map(item => new Option(item.label, item.value)));
      if (values.some(item => item.value === previous)) select.value = previous;
      $("shadow-confirm").disabled = values.length === 0;
      $("shadow-destination-preview").textContent = values.length
        ? `Destination: ${channel} / ${scope} / ${select.value}. Folder structure and media categories will be preserved.`
        : `No ${scope} destination is configured for ${channel}.`;
    }
    async function openShadowDialog(source) {
      if (!shadowData) await loadShadows();
      shadowSource = source;
      $("shadow-source-label").textContent = `Source: ${source.label}`;
      updateShadowDestination();
      $("shadow-dialog").showModal();
    }
    async function createShadows() {
      if (!shadowSource) return;
      const payload = {
        ...shadowSource,
        label: undefined,
        channel: $("shadow-channel").value,
        scope: $("shadow-scope").value,
        value: $("shadow-value").value
      };
      $("shadow-confirm").disabled = true;
      try {
        const result = await api("/api/shadows", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify(payload)
        });
        $("shadow-dialog").close();
        notice(`Created ${result.created.length} shadow files; ${result.skipped.length} already existed; ${result.conflicts.length} conflicts. Schedules are now stale and will rebuild during maintenance.`);
        await Promise.all([loadShadows(), loadInventory()]);
      } catch (error) {
        notice(error.message, "error");
      } finally {
        $("shadow-confirm").disabled = false;
      }
    }
    async function loadShadows() {
      shadowData = await api("/api/shadows");
      const summary = shadowData.summary;
      $("shadow-stats").replaceChildren(
        card("Shadow files", summary.total), card("Ready", summary.ready),
        card("Needs review", summary.review)
      );
      const channels = $("shadow-channel"); const selectedChannel = channels.value;
      channels.replaceChildren(...shadowData.options.channels.map(
        item => new Option(item.name, item.slug)
      ));
      if ([...channels.options].some(option => option.value === selectedChannel)) {
        channels.value = selectedChannel;
      }
      const folderList = $("shadow-folder-list"); folderList.replaceChildren();
      for (const folder of shadowData.folders) {
        const option = document.createElement("option");
        option.value = folder.path; option.label = `${folder.count} indexed files`;
        folderList.append(option);
      }
      const body = $("shadows"); body.replaceChildren();
      $("shadows-empty").hidden = shadowData.items.length > 0;
      for (const item of shadowData.items) {
        const row = document.createElement("tr");
        addCell(row, item.status, item.status === "ready" ? "status-ready" : "status-open");
        addCell(row, item.shadow_relative_path, "path");
        addCell(row, item.current_target_path || item.target_relative_path, "path");
        addCell(row, String(item.channel_slug || "shared").toUpperCase());
        addCell(row, item.collection_type
          ? `${item.collection_type}: ${item.collection_slug}`
          : (item.daypart_slug || "all-day"));
        addCell(row, item.detail || "—");
        body.append(row);
      }
      updateShadowDestination();
    }

    function byteSize(value) {
      const bytes = Number(value || 0);
      if (bytes < 1024) return `${bytes} B`;
      const units = ["KB", "MB", "GB", "TB"];
      let amount = bytes / 1024; let index = 0;
      while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
      return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[index]}`;
    }

    async function reviewDuplicate(hash, action) {
      try {
        await api(`/api/duplicates/${hash}`, {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({action})
        });
        notice(action === "intentional" ?
          "Duplicate locations marked as intentional." : "Duplicate set returned to review.");
        await loadDuplicates(); await loadStats();
      } catch (error) { notice(error.message, "error"); }
    }

    async function loadDuplicates() {
      const data = await api("/api/duplicates");
      $("duplicate-stats").replaceChildren(
        card("Files hashed", `${data.hashed} / ${data.total}`),
        card("Exact duplicate sets", data.groups.length),
        card("Pending review", data.pending),
        card("Intentional sets", data.intentional),
        card("Duplicated storage", byteSize(data.duplicate_bytes))
      );
      const filter = $("duplicate-filter").value;
      const groups = data.groups.filter(item => filter === "all" || item.status === filter);
      const body = $("duplicates"); body.replaceChildren();
      $("duplicates-empty").hidden = groups.length > 0;
      for (const group of groups) {
        const row = document.createElement("tr");
        addCell(row, group.status);
        addCell(row, group.member_count);
        addCell(row, byteSize(group.duplicate_bytes));
        const locations = addCell(row, "", "path");
        for (const member of group.members) {
          const line = document.createElement("div");
          const assignment = member.collection_slug ?
            `${member.channel_slug || "shared"} / ${member.collection_type} / ${member.collection_slug}` :
            `${member.channel_slug || "shared"} / ${member.daypart_slug || "all-day"}`;
          line.textContent = `#${member.id} ${member.source_relative_path} — ${assignment}`;
          locations.append(line);
        }
        const actions = addCell(row, "", "actions");
        const button = document.createElement("button");
        if (group.status === "pending") {
          button.textContent = "Keep all intentionally";
          button.onclick = () => reviewDuplicate(group.sha256, "intentional");
        } else {
          button.textContent = "Review again";
          button.onclick = () => reviewDuplicate(group.sha256, "pending");
        }
        actions.append(button); body.append(row);
      }
    }

    async function reviewAsset(id, action) {
      await api(`/api/assets/${id}/review`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action})
      });
      await load();
    }

    async function forgetMissingAssetRecord(id, path) {
      if (!confirm(
        `Forget this missing library record?\n\n${path}\n\n` +
        "This does not delete a media file. Historical schedule entries keep " +
        "their title and time, but lose their link to this removed file."
      )) return;
      try {
        const result = await api(`/api/assets/${id}/review`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "forget"})
        });
        notice(`Removed missing record ${id}; ${result.detached_schedule_entries} historical schedule link(s) detached.`);
        await load();
      } catch (error) { notice(`Asset ${id}: ${error.message}`, "error"); }
    }

    async function normalizationAction(id, payload) {
      try {
        const result = await api(`/api/assets/${id}/normalization`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        if (result.updated_count != null) {
          notice(`${result.updated_count} aspect-review video(s) approved in ${result.folder}.`);
        } else {
          notice(`Asset ${id}: ${payload.action} completed.`);
        }
        await load();
      } catch (error) {
        notice(`Asset ${id}: ${error.message}`, "error");
      }
    }

    async function audioAction(id, payload) {
      try {
        await api(`/api/assets/${id}/audio-normalization`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        notice(`Asset ${id}: audio ${payload.action} completed.`);
        await load();
      } catch (error) { notice(`Asset ${id}: ${error.message}`, "error"); }
    }

    function addCell(row, text, className) {
      const cell = document.createElement("td");
      cell.textContent = escText(text);
      if (className) cell.className = className;
      row.appendChild(cell);
      return cell;
    }

    let currentAssetItems = [];
    let holidayReviewState = {items:[], folders:[], media_root:""};
    let selectedHolidayReview = null;

    function updateHolidayMovePreview() {
      const folder = $("holiday-move-folder").value.trim().replaceAll("\\\\", "/").replace(/^[/]+|[/]+$/g, "");
      const item = selectedHolidayReview;
      const category = item?.source_category?.toLowerCase();
      const parts = folder.split("/");
      const descendants = category && parts[parts.length - 1]?.toLowerCase() === category
        ? item.source_subfolders : [];
      $("holiday-move-preview").textContent = item && folder
        ? `Destination: ${holidayReviewState.media_root}/${[folder, ...descendants, item.filename].join("/")}`
        : "Choose a destination folder to see the full path.";
    }

    async function holidayReviewAction(item, action, folder = "") {
      try {
        const result = await api("/api/holiday-reviews/action", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({asset_id:item.asset_id, holiday:item.holiday_slug, action, folder})
        });
        if (action === "move") {
          $("holiday-move-dialog").close();
          selectedHolidayReview = null;
          notice(result.library_review_needed
            ? `Moved to ${result.moved_to}. The chosen folder needs Library review.`
            : `Moved to ${result.moved_to}. Future schedules will refresh during maintenance.`);
        } else notice(`Dismissed ${item.filename} for ${item.holiday_slug}.`);
        await Promise.all([loadHolidayReviews(), load()]);
      } catch (error) { notice(error.message, "error"); }
    }

    async function loadHolidayReviews() {
      const data = await api("/api/holiday-reviews");
      holidayReviewState = data;
      const folders = $("holiday-folders"); folders.replaceChildren();
      for (const folder of data.folders) {
        const option = document.createElement("option"); option.value = folder;
        folders.appendChild(option);
      }
      const body = $("holiday-reviews"); body.replaceChildren();
      $("holiday-reviews-empty").hidden = data.items.length > 0;
      let previousHoliday = "";
      for (const item of data.items) {
        if (item.holiday_slug !== previousHoliday) {
          previousHoliday = item.holiday_slug;
          const heading = document.createElement("tr");
          const cell = document.createElement("th"); cell.colSpan = 6;
          cell.textContent = previousHoliday.replaceAll("-", " ").toUpperCase();
          heading.appendChild(cell); body.appendChild(heading);
        }
        const row = document.createElement("tr");
        [item.holiday_slug, item.filename].forEach(value => addCell(row, value));
        const location = addCell(row, "");
        const link = document.createElement("a");
        link.href = `/api/assets/${encodeURIComponent(item.asset_id)}/play`;
        link.target = "_blank"; link.rel = "noopener";
        link.textContent = item.source_relative_path;
        link.title = "Open this video in a new tab";
        location.appendChild(link);
        [item.media_type || "Unknown", item.matched_clue].forEach(value => addCell(row, value));
        const actions = addCell(row, "", "actions");
        const move = document.createElement("button"); move.textContent = "Move";
        move.onclick = () => {
          selectedHolidayReview = item;
          $("holiday-move-source").textContent = `Moving ${item.source_relative_path}`;
          $("holiday-move-folder").value = item.suggested_destination_folder || "";
          updateHolidayMovePreview();
          $("holiday-move-dialog").showModal();
        };
        const dismiss = document.createElement("button"); dismiss.textContent = "Dismiss";
        dismiss.onclick = () => holidayReviewAction(item, "dismiss");
        actions.append(move, dismiss); body.appendChild(row);
      }
    }
    let graphicWeeklyDestination = "";
    let graphicReplacement = null;

    function assetFolder(item) {
      const normalized = String(item.source_relative_path || "").replaceAll("\\\\", "/");
      return normalized.includes("/") ? normalized.slice(0, normalized.lastIndexOf("/")) : ".";
    }

    async function commercialReviewAction(id, payload) {
      try {
        await api(`/api/assets/${id}/commercial-review`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        notice(`Asset ${id}: ${payload.action} saved. Future schedules will refresh during maintenance.`);
        await load();
      } catch (error) { notice(`Asset ${id}: ${error.message}`, "error"); }
    }

    function setActiveReview(row) {
      document.querySelectorAll(".aspect-review-row.active-review").forEach(
        item => item.classList.remove("active-review")
      );
      if (row) row.classList.add("active-review");
    }

    function renderAssets(items) {
      currentAssetItems = items;
      const body = $("assets");
      body.replaceChildren();
      $("empty").hidden = items.length > 0;
      const viewingAspectReview = $("review").value.startsWith("normalization:");
      const viewingAudioReview = $("review").value.startsWith("audio:");
      const viewingLibraryIgnored = $("review").value === "library:ignored";
      for (const item of items) {
        const reviewingLongCommercial = item.commercial_review_decision === "pending"
          && !viewingAspectReview && !viewingAudioReview;
        const row = document.createElement("tr");
        row.dataset.assetId = item.id;
        if (viewingAspectReview) {
          row.className = "aspect-review-row";
          row.addEventListener("click", () => setActiveReview(row));
        }
        addCell(row, item.id);
        addCell(row, item.display_title || item.original_filename);
        addCell(row, item.external_placements || item.channel_slug || "shared");
        addCell(row, item.collection_type === "holiday"
          ? item.holiday_weekly_slug ? `weekly: ${item.holiday_weekly_slug}`
            : item.daypart_slug ? `holiday: ${item.daypart_slug}` : "holiday: all-day (legacy)"
          : item.daypart_slug || "—");
        addCell(row, item.media_type || "unknown");
        addCell(row, item.duration_seconds == null ? "—" : `${Math.round(item.duration_seconds)}s`);
        addCell(row, item.review_status, `status-${item.review_status}`);
        const normalization = addCell(row, "", "normalization-detail");
        const state = document.createElement("div");
        const correctionState = item.proxy_status === "promoted" ? "correction installed" :
          item.proxy_status === "ready" ? "correction staged" :
          item.proxy_status === "failed" ? "correction failed" :
          item.proxy_status === "stale" ? "correction stale" : "no correction installed";
        state.textContent = `${item.analysis_status || "not analyzed"} / ${item.detected_mode || "—"} / ${correctionState}`;
        const audioState = document.createElement("div");
        audioState.textContent = `audio: ${item.audio_analysis_status || "not analyzed"}` +
          (item.measured_i == null ? "" : ` / ${Number(item.measured_i).toFixed(1)} LUFS`) +
          ` / ${item.audio_correction_status || "not created"}`;
        if (item.source_kind === "external") {
          state.textContent = "External · read-only · normalization unavailable";
          normalization.append(state);
          addCell(row, "Preview using Play");
          addCell(row, `${item.absolute_path} / ${item.source_relative_path}`, "path");
          const safeActions = addCell(row, "", "actions");
          const link = document.createElement("a");
          link.href = `/api/assets/${item.id}/play`; link.target = "_blank";
          link.textContent = "Play / preview"; safeActions.append(link);
          body.appendChild(row); continue;
        }
        const select = document.createElement("select");
        select.className = "mode";
        const modeLabels = {
          auto: "Automatic suggestion",
          native_4_3: "Native 4:3",
          pillarboxed_4_3: "Crop pillarbox",
          pillarboxed_stretched_4_3: "Crop + fix stretch",
          stretched_4_3: "Fix stretched 4:3",
          preserve_frame: "Keep original frame",
          custom: "Custom crop"
        };
        const previewRecipe = viewingAspectReview ? item.draft_preview : null;
        const modeValues = viewingAspectReview
          ? ["native_4_3", "pillarboxed_4_3", "pillarboxed_stretched_4_3", "stretched_4_3", "preserve_frame", "custom"]
          : ["auto", "native_4_3", "pillarboxed_4_3", "pillarboxed_stretched_4_3", "stretched_4_3", "preserve_frame", "custom"];
        const initialMode = previewRecipe?.mode ||
          (item.override_mode && item.override_mode !== "auto" ? item.override_mode : null) ||
          (item.proposed_mode && item.proposed_mode !== "auto" ? item.proposed_mode : "preserve_frame");
        for (const mode of modeValues) {
          const option = document.createElement("option");
          option.value = mode;
          option.textContent = modeLabels[mode];
          option.selected = mode === (viewingAspectReview ? initialMode : (item.override_mode || "auto"));
          select.appendChild(option);
        }
        const cropControls = document.createElement("div");
        cropControls.className = "crop-controls";
        const cropInputs = {};
        for (const [key, label] of [
          ["width", "W"], ["height", "H"], ["x", "X"], ["y", "Y"]
        ]) {
          const input = document.createElement("input");
          input.type = "number";
          input.min = "0";
          input.step = "1";
          input.placeholder = label;
          input.title = `Crop ${key}`;
          const storedKey = `crop_${key}`;
          input.value = previewRecipe?.crop?.[key] ?? item[storedKey] ?? item[`suggested_${storedKey}`] ?? "";
          cropInputs[key] = input;
          cropControls.appendChild(input);
        }
        const applyCrop = document.createElement("button");
        applyCrop.textContent = "Apply crop";
        applyCrop.onclick = () => {
          const crop = Object.fromEntries(
            Object.entries(cropInputs).map(([key, input]) => [
              key, input.value === "" ? null : Number(input.value)
            ])
          );
          if (Object.values(crop).some(value => value == null || !Number.isInteger(value))) {
            notice(`Asset ${item.id}: enter whole-number W, H, X, and Y crop values.`, "error");
            return;
          }
          const mode = ["pillarboxed_4_3", "pillarboxed_stretched_4_3"].includes(select.value)
            ? select.value : "custom";
          normalizationAction(item.id, {action: "override", mode, crop});
        };
        cropControls.appendChild(applyCrop);
        const updateCropVisibility = () => {
          cropControls.hidden = !["pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"].includes(select.value);
        };
        select.onchange = () => {
          updateCropVisibility();
          if (viewingAspectReview) return;
          if (["pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"].includes(select.value)) {
            notice(`Asset ${item.id}: enter W, H, X, and Y, then click Apply crop.`);
            return;
          }
          normalizationAction(item.id, {action: "override", mode: select.value});
        };
        updateCropVisibility();
        const links = document.createElement("div");
        links.className = "preview-links";
        if (item.source_preview_relative_path) {
          const original = document.createElement("a");
          original.href = `/api/assets/${item.id}/preview/source`;
          original.target = "_blank";
          original.textContent = "original";
          links.appendChild(original);
        }
        if (item.normalized_preview_relative_path) {
          const corrected = document.createElement("a");
          corrected.href = `/api/assets/${item.id}/preview/normalized`;
          corrected.target = "_blank";
          corrected.textContent = "corrected";
          links.appendChild(corrected);
        }
        normalization.append(state, audioState, select, cropControls);
        if (!viewingAspectReview) normalization.append(links);
        const previewCell = document.createElement("td");
        previewCell.className = viewingAspectReview ? "aspect-preview-cell" : "";
        let previewToken = previewRecipe?.token || null;
        let correctedImage = null;
        let correctedPlaceholder = null;
        let previewStatus = null;
        let approveShown = null;
        let approveFolder = null;
        let originalPanel = null;
        let correctedPanel = null;
        let installOriginalImage = null;
        let installCorrectedImage = null;
        let installExpandedComparison = null;

        const selectedCrop = () => {
          if (!["pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"].includes(select.value)) {
            return null;
          }
          const crop = Object.fromEntries(
            Object.entries(cropInputs).map(([key, input]) => [
              key, input.value === "" ? null : Number(input.value)
            ])
          );
          if (Object.values(crop).some(value => value == null || !Number.isInteger(value))) {
            throw new Error("Enter whole-number W, H, X, and Y crop values.");
          }
          return crop;
        };

        const invalidatePreview = () => {
          if (!viewingAspectReview) return;
          previewToken = null;
          previewCell.querySelectorAll(".aspect-preview-panel:nth-child(2) img").forEach(
            image => image.classList.add("stale")
          );
          if (previewStatus) previewStatus.textContent = "Settings changed — preview again before approval.";
          if (approveShown) approveShown.disabled = true;
          if (approveFolder) approveFolder.disabled = true;
        };

        if (viewingAspectReview) {
          select.addEventListener("change", invalidatePreview);
          Object.values(cropInputs).forEach(input => input.addEventListener("input", invalidatePreview));
          applyCrop.hidden = true;

          const comparison = document.createElement("div");
          comparison.className = "aspect-comparison";
          const makePanel = label => {
            const panel = document.createElement("div");
            panel.className = "aspect-preview-panel";
            const heading = document.createElement("strong");
            heading.textContent = label;
            panel.appendChild(heading);
            comparison.appendChild(panel);
            return panel;
          };
          originalPanel = makePanel("Original");
          correctedPanel = makePanel("Corrected draft");

          const clearPanelFrames = panel => {
            panel.querySelectorAll("a,.preview-placeholder").forEach(node => node.remove());
            panel.classList.remove("expanded");
          };

          installOriginalImage = () => {
            clearPanelFrames(originalPanel);
            const link = document.createElement("a");
            link.href = `/api/assets/${item.id}/preview/source`;
            link.target = "_blank";
            const image = document.createElement("img");
            image.loading = "lazy";
            image.alt = `Original preview for ${item.display_title || item.original_filename}`;
            image.src = `${link.href}?v=${item.modified_ns || Date.now()}`;
            link.appendChild(image); originalPanel.appendChild(link);
          };
          installCorrectedImage = token => {
            clearPanelFrames(correctedPanel);
            const link = document.createElement("a");
            link.href = `/api/assets/${item.id}/preview/normalized`;
            link.target = "_blank";
            correctedImage = document.createElement("img");
            correctedImage.alt = `Corrected preview for ${item.display_title || item.original_filename}`;
            correctedImage.src = `${link.href}?v=${token}`;
            link.appendChild(correctedImage); correctedPanel.appendChild(link);
          };
          installExpandedComparison = token => {
            for (const [panel, kind, label] of [
              [originalPanel, "source", "Original"],
              [correctedPanel, "normalized", "Corrected"]
            ]) {
              clearPanelFrames(panel); panel.classList.add("expanded");
              for (let index = 1; index <= 3; index += 1) {
                const link = document.createElement("a");
                link.href = `/api/assets/${item.id}/preview/${kind}?frame=${index}`;
                link.target = "_blank";
                const image = document.createElement("img");
                image.loading = "lazy";
                image.alt = `${label} preview ${index} for ${item.display_title || item.original_filename}`;
                image.src = `${link.href}&v=${token}`;
                link.appendChild(image); panel.appendChild(link);
                if (kind === "normalized" && index === 2) correctedImage = image;
              }
            }
          };
          if (item.source_preview_relative_path) {
            installOriginalImage();
          } else {
            const pending = document.createElement("div");
            pending.className = "preview-placeholder";
            const generate = document.createElement("button");
            generate.textContent = "Generate original now";
            generate.onclick = async () => {
              generate.disabled = true; generate.textContent = "Generating…";
              try {
                await api(`/api/assets/${item.id}/normalization`, {
                  method:"POST", headers:{"Content-Type":"application/json"},
                  body:JSON.stringify({action:"source-preview"})
                });
                item.source_preview_relative_path = `${item.id}-source.jpg`;
                installOriginalImage();
              } catch (error) {
                generate.disabled = false; generate.textContent = "Retry original";
                notice(`Asset ${item.id}: ${error.message}`, "error");
              }
            };
            pending.append("Preview pending maintenance. ", generate);
            originalPanel.appendChild(pending);
          }

          correctedPlaceholder = document.createElement("div");
          correctedPlaceholder.className = "preview-placeholder";
          correctedPlaceholder.textContent = previewToken ? "Loading corrected preview…" : "Choose settings, then preview the correction.";
          correctedPanel.appendChild(correctedPlaceholder);
          if (previewToken && item.normalized_preview_relative_path) {
            correctedPlaceholder.remove();
            correctedPlaceholder = null;
            if (previewRecipe?.frame_count === 3) {
              installExpandedComparison(previewToken);
            } else {
              installCorrectedImage(previewToken);
            }
          }
          previewStatus = document.createElement("div");
          previewStatus.className = "preview-status";
          previewStatus.textContent = previewToken
            ? "This correction is ready for approval. Click either image to enlarge it."
            : "No changes are saved until you approve the rendered correction.";
          previewCell.append(comparison, previewStatus);
        } else {
          previewCell.textContent = "—";
        }
        if (reviewingLongCommercial) {
          normalization.textContent = `Threshold: ${Math.round(Number(item.commercial_review_threshold || 180) / 60)} minutes`;
          const player = document.createElement("video");
          player.controls = true; player.preload = "metadata"; player.width = 280;
          player.src = `/api/assets/${item.id}/play`;
          const openVideo = document.createElement("a");
          openVideo.href = player.src; openVideo.target = "_blank";
          openVideo.textContent = "Open video";
          previewCell.replaceChildren(player, openVideo);
        }
        row.appendChild(previewCell);
        addCell(row, item.source_relative_path, "path");
        const actions = addCell(row, "", "actions");
        const analyze = document.createElement("button");
        analyze.textContent = "Analyze";
        analyze.onclick = () => normalizationAction(item.id, {action: "analyze"});
        const preview = document.createElement("button");
        preview.textContent = "Preview";
        preview.onclick = () => normalizationAction(item.id, {action: "preview"});
        const replace = document.createElement("button");
        replace.textContent = "Replace + backup";
        replace.onclick = () => {
          if (confirm(`Replace asset ${item.id} with its validated correction? The original will be backed up.`)) {
            normalizationAction(item.id, {action: "replace", confirm_replace: true});
          }
        };
        const approve = document.createElement("button");
        approve.textContent = viewingAspectReview ? "Approve aspect" : "Approve";
        approve.onclick = () => {
          if (viewingAspectReview) {
            normalizationAction(item.id, {action: "approve"});
          } else {
            reviewAsset(item.id, "approve");
          }
        };
        const legacyApproveFolder = document.createElement("button");
        legacyApproveFolder.textContent = "Apply + approve folder";
        legacyApproveFolder.title = "Apply this correction to every aspect-review video in this exact folder";
        legacyApproveFolder.onclick = () => {
          const mode = select.value;
          if (mode === "auto") {
            notice(`Asset ${item.id}: choose a specific correction first.`, "error");
            return;
          }
          const payload = {action: "approve-folder", mode};
          if (["pillarboxed_4_3", "pillarboxed_stretched_4_3", "custom"].includes(mode)) {
            const crop = Object.fromEntries(
              Object.entries(cropInputs).map(([key, input]) => [
                key, input.value === "" ? null : Number(input.value)
              ])
            );
            if (Object.values(crop).some(value => value == null || !Number.isInteger(value))) {
              notice(`Asset ${item.id}: enter whole-number W, H, X, and Y crop values.`, "error");
              return;
            }
            payload.crop = crop;
          }
          const normalizedPath = String(item.source_relative_path || "").replaceAll("\\\\", "/");
          const folder = normalizedPath.includes("/")
            ? normalizedPath.slice(0, normalizedPath.lastIndexOf("/")) : ".";
          if (confirm(`Are you sure?\n\nApply ${mode.replaceAll("_", " ")} to every aspect-review video in ${folder}?`)) {
            normalizationAction(item.id, payload);
          }
        };
        const ignore = document.createElement("button");
        ignore.textContent = "Ignore";
        ignore.onclick = () => reviewAsset(item.id, "ignore");
        if (reviewingLongCommercial) {
          const openFolder = document.createElement("button");
          openFolder.textContent = "Open folder";
          openFolder.title = "Open the folder that contains this commercial";
          openFolder.onclick = async () => {
            try {
              await api("/api/inventory/open-folder", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({path: assetFolder(item)})
              });
            } catch (error) { notice(error.message, "error"); }
          };
          const approveLong = document.createElement("button");
          approveLong.textContent = "Approve exception";
          approveLong.onclick = () => commercialReviewAction(item.id, {action:"approve"});
          const disableLong = document.createElement("button");
          disableLong.textContent = "Disable for scheduling";
          disableLong.onclick = () => commercialReviewAction(item.id, {action:"disable"});
          const category = document.createElement("select");
          for (const value of ["episodes", "shorts", "blocks", "promos", "filler"]) {
            const option = document.createElement("option"); option.value = value;
            option.textContent = `Move to ${value}`; category.appendChild(option);
          }
          const move = document.createElement("button");
          move.textContent = "Move file";
          move.onclick = () => {
            if (confirm(`Move ${item.source_relative_path} to its ${category.value} folder?`))
              commercialReviewAction(item.id, {action:"move", category:category.value});
          };
          actions.append(openFolder, approveLong, disableLong, category, move);
        } else if (viewingLibraryIgnored) {
          const forget = document.createElement("button");
          forget.className = "danger";
          forget.textContent = "Forget record";
          forget.title = "Remove this missing-file record from CRTV Sim's database";
          forget.onclick = () => forgetMissingAssetRecord(
            item.id, item.source_relative_path || item.original_filename
          );
          actions.append(forget);
        } else if (viewingAudioReview) {
          const retryAudio = document.createElement("button");
          retryAudio.textContent = "Retry audio analysis";
          retryAudio.onclick = () => audioAction(item.id, {action:"retry"});
          const skipAudio = document.createElement("button");
          skipAudio.textContent = "Skip audio correction";
          skipAudio.onclick = () => audioAction(item.id, {action:"skip"});
          actions.append(retryAudio, skipAudio);
        } else {
          actions.append(analyze, preview, replace, approve);
          if (viewingAspectReview) actions.append(legacyApproveFolder);
          if (!viewingAspectReview) actions.append(ignore);
        }
        if (viewingAspectReview) {
          actions.replaceChildren();
          const previewCorrection = document.createElement("button");
          previewCorrection.className = "preview-correction";
          previewCorrection.textContent = "Preview correction";
          let requestedFrameCount = 1;
          previewCorrection.onclick = async () => {
            previewCorrection.disabled = true;
            previewCorrection.textContent = "Rendering…";
            if (previewStatus) previewStatus.textContent = "Rendering the selected correction…";
            try {
              const result = await api(`/api/assets/${item.id}/normalization`, {
                method:"POST", headers:{"Content-Type":"application/json"},
                body:JSON.stringify({
                  action:"draft-preview", mode:select.value, crop:selectedCrop(),
                  frame_count:requestedFrameCount
                })
              });
              previewToken = result.token;
              item.source_preview_relative_path = result.source;
              item.normalized_preview_relative_path = result.normalized;
              item.draft_preview = result;
              const originalLink = originalPanel.querySelector("a");
              if (!originalLink) installOriginalImage();
              if (result.frame_count === 3) {
                installExpandedComparison(previewToken);
              } else {
                installCorrectedImage(previewToken);
              }
              previewStatus.textContent = "This correction is ready for approval. Click either image to enlarge it.";
              approveShown.disabled = false;
              approveFolder.disabled = false;
            } catch (error) {
              previewStatus.textContent = "Preview failed.";
              notice(`Asset ${item.id}: ${error.message}`, "error");
            } finally {
              previewCorrection.disabled = false;
              previewCorrection.textContent = "Preview correction";
              requestedFrameCount = 1;
            }
          };

          const compareThree = document.createElement("button");
          compareThree.textContent = "Compare 3 frames";
          compareThree.title = "Render matched frames from early, middle, and late in the video";
          compareThree.onclick = () => {
            requestedFrameCount = 3;
            previewCorrection.click();
          };

          approveShown = document.createElement("button");
          approveShown.className = "approve-correction primary";
          approveShown.textContent = "Approve shown correction";
          approveShown.disabled = !previewToken;
          approveShown.onclick = async () => {
            if (!previewToken) return;
            approveShown.disabled = true;
            try {
              await api(`/api/assets/${item.id}/normalization`, {
                method:"POST", headers:{"Content-Type":"application/json"},
                body:JSON.stringify({action:"approve-preview", token:previewToken})
              });
              const next = row.nextElementSibling || row.previousElementSibling;
              row.remove();
              currentAssetItems = currentAssetItems.filter(value => value.id !== item.id);
              $("empty").hidden = body.children.length > 0;
              setActiveReview(next);
              if (next) next.scrollIntoView({block:"nearest"});
              notice(`Asset ${item.id}: approved exactly as previewed.`);
              await loadStats();
            } catch (error) {
              approveShown.disabled = false;
              notice(`Asset ${item.id}: ${error.message}`, "error");
            }
          };

          approveFolder = document.createElement("button");
          approveFolder.textContent = "Apply shown correction to folder";
          approveFolder.disabled = !previewToken;
          approveFolder.onclick = async () => {
            if (!previewToken) return;
            const folder = assetFolder(item);
            const displayedCount = currentAssetItems.filter(value => assetFolder(value) === folder).length;
            if (!confirm(
              `Apply the correction shown for asset ${item.id} to every remaining Aspect Review video in:\n\n${folder}\n\n` +
              `${displayedCount} matching review(s) are currently displayed. CRTV Sim will validate every file before changing any record.`
            )) return;
            approveFolder.disabled = true;
            try {
              const result = await api(`/api/assets/${item.id}/normalization`, {
                method:"POST", headers:{"Content-Type":"application/json"},
                body:JSON.stringify({action:"approve-preview-folder", token:previewToken})
              });
              const removed = new Set(result.asset_ids || []);
              body.querySelectorAll("tr[data-asset-id]").forEach(candidate => {
                if (removed.has(Number(candidate.dataset.assetId))) candidate.remove();
              });
              currentAssetItems = currentAssetItems.filter(value => !removed.has(value.id));
              $("empty").hidden = body.children.length > 0;
              setActiveReview(body.querySelector(".aspect-review-row"));
              notice(`${result.updated_count} aspect-review video(s) approved exactly as previewed in ${result.folder}.`);
              await loadStats();
            } catch (error) {
              approveFolder.disabled = false;
              notice(`Asset ${item.id}: ${error.message}`, "error");
            }
          };
          actions.append(previewCorrection, compareThree, approveShown, approveFolder, analyze);
        }
        const shadow = document.createElement("button");
        shadow.textContent = "Create shadow";
        shadow.onclick = () => openShadowDialog({
          asset_id:item.id,
          label:item.display_title || item.original_filename
        });
        if (!viewingLibraryIgnored) actions.append(shadow);
        body.appendChild(row);
      }
      if (viewingAspectReview) setActiveReview(body.querySelector(".aspect-review-row"));
    }

    function formatBreakpoint(milliseconds) {
      if (milliseconds == null) return "—";
      const total = Number(milliseconds) / 1000;
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const seconds = total % 60;
      return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${seconds.toFixed(3).padStart(6, "0")}`;
    }

    async function breakpointAction(id, payload) {
      try {
        await api(`/api/assets/${id}/breakpoint`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        notice(`Asset ${id}: breakpoint ${payload.action} completed.`);
        await loadBreakpoints();
      } catch (error) { notice(`Asset ${id}: ${error.message}`, "error"); }
    }

    async function loadBreakpoints() {
      const params = new URLSearchParams({limit: "250"});
      if ($("breakpoint-filter").value) params.set("status", $("breakpoint-filter").value);
      const data = await api(`/api/breakpoints?${params}`);
      const body = $("breakpoints"); body.replaceChildren();
      $("breakpoints-empty").hidden = data.items.length > 0;
      for (const item of data.items) {
        const row = document.createElement("tr");
        addCell(row, item.id); addCell(row, item.display_title || item.original_filename);
        addCell(row, `${Math.round(item.duration_seconds)}s`);
        addCell(row, item.break_status || "pending");
        const selectedOffsets = item.selected_offsets_ms
          ? String(item.selected_offsets_ms).split(",").map(Number).sort((a, b) => a - b)
          : item.selected_offset_ms == null ? [] : [Number(item.selected_offset_ms)];
        addCell(row, selectedOffsets.length
          ? selectedOffsets.map(formatBreakpoint).join(", ") : "—");
        const evidence = (item.selected_evidence || []).map(candidate => {
          const signals = candidate.signals || {};
          const transition = signals.black_duration ? `black ${Number(signals.black_duration).toFixed(2)}s` :
            signals.scene_change ? "scene change" : candidate.method;
          const silence = signals.silence ? ", silence" : "";
          const chapter = signals.chapter_supported
            ? `, chapter ${formatBreakpoint(signals.chapter_marker_ms)} (${(signals.chapter_distance_ms / 1000).toFixed(1)}s away)` : "";
          return `${formatBreakpoint(candidate.offset_ms)}: ${transition}${silence}${chapter}`;
        });
        addCell(row, evidence.join("; ") || item.selected_method || "—");
        addCell(row, item.confidence == null ? "—" : Number(item.confidence).toFixed(2));
        const actions = addCell(row, "", "actions");
        const analyze = document.createElement("button"); analyze.textContent = "Analyze";
        analyze.onclick = () => breakpointAction(item.id, {action:"analyze"});
        actions.append(analyze);
        for (const target of item.targets || []) {
          const targetRow = document.createElement("div");
          const label = document.createElement("span");
          label.textContent = `Break ${target.index} (near ${formatBreakpoint(target.target_ms)}): `;
          const current = document.createElement("span");
          current.textContent = target.selected
            ? `${formatBreakpoint(target.selected.offset_ms)} — ${target.selected.method} (${Number(target.selected.score).toFixed(2)})${target.selected.signals?.reason ? `; ${target.selected.signals.reason}` : ""}`
            : "no selected break";
          const manual = document.createElement("input"); manual.placeholder = "HH:MM:SS";
          manual.value = target.selected ? formatBreakpoint(target.selected.offset_ms) : "";
          const apply = document.createElement("button"); apply.textContent = "Set manual";
          apply.onclick = () => {
          const parts = manual.value.trim().split(":").map(Number);
          let seconds = NaN;
          if (parts.length === 1) seconds = parts[0];
          if (parts.length === 2) seconds = parts[0] * 60 + parts[1];
          if (parts.length === 3) seconds = parts[0] * 3600 + parts[1] * 60 + parts[2];
          if (!Number.isFinite(seconds) || seconds <= 0) return notice("Enter HH:MM:SS, MM:SS, or seconds.", "error");
          breakpointAction(item.id, {action:"manual", target_index:target.index,
            offset_ms:Math.round(seconds * 1000)});
          };
          targetRow.append(label, current, manual, apply);
          if (target.manual) {
            const reset = document.createElement("button"); reset.textContent = "Reset";
            reset.onclick = () => breakpointAction(item.id, {action:"reset", target_index:target.index});
            targetRow.append(reset);
          }
          actions.append(targetRow);
        }
        body.append(row);
      }
    }

    function renderSchedule(items) {
      const body = $("schedule");
      body.replaceChildren();
      $("schedule-empty").hidden = items.length > 0;
      for (const item of items) {
        const row = document.createElement("tr");
        addCell(row, new Date(item.starts_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}), "schedule-time");
        addCell(row, new Date(item.ends_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}), "schedule-time");
        addCell(row, item.daypart_slug);
        addCell(row, item.entry_type);
        const episodeTitle = item.entry_type === "episode" && item.series_title
          && !String(item.title).toLowerCase().startsWith(`${String(item.series_title).toLowerCase()} - `)
          ? `${item.series_title} - ${item.title}` : item.title;
        addCell(row, episodeTitle);
        addCell(
          row,
          item.protected_block
            ? "protected block"
            : (item.programming_owner || "base")
        );
        let rules = "";
        try {
          const explanation = JSON.parse(item.explanation_json || "{}");
          rules = (explanation.active_rules || []).map(rule => rule.rule).join(", ");
        } catch (_) {}
        addCell(row, rules || "—");
        addCell(row, item.source_relative_path || "generated continuity", "path");
        body.appendChild(row);
      }
    }

    async function loadProgrammingRules() {
      const data = await api("/api/programming-rules");
      const body = $("programming-rules");
      body.replaceChildren();
      for (const item of data.items) {
        const row = document.createElement("tr");
        addCell(row, item.slug);
        addCell(row, item.rule_type);
        addCell(row, item.channel_slug || "all eligible channels");
        const when = item.rule_type === "holiday"
          ? `${item.start_month_day} through ${item.end_month_day}`
          : `${item.weekdays} ${item.start_time}–${item.end_time}`;
        addCell(row, when);
        addCell(row, item.mode);
        addCell(row, item.priority);
        addCell(row, item.enabled ? "yes" : "no");
        addCell(row, item.ready_assets);
        body.appendChild(row);
      }
    }

    let blockItems = [];

    function formatCueTime(milliseconds) {
      if (milliseconds == null) return "";
      const total = Math.max(0, Number(milliseconds)) / 1000;
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const seconds = total % 60;
      return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${seconds.toFixed(3).padStart(6, "0")}`;
    }

    function addCueRow(cue = {}) {
      const row = document.createElement("tr");
      const inputCell = (className, value, placeholder) => {
        const cell = document.createElement("td");
        const input = document.createElement("input");
        input.className = className;
        input.value = value ?? "";
        input.placeholder = placeholder;
        cell.appendChild(input);
        row.appendChild(cell);
      };
      inputCell("cue-start", cue.offset_ms != null ? formatCueTime(cue.offset_ms) : "", "00:00:00");
      inputCell("cue-end", cue.end_offset_ms != null ? formatCueTime(cue.end_offset_ms) : "", "optional");
      const typeCell = document.createElement("td");
      const type = document.createElement("select");
      type.className = "cue-type";
      for (const value of ["program", "commercial_break", "bumper", "promo", "station_id", "other"]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value.replaceAll("_", " ");
        type.appendChild(option);
      }
      type.value = cue.cue_type || "program";
      typeCell.appendChild(type);
      row.appendChild(typeCell);
      inputCell("cue-title", cue.title, "Guide title");
      inputCell("cue-description", cue.description, "optional");
      const actionCell = document.createElement("td");
      const remove = document.createElement("button");
      remove.textContent = "Remove";
      remove.onclick = () => row.remove();
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      $("block-cue-rows").appendChild(row);
    }

    async function loadBlocks() {
      const data = await api("/api/blocks");
      blockItems = data.items;
      const select = $("block-asset");
      const selected = select.value;
      select.replaceChildren();
      for (const item of blockItems) {
        const option = document.createElement("option");
        option.value = item.asset_id;
        option.textContent = `${item.asset_id}: ${item.general_title} (${item.cue_count} cues)`;
        select.appendChild(option);
      }
      if (selected && blockItems.some(item => String(item.asset_id) === selected)) {
        select.value = selected;
      }
      if (blockItems.length) await loadSelectedBlock();
    }

    async function loadSelectedBlock() {
      const assetId = $("block-asset").value;
      if (!assetId) return;
      const item = blockItems.find(value => String(value.asset_id) === assetId);
      $("block-title").value = item.general_title;
      $("block-boundary").value = item.boundary_policy;
      $("block-probability").value = item.probability_override ?? "";
      const data = await api(`/api/blocks/${assetId}/cues`);
      $("block-cue-rows").replaceChildren();
      for (const cue of data.items) addCueRow(cue);
    }

    async function saveBlockProfile() {
      const assetId = $("block-asset").value;
      try {
        await api(`/api/blocks/${assetId}/profile`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            general_title: $("block-title").value,
            boundary_policy: $("block-boundary").value,
            probability_override: $("block-probability").value
          })
        });
        notice("Block settings saved.");
        await loadBlocks();
      } catch (error) {
        notice(error.message, "error");
      }
    }

    async function saveBlockCues() {
      const assetId = $("block-asset").value;
      try {
        const cues = Array.from($("block-cue-rows").querySelectorAll("tr")).map(row => ({
          offset: row.querySelector(".cue-start").value,
          end: row.querySelector(".cue-end").value,
          cue_type: row.querySelector(".cue-type").value,
          title: row.querySelector(".cue-title").value,
          description: row.querySelector(".cue-description").value
        }));
        const data = await api(`/api/blocks/${assetId}/cues`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({cues})
        });
        notice(`Saved ${data.cue_count} validated cues.`);
        await loadBlocks();
      } catch (error) {
        notice(error.message, "error");
      }
    }

    async function loadSchedule() {
      const params = new URLSearchParams({
        channel: $("schedule-channel").value || "fox",
        date: $("schedule-date").value
      });
      if ($("programs-only").checked) params.set("programs_only", "1");
      try {
        const data = await api(`/api/schedule?${params}`);
        renderSchedule(data.items);
      } catch (error) {
        renderSchedule([]);
        notice(error.message, "error");
      }
    }

    function updateSchedulePresentationHelp() {
      const contentOnly = $("schedule-presentation-format").value === "content_only";
      $("schedule-presentation-help").textContent = contentOnly
        ? "Plays programs back-to-back without inserted commercials, filler, bumpers, or Up Next."
        : "Uses clock-aligned blocks and may insert bumpers, commercials, filler, and Up Next.";
    }

    function updateRecurringScheduleControls(preferences) {
      const recurring = Boolean(preferences?.recurring_enabled);
      const unique = recurring && Boolean(preferences?.unique_weekends);
      const contentOnly = preferences?.presentation_format === "content_only";
      $("schedule-presentation-format").value = contentOnly ? "content_only" : "standard";
      $("schedule-recurring").checked = recurring;
      $("schedule-unique-weekends").checked = unique;
      $("schedule-recurring").disabled = contentOnly;
      $("schedule-unique-weekends").disabled = !recurring || contentOnly;
      $("schedule-unique-weekends-label").classList.toggle("muted", !recurring || contentOnly);
      updateSchedulePresentationHelp();
    }

    async function loadRecurringLineup() {
      const panel = $("recurring-lineup-overview");
      const patterns = $("recurring-lineup-patterns");
      const waiting = $("recurring-lineup-waiting");
      if (!$('schedule-recurring').checked) {
        panel.hidden = true;
        patterns.replaceChildren(); waiting.replaceChildren();
        return;
      }
      try {
        const data = await api(`/api/schedule/lineup?channel=${encodeURIComponent($('schedule-channel').value)}`);
        panel.hidden = !data.enabled;
        patterns.replaceChildren(); waiting.replaceChildren();
        const patternNames = {weekday:"Weekdays",daily:"Every day",saturday:"Saturday",sunday:"Sunday"};
        const appendPatterns = (values, prefix = "") => {
        for (const [pattern, dayparts] of Object.entries(values || {})) {
          const section = document.createElement("section");
          const heading = document.createElement("h4");
          heading.textContent = prefix + (patternNames[pattern] || pattern);
          section.append(heading);
          for (const [daypart, slots] of Object.entries(dayparts || {})) {
            const row = document.createElement("p");
            const labels = slots.map(slot => {
              const hour = Math.floor(slot.slot / 60) % 12 || 12;
              const minute = String(slot.slot % 60).padStart(2, "0");
              const meridiem = slot.slot < 720 ? "AM" : "PM";
              const fallback = slot.weekday_fallback ? " (weekday fallback)" : "";
              return `${hour}:${minute} ${meridiem} — ${slot.series || "Open slot"}${fallback}`;
            });
            row.textContent = `${daypart}: ${labels.join(" · ") || "No recurring assignments yet"}`;
            section.append(row);
          }
          patterns.append(section);
        }
        };
        if ((data.holidays || []).length) {
          const message = document.createElement('p');
          message.textContent = `Holiday programming has priority. Regular programming fills remaining slots. Normal lineup resumes ${data.normal_resumes_on || 'when the active holidays end'}.`;
          patterns.append(message);
          for (const holiday of data.holidays) {
            appendPatterns(holiday.patterns, `${holiday.slug} — `);
            if (holiday.upcoming_airings?.length) {
              const row = document.createElement('p');
              row.textContent = `Upcoming seasonal airings: ${holiday.upcoming_airings.map(item =>
                `${item.target_date} ${item.daypart} — ${item.series}`
              ).join(' · ')}`;
              patterns.append(row);
            }
            for (const [template, parts] of Object.entries(holiday.waiting || {})) {
              for (const [part, names] of Object.entries(parts)) {
                if (!names.length) continue;
                const row = document.createElement('p');
                row.textContent = `${holiday.slug} waiting (${patternNames[template] || template}, ${part}): ${names.join(' → ')}`;
                patterns.append(row);
              }
            }
            if (holiday.deferred?.length) {
              const row = document.createElement('p');
              row.textContent = `Deferred on ${holiday.last_date}: ${holiday.deferred.map(item => item.series).join(', ')}`;
              patterns.append(row);
            }
          }
          appendPatterns(data.regular_fallback_patterns, 'Regular fallback — ');
          const heading = document.createElement('h4'); heading.textContent = 'Normal lineup saved for after the holiday'; patterns.append(heading);
        }
        appendPatterns(data.patterns);
        const waitingRows = Object.entries(data.waiting || {}).filter(([, names]) => names.length);
        if (waitingRows.length) {
          const heading = document.createElement("h4"); heading.textContent = "Waiting by daypart";
          waiting.append(heading);
          for (const [daypart, names] of waitingRows) {
            const row = document.createElement("p");
            row.textContent = `${daypart}: ${names.join(" → ")}`;
            waiting.append(row);
          }
        } else {
          const row = document.createElement("p"); row.textContent = "No eligible series are waiting."; waiting.append(row);
        }
      } catch (error) {
        panel.hidden = false;
        patterns.textContent = `Could not load recurring lineup: ${error.message}`;
        waiting.replaceChildren();
      }
    }

    async function loadSchedulePreferences() {
      const channel = $("schedule-channel").value;
      if (!channel) return;
      try {
        const data = await api(`/api/schedule/preferences?channel=${encodeURIComponent(channel)}`);
        updateRecurringScheduleControls(data);
        await loadRecurringLineup();
      } catch (error) { notice(error.message, "error"); }
    }

    async function saveSchedulePreferences() {
      const channel = $("schedule-channel").value;
      if (!channel) return;
      const recurring = $("schedule-recurring").checked;
      const unique = recurring && $("schedule-unique-weekends").checked;
      const presentation = $("schedule-presentation-format").value;
      try {
        const data = await api("/api/schedule/preferences", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({channel, recurring_enabled:recurring, unique_weekends:unique, presentation_format:presentation})
        });
        updateRecurringScheduleControls(data);
        await loadRecurringLineup();
        notice(data.presentation_format === "content_only"
          ? `Content Only saved for ${channel}. It schedules real media back-to-back with no station interruptions. Existing schedules are stale; generate or run maintenance to update them.`
          : (data.recurring_enabled
          ? `Recurring schedule saved for ${channel}${data.unique_weekends ? "; weekends use their own patterns." : "."} Existing schedules are stale; generate or run maintenance to update them.`
          : `Recurring schedule disabled for ${channel}. Existing schedules are stale; generate or run maintenance to update them.`));
      } catch (error) {
        notice(error.message, "error");
        await loadSchedulePreferences();
      }
    }

    async function loadScheduleChannels() {
      const data = await api("/api/channels");
      const select = $("schedule-channel");
      const previous = select.value;
      select.replaceChildren(...data.items.map(item => {
        const option = document.createElement("option");
        option.value = item.slug;
        option.textContent = `${item.channel_number ?? "—"} · ${item.display_name}`;
        return option;
      }));
      if (data.items.some(item => item.slug === previous)) select.value = previous;
      const graphicSelect = $("graphic-channel");
      const graphicPrevious = graphicSelect.value;
      graphicSelect.replaceChildren(...data.items.map(item => {
        const option = document.createElement("option");
        option.value = item.slug;
        option.textContent = item.display_name;
        return option;
      }));
      if (data.items.some(item => item.slug === graphicPrevious)) graphicSelect.value = graphicPrevious;
    }

    async function loadWeatherGraphics() {
      const data = await api('/api/weather-graphics');
      const rows = $('weather-graphics-rows'); rows.replaceChildren();
      $('weather-graphics-status').textContent = data.ai_available ? 'AI generation available.' : 'Upload files, or add your OpenAI key in Settings to generate artwork.';
      for (const slot of data.slots) {
        const row = document.createElement('tr');
        const label = document.createElement('td'); label.textContent = slot.label;
        if (slot.overlay) label.append(document.createTextNode(` (${slot.start}–${slot.end}${slot.enabled === false ? ', disabled' : ''})`));
        const artwork = document.createElement('td');
        if (!slot.assets.length) artwork.textContent = 'Built-in fallback / no layer installed';
        for (const asset of slot.assets) {
          const link = document.createElement('a'); link.target = '_blank'; link.rel = 'noopener';
          link.href = '/api/weather-graphics/file?' + new URLSearchParams({key:slot.key, filename:asset.name});
          link.textContent = asset.name + (asset.valid ? '' : ' (invalid)'); artwork.append(link, document.createElement('br'));
        }
        const actions = document.createElement('td');
        const file = document.createElement('input'); file.type = 'file'; file.hidden = true;
        file.accept = slot.overlay ? '.png,.webp' : '.png,.jpg,.jpeg,.webp,.mp4,.mkv,.webm';
        const upload = document.createElement('button'); upload.textContent = 'Upload'; upload.onclick = () => file.click();
        const generate = document.createElement('button'); generate.textContent = slot.assets.some(a => a.name === 'ai-weather.png') ? 'Regenerate' : 'Generate'; generate.disabled = !data.ai_available;
        async function submit(action, values) {
          upload.disabled = generate.disabled = true;
          $('weather-graphics-status').textContent = action === 'generate' ? `Generating ${slot.label}… This may take a few minutes.` : `Uploading ${slot.label}…`;
          try {
            await api('/api/weather-graphics/' + action, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:slot.key,...values})});
            await loadWeatherGraphics();
            $('weather-graphics-status').textContent = 'Artwork installed. It will be used on the next Weather render.';
          } catch (error) {
            $('weather-graphics-status').textContent = error.message;
            upload.disabled = false; generate.disabled = !data.ai_available;
          }
        }
        file.onchange = async () => {
          const selected = file.files[0]; if (!selected) return;
          if (selected.size > 40*1024*1024) { $('weather-graphics-status').textContent = 'Choose a file no larger than 40 MiB.'; return; }
          try {
            const content = await new Promise((resolve,reject) => {const reader = new FileReader(); reader.onload=()=>resolve(String(reader.result).split(',')[1]); reader.onerror=()=>reject(new Error('Unable to read the selected file.')); reader.readAsDataURL(selected);});
            await submit('upload', {filename:selected.name, content_base64:content});
          } catch (error) { $('weather-graphics-status').textContent = error.message; }
        };
        generate.onclick = () => submit('generate', {art_direction:$('weather-art-direction').value});
        actions.append(upload, generate, file); row.append(label, artwork, actions); rows.append(row);
      }
    }
    $('refresh-weather-graphics').onclick = () => loadWeatherGraphics().catch(error => notice(error.message, 'error'));

    async function loadGraphics() {
      const data = await api("/api/graphics");
      const daypart = $("graphic-daypart");
      const selectedDaypart = daypart.value;
      daypart.replaceChildren(new Option("Default channel branding", ""),
        ...data.dayparts.map(value => new Option(`${value} override`, value)));
      if (data.dayparts.includes(selectedDaypart)) daypart.value = selectedDaypart;
      const todo = data.placeholder_todo || [];
      $("graphics-todo-count").textContent = todo.length;
      $("graphics-todo").replaceChildren(...todo.map(item => {
        const row = document.createElement("tr");
        [item.channel_name, item.daypart_slug || "Default", item.weekly_collection_slug || "—", item.purpose,
         item.holiday_slug || "Year-round", item.replacement_folder].forEach(value => {
          const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
        });
        const preview = document.createElement("td");
        const link = document.createElement("a"); link.href = `/api/graphics/${item.id}/preview`;
        link.target = "_blank"; link.textContent = "preview"; preview.append(link); row.append(preview);
        const action = document.createElement("td"); const replace = document.createElement("button");
        replace.textContent = "Choose replacement";
        replace.onclick = () => {
          $("graphic-channel").value = item.channel_slug;
          $("graphic-purpose").value = item.purpose;
          $("graphic-daypart").value = item.daypart_slug || "";
          $("graphic-holiday").value = item.holiday_slug || "";
          graphicWeeklyDestination = item.weekly_collection_slug || "";
          $("graphic-file").click();
        };
        const generate = document.createElement("button");
        generate.textContent = "Generate and use AI";
        generate.onclick = () => generateGraphic(item);
        action.append(replace, generate); row.append(action); return row;
      }));
      $("graphics-todo-empty").hidden = todo.length > 0;
      const body = $("graphics");
      body.replaceChildren(...data.items.map(item => {
        const row = document.createElement("tr");
        [item.channel_name, item.daypart_slug || "Default", item.weekly_collection_slug || "—", item.purpose || "All graphics", item.holiday_slug || "Year-round",
         item.source_kind, item.provenance, item.relative_path].forEach(value => {
          const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
        });
        const preview = document.createElement("td");
        const link = document.createElement("a"); link.href = `/api/graphics/${item.id}/preview`;
        link.target = "_blank"; link.textContent = "preview"; preview.append(link); row.append(preview);
        const actions = document.createElement("td");
        if (item.catalog_kind === "graphic" && item.provenance === "ai") {
          const regenerate = document.createElement("button");
          regenerate.textContent = "Regenerate";
          regenerate.onclick = () => generateGraphic(item);
          const replace = document.createElement("button");
          replace.textContent = "Replace with file";
          replace.onclick = () => chooseGraphicReplacement(item);
          const remove = document.createElement("button");
          remove.textContent = "Delete";
          remove.onclick = () => deleteAIGraphic(item);
          const variation = document.createElement("button"); variation.textContent = "Generate variation";
          variation.onclick = () => generateGraphic(item, true);
          actions.append(regenerate, variation, replace, remove);
        }
        row.append(actions);
        return row;
      }));
      $("graphics-empty").hidden = data.items.length > 0;
    }

    async function uploadGraphic() {
      const file = $("graphic-file").files[0];
      if (!file) return notice("Choose a graphic file first.", "error");
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.onerror = reject; reader.readAsDataURL(file);
      });
      try {
        await api("/api/graphics/upload", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
          channel:$("graphic-channel").value, purpose:$("graphic-purpose").value,
          daypart:$("graphic-daypart").value, holiday:$("graphic-holiday").value,
          weekly:graphicWeeklyDestination, filename:file.name, content_base64:encoded,
          replace_graphic_id:graphicReplacement?.id || ""
        })});
        notice(graphicReplacement ? `${file.name} replaced the AI graphic.` : `Added ${file.name} to the channel graphics library.`);
        graphicReplacement = null; $("graphic-file").value = ""; await loadGraphics();
      } catch (error) { notice(error.message, "error"); }
    }

    async function regeneratePlaceholder() {
      try {
        await api("/api/graphics/placeholder", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
          channel:$("graphic-channel").value, purpose:$("graphic-purpose").value,
          daypart:$("graphic-daypart").value, holiday:$("graphic-holiday").value
        })});
        notice("Placeholder regenerated."); await loadGraphics();
      } catch (error) { notice(error.message, "error"); }
    }

    function profileField(profile, name, kind="input") {
      const element = document.createElement(kind);
      element.dataset.field = name;
      if (kind === "textarea") element.value = profile[name] || "";
      else { element.type = name === "maximum_words" ? "number" : "text"; element.value = profile[name] ?? ""; }
      return element;
    }

    const breezeOptionsPromises = new Map();
    function breezeOptions(refresh=false, parameters={}) {
      if (refresh) breezeOptionsPromises.clear();
      const query = new URLSearchParams(parameters).toString();
      if (!breezeOptionsPromises.has(query)) {
        const pending = api('/api/tts/provider-options' + (query ? '?' + query : '')).catch(error => {
          if (breezeOptionsPromises.get(query) === pending) breezeOptionsPromises.delete(query);
          throw error;
        });
        breezeOptionsPromises.set(query, pending);
      }
      return breezeOptionsPromises.get(query);
    }
    async function loadAnnouncerProfiles() {
      const data = await api("/api/announcement-profiles");
      $("announcer-key-status").textContent = data.api_key_configured
        ? `OpenAI API key detected. Cloud generation is available. TLS: ${data.tls_backend}.`
        : `OpenAI API key is not configured; add it in Settings; saved profiles and music-only fallbacks still work. TLS: ${data.tls_backend}.`;
      $("announcer-key-status").textContent += data.breeze_key_configured ? ' Breeze Blue key configured.' : ' Breeze Blue key is not configured.';
      const createProfileCard = profile => {
        const card = document.createElement("section"); card.className = "announcer-profile"; card.dataset.channel = profile.channel_slug; card.dataset.daypart = profile.daypart_slug || ""; card.dataset.weekly = profile.weekly_collection_slug || "";
        const heading = document.createElement("h3");
        heading.textContent = profile.weekly_collection_slug
          ? `${profile.channel_name} · ${profile.channel_slug.toUpperCase()} weekly special`
          : (profile.daypart_slug ? `${profile.channel_name} · ${profile.daypart_slug} override` : profile.channel_name);
        card.append(heading);
        const group = title => {
          const section = document.createElement("section"); section.className = "announcer-group";
          const titleElement = document.createElement("h4"); titleElement.textContent = title;
          section.append(titleElement); card.append(section); return section;
        };
        const checkbox = (parent, labelText, name) => {
          const label = document.createElement("label"); label.className = "announcer-check";
          const input = document.createElement("input"); input.type = "checkbox"; input.dataset.field = name; input.checked = !!profile[name];
          label.append(input, labelText); parent.append(label); return input;
        };
        const field = (parent, labelText, name, kind="input") => {
          const row = document.createElement("div"); row.className = "announcer-field";
          const label = document.createElement("label"); label.textContent = labelText;
          const input = profileField(profile, name, kind);
          input.id = `announcer-${profile.channel_slug}-${profile.daypart_slug || profile.weekly_collection_slug || "default"}-${name}`;
          label.htmlFor = input.id;
          row.append(label, input); parent.append(row); return input;
        };
        if (profile.weekly_collection_slug) {
          const promos = group("Promo settings");
          checkbox(promos, "Show promos for this weekly special", "promo_enabled");
          checkbox(promos, "Allow AI narration when the announcer is eligible", "promo_narration_enabled");
          field(promos, "Narration creative direction", "promo_prompt", "textarea");
          const identity = group("Weekly announcer identity");
          checkbox(identity, "Use this weekly announcer profile instead of the channel profile", "override_enabled");
          for (const [labelText, name] of [
            ["Weekly identity name", "identity_name"],
            ["On-screen tagline", "tagline"],
            ["Spoken tagline", "spoken_tagline"]
          ]) {
            field(identity, labelText, name);
          }
        }
        const announcer = group("AI announcer");
        checkbox(announcer, "Enable dynamic AI announcer", "enabled");
        const providerRow = document.createElement('label'); providerRow.className = 'announcer-field'; providerRow.textContent = 'Speech provider ';
        const providerSelect = document.createElement('select'); providerSelect.dataset.field = 'tts_provider';
        for (const [value, label] of [['openai','OpenAI'],['breeze','Breeze Blue']]) {
          const option = new Option(label, value); option.selected = value === profile.tts_provider; providerSelect.add(option);
        }
        providerRow.append(providerSelect); announcer.append(providerRow);
        const breezeGroup = document.createElement('div');
        const modelSelect = document.createElement('select'); modelSelect.dataset.field = 'breeze_speech_model';
        const voiceSelect = document.createElement('select'); voiceSelect.dataset.field = 'breeze_voice_id';
        const optionStatus = document.createElement('p'); optionStatus.className = 'muted';
        function fillSelect(select, choices, saved) {
          select.replaceChildren(new Option('Choose…', ''));
          if (saved && !choices.some(v => v.id === saved)) select.add(new Option(saved + ' (selected)', saved));
          for (const choice of choices) select.add(new Option(choice.name, choice.id));
          select.value = saved || '';
        }
        fillSelect(modelSelect, [], profile.breeze_speech_model); fillSelect(voiceSelect, [], profile.breeze_voice_id);
        const refresh = document.createElement('button'); refresh.textContent = 'Refresh Breeze models and voices';
        const searchInput = document.createElement('input'); searchInput.type = 'search'; searchInput.placeholder = 'Search Breeze voices'; searchInput.setAttribute('aria-label', 'Search Breeze voices');
        const searchButton = document.createElement('button'); searchButton.textContent = 'Search voices';
        const moreButton = document.createElement('button'); moreButton.textContent = 'Load more voices'; moreButton.hidden = true;
        const browseControls = document.createElement('div'); browseControls.className = 'announcer-actions';
        browseControls.append(searchInput, searchButton, moreButton);
        let loadedVoices = new Map(), loadedSearch = '', nextPage = 1, nextToken = null, hasMore = false, requestId = 0;
        let seenTokens = new Set();
        async function loadOptions(force=false, append=false) {
          const currentRequest = ++requestId;
          const search = append ? loadedSearch : searchInput.value.trim();
          const page = append ? nextPage : 1;
          const parameters = {page: String(page), search};
          if (append && nextToken) parameters.next_page_token = nextToken;
          refresh.disabled = searchButton.disabled = moreButton.disabled = true;
          optionStatus.textContent = append ? 'Loading more Breeze voices…' : 'Loading Breeze models and first voice page…';
          try {
            const options = await breezeOptions(force, parameters);
            if (currentRequest !== requestId) return;
            if (append && options.next_page_token && seenTokens.has(options.next_page_token)) throw new Error('Breeze returned a repeated voice page. Refresh the list to retry.');
            if (!append) { loadedVoices = new Map(); seenTokens = new Set(); }
            for (const voice of options.voices) loadedVoices.set(voice.id, voice);
            if (options.models.length) fillSelect(modelSelect, options.models, modelSelect.value);
            fillSelect(voiceSelect, [...loadedVoices.values()].sort((a,b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id)), voiceSelect.value);
            loadedSearch = search; nextPage = page + 1; nextToken = options.next_page_token || null; hasMore = !!options.has_more;
            if (nextToken) seenTokens.add(nextToken);
            moreButton.hidden = !hasMore;
            optionStatus.textContent = !options.configured ? 'Configure a Breeze API key in Settings.'
              : `${loadedVoices.size} voices loaded.${hasMore ? ' Search for a voice or load more.' : loadedVoices.size ? '' : ' No matching voices.'}`;
            if (providerSelect.value === 'breeze') generate.disabled = testVoice.disabled = !data.breeze_key_configured || !voiceSelect.value;
          } catch (error) { if (currentRequest === requestId) optionStatus.textContent = error.message; }
          finally { if (currentRequest === requestId) refresh.disabled = searchButton.disabled = moreButton.disabled = false; }
        }
        refresh.onclick = () => loadOptions(true);
        searchButton.onclick = () => loadOptions();
        searchInput.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); loadOptions(); } };
        moreButton.onclick = () => loadOptions(false, true);
        const modelLabel = document.createElement('label'); modelLabel.className = 'announcer-field'; modelLabel.textContent = 'Breeze model '; modelLabel.append(modelSelect);
        const voiceLabel = document.createElement('label'); voiceLabel.className = 'announcer-field'; voiceLabel.textContent = 'Breeze account voice '; voiceLabel.append(voiceSelect);
        breezeGroup.append(modelLabel, voiceLabel, browseControls, refresh, optionStatus); announcer.append(breezeGroup);
        const fields = [
          ["Script model", "script_model", "input"], ["TTS model", "speech_model", "input"],
          ["Voice", "voice", "input"], ["Maximum words", "maximum_words", "input"],
          ["Station personality prompt", "personality_prompt", "textarea"],
          ["Voice delivery instructions", "voice_instructions", "textarea"],
          ["Reusable generic fallback script", "generic_script", "textarea"]
        ];
        for (const [labelText, name, kind] of fields) {
          field(announcer, labelText, name, kind);
        }
        const controls = document.createElement("div"); controls.className = "announcer-actions";
        const status = document.createElement("span"); status.textContent = profile.generic?.status || "not generated";
        const save = document.createElement("button"); save.textContent = "Save profile"; save.onclick = () => saveAnnouncerProfile(card, false);
        const generate = document.createElement("button"); generate.textContent = "Save & generate generic voice";
        generate.disabled = providerSelect.value === "breeze" ? !data.breeze_key_configured : !data.api_key_configured;
        if (generate.disabled) generate.title = "Add an OpenAI API key in Settings";
        generate.onclick = () => saveAnnouncerProfile(card, true);
        controls.append(save, generate, status); announcer.append(controls);
        const testControls = document.createElement("div"); testControls.className = "announcer-actions";
        const testText = document.createElement("button"); testText.textContent = "Test announcer text";
        const testVoice = document.createElement("button"); testVoice.textContent = "Test TTS voice";
        testText.disabled = !data.api_key_configured;
        testVoice.disabled = generate.disabled;
        if (!data.api_key_configured) {
          testText.title = testVoice.title = "OpenAI API key is not configured; add it in Settings";
        }
        const testOutput = document.createElement("div"); testOutput.className = "preview-status";
        testOutput.textContent = "Tests use the values currently shown and do not save the profile.";
        function updateProvider() {
          const breeze = providerSelect.value === 'breeze'; breezeGroup.hidden = !breeze;
          for (const name of ['speech_model','voice']) card.querySelector(`[data-field="${name}"]`).closest('.announcer-field').hidden = breeze;
          generate.disabled = testVoice.disabled = breeze ? !data.breeze_key_configured || !voiceSelect.value : !data.api_key_configured;
          generate.title = testVoice.title = generate.disabled ? 'Configure the selected provider key and voice.' : '';
          if (breeze) loadOptions();
        }
        providerSelect.onchange = updateProvider;
        voiceSelect.onchange = () => { generate.disabled = testVoice.disabled = !data.breeze_key_configured || !voiceSelect.value; };
        updateProvider();
        const testButtons = [testText, testVoice];
        testText.onclick = () => testAnnouncerProfile(card, "text", testOutput, testButtons);
        testVoice.onclick = () => testAnnouncerProfile(card, "tts", testOutput, testButtons);
        testControls.append(testText, testVoice);
        testControls.append(testOutput);
        announcer.append(testControls); return card;
      };
      const standardProfiles = data.items.filter(profile => !profile.weekly_collection_slug);
      const weeklyProfiles = data.items.filter(profile => profile.weekly_collection_slug);
      $("announcer-profiles").replaceChildren(...standardProfiles.map(createProfileCard));
      $("weekly-announcer-profiles").replaceChildren(...weeklyProfiles.map(createProfileCard));
      $("weekly-announcer-empty").hidden = weeklyProfiles.length > 0;
    }

    async function saveAnnouncerProfile(card, generate) {
      const payload = {action: generate ? "generate-generic" : "save", daypart_slug: card.dataset.daypart || null, weekly_collection_slug: card.dataset.weekly || null};
      for (const field of card.querySelectorAll("[data-field]")) {
        payload[field.dataset.field] = field.type === "checkbox" ? field.checked : field.value;
      }
      try {
        notice(generate ? "Generating the reusable station voice…" : "Saving announcer profile…");
        const result = await api(`/api/announcement-profiles/${encodeURIComponent(card.dataset.channel)}`, {
          method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
        });
        if (result.generation_error) {
          notice(`Profile saved, but generic voice generation failed: ${result.generation_error}`, "error");
        } else {
          notice(generate ? "Generic station voice generated and ready." : "Announcer profile saved.");
        }
        await loadAnnouncerProfiles();
      } catch (error) { notice(error.message, "error"); }
    }

    function graphicScope(item) {
      return item ? {
        channel:item.channel_slug, purpose:item.purpose, daypart:item.daypart_slug || "",
        holiday:item.holiday_slug || "", weekly:item.weekly_collection_slug || ""
      } : {
        channel:$("graphic-channel").value, purpose:$("graphic-purpose").value,
        daypart:$("graphic-daypart").value, holiday:$("graphic-holiday").value,
        weekly:graphicWeeklyDestination
      };
    }

    async function generateGraphic(item = null, variation = false) {
      const scope = graphicScope(item);
      if (scope.purpose === "channel-music") return notice("AI generation creates Up Next or interstitial images, not channel music.", "error");
      try {
        if (!variation) {
          const existing = await api("/api/graphics");
          const matches = existing.items.some(g => g.provenance === "ai" && g.channel_slug === scope.channel && g.purpose === scope.purpose && (g.daypart_slug || "") === scope.daypart && (g.holiday_slug || "") === scope.holiday && (g.weekly_collection_slug || "") === scope.weekly);
          if (matches && !confirm("Replace every AI graphic in this exact scope, including all variations?")) return;
        }
        notice(variation ? "Generating a separate graphic variation…" : "Generating and installing the AI graphic…");
        await api("/api/graphics/generate", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({action:variation ? "variation" : "install", graphic_id:item?.id, ...scope,
            art_note:$("graphic-art-direction").value.trim()})
        });
        notice(variation ? "Graphic variation added to the rotation." : "AI graphic installed and ready to use."); await loadGraphics();
      } catch (error) { notice(error.message, "error"); }
    }

    function chooseGraphicReplacement(item) {
      $("graphic-channel").value = item.channel_slug;
      $("graphic-purpose").value = item.purpose;
      $("graphic-daypart").value = item.daypart_slug || "";
      $("graphic-holiday").value = item.holiday_slug || "";
      graphicWeeklyDestination = item.weekly_collection_slug || "";
      graphicReplacement = item;
      $("graphic-file").value = "";
      $("graphic-file").click();
    }

    async function deleteAIGraphic(item) {
      if (!confirm("Delete this generated graphic? The placeholder will become active again unless another replacement exists.")) return;
      try {
        await api("/api/graphics/manage", {method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({action:"delete-ai", graphic_id:item.id})});
        notice("Generated graphic deleted."); await loadGraphics();
      } catch (error) { notice(error.message, "error"); }
    }

    function announcerPayload(card) {
      const payload = {daypart_slug: card.dataset.daypart || null, weekly_collection_slug: card.dataset.weekly || null};
      for (const field of card.querySelectorAll("[data-field]")) {
        payload[field.dataset.field] = field.type === "checkbox" ? field.checked : field.value;
      }
      return payload;
    }

    async function testAnnouncerProfile(card, kind, output, buttons) {
      buttons.forEach(button => button.disabled = true);
      output.replaceChildren(document.createTextNode(
        kind === "text" ? "Testing text generation…" : "Generating a short voice sample…"
      ));
      try {
        const payload = {...announcerPayload(card), kind};
        const result = await api(`/api/announcement-profiles/${encodeURIComponent(card.dataset.channel)}/test`, {
          method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
        });
        if (kind === "text") {
          const sample = document.createElement("blockquote");
          sample.textContent = result.text;
          const detail = document.createElement("div"); detail.className = "muted";
          detail.textContent = `${result.model} · ${result.elapsed_ms} ms`;
          output.replaceChildren(sample, detail);
        } else {
          const audio = document.createElement("audio"); audio.controls = true;
          audio.src = `${result.audio_url}?t=${Date.now()}`;
          const detail = document.createElement("div"); detail.className = "muted";
          detail.textContent = `${result.model} · ${result.voice} · ${result.elapsed_ms} ms`;
          output.replaceChildren(audio, detail);
          audio.play().catch(() => {});
        }
      } catch (error) {
        output.replaceChildren(document.createTextNode(`Test failed: ${error.message}`));
      } finally {
        buttons.forEach(button => button.disabled = false);
        card.querySelector('[data-field="tts_provider"]')?.dispatchEvent(new Event("change"));
      }
    }

    async function generateSchedule(reroll = false) {
      if (reroll && !window.confirm(
        `Replace the ${$("schedule-channel").selectedOptions[0]?.textContent || "selected channel"} schedule for ${$("schedule-date").value} with a different valid arrangement?` +
        ($("schedule-recurring").checked
          ? " This new arrangement will become the recurring base for later matching days."
          : "")
      )) return;
      try {
        const data = await api("/api/schedule/generate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            channel: $("schedule-channel").value || "fox",
            date: $("schedule-date").value,
            reroll
          })
        });
        const propagated = data.recurring_schedules_refreshed || 0;
        const locked = data.recurring_schedules_locked || 0;
        notice(`${data.rerolled ? "Re-rolled" : "Generated"} ${data.channel} ${data.date}: ${data.programs} programs, ${data.validation}.` +
          (propagated ? ` Refreshed ${propagated} later recurring schedule${propagated === 1 ? "" : "s"}.` : "") +
          (locked ? ` Preserved ${locked} locked schedule${locked === 1 ? "" : "s"}.` : ""));
        await loadSchedule();
        await loadProgrammingRules();
        await loadBlocks();
        await loadStats();
      } catch (error) {
        notice(error.message, "error");
      }
    }

    async function load() {
      const reviewFilter = $("review").value;
      $("aspect-review-help").hidden = !reviewFilter.startsWith("normalization:");
      const params = new URLSearchParams({
        limit: reviewFilter.startsWith("normalization:") ? "1000" : "250"
      });
      if ($("search").value) params.set("search", $("search").value);
      if (reviewFilter.startsWith("library:")) {
        params.set("review_status", reviewFilter.split(":", 2)[1]);
      } else if (reviewFilter.startsWith("normalization:")) {
        params.set("analysis_status", reviewFilter.split(":", 2)[1]);
      } else if (reviewFilter.startsWith("audio:")) {
        params.set("audio_analysis_status", reviewFilter.split(":", 2)[1]);
      } else if (reviewFilter === "commercial:pending") {
        params.set("commercial_review", "pending");
      }
      const data = await api(`/api/assets?${params}`);
      renderAssets(data.items);
      await loadStats();
    }

    initializeCollapsibleSections();
    let timer;
    $("search").addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(load, 200);
    });
    $("review").addEventListener("change", load);
    $("refresh").addEventListener("click", load);
    document.addEventListener("keydown", event => {
      if (!$("review").value.startsWith("normalization:")) return;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
      const rows = [...document.querySelectorAll(".aspect-review-row")];
      if (!rows.length) return;
      let index = rows.findIndex(row => row.classList.contains("active-review"));
      if (index < 0) index = 0;
      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault(); index = Math.min(rows.length - 1, index + 1);
        setActiveReview(rows[index]); rows[index].scrollIntoView({block:"nearest"});
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault(); index = Math.max(0, index - 1);
        setActiveReview(rows[index]); rows[index].scrollIntoView({block:"nearest"});
      } else if (event.key.toLowerCase() === "p") {
        rows[index].querySelector(".preview-correction")?.click();
      } else if (event.key.toLowerCase() === "a") {
        const button = rows[index].querySelector(".approve-correction");
        if (button && !button.disabled) button.click();
      }
    });
    $("refresh-inventory").addEventListener("click", loadInventory);
    $("refresh-bumper-review").addEventListener("click", loadBumperReview);
    $("bumper-review-filter").addEventListener("change", loadBumperReview);
    $("refresh-shadows").addEventListener("click", loadShadows);
    $("shadow-folder-create").addEventListener("click", () => {
      const folder = $("shadow-folder-source").value.trim();
      const match = shadowData && shadowData.folders.find(item => item.path === folder);
      if (!match) { notice("Choose an indexed source folder from the list.", "error"); return; }
      openShadowDialog({folder, label:`${folder} (${match.count} indexed files)`});
    });
    $("shadow-channel").addEventListener("change", updateShadowDestination);
    $("shadow-scope").addEventListener("change", updateShadowDestination);
    $("shadow-value").addEventListener("change", updateShadowDestination);
    $("shadow-cancel").addEventListener("click", () => $("shadow-dialog").close());
    $("shadow-confirm").addEventListener("click", createShadows);
    $("shadow-delete-cancel").addEventListener("click", () => $("shadow-delete-dialog").close());
    $("shadow-delete-confirm").addEventListener("click", deleteShadows);
    $("inventory-type").addEventListener("change", renderInventory);
    $("inventory-channel").addEventListener("change", renderInventory);
    $("inventory-count").addEventListener("change", renderInventory);
    let inventoryTimer;
    $("inventory-search").addEventListener("input", () => {
      clearTimeout(inventoryTimer); inventoryTimer = setTimeout(renderInventory, 150);
    });
    $("refresh-duplicates").addEventListener("click", loadDuplicates);
    $("duplicate-filter").addEventListener("change", loadDuplicates);
    $("refresh-breakpoints").addEventListener("click", loadBreakpoints);
    $("refresh-holiday-reviews").addEventListener("click", loadHolidayReviews);
    $("holiday-move-folder").addEventListener("input", updateHolidayMovePreview);
    $("holiday-move-cancel").addEventListener("click", () => $("holiday-move-dialog").close());
    $("holiday-move-confirm").addEventListener("click", () => {
      if (!selectedHolidayReview || !$("holiday-move-folder").value.trim()) {
        notice("Choose a destination folder.", "error"); return;
      }
      holidayReviewAction(selectedHolidayReview, "move", $("holiday-move-folder").value.trim());
    });
    $("breakpoint-filter").addEventListener("change", loadBreakpoints);
    const localNow = new Date();
    localNow.setMinutes(localNow.getMinutes() - localNow.getTimezoneOffset());
    $("schedule-date").value = localNow.toISOString().slice(0, 10);
    $("refresh-schedule").addEventListener("click", loadSchedule);
    $("generate-schedule").addEventListener("click", () => generateSchedule(false));
    $("reroll-schedule").addEventListener("click", () => generateSchedule(true));
    $("schedule-channel").addEventListener("change", async () => {
      await Promise.all([loadSchedule(), loadSchedulePreferences()]);
    });
    $("schedule-recurring").addEventListener("change", saveSchedulePreferences);
    $("schedule-unique-weekends").addEventListener("change", saveSchedulePreferences);
    $("schedule-presentation-format").addEventListener("change", () => {
      updateSchedulePresentationHelp();
      saveSchedulePreferences();
    });
    $("programs-only").addEventListener("change", loadSchedule);
    $("block-asset").addEventListener("change", loadSelectedBlock);
    $("add-block-cue").addEventListener("click", () => addCueRow());
    $("save-block-profile").addEventListener("click", saveBlockProfile);
    $("save-block-cues").addEventListener("click", saveBlockCues);
    $("upload-graphic").addEventListener("click", uploadGraphic);
    $("graphic-file").addEventListener("change", () => {
      if (graphicReplacement && $("graphic-file").files[0]) uploadGraphic();
    });
    $("generate-graphic").addEventListener("click", () => generateGraphic());
    $("regenerate-placeholder").addEventListener("click", regeneratePlaceholder);
    $("graphic-channel").addEventListener("change", () => { graphicWeeklyDestination = ""; });
    function updateGraphicDestinationControls() {
      const musicRoot = $("graphic-purpose").value === "channel-music";
      $("graphic-holiday").disabled = musicRoot;
      if (musicRoot) $("graphic-holiday").value = "";
      $("regenerate-placeholder").disabled = musicRoot;
    }
    $("graphic-purpose").addEventListener("change", updateGraphicDestinationControls);
    updateGraphicDestinationControls();
    async function initialize() {
      await loadScheduleChannels();
      await Promise.all([
        load(), loadHolidayReviews(), loadBumperReview(), loadInventory(), loadShadows(), loadDuplicates(), loadBreakpoints(), loadSchedule(), loadSchedulePreferences(), loadProgrammingRules(), loadBlocks(), loadGraphics(), loadWeatherGraphics(), loadAnnouncerProfiles()
      ]);
    }
    initialize().catch((error) => document.body.textContent = error);
  </script>
</body>
</html>
"""


INTERNET_MEDIA_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CRTV Sim Internet Media</title><style>
:root{color-scheme:dark;--bg:#0d1118;--panel:#19212d;--line:#334154;--text:#f3f6fa;--muted:#9eacbd;--accent:#67d6a0;--warn:#f1bd64;--bad:#ff858d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}header{padding:20px clamp(18px,4vw,48px);background:#121923;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:20px}nav{display:flex;gap:14px;flex-wrap:wrap}a{color:var(--accent)}main{padding:24px clamp(18px,4vw,48px);max-width:1300px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}h1,h2,h3,p{margin-top:0}.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.wide{grid-column:1/-1}label{display:grid;gap:5px;color:var(--muted)}input,select,button{color:var(--text);background:#0d141d;border:1px solid var(--line);border-radius:7px;padding:9px 11px;font:inherit}button{cursor:pointer}button:hover{border-color:var(--accent)}button.primary{background:#174b38;border-color:#348965}button.danger{border-color:#a34850;background:#411e24}.message{min-height:24px;color:var(--muted);white-space:pre-wrap}.bad{color:var(--bad)}.warn{color:var(--warn)}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}.scroll{overflow:auto;max-height:480px}.source{border-top:1px solid var(--line);padding-top:14px;margin-top:14px}.actions{display:flex;gap:8px;flex-wrap:wrap}.pill{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:2px 8px;color:var(--muted)}small{color:var(--muted)}dialog{max-width:620px;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:20px}dialog::backdrop{background:#000a}
</style></head><body><header><div><h1>Internet Media</h1><p>Remote collections and Watch at Your Pace channels</p></div><nav><a href="/">Dashboard</a><a href="/library">Library</a><a href="/settings">Settings</a></nav></header><main>
<div class="grid"><section class="panel"><h2>Channel setup</h2><div class="form">
<label>Channel name<input id="channel-name" placeholder="Monday Night Wars"></label><label>Channel number<input id="channel-number" type="number" min="1" max="999"></label>
<label>Playback mode<select id="channel-mode"><option value="watch_at_your_pace">Watch at Your Pace</option><option value="scheduled">Scheduled</option></select></label>
<label>Storage<select id="channel-storage"><option value="save_space">Save Space</option><option value="keep">Keep Episodes</option></select></label>
<label>Episodes ahead<input id="prepare-ahead" type="number" min="0" max="20" value="1"></label><label>Same-date show order<input id="same-date-order" placeholder="Raw, Nitro, Thunder"></label>
<div class="wide actions"><button class="primary" id="save-channel">Create or update channel</button></div></div><div id="channel-message" class="message"></div></section>
<section class="panel"><h2>Add a source</h2><div class="form"><label>Channel<select id="source-channel"></select></label><label>Show name<input id="show-title" placeholder="WWF Raw"></label><label>Daypart<select id="daypart"><option>all-day</option><option>morning</option><option>afternoon</option><option>evening</option><option>night</option></select></label><label class="wide">Internet Archive item or direct video URL<input id="source-url" type="url" placeholder="https://archive.org/details/WAR-1998"></label><div class="wide actions"><button id="preview" class="primary">Preview catalog</button><button id="add-source" disabled>Add selected episodes</button></div></div><div id="source-message" class="message"></div></section></div>
<section class="panel"><h2>Change watching position</h2><p>A new Watch at Your Pace channel will not prepare or play episodes until you choose where to begin.</p><div class="form"><label>Watch at Your Pace channel<select id="position-channel"></select></label><label>Start on or after<input id="position-date" type="date"></label><label>Initial timestamp (seconds)<input id="position-seconds" type="number" min="0" step="1" value="0"></label><div class="actions"><button id="start-beginning">Start at first episode</button><button id="start-date" class="primary">Set date position</button></div></div><div id="position-message" class="message"></div></section>
<section class="panel"><h2>Delete a channel</h2><p>This removes the channel's database records, Internet Media source files, and unfinished downloads. Finished media and backups stay on disk. A later scan will recreate the channel when its media folder remains.</p><div class="form"><label>Channel<select id="delete-channel"></select></label><div class="actions"><button id="preview-delete" class="danger">Review deletion</button></div></div><div id="delete-message" class="message"></div></section>
<dialog id="delete-dialog"><h2>Delete <span id="delete-name"></span>?</h2><p id="delete-summary"></p><p class="warn">This deletes the channel's database setup and source definitions. Finished media remains and can recreate the channel during scanning.</p><label><input id="delete-ack" type="checkbox"> I understand preserved media can make the scanner recreate this channel.</label><label>Type <strong id="delete-required"></strong><input id="delete-confirmation" autocomplete="off"></label><div class="actions"><button id="cancel-delete">Cancel</button><button id="confirm-delete" class="danger">Delete channel</button></div></dialog>
<section id="preview-panel" class="panel" hidden><h2>Catalog preview</h2><div class="actions"><label>From date<input id="preview-from" type="date"></label><label>Through date<input id="preview-through" type="date"></label><button id="select-ready">Select dated episodes in range</button><button id="clear-selection">Clear</button></div><div class="scroll"><table><thead><tr><th>Import</th><th>Date</th><th>Episode</th><th>Duration</th><th>Size</th><th>Status</th></tr></thead><tbody id="preview-rows"></tbody></table></div></section>
<section class="panel"><h2>Chronological playlists</h2><div id="playlists"></div></section>
<section class="panel"><h2>Sources and progress</h2><p id="cache"></p><div id="restore"></div><div id="sources"></div><div id="overview-message" class="message"></div></section>
<script>
const $=id=>document.getElementById(id);let state=null,preview=null,deletePreview=null,selectedSourceChannel='',selectedPositionChannel='',selectedDeleteChannel='';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options={}){const response=await fetch(url,options);let data={};try{data=await response.json()}catch{}if(!response.ok)throw new Error(data.error||`${response.status} ${response.statusText}`);return data}
const post=(url,value)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
const humanBytes=n=>{if(!n)return '—';let v=Number(n),u=['B','KiB','MiB','GiB'];let i=0;while(v>=1024&&i<u.length-1){v/=1024;i++}return `${v.toFixed(i?1:0)} ${u[i]}`};
const duration=n=>n?`${Math.floor(n/3600)}:${String(Math.floor(n%3600/60)).padStart(2,'0')}:${String(Math.floor(n%60)).padStart(2,'0')}`:'—';
function restoreSelectedOption(id,value){const select=$(id);if(value&&[...select.options].some(option=>option.value===value))select.value=value}
function captureScrollState(){return{x:window.scrollX,y:window.scrollY,lists:[...document.querySelectorAll('.scroll')].map(element=>({left:element.scrollLeft,top:element.scrollTop}))}}
function restoreScrollState(snapshot){[...document.querySelectorAll('.scroll')].forEach((element,index)=>{const saved=snapshot.lists[index];if(saved){element.scrollLeft=saved.left;element.scrollTop=saved.top}});window.scrollTo(snapshot.x,snapshot.y)}
function renderPreview(){const episodes=preview?.episodes||[];$('preview-panel').hidden=false;$('preview-rows').innerHTML=episodes.map((e,i)=>`<tr><td><input class="pick" data-key="${esc(e.stable_key)}" type="checkbox" ${e.metadata_status==='ready'?'checked':''}></td><td>${esc(e.episode_date||'Needs review')}</td><td>${esc(e.display_title)}</td><td>${duration(e.duration_seconds)}</td><td>${humanBytes(e.size_bytes)}</td><td class="${e.metadata_status==='review'?'warn':''}">${esc(e.metadata_status)}</td></tr>`).join('');$('add-source').disabled=false}
function render(){const channels=state.channels||[];$('source-channel').innerHTML=channels.map(c=>`<option value="${esc(c.slug)}">${esc(c.display_name)} — ${esc(c.playback_mode.replaceAll('_',' '))}</option>`).join('');$('position-channel').innerHTML=channels.filter(c=>c.playback_mode==='watch_at_your_pace').map(c=>`<option value="${esc(c.slug)}">${esc(c.display_name)}</option>`).join('');$('delete-channel').innerHTML=channels.map(c=>`<option value="${esc(c.slug)}">${esc(c.display_name)}</option>`).join('');const c=state.cache;$('cache').textContent=`Managed cache: ${humanBytes(c.size_bytes)} / ${humanBytes(c.limit_bytes)} · Free: ${humanBytes(c.free_bytes)}`;const r=state.restore_point;$('restore').innerHTML=`<p><span class="pill">Rollback</span> ${r.available?`Restore point created ${esc(r.created_at||'')}`:'No pre-feature restore point available'}</p>`;$('playlists').innerHTML=(state.playlists||[]).map(p=>`<article class="source"><h3>${esc(p.display_name)} <small>${p.status.position_confirmed?`${esc(p.status.state)} · saved at ${duration(p.status.position_seconds)}`:'Starting position required'}</small></h3><div class="scroll"><table><thead><tr><th>#</th><th>Date</th><th>Show</th><th>Episode</th><th>Ready</th></tr></thead><tbody>${p.episodes.map((e,i)=>`<tr><td>${i+1}</td><td>${esc(e.episode_date)}</td><td>${esc(e.show_title)}</td><td>${esc(e.display_title)}${p.status.current&&p.status.current.id===e.id?' <span class="pill">Current</span>':''}</td><td>${esc(e.preparation_state)}</td></tr>`).join('')}</tbody></table></div></article>`).join('')||'<p>No Watch at Your Pace playlists yet.</p>';$('sources').innerHTML=(state.sources||[]).map(s=>`<article class="source"><h3>${esc(s.show_title)} <small>${esc(s.channel_name)} / ${esc(s.daypart_slug)}</small></h3><p><a href="${esc(s.url)}" target="_blank" rel="noreferrer">${esc(s.url)}</a></p><div class="actions"><button onclick="refreshSource(${s.id})">Refresh</button><button onclick="disableSource(${s.id})">Disable</button></div><div class="scroll"><table><thead><tr><th>Use</th><th>Date</th><th>Tie order</th><th>Episode</th><th>Preparation</th><th>Progress</th><th></th></tr></thead><tbody>${s.episodes.map(e=>`<tr><td><input id="selected-${e.id}" type="checkbox" ${e.selected?'checked':''}></td><td><input id="date-${e.id}" type="date" value="${esc(e.episode_date||'')}"></td><td><input id="order-${e.id}" type="number" value="${Number(e.manual_order)||0}" style="width:74px"></td><td>${esc(e.display_title)}${e.metadata_status==='review'?'<br><span class="warn">Date review required</span>':''}${e.catchup_status==='review'?'<br><span class="warn">Older episode needs catch-up decision</span>':''}${e.availability_status!=='available'?`<br><span class="bad">Remote file ${esc(e.availability_status)}</span>`:''}</td><td>${esc(e.preparation_state)}${e.error_detail?`<br><span class="bad">${esc(e.error_detail)}</span>`:''}</td><td>${e.completed_at?'Watched':e.local_path?'Ready locally':'—'}</td><td><div class="actions"><button onclick="saveEpisode(${e.id})">Save</button>${e.metadata_status==='review'?`<button onclick="excludeEpisode(${e.id})">Exclude</button>`:''}<button onclick="startHere('${esc(s.channel_slug)}',${e.id})">Start here</button>${e.availability_status!=='available'?`<button onclick="repairEpisode(${e.id})">Repair</button>`:''}${e.catchup_status==='review'?`<button onclick="catchup(${e.id},'accepted')">Add catch-up</button><button onclick="catchup(${e.id},'excluded')">Exclude</button>`:''}${['failed','review','paused_space'].includes(e.preparation_state)?`<button onclick="retryJob(${e.id})">Retry</button>`:''}</div></td></tr>`).join('')}</tbody></table></div></article>`).join('')||'<p>No internet sources yet.</p>'}
async function load(){try{const nextState=await api('/api/internet-media');const scrollState=captureScrollState();state=nextState;render();restoreSelectedOption('source-channel',selectedSourceChannel);restoreSelectedOption('position-channel',selectedPositionChannel);restoreSelectedOption('delete-channel',selectedDeleteChannel);restoreScrollState(scrollState)}catch(e){$('overview-message').textContent=e.message;$('overview-message').className='message bad'}}
$('source-channel').addEventListener('change',()=>{selectedSourceChannel=$('source-channel').value});
$('position-channel').addEventListener('change',()=>{selectedPositionChannel=$('position-channel').value});
$('delete-channel').addEventListener('change',()=>{selectedDeleteChannel=$('delete-channel').value});
$('channel-mode').onchange=()=>{const scheduled=$('channel-mode').value==='scheduled';$('channel-storage').disabled=scheduled;if(scheduled)$('channel-storage').value='keep'};
$('save-channel').onclick=async()=>{try{const name=$('channel-name').value;await post('/api/internet-media/channels',{display_name:name,channel_number:$('channel-number').value||null,playback_mode:$('channel-mode').value,storage_mode:$('channel-storage').value,prepare_ahead:$('prepare-ahead').value,same_date_order:$('same-date-order').value.split(',').map(x=>x.trim()).filter(Boolean)});$('channel-message').textContent='Channel saved.';await load()}catch(e){$('channel-message').textContent=e.message;$('channel-message').className='message bad'}};
$('preview').onclick=async()=>{try{$('source-message').textContent='Reading catalog…';preview=await post('/api/internet-media/preview',{url:$('source-url').value,show_title:$('show-title').value});renderPreview();$('source-message').textContent=`Found ${preview.episodes.length} video episode(s). Review dates before importing.`}catch(e){$('source-message').textContent=e.message;$('source-message').className='message bad'}};
$('select-ready').onclick=()=>{const from=$('preview-from').value||'0000-00-00',through=$('preview-through').value||'9999-99-99';document.querySelectorAll('.pick').forEach((box,i)=>{const e=preview.episodes[i];box.checked=e.metadata_status==='ready'&&e.episode_date>=from&&e.episode_date<=through})};$('clear-selection').onclick=()=>document.querySelectorAll('.pick').forEach(box=>box.checked=false);
$('add-source').onclick=async()=>{try{const selected=[...document.querySelectorAll('.pick:checked')].map(x=>x.dataset.key);await post('/api/internet-media/sources',{channel:$('source-channel').value,show_title:$('show-title').value,url:$('source-url').value,daypart_slug:$('daypart').value,selected_keys:selected});preview=null;$('preview-panel').hidden=true;$('source-message').textContent='Source added. Watch at Your Pace channels wait for you to choose a starting position.';await load()}catch(e){$('source-message').textContent=e.message;$('source-message').className='message bad'}};
async function refreshSource(id){try{await post(`/api/internet-media/sources/${id}/refresh`,{});await load()}catch(e){alert(e.message)}}async function disableSource(id){try{await post(`/api/internet-media/sources/${id}/disable`,{});await load()}catch(e){alert(e.message)}}
async function saveEpisode(id){try{await post(`/api/internet-media/episodes/${id}`,{selected:$(`selected-${id}`).checked,episode_date:$(`date-${id}`).value||null,manual_order:Number($(`order-${id}`).value)||0});await load()}catch(e){alert(e.message)}}async function excludeEpisode(id){try{await post(`/api/internet-media/episodes/${id}`,{selected:false,excluded:true});await load()}catch(e){alert(e.message)}}async function repairEpisode(id){try{await post(`/api/internet-media/episodes/${id}/repair`,{});await load()}catch(e){alert(e.message)}}async function catchup(id,action){try{await post(`/api/internet-media/episodes/${id}`,{catchup_action:action});await load()}catch(e){alert(e.message)}}async function startHere(channel,id){let value=prompt('Resume timestamp in seconds','0');if(value===null)return;try{await post('/api/internet-media/position',{channel,episode_id:id,position_seconds:Number(value)||0});await load()}catch(e){alert(e.message)}}async function retryJob(episodeId){try{await post('/api/internet-media/retry',{episode_id:episodeId});await load()}catch(e){alert(e.message)}}
$('start-date').onclick=async()=>{try{const result=await post('/api/internet-media/position',{channel:$('position-channel').value,start_date:$('position-date').value,position_seconds:Number($('position-seconds').value)||0});$('position-message').textContent=`Position set to ${result.current?.display_title||'selected episode'}.`;await load()}catch(e){$('position-message').textContent=e.message;$('position-message').className='message bad'}};
$('start-beginning').onclick=async()=>{try{const result=await post('/api/internet-media/position',{channel:$('position-channel').value,start_at_beginning:true,position_seconds:Number($('position-seconds').value)||0});$('position-message').textContent=`Position set to ${result.current?.display_title||'first episode'}.`;await load()}catch(e){$('position-message').textContent=e.message;$('position-message').className='message bad'}};
$('preview-delete').onclick=async()=>{try{const slug=$('delete-channel').value;if(!slug)throw new Error('Choose a channel');deletePreview=await api(`/api/channels/${encodeURIComponent(slug)}/deletion-preview`);$('delete-name').textContent=deletePreview.display_name;$('delete-required').textContent=deletePreview.confirmation_text;$('delete-summary').textContent=`Records: ${deletePreview.records.assets} media, ${deletePreview.records.schedule_generations} schedules, ${deletePreview.records.internet_sources} sources, ${deletePreview.records.remote_episodes} remote episodes. ${deletePreview.source_files_to_delete} source files and ${deletePreview.unfinished_files_to_delete} unfinished files will be removed.`;$('delete-confirmation').value='';$('delete-ack').checked=false;$('delete-dialog').showModal()}catch(e){$('delete-message').textContent=e.message;$('delete-message').className='message bad'}};
$('cancel-delete').onclick=()=>$('delete-dialog').close();
$('confirm-delete').onclick=async()=>{try{const result=await post(`/api/channels/${encodeURIComponent(deletePreview.slug)}/delete`,{confirmation_text:$('delete-confirmation').value,acknowledge_rescan:$('delete-ack').checked});$('delete-dialog').close();selectedDeleteChannel='';$('delete-message').textContent=result.scanner_will_recreate?'Channel deleted. Its media folder remains, so a future scan can recreate it.':'Channel deleted. Finished media was preserved.';await load()}catch(e){$('delete-message').textContent=e.message;$('delete-message').className='message bad'}};
load();setInterval(load,5000);
</script></main></body></html>"""


SETTINGS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>CRTV Sim Settings</title>
  <style>
    :root { color-scheme:dark; --bg:#0d1118; --panel:#19212d; --line:#334154;
      --text:#f3f6fa; --muted:#aab6c5; --accent:#67d6a0; --warn:#f1bd64; --bad:#ff858d; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui,sans-serif; }
    header { padding:20px clamp(16px,4vw,48px); background:#121923; border-bottom:1px solid var(--line);
      display:flex; justify-content:space-between; align-items:center; gap:20px; }
    h1,h2,h3,p { margin-top:0; } h1 { margin-bottom:3px; }
    nav { display:flex; gap:14px; flex-wrap:wrap; } a { color:var(--accent); }
    main { max-width:1400px; margin:auto; padding:22px clamp(16px,4vw,48px) 110px; }
    .intro,.panel { background:var(--panel); border:1px solid var(--line); border-radius:11px; }
    .intro { padding:16px; margin-bottom:16px; }
    .intro p:last-child { margin-bottom:0; }
    .muted { color:var(--muted); } .restart { color:var(--warn); font-size:12px; }
    .groups { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:14px; }
    details.panel { overflow:hidden; }
    summary { cursor:pointer; padding:15px 17px; font-size:18px; font-weight:700; }
    .fields { padding:0 17px 17px; display:grid; gap:12px; }
    label.field { display:grid; grid-template-columns:minmax(150px,1fr) minmax(150px,1fr); gap:12px; align-items:center; }
    input,select,button { color:var(--text); background:#0d141d; border:1px solid var(--line);
      border-radius:7px; padding:8px 10px; font:inherit; }
    input[type=checkbox] { min-width:0; width:20px; height:20px; justify-self:start; }
    input[type=color] { padding:2px; height:39px; width:80px; }
    button { cursor:pointer; } button:hover { border-color:var(--accent); }
    button.primary { background:#174b38; border-color:#348965; }
    button.danger { border-color:#784046; color:#ffb1b6; }
    .wide { margin-top:14px; }
    .table-wrap { overflow:auto; padding:0 16px 16px; }
    table { width:100%; border-collapse:collapse; min-width:760px; }
    th,td { padding:8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }
    th { color:var(--muted); font-size:12px; text-transform:uppercase; }
    td input,td select { width:100%; min-width:85px; }
    .section-actions { padding:0 16px 16px; display:flex; gap:8px; }
    .savebar { position:fixed; left:0; right:0; bottom:0; background:#121923ee;
      border-top:1px solid var(--line); padding:14px clamp(16px,4vw,48px); display:flex;
      gap:10px; align-items:center; z-index:10; backdrop-filter:blur(8px); }
    #message { margin-left:auto; color:var(--muted); }
    .clue-row { display:grid; grid-template-columns:220px minmax(200px,1fr) auto; gap:12px; align-items:center; margin-bottom:10px; }
    .clue-row input { width:100%; min-width:0; }
    .clue-row button { grid-column:3; }
    .clue-panel { padding:0 17px 17px; }
    .clue-panel > label.field { margin:12px 0; }
    .clue-panel input { min-width:0; }
    @media(max-width:850px) { .clue-row { grid-template-columns:1fr; } .clue-row button { grid-column:1; justify-self:start; } }
    @media(max-width:650px) { label.field { grid-template-columns:1fr; gap:4px; } #message { display:none; } }
  </style>
</head>
<body>
  <header>
    <div><h1>CRTV Sim Settings</h1><p>Routine configuration without TOML editing</p></div>
    <nav><a href="/">Dashboard</a><a href="/library">Library &amp; Schedules</a><a href="/guide">Live Guide</a></nav>
  </header>
  <main>
    <section class="intro">
      <p><strong>Profile:</strong> <span id="profile">Loading...</span></p>
      <p class="muted">Settings are stored in a profile-specific override file. Your base and local TOML files are preserved. Fields marked “restart required” are applied the next time CRTV Sim starts.</p>
      <p class="muted"><strong>Settings file:</strong> <span id="override-path">—</span></p>
    </section>
    <section id="groups" class="groups"></section>

    <details class="panel wide" open>
      <summary>Channel numbers</summary>
      <div class="table-wrap"><table><thead><tr><th>Channel</th><th>Folder slug</th><th>Number</th></tr></thead><tbody id="channel-rows"></tbody></table></div>
    </details>

    <details class="panel wide" open>
      <summary>Dayparts</summary>
      <div class="table-wrap"><table><thead><tr><th>Name</th><th>Starts</th><th>Ends</th><th>Boundary rule</th><th></th></tr></thead><tbody id="daypart-rows"></tbody></table></div>
      <div class="section-actions"><button id="add-daypart">Add daypart</button></div>
    </details>

    <details class="panel wide">
      <summary>Holiday programming</summary>
      <p class="muted">On recurring channels, Boost and Replace give holiday episodes, shorts, and movies priority in a temporary recurring lineup. Each holiday series gets one placement per folder scope; regular shows fill remaining slots. Decorate leaves programming alone. Non-recurring channels retain weighted Boost and pool Replace behavior. Commercials, promos, and bumpers keep their existing holiday rules.</p>
      <div class="table-wrap"><table><thead><tr><th>Holiday</th><th>Start</th><th>End</th><th>Mode</th><th>Priority</th><th>Max/break</th><th>Cooldown sec</th><th>Weights C/B/P/E/M</th><th>Shared movie channels</th><th></th></tr></thead><tbody id="holiday-rows"></tbody></table></div>
      <div class="section-actions"><button id="add-holiday">Add holiday</button></div>
    </details>

    <details class="panel wide" open>
      <summary>Filename Clues</summary>
      <div class="clue-panel">
      <p class="muted">Comma-separated phrases. Holiday matches are suggestions only. Configured holiday names always match; optional phrases can be removed.</p>
      <div id="clue-holidays"></div>
      <button id="add-clue-group">Add holiday review group</button>
      <label class="field">Break-start clues<input id="clue-start"></label>
      <label class="field">Break-end clues<input id="clue-end"></label>
      <button id="reset-clues">Reset filename clues to defaults</button>
      <p class="muted">Save to apply to future scans and new schedules. Manual bumper choices remain in effect.</p>
      </div>
    </details>
    <details class="panel wide">
      <summary>OpenAI credential</summary>
      <div class="clue-panel">
      <p id="openai-credential-status" class="muted"></p>
      <label class="field">API key<input id="openai-key" type="password" autocomplete="new-password"></label>
      <button id="save-openai-key">Save / replace key</button><button id="remove-openai-key">Remove saved key</button>
      <p class="muted">Optional AI generation may incur provider charges. Keys stay in Windows Credential Manager; OPENAI_API_KEY overrides the saved key. Keys are never included in profile backups.</p>
      </div>
    </details>
    <details class="panel wide">
      <summary>Breeze Blue credential</summary>
      <div class="clue-panel">
      <p id="breeze-credential-status" class="muted"></p>
      <label class="field">API key<input id="breeze-key" type="password" autocomplete="new-password"></label>
      <button id="save-breeze-key">Save / replace key</button><button id="remove-breeze-key">Remove saved key</button>
      <p class="muted">Stored once in Windows Credential Manager. BREEZE_API_KEY overrides the saved key.</p>
      </div>
    </details>
    <details class="panel wide">
      <summary>Weekly programming blocks</summary>
      <p class="muted">Saving creates each collection's drag-and-drop media folders under <code>&lt;channel&gt;/specials/weekly/&lt;collection&gt;/</code>. Existing folders and media are never overwritten.</p>
      <div class="table-wrap"><table><thead><tr><th>Block</th><th>Enabled</th><th>Channel</th><th>Weekdays</th><th>Start</th><th>End</th><th>Collection</th><th>Guide label</th><th>Priority</th><th></th></tr></thead><tbody id="window-rows"></tbody></table></div>
      <div class="section-actions"><button id="add-window">Add weekly block</button></div>
    </details>
  </main>
  <div class="savebar">
    <button id="save" class="primary">Save settings</button>
    <button id="save-update">Save &amp; update schedules</button>
    <span id="message">Loading settings...</span>
  </div>
  <script>
    const $ = id => document.getElementById(id);
    let state = null;
    const esc = value => String(value ?? "").replace(/[&<>\"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
    async function request(path, options) {
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
      return data;
    }
    function scalarInput(field) {
      const id = `setting-${field.path.replaceAll('.', '-')}`;
      let input;
      if (field.type === "boolean") {
        input = `<input id="${id}" data-path="${field.path}" data-type="boolean" type="checkbox" ${field.value ? "checked" : ""}>`;
      } else if (field.type === "select") {
        input = `<select id="${id}" data-path="${field.path}" data-type="select">${field.options.map(value => `<option ${value === field.value ? "selected" : ""}>${esc(value)}</option>`).join("")}</select>`;
      } else if (field.type === "channel") {
        const choices = field.path === "application_layer.startup_channel"
          ? (state.startup_destinations || state.channels) : state.channels;
        input = `<select id="${id}" data-path="${field.path}" data-type="channel">${choices.map(channel => `<option value="${esc(channel.slug)}" ${channel.slug === field.value ? "selected" : ""}>${esc(channel.display_name)}</option>`).join("")}</select>`;
      } else {
        const type = field.type === "number" ? "number" : field.type === "color" ? "color" : field.type === "time" ? "time" : "text";
        const limits = field.type === "number" ? ` min="${field.min}" max="${field.max}" step="${field.step ?? 1}"` : "";
        input = `<input id="${id}" data-path="${field.path}" data-type="${field.type}" type="${type}" value="${esc(field.value)}"${limits}>`;
      }
      return `<label class="field"><span>${esc(field.label)}${field.restart ? '<br><span class="restart">Restart required</span>' : ""}</span>${input}</label>`;
    }
    function renderFields() {
      const groups = new Map();
      for (const field of state.fields) {
        if (!groups.has(field.group)) groups.set(field.group, []);
        groups.get(field.group).push(field);
      }
      $("groups").innerHTML = [...groups].map(([name, fields]) =>
        `<details class="panel"><summary>${esc(name)}</summary><div class="fields">${fields.map(scalarInput).join("")}</div></details>`
      ).join("");
      const preset = $("setting-normalization-resolution_preset");
      const width = $("setting-normalization-canvas_width"), height = $("setting-normalization-canvas_height");
      if (preset && width && height) {
        const sizes = {"480p":[640,480],"720p":[1280,720],"1080p":[1920,1080],"4K":[3840,2160]};
        preset.onchange = () => { if (sizes[preset.value]) [width.value,height.value] = sizes[preset.value]; };
        width.oninput = height.oninput = () => { preset.value = 'Custom'; };
        const help = document.createElement('p'); help.className = 'muted';
        help.textContent = 'Maximum dimensions: 480p 640×480; 720p 1280×720; 1080p 1920×1080; 4K 3840×2160. Larger videos are downscaled to fit; smaller videos are not upscaled unless Upscale smaller video is enabled. Aspect correction is independent. Changing settings applies to future work; it does not restore discarded detail or automatically reconvert the library. Proxy keeps source video in place; audio normalization and commercial trimming still replace their working files with verified backups.';
        preset.closest('.fields').prepend(help);
      }
    }
    function phrases(value) { return value.split(',').map(v => v.trim()).filter(Boolean); }
    function renderClues() {
      $("clue-holidays").replaceChildren();
      for (const [slug, list] of Object.entries(state.filename_clues.holidays)) addClueRow(slug, list);
      $("clue-start").value = state.filename_clues.bumpers.break_start.join(', ');
      $("clue-end").value = state.filename_clues.bumpers.break_end.join(', ');
    }
    function addClueRow(slug, list) {
      const row = document.createElement('div'); row.className = 'clue-row'; row.dataset.clueGroup = slug;
      const label = document.createElement('span'); label.textContent = slug;
      const input = document.createElement('input'); input.value = list.join(', ');
      const remove = document.createElement('button'); remove.textContent = 'Remove optional clues';
      remove.onclick = () => { input.value = ''; };
      if (!(state.filename_clues.mandatory_holidays || []).includes(slug) && !Object.hasOwn(state.filename_clues.defaults.holidays, slug)) {
        const deleteGroup = document.createElement('button'); deleteGroup.textContent = 'Remove group'; deleteGroup.onclick = () => row.remove(); row.append(deleteGroup);
      }
      if ((state.filename_clues.mandatory_holidays || []).includes(slug)) label.textContent += ' (name always matches)';
      row.append(label, input, remove); $("clue-holidays").append(row);
    }
    function syncHolidayClues() {
      for (const row of rows('holiday-rows')) {
        const slug = row.querySelector('.slug').value.trim().toLowerCase();
        if (slug && ![...$("clue-holidays").children].some(r => r.dataset.clueGroup === slug)) addClueRow(slug, [slug.replaceAll('-', ' ')]);
        const group = [...$("clue-holidays").children].find(r => r.dataset.clueGroup === slug);
        if (group) group.querySelector('span').textContent = slug + ' (name always matches)';
      }
    }
    $("holiday-rows").addEventListener('input', syncHolidayClues);
    $("add-clue-group").onclick = () => {
      const slug = prompt('Holiday review group slug (for example summer):')?.trim().toLowerCase();
      if (!slug) return;
      if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(slug)) return alert('Use a lowercase slug with hyphens.');
      if ([...$("clue-holidays").children].some(r => r.dataset.clueGroup === slug)) return alert('This group already exists.');
      addClueRow(slug, [slug.replaceAll('-', ' ')]);
    };
    $("reset-clues").onclick = () => {
      if (!confirm('Replace all customized filename clue lists with defaults? Configured holiday groups keep their names. Additional custom-only groups are removed. Holiday definitions and media are unchanged. Save settings to apply.')) return;
      state.filename_clues.holidays = structuredClone(state.filename_clues.defaults.holidays);
      for (const slug of state.filename_clues.mandatory_holidays) {
        if (!rows('holiday-rows').some(row => row.querySelector('.slug').value.trim().toLowerCase() === slug) && !Object.hasOwn(state.filename_clues.defaults.holidays, slug)) delete state.filename_clues.holidays[slug];
      }
      state.filename_clues.bumpers = structuredClone(state.filename_clues.defaults.bumpers);
      renderClues(); syncHolidayClues();
    };
    async function breezeCredential(action) {
      try {
        const result = await request('/api/credentials/breeze', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,key:$("breeze-key").value})});
        $("breeze-key").value = ''; $("breeze-credential-status").textContent = result.configured ? 'Breeze key configured (an environment override may remain active).' : 'Breeze key is not configured.';
      } catch (error) { $("message").textContent = error.message; }
    }
    $("save-breeze-key").onclick = () => breezeCredential('save');
    async function openaiCredential(action) {
      try {
        const result = await request('/api/credentials/openai', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,key:$("openai-key").value})});
        $("openai-key").value = ''; $("openai-credential-status").textContent = result.configured ? 'OpenAI key configured (an environment override may remain active).' : 'OpenAI key is not configured.';
      } catch (error) { $("message").textContent = error.message; }
    }
    $("save-openai-key").onclick = () => openaiCredential('save');
    $("remove-openai-key").onclick = () => { if (confirm('Remove the saved OpenAI key? An environment override will remain active.')) openaiCredential('remove'); };
    $("remove-breeze-key").onclick = () => { if (confirm('Remove the saved Breeze key? An environment override will remain active.')) breezeCredential('remove'); };
    function renderChannels() {
      $("channel-rows").innerHTML = state.channels.map(channel => `<tr data-slug="${esc(channel.slug)}"><td>${esc(channel.display_name)}</td><td>${esc(channel.slug)}</td><td><input class="channel-number" type="number" min="1" max="999" value="${esc(state.channel_numbers[channel.slug] ?? channel.channel_number ?? '')}"></td></tr>`).join("");
    }
    function removeButton(kind) { return `<button class="danger remove-${kind}" type="button">Remove</button>`; }
    function renderDayparts() {
      $("daypart-rows").innerHTML = state.dayparts.map(item => `<tr><td><input class="slug" value="${esc(item.slug)}"></td><td><input class="start" type="time" value="${esc(item.start)}"></td><td><input class="end" type="time" value="${esc(item.end)}"></td><td><select class="policy"><option ${item.boundary_policy === 'must_fit' ? 'selected' : ''}>must_fit</option><option ${item.boundary_policy === 'may_overrun' ? 'selected' : ''}>may_overrun</option></select></td><td>${removeButton('daypart')}</td></tr>`).join("");
    }
    function weightText(item) { const w=item.weights||{}; return ["commercial","bumper","promo","episode","movie"].map(k => w[k] ?? 1).join(","); }
    function renderHolidays() {
      $("holiday-rows").innerHTML = Object.entries(state.holidays).map(([slug,item]) => `<tr><td><input class="slug" value="${esc(slug)}"></td><td><input class="start" value="${esc(item.start)}" placeholder="MM-DD"></td><td><input class="end" value="${esc(item.end)}" placeholder="MM-DD"></td><td><select class="mode">${["decorate","boost","replace"].map(v=>`<option ${v===item.mode?'selected':''}>${v}</option>`).join('')}</select></td><td><input class="priority" type="number" value="${esc(item.priority ?? 200)}"></td><td><input class="maximum" type="number" min="0" value="${esc(item.maximum_per_break ?? 2)}"></td><td><input class="cooldown" type="number" min="0" value="${esc(item.cooldown_seconds ?? 3600)}"></td><td><input class="weights" value="${esc(weightText(item))}"></td><td><input class="movie-channels" value="${esc((item.shared_movie_channels||[]).join(','))}"></td><td>${removeButton('holiday')}</td></tr>`).join("");
    }
    function channelOptions(selected) { return state.channels.filter(c => !c.synthetic).map(c => `<option value="${esc(c.slug)}" ${c.slug===selected?'selected':''}>${esc(c.display_name)}</option>`).join(''); }
    function renderWindows() {
      $("window-rows").innerHTML = Object.entries(state.windows).map(([slug,item]) => `<tr><td><input class="slug" value="${esc(slug)}"></td><td><input class="enabled" type="checkbox" ${item.enabled?'checked':''}></td><td><select class="channel">${channelOptions(item.channel)}</select></td><td><input class="weekdays" value="${esc((item.weekdays||[]).join(','))}"></td><td><input class="start" type="time" value="${esc(item.start)}"></td><td><input class="end" type="time" value="${esc(item.end)}"></td><td><input class="collection" value="${esc(item.collection)}"></td><td><input class="label" value="${esc(item.guide_label)}"></td><td><input class="priority" type="number" value="${esc(item.priority ?? 300)}"></td><td>${removeButton('window')}</td></tr>`).join("");
    }
    function wireRemoveButtons() {
      for (const kind of ["daypart","holiday","window"]) document.querySelectorAll(`.remove-${kind}`).forEach(button => button.onclick = () => button.closest('tr').remove());
    }
    function render() {
      $("profile").textContent = `${state.profile} (${state.profile_path})`;
      $("override-path").textContent = state.override_path;
      renderFields(); renderChannels(); renderDayparts(); renderHolidays(); renderWindows(); renderClues(); wireRemoveButtons();
      $("breeze-credential-status").textContent = state.breeze_key_configured ? 'Breeze key configured.' : 'Breeze key is not configured.';
      $("openai-credential-status").textContent = state.openai_key_configured ? 'OpenAI key configured.' : 'OpenAI key is not configured.';
    }
    function rows(id) { return [...$(id).querySelectorAll('tr')]; }
    function collect() {
      const values = {};
      document.querySelectorAll('[data-path]').forEach(input => {
        values[input.dataset.path] = input.dataset.type === 'boolean' ? input.checked : input.dataset.type === 'number' ? input.valueAsNumber : input.value;
      });
      const channel_numbers = {};
      rows('channel-rows').forEach(row => channel_numbers[row.dataset.slug] = Number(row.querySelector('.channel-number').value));
      const dayparts = rows('daypart-rows').map(row => ({slug:row.querySelector('.slug').value,start:row.querySelector('.start').value,end:row.querySelector('.end').value,boundary_policy:row.querySelector('.policy').value}));
      const holidays = {};
      rows('holiday-rows').forEach(row => { const keys=["commercial","bumper","promo","episode","movie"], parts=row.querySelector('.weights').value.split(',').map(Number); holidays[row.querySelector('.slug').value]={start:row.querySelector('.start').value,end:row.querySelector('.end').value,mode:row.querySelector('.mode').value,priority:Number(row.querySelector('.priority').value),maximum_per_break:Number(row.querySelector('.maximum').value),cooldown_seconds:Number(row.querySelector('.cooldown').value),shared_movie_channels:row.querySelector('.movie-channels').value.split(',').map(v=>v.trim()).filter(Boolean),weights:Object.fromEntries(keys.map((key,index)=>[key,parts[index] ?? 1]))}; });
      const windows = {};
      rows('window-rows').forEach(row => { windows[row.querySelector('.slug').value]={enabled:row.querySelector('.enabled').checked,channel:row.querySelector('.channel').value,weekdays:row.querySelector('.weekdays').value.split(',').map(v=>v.trim().toLowerCase()).filter(Boolean),start:row.querySelector('.start').value,end:row.querySelector('.end').value,collection:row.querySelector('.collection').value,guide_label:row.querySelector('.label').value,priority:Number(row.querySelector('.priority').value)}; });
      syncHolidayClues();
      const clueGroups = Object.fromEntries([...$("clue-holidays").children].map(row => [row.dataset.clueGroup, phrases(row.querySelector('input').value)]));
      return {values, channel_numbers, dayparts, holidays, windows, filename_clues:{holidays:clueGroups,bumpers:{break_start:phrases($("clue-start").value),break_end:phrases($("clue-end").value)}}};
    }
    async function save(update=false) {
      try {
        $("message").textContent = "Saving...";
        const result = await request('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())});
        $("message").textContent = result.message;
        if (update) {
          const maintenance = await request('/api/runtime/maintenance', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
          $("message").textContent = `${result.message} ${maintenance.message || 'Schedule update requested.'}`;
        }
      } catch (error) { $("message").textContent = error.message; }
    }
    $("add-daypart").onclick = () => { $("daypart-rows").insertAdjacentHTML('beforeend', `<tr><td><input class="slug" value="new-daypart"></td><td><input class="start" type="time" value="00:00"></td><td><input class="end" type="time" value="00:00"></td><td><select class="policy"><option>must_fit</option><option>may_overrun</option></select></td><td>${removeButton('daypart')}</td></tr>`); wireRemoveButtons(); };
    $("add-holiday").onclick = () => { $("holiday-rows").insertAdjacentHTML('beforeend', `<tr><td><input class="slug" value="new-holiday"></td><td><input class="start" value="01-01"></td><td><input class="end" value="01-01"></td><td><select class="mode"><option>decorate</option><option selected>boost</option><option>replace</option></select></td><td><input class="priority" type="number" value="200"></td><td><input class="maximum" type="number" value="2"></td><td><input class="cooldown" type="number" value="3600"></td><td><input class="weights" value="1,1,1,1,1"></td><td><input class="movie-channels"></td><td>${removeButton('holiday')}</td></tr>`); wireRemoveButtons(); syncHolidayClues(); };
    $("add-window").onclick = () => { $("window-rows").insertAdjacentHTML('beforeend', `<tr><td><input class="slug" value="new-block"></td><td><input class="enabled" type="checkbox"></td><td><select class="channel">${channelOptions(state.channels[0]?.slug)}</select></td><td><input class="weekdays" value="saturday"></td><td><input class="start" type="time" value="20:00"></td><td><input class="end" type="time" value="22:00"></td><td><input class="collection" value="new-block"></td><td><input class="label" value="New Block"></td><td><input class="priority" type="number" value="300"></td><td>${removeButton('window')}</td></tr>`); wireRemoveButtons(); };
    $("save").onclick = () => save(false); $("save-update").onclick = () => save(true);
    request('/api/settings').then(data => { state=data; render(); $("message").textContent='Ready.'; }).catch(error => $("message").textContent=error.message);
  </script>
</body>
</html>
"""


GUIDE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CRTV Sim Guide</title>
  <style>
    :root { color-scheme: dark; font-family: Arial, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #020814; }
    #guide {
      width: min(100vw, calc(100vh * 4 / 3)); aspect-ratio: 4 / 3;
      background: linear-gradient(#07172f, #102b55); color: white;
      padding: 4%; overflow: hidden; display: grid;
      grid-template-rows: 13% 11% 52% 18% 6%;
    }
    header { display: flex; align-items: center; }
    h1 { margin: 0; font-size: clamp(16px, 4vw, 28px); }
    .head, .row { display: grid; grid-template-columns: 23% repeat(3, 1fr); }
    .head > div { color: #ffe66d; font-weight: bold; padding: 7px; }
    .row > div { border: 1px solid #5278a5; padding: 8px; min-width: 0; overflow: hidden; }
    .channel { font-weight: bold; background: #173e70; }
    .cell {
      background: #0c2447; font-size: clamp(11px, 2.1vw, 16px);
      display: flex; flex-direction: column; justify-content: center;
      line-height: 1.12;
    }
    .series-name { font-size: 1.08em; font-weight: bold; }
    .file-name { margin-top: 2px; font-size: .78em; color: #d7e5f5; }
    #rows { display: grid; grid-template-rows: repeat(4, 1fr); }
    #description { margin-top: 8px; border: 2px solid #638ab7; padding: 10px; font-size: clamp(12px, 2.4vw, 18px); }
    footer { display: flex; justify-content: space-between; align-items: end; color: #a9c2dc; }
  </style>
</head>
<body>
  <main id="guide">
    <header><h1>RETRO TV GUIDE <span id="guide-number"></span></h1></header>
    <div class="head"><div>CHANNEL</div><div>NOW</div><div id="plus30">+30</div><div id="plus60">+60</div></div>
    <section id="rows"></section>
    <section id="description">Loading guide…</section>
    <footer><span>NOW / NEXT</span><span id="page"></span></footer>
  </main>
  <script>
    let data = null;
    let page = 0;
    const perPage = 4;
    const title = listing => listing ? listing.title : "TBD";
    function render() {
      if (!data) return;
      const channels = data.channels.slice(page * perPage, (page + 1) * perPage);
      const rows = document.getElementById("rows");
      rows.replaceChildren();
      for (const channel of channels) {
        const row = document.createElement("div");
        row.className = "row";
        const channelCell = document.createElement("div");
        channelCell.className = "channel";
        channelCell.textContent = `${channel.channel_number ?? "—"}  ${channel.display_name}`;
        row.appendChild(channelCell);
        for (let columnIndex = 0; columnIndex < channel.columns.length;) {
          const listing = channel.columns[columnIndex];
          let span = 1;
          while (columnIndex + span < channel.columns.length) {
            const following = channel.columns[columnIndex + span];
            const sameContinuousListing = listing && following
              && listing.entry_id === following.entry_id
              && listing.starts_at === following.starts_at
              && listing.ends_at === following.ends_at
              && listing.title === following.title;
            if (!sameContinuousListing) break;
            span += 1;
          }
          const cell = document.createElement("div");
          cell.className = "cell";
          if (span > 1) cell.style.gridColumn = `span ${span}`;
          if (listing?.series_title && listing?.file_title) {
            const series = document.createElement("div");
            series.className = "series-name";
            series.textContent = listing.series_title;
            const file = document.createElement("div");
            file.className = "file-name";
            file.textContent = listing.file_title;
            cell.append(series, file);
          } else {
            cell.textContent = title(listing);
          }
          row.appendChild(cell);
          columnIndex += span;
        }
        rows.appendChild(row);
      }
      while (rows.children.length < perPage) {
        const row = document.createElement("div"); row.className = "row";
        for (let i = 0; i < 4; i++) row.appendChild(document.createElement("div"));
        rows.appendChild(row);
      }
      const selected = channels[0];
      document.getElementById("description").textContent = selected
        ? `NOW: ${title(selected.now)}   •   NEXT: ${title(selected.next)}`
        : "No active channel schedules for this time.";
      document.getElementById("guide-number").textContent = `CH ${data.guide_channel_number}`;
      const labels = data.column_times.map((value, index) =>
        index === 0
          ? "NOW"
          : new Date(value).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"})
      );
      document.getElementById("plus30").textContent = labels[1] ?? "";
      document.getElementById("plus60").textContent = labels[2] ?? "";
      const pages = Math.max(1, Math.ceil(data.channels.length / perPage));
      document.getElementById("page").textContent = `${page + 1}/${pages}`;
    }
    async function load() {
      const requestedAt = new URLSearchParams(location.search).get("at");
      const response = await fetch(
        requestedAt ? `/api/guide?at=${encodeURIComponent(requestedAt)}` : "/api/guide"
      );
      data = await response.json();
      const pages = Math.max(1, Math.ceil(data.channels.length / perPage));
      page %= pages;
      render();
    }
    load();
    setInterval(() => {
      if (!data) return;
      page = (page + 1) % Math.max(1, Math.ceil(data.channels.length / perPage));
      render();
    }, 10000);
    setInterval(load, 15000);
  </script>
</body>
</html>
"""


def _asset_query(params: dict[str, list[str]]) -> tuple[str, list[object]]:
    where: list[str] = []
    values: list[object] = []
    for key in ("review_status", "channel_slug", "daypart_slug", "media_type"):
        value = params.get(key, [""])[0].strip()
        if value:
            where.append(f"a.{key} = ?")
            values.append(value)
    analysis_status = params.get("analysis_status", [""])[0].strip()
    if analysis_status:
        where.append("a.integrity_status IN ('ready', 'not_scanned')")
        where.append("n.analysis_status = ?")
        values.append(analysis_status)
    audio_analysis_status = params.get("audio_analysis_status", [""])[0].strip()
    if audio_analysis_status:
        if not analysis_status:
            where.append("a.integrity_status IN ('ready', 'not_scanned')")
        where.append("an.analysis_status = ?")
        values.append(audio_analysis_status)
    if params.get("commercial_review", [""])[0] == "pending":
        where.append("cdr.decision = 'pending'")
    search = params.get("search", [""])[0].strip()
    if search:
        id_search = search.casefold()
        if id_search.startswith("id:"):
            id_search = id_search[3:].strip()
        elif id_search.startswith("#"):
            id_search = id_search[1:].strip()
        if id_search.isdigit():
            where.append("a.id = ?")
            values.append(int(id_search))
        else:
            where.append(
                """
                (a.display_title LIKE ? OR a.original_filename LIKE ?
                 OR a.source_relative_path LIKE ?)
                """
            )
            pattern = f"%{search}%"
            values.extend((pattern, pattern, pattern))
    clause = " WHERE " + " AND ".join(where) if where else ""
    return clause, values


def _unmatched_named_bumpers(connection: sqlite3.Connection, config=None) -> list[dict[str, object]]:
    return [
        {"id": item["id"], "original_filename": item["original_filename"],
         "source_relative_path": item["source_relative_path"],
         "series_hint": item["automatic_series_hint"],
         "position": item["automatic_position"]}
        for item in bumper_review_catalog(connection, config)["items"]
        if item["review_required"]
    ]


def forget_missing_asset_record(
    connection: sqlite3.Connection,
    config: AppConfig,
    asset_id: int,
) -> int:
    """Remove one missing, ignored library record without deleting media."""
    from retrotv.asset_paths import require_managed_asset
    require_managed_asset(connection, config, asset_id)
    row = connection.execute(
        """
        SELECT id, source_relative_path, integrity_status, review_status
        FROM assets WHERE id=?
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Asset not found")
    if row["integrity_status"] != "missing" or row["review_status"] != "ignored":
        raise ValueError("Only missing Library Ignored records can be forgotten")

    media_root = config.storage_root("media").resolve()
    source = (media_root / Path(row["source_relative_path"])).resolve()
    try:
        source.relative_to(media_root)
    except ValueError as exc:
        raise ValueError("Asset path is outside the configured media root") from exc
    if source.is_file():
        raise ValueError("The media file exists again; scan it instead of forgetting its record")

    detached = connection.execute(
        """
        UPDATE schedule_entries
        SET asset_id=NULL, media_start_ms=NULL, media_end_ms=NULL
        WHERE asset_id=?
        """,
        (asset_id,),
    ).rowcount
    # This early promotion table predates cascading asset foreign keys.
    connection.execute("DELETE FROM normalization_promotions WHERE asset_id=?", (asset_id,))
    connection.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    return max(0, int(detached))


def _media_inventory(connection: sqlite3.Connection) -> dict[str, object]:
    rows = connection.execute(
        """
        SELECT a.id AS asset_id,a.original_filename,a.media_type,a.display_title,
               a.series_title,a.channel_slug,a.daypart_slug,a.collection_type,
               a.collection_slug,a.holiday_weekly_slug,a.review_status,a.enabled_for_scheduling,
               a.source_relative_path AS placement_path,0 AS is_shadow,
               NULL AS shadow_id,m.duration_seconds
        FROM assets a LEFT JOIN asset_media_info m ON m.asset_id=a.id
        WHERE a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') AND a.integrity_status IN ('ready','not_scanned') AND a.media_type IS NOT NULL
        UNION ALL
        SELECT a.id AS asset_id,a.original_filename,a.media_type,a.display_title,
               COALESCE(s.series_title,a.series_title),s.channel_slug,s.daypart_slug,
               s.collection_type,s.collection_slug,s.holiday_weekly_slug,a.review_status,
               a.enabled_for_scheduling,s.shadow_relative_path,1 AS is_shadow,
               s.id AS shadow_id,m.duration_seconds
        FROM shadow_files s JOIN assets a ON a.id=s.target_asset_id
        LEFT JOIN asset_media_info m ON m.asset_id=a.id
        WHERE s.status='ready' AND a.integrity_status IN ('ready','not_scanned')
        """
    ).fetchall()
    rows = list(rows) + list(connection.execute("""
        SELECT a.id AS asset_id,a.original_filename,a.media_type,a.display_title,
               a.series_title,x.channel_slug,x.scope AS daypart_slug,NULL AS collection_type,
               NULL AS collection_slug,NULL AS holiday_weekly_slug,a.review_status,
               CASE WHEN r.enabled=1 AND es.availability='available' THEN a.enabled_for_scheduling ELSE 0 END AS enabled_for_scheduling,
               a.source_relative_path AS placement_path,0 AS is_shadow,NULL AS shadow_id,
               m.duration_seconds,r.absolute_path,'external' AS source_kind
        FROM assets a JOIN external_assignments x ON x.root_id=a.source_root_id AND x.enabled=1
        JOIN storage_roots r ON r.id=a.source_root_id JOIN external_sources es ON es.root_id=r.id
        LEFT JOIN asset_media_info m ON m.asset_id=a.id
        WHERE a.integrity_status IN ('ready','not_scanned')
        """).fetchall())
    groups: dict[tuple[object, ...], dict[str, object]] = {}
    for raw in rows:
        row = dict(raw)
        media_type = str(row["media_type"])
        if media_type in {"episode", "short"}:
            title = row["series_title"] or row["display_title"] or row["original_filename"]
        else:
            title = {
                "commercial": "Commercials", "bumper": "Bumpers",
                "promo": "Promos", "station_id": "Station IDs", "filler": "Filler",
            }.get(media_type, row["display_title"] or row["original_filename"])
        channel = row["channel_slug"] or "shared"
        location = (
            f"holiday: {row['collection_slug']} / weekly: {row['holiday_weekly_slug']}"
            if row["collection_type"] == "holiday" and row["holiday_weekly_slug"]
            else f"holiday: {row['collection_slug']} / {row['daypart_slug']}"
            if row["collection_type"] == "holiday" and row["daypart_slug"]
            else f"holiday: {row['collection_slug']} / ordinary all-day (legacy)"
            if row["collection_type"] == "holiday"
            else f"{row['collection_type']}: {row['collection_slug'] or 'unspecified'}"
            if row["collection_type"] else (row["daypart_slug"] or "all-day")
        )
        key = (
            str(title), media_type, str(channel), str(location),
            row["collection_type"], row["collection_slug"], row["daypart_slug"],
            row["holiday_weekly_slug"],
            bool(row["is_shadow"]),
        )
        group = groups.setdefault(key, {
            "title": str(title), "media_type": media_type,
            "channel_slug": str(channel), "location": str(location),
            "collection_type": row["collection_type"],
            "collection_slug": row["collection_slug"],
            "daypart_slug": row["daypart_slug"], "asset_ids": set(),
            "ready_ids": set(), "review_ids": set(), "durations": {},
            "paths": [], "shadow_ids": set(), "placement_count": 0,
            "row_kind": "external" if row.get("source_kind") == "external" else "shadow" if bool(row["is_shadow"]) else "native",
            "physical_root": row.get("absolute_path"),
        })
        asset_id = int(row["asset_id"])
        group["asset_ids"].add(asset_id)
        if row["review_status"] == "ready" and bool(row["enabled_for_scheduling"]):
            group["ready_ids"].add(asset_id)
        if row["review_status"] == "open":
            group["review_ids"].add(asset_id)
        group["durations"][asset_id] = float(row["duration_seconds"] or 0)
        group["paths"].append(str(row["placement_path"]))
        if row["shadow_id"] is not None:
            group["shadow_ids"].add(int(row["shadow_id"]))
        group["placement_count"] += 1

    order = {
        "episode": 1, "short": 2, "movie": 3, "complete_block": 4,
        "bumper": 5, "commercial": 6, "promo": 7, "station_id": 8,
    }
    # Recurring-lineup coverage is intentionally a warning, not an automatic
    # removal or a scheduling block. It lets the owner see that a ready series
    # has no place in the currently generated future schedule.
    recurring_channels: set[str] = set()
    scheduled_counts: dict[tuple[str, str, str], int] = {}
    scheduled_any_daypart: dict[tuple[str, str], int] = {}
    recurring_slot_assignments: dict[tuple[str, str], set[str]] = {}
    seasonal_upcoming: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    try:
        preference_rows = connection.execute(
            """
            SELECT c.slug
            FROM channel_schedule_preferences p
            JOIN channels c ON c.id=p.channel_id
            WHERE p.recurring_enabled=1
            """
        ).fetchall()
        recurring_channels = {str(row["slug"]) for row in preference_rows}
        scheduled_rows = connection.execute(
            """
            SELECT c.slug AS channel_slug,se.daypart_slug,
                   COALESCE(a.series_title,a.display_title,a.original_filename)
                       AS series_key,
                   COUNT(*) AS appearances
            FROM schedule_entries se
            JOIN schedule_generations sg ON sg.id=se.generation_id
            JOIN channels c ON c.id=se.channel_id
            JOIN assets a ON a.id=se.asset_id
            WHERE sg.status='active'
              AND sg.schedule_date >= ?
              AND se.is_program=1
              AND COALESCE(se.programming_owner,'base')='base'
            GROUP BY c.slug,se.daypart_slug,series_key
            """,
            (date.today().isoformat(),),
        ).fetchall()
        for row in scheduled_rows:
            channel = str(row["channel_slug"])
            series = str(row["series_key"])
            count = int(row["appearances"])
            scheduled_counts[(channel, str(row["daypart_slug"]), series)] = count
            scheduled_any_daypart[(channel, series)] = (
                scheduled_any_daypart.get((channel, series), 0) + count
            )
        snapshot_rows = connection.execute(
            """SELECT c.slug AS channel_slug,r.state_json
               FROM recurring_lineup_snapshots r
               JOIN schedule_generations sg ON sg.id=r.generation_id
               JOIN channels c ON c.id=r.channel_id
               WHERE sg.status='active' AND sg.schedule_date>=?
               ORDER BY sg.schedule_date""",
            (date.today().isoformat(),),
        ).fetchall()
        for snapshot in snapshot_rows:
            channel = str(snapshot["channel_slug"])
            try:
                state = json.loads(str(snapshot["state_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for key, assignments in state.get("slots", {}).items():
                if not isinstance(assignments, dict) or ":" not in str(key):
                    continue
                daypart = str(key).split(":", 1)[1]
                for series in assignments.values():
                    if series:
                        recurring_slot_assignments.setdefault((channel, daypart), set()).add(str(series))
            for holiday in state.get("holiday_lineups", {}).values():
                for series, entries in holiday.get("calendar", {}).items():
                    for entry in entries:
                        target_date = str(entry.get("target_date", ""))
                        if target_date < date.today().isoformat():
                            continue
                        key = (channel, str(series), str(entry.get("daypart", "")))
                        seasonal_upcoming.setdefault(key, []).append({
                            "target_date": target_date,
                            "holiday": str(holiday.get("slug", "holiday")),
                            "series_folder_name": entry.get("series_folder_name"),
                        })
    except sqlite3.OperationalError:
        # Small test/repair databases can legitimately predate scheduling.
        pass

    items = []
    for group in groups.values():
        parent_paths = [posixpath.dirname(path.replace("\\", "/")) for path in group.pop("paths")]
        item = {
            key: value for key, value in group.items()
            if key not in {"asset_ids", "ready_ids", "review_ids", "durations", "shadow_ids"}
        }
        item.update({
            "item_count": len(group["asset_ids"]),
            "preview_asset_id": min(group["asset_ids"]),
            "ready_count": len(group["ready_ids"]),
            "review_count": len(group["review_ids"]),
            "shadow_count": len(group["shadow_ids"]),
            "shadow_ids": sorted(group["shadow_ids"]),
            "duration_seconds": round(sum(group["durations"].values()), 1),
            "folder_relative_path": posixpath.commonpath(parent_paths) if parent_paths else "",
        })
        channel = str(item["channel_slug"])
        title = str(item["title"])
        ordinary_series = (
            item["media_type"] in {"episode", "short"}
            and item["collection_type"] is None
            and channel in recurring_channels
            and int(item["ready_count"]) > 0
        )
        location = str(item["daypart_slug"] or "")
        series_folder_name = posixpath.basename(str(item.get("folder_relative_path", ""))) or None
        if location in {"", "all-day", "daytime"}:
            upcoming = scheduled_any_daypart.get((channel, title), 0)
            assigned = any(
                title in series
                for (assigned_channel, _part), series in recurring_slot_assignments.items()
                if assigned_channel == channel
            )
            holiday_airings = [
                entry for (airing_channel, series, _part), entries in seasonal_upcoming.items()
                if airing_channel == channel and series == title for entry in entries
                if series_folder_name and entry.get("series_folder_name") == series_folder_name
            ]
        else:
            upcoming = scheduled_counts.get((channel, location, title), 0)
            compatible = {location}
            if location == "daytime":
                compatible = {"morning", "afternoon", "evening"}
            assigned = any(
                title in recurring_slot_assignments.get((channel, part), set())
                for part in compatible
            )
            holiday_airings = [
                entry for part in compatible
                for entry in seasonal_upcoming.get((channel, title, part), [])
                if series_folder_name and entry.get("series_folder_name") == series_folder_name
            ]
        item["upcoming_appearances"] = upcoming if ordinary_series else 0
        item["recurring_assigned"] = bool(ordinary_series and assigned)
        item["holiday_upcoming_airings"] = sorted(
            holiday_airings, key=lambda entry: entry["target_date"]
        ) if ordinary_series else []
        item["recurring_displaced"] = bool(
            ordinary_series and upcoming == 0 and assigned and holiday_airings
        )
        item["recurring_unplaced"] = bool(
            ordinary_series and upcoming == 0 and not assigned
        )
        items.append(item)
    items.sort(key=lambda item: (
        order.get(str(item["media_type"]), 9), str(item["title"]).casefold(),
        str(item["channel_slug"]), str(item["location"]),
    ))
    channels = sorted({str(item["channel_slug"]) for item in items})
    media_types = sorted({str(item["media_type"]) for item in items})
    return {
        "items": items,
        "channels": channels,
        "media_types": media_types,
        "summary": {
            "series": len({
                (item["title"], item["media_type"])
                for item in items
                if item["media_type"] in {"episode", "short"}
            }),
            "movies": sum(
                int(item["item_count"])
                for item in items if item["media_type"] == "movie"
            ),
            "blocks": sum(
                int(item["item_count"])
                for item in items if item["media_type"] == "complete_block"
            ),
            "assets": int(connection.execute(
                "SELECT COUNT(*) count FROM assets WHERE integrity_status IN ('ready','not_scanned')"
            ).fetchone()["count"]),
            "placements": len(rows),
            "unplaced_recurring": sum(
                1 for item in items if item["recurring_unplaced"]
            ),
            "review": int(connection.execute(
                "SELECT COUNT(*) count FROM assets WHERE review_status='open'"
            ).fetchone()["count"]) + int(connection.execute(
                "SELECT COUNT(*) count FROM shadow_files WHERE status='review'"
            ).fetchone()["count"]),
        },
    }


def make_handler(config: AppConfig, runtime=None) -> type[BaseHTTPRequestHandler]:
    database_path = config.database_path

    class Handler(BaseHTTPRequestHandler):
        server_version = f"CRTV-Sim/{__version__}"

        def _json(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def _html(self, value: str) -> None:
            body = value.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._json({"error": "Preview not found"}, 404)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _video_file(self, path: Path) -> None:
            if not path.is_file():
                self._json({"error": "Media not found"}, 404)
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            raw_range = self.headers.get("Range", "")
            if raw_range.startswith("bytes="):
                try:
                    first, _, last = raw_range[6:].partition("-")
                    start = int(first) if first else max(0, size - int(last))
                    end = min(size - 1, int(last)) if first and last else size - 1
                    if start < 0 or start > end:
                        raise ValueError
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(HTTPStatus.PARTIAL_CONTENT if raw_range else HTTPStatus.OK)
            self.send_header("Content-Type", "video/mp4" if path.suffix.casefold() in {".mp4", ".m4v"} else "video/x-matroska")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if raw_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = source.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _announcement_test_root(self) -> Path:
            raw = str(
                config.data.get("announcements", {}).get(
                    "output_dir", "./data/generated/announcements"
                )
            )
            path = Path(os.path.expandvars(os.path.expanduser(raw)))
            output = path if path.is_absolute() else (config.project_root / path)
            return (output / "tests").resolve()

        def _ai_graphic_draft_root(self) -> Path:
            root = config.storage_root("previews").resolve() / "ai-graphics"
            root.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - 24 * 60 * 60
            for path in root.glob("*.png"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue
            return root

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/api/weather-graphics", "/api/weather-graphics/file"}:
                try:
                    from retrotv.weather_graphics import WeatherGraphicsService, sync_weather_holidays
                    service = WeatherGraphicsService(config)
                    if parsed.path.endswith("/file"):
                        query = parse_qs(parsed.query)
                        path = service.asset_path(query.get("key", [""])[0], query.get("filename", [""])[0])
                        if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi"}:
                            self._video_file(path)
                        else:
                            import mimetypes
                            self._file(path, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                    else:
                        sync_weather_holidays(config)
                        self._json(service.catalog())
                except (ValueError, OSError, RuntimeError) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path == "/":
                self._html(DASHBOARD_HTML if runtime is not None else INDEX_HTML)
                return
            if parsed.path == "/library":
                self._html(INDEX_HTML)
                return
            if parsed.path == "/internet-media":
                self._html(INTERNET_MEDIA_HTML)
                return
            if parsed.path == "/settings":
                self._html(SETTINGS_HTML)
                return
            if parsed.path == "/guide":
                self._html(GUIDE_HTML)
                return
            if parsed.path == "/api/guide":
                params = parse_qs(parsed.query)
                raw_at = params.get("at", [""])[0].strip()
                try:
                    when = datetime.fromisoformat(raw_at) if raw_at else None
                    with connect_database(database_path) as connection:
                        payload = GuideService(connection, config).snapshot(when)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(payload)
                return
            if parsed.path == "/api/external-media":
                with closing(connect_database(database_path)) as connection:
                    self._json(ExternalMediaService(connection, config).overview())
                return
            if parsed.path == "/api/internet-media":
                with connect_database(database_path) as connection:
                    payload = InternetMediaService(connection, config).overview()
                payload["restore_point"] = RestorePointService(config).inventory()
                self._json(payload)
                return
            if parsed.path == "/api/holiday-reviews":
                with connect_database(database_path) as connection:
                    service = HolidayReviewService(connection, config)
                    items = service.pending()
                    paths = connection.execute("""
                        SELECT a.source_relative_path FROM assets a
                        JOIN storage_roots r ON r.id=a.source_root_id
                        WHERE r.logical_name='media' AND a.integrity_status!='missing'
                    """).fetchall()
                folders = set()
                for row in paths:
                    folder = Path(str(row["source_relative_path"])).parent
                    while folder != Path("."):
                        folders.add(folder.as_posix())
                        folder = folder.parent
                # The asset index only knows folders containing indexed media.
                # Include the actual directory tree too, so pre-created empty
                # destinations (such as holiday/daypart folders) are selectable.
                media_root = config.storage_root("media").resolve()
                if media_root.is_dir():
                    for candidate in media_root.rglob("*"):
                        if candidate.is_dir():
                            try:
                                resolved = candidate.resolve()
                                relative = resolved.relative_to(media_root)
                            except (OSError, ValueError):
                                continue
                            folders.add(relative.as_posix())
                self._json({"items": items, "folders": sorted(folders),
                            "media_root": str(media_root)})
                return
            parts = parsed.path.strip("/").split("/")
            if (
                len(parts) == 4
                and parts[:2] == ["api", "channels"]
                and parts[3] == "deletion-preview"
            ):
                try:
                    with connect_database(database_path) as connection:
                        payload = ChannelDeletionService(connection, config).preview(
                            unquote(parts[2])
                        )
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(payload)
                return
            if parsed.path in {
                "/api/templates/up-next-background",
                "/api/templates/up-next-background-template.png",
            }:
                self._file(
                    config.project_root / "assets" / "templates"
                    / "up-next-background-template.png",
                    "image/png",
                )
                return
            if parsed.path == "/api/templates/up-next-layout":
                self._file(
                    config.project_root / "assets" / "templates"
                    / "up-next-layout-guide.svg",
                    "image/svg+xml; charset=utf-8",
                )
                return
            if parsed.path == "/api/health":
                with connect_database(database_path) as connection:
                    row = connection.execute(
                        """
                        SELECT
                          COUNT(*) AS assets,
                          SUM(review_status = 'ready') AS ready,
                          SUM(review_status = 'open') AS needs_review,
                          SUM(enabled_for_scheduling = 1) AS enabled,
                          (SELECT COUNT(*) FROM commercial_duration_reviews
                           WHERE decision='pending') AS long_commercial_review,
                          (SELECT COUNT(*) FROM normalization_profiles n
                           JOIN assets na ON na.id = n.asset_id
                           WHERE n.analysis_status = 'analyzed'
                             AND na.integrity_status IN ('ready', 'not_scanned'))
                              AS normalization_analyzed,
                          (SELECT COUNT(*) FROM normalization_profiles n
                           JOIN assets na ON na.id = n.asset_id
                           WHERE n.analysis_status = 'review'
                             AND na.integrity_status IN ('ready', 'not_scanned'))
                              AS normalization_review,
                          (SELECT COUNT(*) FROM audio_normalization_profiles an
                           JOIN assets aa ON aa.id = an.asset_id
                           WHERE an.analysis_status IN ('analyzed', 'not_applicable')
                             AND aa.integrity_status IN ('ready', 'not_scanned'))
                              AS audio_analyzed,
                          (SELECT COUNT(*) FROM audio_normalization_profiles an
                           JOIN assets aa ON aa.id = an.asset_id
                           WHERE an.analysis_status = 'review'
                             AND aa.integrity_status IN ('ready', 'not_scanned'))
                              AS audio_review,
                          (SELECT COUNT(*) FROM schedule_generations
                           WHERE status = 'active')
                              AS active_schedules
                        FROM assets
                        """
                    ).fetchone()
                    pending = GuideService(
                        connection, config
                    )._pending_action_summary()
                response = {key: int(row[key] or 0) for key in row.keys()}
                response["conversion_ready"] = int(pending["conversion_ready"])
                response["audio_conversion_ready"] = int(
                    pending["audio_conversion_ready"]
                )
                response["pending_total"] = int(pending["total"])
                self._json(response)
                return
            if parsed.path == "/api/runtime/status":
                if runtime is None:
                    self._json({"error": "Application runtime is not active"}, 503)
                    return
                self._json(runtime.status())
                return
            if parsed.path == "/api/runtime/shutdown-status":
                if runtime is None:
                    self._json({"error": "Application runtime is not active"}, 503)
                    return
                self._json(runtime.shutdown_status())
                return
            if parsed.path == "/api/runtime/internet-cache-preview":
                if runtime is None:
                    self._json({"error": "Application runtime is not active"}, 503)
                    return
                self._json(runtime.internet_cache_preview())
                return
            if parsed.path == "/api/runtime/database-compaction-preview":
                if runtime is None:
                    self._json({"error": "Application runtime is not active"}, 503)
                    return
                self._json(runtime.database_compaction_preview())
                return
            if parsed.path == "/api/channels":
                configured = config.data.get("guide", {}).get(
                    "channel_numbers", {}
                )
                with connect_database(database_path) as connection:
                    rows = connection.execute(
                        """
                        SELECT slug, display_name, channel_number
                        FROM channels
                        WHERE enabled = 1
                        ORDER BY channel_number IS NULL, channel_number,
                                 display_name
                        """
                    ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    if item["slug"] == "weather":
                        continue
                    item["channel_number"] = configured.get(
                        item["slug"], item["channel_number"]
                    )
                    items.append(item)
                items.sort(
                    key=lambda item: (
                        item["channel_number"] is None,
                        item["channel_number"] or 9999,
                        str(item["display_name"]).casefold(),
                    )
                )
                self._json({"items": items})
                return
            if parsed.path == "/api/settings":
                with connect_database(database_path) as connection:
                    rows = connection.execute(
                        """
                        SELECT slug, display_name, channel_number
                        FROM channels WHERE enabled=1
                        ORDER BY channel_number IS NULL, channel_number, display_name
                        """
                    ).fetchall()
                channels = [
                    dict(row) for row in rows if str(row["slug"]) != "weather"
                ]
                weather = weather_channel_definition(config)
                channels.append(
                    {
                        "slug": weather.slug,
                        "display_name": weather.display_name,
                        "channel_number": weather.channel_number,
                        "synthetic": True,
                    }
                )
                try:
                    payload = SettingsService(config).snapshot(channels)
                except (ValueError, OSError) as exc:
                    self._json({"error": str(exc)}, 500)
                    return
                self._json(payload)
                return
            if parsed.path == "/api/tts/provider-options":
                try:
                    query = parse_qs(parsed.query)
                    page = int(query.get("page", ["1"])[0])
                    search = query.get("search", [""])[0].strip()
                    token = query.get("next_page_token", [""])[0] or None
                    if not 1 <= page <= 10_000 or len(search) > 200 or (token and len(token) > 4096):
                        raise ValueError("Invalid Breeze voice search or page")
                    configured = bool(configured_breeze_api_key())
                    options = {"models": [], "voices": [], "page": page, "has_more": False,
                               "next_page_token": None}
                    if configured:
                        provider = BreezeTTSProvider(timeout_seconds=config.data.get(
                            "announcements", {}).get("provider_timeout_seconds", 90))
                        try:
                            options = provider.options(page=page, search=search,
                                                       next_page_token=token, include_models=page == 1)
                        finally:
                            provider.close()
                    self._json({"provider": "breeze", "configured": configured, **options})
                except (ValueError, TypeError, RuntimeError, OSError) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path == "/api/announcement-profiles":
                configured_key = configured_openai_api_key()
                tls_backend = OpenAIHTTPClient(
                    api_key=configured_key or "diagnostic-only"
                ).ssl_backend
                with connect_database(database_path) as connection:
                    service = AnnouncementProfileService(connection, config)
                    items = []
                    for profile in service.profiles():
                        item = {
                            "channel_slug": profile.channel_slug,
                            "channel_name": profile.channel_name,
                            "daypart_slug": profile.daypart_slug,
                            "weekly_collection_slug": profile.weekly_collection_slug,
                            "profile_scope": profile.profile_scope,
                            "override_enabled": profile.override_enabled,
                            "identity_name": profile.channel_name,
                            "tagline": profile.tagline,
                            "spoken_tagline": profile.spoken_tagline,
                            "promo_enabled": profile.promo_enabled,
                            "promo_narration_enabled": profile.promo_narration_enabled,
                            "promo_prompt": profile.promo_prompt,
                            "enabled": profile.enabled,
                            "script_model": profile.script_model,
                            "tts_provider": profile.tts_provider,
                            "breeze_speech_model": profile.breeze_speech_model,
                            "breeze_voice_id": profile.breeze_voice_id,
                            "speech_model": profile.speech_model,
                            "voice": profile.voice,
                            "personality_prompt": profile.personality_prompt,
                            "voice_instructions": profile.voice_instructions,
                            "generic_script": profile.generic_script,
                            "maximum_words": profile.maximum_words,
                            "generic": service.generic_status(
                                profile.channel_slug,
                                profile.daypart_slug,
                                profile.weekly_collection_slug,
                            ),
                        }
                        items.append(item)
                self._json({
                    "items": items,
                    "breeze_key_configured": bool(configured_breeze_api_key()),
                    "api_key_configured": bool(configured_key),
                    "tls_backend": tls_backend,
                })
                return
            if parsed.path == "/api/bumper-cues/review":
                with connect_database(database_path) as connection:
                    payload = bumper_review_catalog(connection, config)
                self._json(payload)
                return
            announcement_test_parts = parsed.path.strip("/").split("/")
            if (
                len(announcement_test_parts) == 3
                and announcement_test_parts[:2] == ["api", "announcement-tests"]
            ):
                filename = unquote(announcement_test_parts[2])
                if (
                    Path(filename).name != filename
                    or not filename.startswith("announcer-test-")
                    or not filename.lower().endswith(".wav")
                ):
                    self._json({"error": "Invalid announcer test file"}, 400)
                    return
                self._file(self._announcement_test_root() / filename, "audio/wav")
                return
            if parsed.path == "/api/graphics":
                with connect_database(database_path) as connection:
                    service = GraphicsService(connection, config)
                    service.ensure_all()
                    items = service.inventory()
                    placeholder_todo = service.placeholder_todo()
                dayparts = [
                    str(item.get("slug", "")).strip().casefold()
                    for item in config.data.get("scheduling", {}).get("dayparts", [])
                    if str(item.get("slug", "")).strip()
                ]
                self._json({
                    "items": items,
                    "dayparts": dayparts,
                    "placeholder_todo": placeholder_todo,
                    "placeholder_todo_count": len(placeholder_todo),
                })
                return
            if parsed.path == "/api/duplicates":
                with connect_database(database_path) as connection:
                    result = DuplicateService(connection, config).groups()
                self._json(result)
                return
            if parsed.path == "/api/inventory":
                with connect_database(database_path) as connection:
                    result = _media_inventory(connection)
                self._json(result)
                return
            if parsed.path == "/api/shadows":
                with connect_database(database_path) as connection:
                    service = ShadowService(connection, config)
                    result = service.inventory()
                    result["folders"] = service.source_folders()
                    result["options"] = service.options()
                self._json(result)
                return
            if parsed.path == "/api/breakpoints":
                params = parse_qs(parsed.query)
                try:
                    limit = min(1000, max(1, int(params.get("limit", ["250"])[0])))
                except ValueError:
                    self._json({"error": "Invalid limit"}, 400)
                    return
                status = params.get("status", [""])[0].strip().casefold()
                settings = config.data.get("scheduling", {})
                minimum_seconds = float(settings.get("midroll_min_media_minutes", 15)) * 60
                where = ["a.media_type IN ('episode','movie')", "m.duration_seconds > ?"]
                values: list[object] = [minimum_seconds]
                if status == "pending":
                    where.append("(b.asset_id IS NULL OR b.status IN ('pending','error'))")
                elif status in {"analyzed", "manual"}:
                    where.append("b.status=?")
                    values.append(status)
                elif status == "midpoint":
                    where.append("EXISTS (SELECT 1 FROM asset_break_candidates c WHERE c.asset_id=a.id AND c.selected=1 AND c.method='midpoint')")
                elif status:
                    self._json({"error": "Invalid breakpoint status"}, 400)
                    return
                with connect_database(database_path) as connection:
                    rows = connection.execute(
                        f"""
                        SELECT a.id,a.display_title,a.original_filename,
                               a.source_relative_path,m.duration_seconds,
                               b.status AS break_status,b.selected_offset_ms,
                               b.selected_method,b.confidence,b.detail,b.analyzed_at,
                               (SELECT GROUP_CONCAT(c.offset_ms, ',')
                                FROM asset_break_candidates c
                                WHERE c.asset_id=a.id AND c.selected=1)
                                 AS selected_offsets_ms
                        FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
                        LEFT JOIN asset_break_analyses b ON b.asset_id=a.id
                        WHERE a.enabled_for_scheduling=1
                          AND a.review_status='ready'
                          AND ({' AND '.join(where)})
                        ORDER BY b.status IS NULL DESC,a.channel_slug,a.display_title
                        LIMIT ?
                        """,
                        (*values, limit),
                    ).fetchall()
                items = []
                with connect_database(database_path) as connection:
                    for row in rows:
                        item = dict(row)
                        evidence = connection.execute(
                            """SELECT offset_ms,method,score,signals_json
                               FROM asset_break_candidates
                               WHERE asset_id=? AND selected=1 ORDER BY offset_ms""",
                            (row["id"],),
                        ).fetchall()
                        item["selected_evidence"] = [
                            {"offset_ms": entry["offset_ms"], "method": entry["method"],
                             "score": entry["score"],
                             "signals": json.loads(entry["signals_json"] or "{}")}
                            for entry in evidence
                        ]
                        overrides = {entry["target_index"] for entry in connection.execute(
                            "SELECT target_index FROM asset_break_overrides WHERE asset_id=?",
                            (row["id"],),
                        )}
                        targets = BreakpointAnalyzer(connection, config).target_offsets(
                            round(float(row["duration_seconds"]) * 1000))
                        item["targets"] = [{"index": index, "target_ms": target,
                            "manual": index in overrides,
                            "selected": next((entry for entry in item["selected_evidence"]
                                if entry["signals"].get("window") == index), None)}
                            for index, target in enumerate(targets, 1)]
                        items.append(item)
                self._json({"items": items})
                return
            graphic_parts = parsed.path.strip("/").split("/")
            if (
                len(graphic_parts) == 4
                and graphic_parts[:2] == ["api", "graphics"]
                and graphic_parts[3] == "preview"
            ):
                try:
                    key = graphic_parts[2]
                    kind, _, raw_id = key.partition("-")
                    graphic_id = int(raw_id)
                    with connect_database(database_path) as connection:
                        table = "graphic_audio_assets" if kind == "a" else "graphic_assets"
                        row = connection.execute(
                            f"SELECT relative_path FROM {table} WHERE id=? AND validation_status='valid'",
                            (graphic_id,),
                        ).fetchone()
                    if row is None:
                        raise ValueError("Graphic was not found")
                    path = config.storage_root("media") / Path(row["relative_path"])
                    content_types = {
                        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".webp": "image/webp", ".mp4": "video/mp4",
                        ".mkv": "video/x-matroska", ".webm": "video/webm",
                        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
                        ".aac": "audio/aac", ".flac": "audio/flac",
                        ".ogg": "audio/ogg", ".opus": "audio/ogg",
                    }
                    self._file(path, content_types.get(path.suffix.casefold(), "application/octet-stream"))
                except (ValueError, TypeError) as exc:
                    self._json({"error": str(exc)}, 404)
                return
            if (len(graphic_parts) == 4 and graphic_parts[:3] == ["api", "graphics", "ai-draft"]):
                token = graphic_parts[3]
                if not token or any(char not in "0123456789abcdef" for char in token):
                    self._json({"error": "Invalid AI graphic draft"}, 400)
                    return
                self._file(self._ai_graphic_draft_root() / f"{token}.png", "image/png")
                return
            if parsed.path == "/api/programming-rules":
                with connect_database(database_path) as connection:
                    scheduler = ScheduleBuilder(connection, config)
                    scheduler.rules.sync_catalog()
                    rows = connection.execute(
                        """
                        SELECT cr.*,
                               COALESCE((
                                   SELECT COUNT(DISTINCT a.id)
                                   FROM assets a
                                   WHERE a.collection_slug = cr.collection_slug
                                     AND a.collection_type = cr.rule_type
                                     AND a.review_status = 'ready'
                                     AND (
                                           cr.channel_slug IS NULL
                                           OR a.channel_slug = cr.channel_slug
                                           OR a.channel_slug IS NULL
                                         )
                               ), 0) AS ready_assets
                        FROM calendar_rules cr
                        ORDER BY cr.rule_type, cr.slug
                        """
                    ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    weekdays = json.loads(item.get("weekdays_json") or "[]")
                    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                    item["weekdays"] = ", ".join(names[index] for index in weekdays)
                    items.append(item)
                self._json({"items": items})
                return
            if parsed.path == "/api/blocks":
                with connect_database(database_path) as connection:
                    rows = BlockService(connection).blocks()
                self._json({"items": [dict(row) for row in rows]})
                return
            block_parts = parsed.path.strip("/").split("/")
            if (
                len(block_parts) == 4
                and block_parts[:2] == ["api", "blocks"]
                and block_parts[3] == "cues"
            ):
                try:
                    asset_id = int(block_parts[2])
                    with connect_database(database_path) as connection:
                        rows = BlockService(connection).cues(asset_id)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 404)
                    return
                self._json({"asset_id": asset_id, "items": [dict(row) for row in rows]})
                return
            if parsed.path == "/api/schedule":
                params = parse_qs(parsed.query)
                channel = params.get("channel", ["fox"])[0].strip() or "fox"
                raw_date = params.get("date", [""])[0].strip()
                try:
                    target = date.fromisoformat(raw_date)
                except ValueError:
                    self._json({"error": "Invalid schedule date"}, 400)
                    return
                programs_only = params.get("programs_only", ["0"])[0] == "1"
                with connect_database(database_path) as connection:
                    scheduler = ScheduleBuilder(connection, config)
                    generation = scheduler.active_generation(channel, target)
                    if generation is None:
                        self._json(
                            {
                                "channel": channel,
                                "date": target.isoformat(),
                                "generation": None,
                                "items": [],
                            }
                        )
                        return
                    rows = scheduler.entries(
                        int(generation["id"]), programs_only=programs_only
                    )
                self._json(
                    {
                        "channel": channel,
                        "date": target.isoformat(),
                        "generation": dict(generation),
                        "items": [dict(row) for row in rows],
                    }
                )
                return
            if parsed.path == "/api/schedule/preferences":
                params = parse_qs(parsed.query)
                channel = params.get("channel", [""])[0].strip()
                if not channel:
                    self._json({"error": "Choose a channel"}, 400)
                    return
                with connect_database(database_path) as connection:
                    preferences = ScheduleBuilder(
                        connection, config
                    ).schedule_preferences(channel)
                self._json({"channel": channel, **preferences})
                return
            if parsed.path == "/api/schedule/lineup":
                params = parse_qs(parsed.query)
                channel = params.get("channel", [""])[0].strip()
                if not channel:
                    self._json({"error": "Choose a channel"}, 400)
                    return
                with connect_database(database_path) as connection:
                    lineup = ScheduleBuilder(connection, config).recurring_lineup(channel)
                self._json({"channel": channel, **lineup})
                return
            video_parts = parsed.path.strip("/").split("/")
            if (len(video_parts) == 4 and video_parts[:2] == ["api", "assets"]
                    and video_parts[3] == "play"):
                try:
                    asset_id = int(video_parts[2])
                    with closing(connect_database(database_path)) as connection:
                        path = resolve_asset_path(connection, config, asset_id)
                    self._video_file(path)
                except (ValueError, OSError, BrokenPipeError) as exc:
                    if not isinstance(exc, BrokenPipeError):
                        self._json({"error": str(exc)}, 404)
                return
            if parsed.path == "/api/assets":
                params = parse_qs(parsed.query)
                try:
                    limit = min(1000, max(1, int(params.get("limit", ["250"])[0])))
                    offset = max(0, int(params.get("offset", ["0"])[0]))
                except ValueError:
                    self._json({"error": "Invalid pagination"}, 400)
                    return
                where, values = _asset_query(params)
                query = f"""
                    SELECT a.*, r.source_kind, r.absolute_path,
                           (SELECT group_concat(x.channel_slug || " / " || x.scope, "; ") FROM external_assignments x WHERE x.root_id=a.source_root_id AND x.enabled=1) AS external_placements,
                           m.duration_seconds, m.width, m.height,
                           m.video_codec, m.audio_codec,
                           n.analysis_status, n.detected_mode, n.proposed_mode,
                           n.override_mode, n.confidence, n.analysis_detail,
                           n.crop_x, n.crop_y, n.crop_width, n.crop_height,
                           n.sample_results_json,
                           n.proxy_status, n.proxy_relative_path, n.proxy_error,
                           n.source_preview_relative_path,
                           n.normalized_preview_relative_path,
                           an.analysis_status AS audio_analysis_status,
                           an.measured_i, an.measured_tp, an.measured_lra,
                           an.needs_correction AS audio_needs_correction,
                           an.approval_status AS audio_approval_status,
                           an.correction_status AS audio_correction_status,
                           an.detail AS audio_detail, an.error AS audio_error
                           ,cdr.decision AS commercial_review_decision,
                           cdr.threshold_seconds AS commercial_review_threshold
                    FROM assets a
                    JOIN storage_roots r ON r.id=a.source_root_id
                    LEFT JOIN asset_media_info m ON m.asset_id = a.id
                    LEFT JOIN normalization_profiles n ON n.asset_id = a.id
                    LEFT JOIN audio_normalization_profiles an ON an.asset_id = a.id
                    LEFT JOIN commercial_duration_reviews cdr ON cdr.asset_id = a.id
                    {where}
                    ORDER BY a.review_status = 'open' DESC,
                             a.channel_slug, a.daypart_slug, a.display_title
                    LIMIT ? OFFSET ?
                """
                with connect_database(database_path) as connection:
                    rows = connection.execute(
                        query,
                        (*values, limit, offset),
                    ).fetchall()
                    preview_service = NormalizationService(connection, config)
                    preview_recipes = {
                        int(row["id"]): preview_service.preview_recipe(int(row["id"]))
                        for row in rows
                        if row["analysis_status"] == "review"
                    }
                items = []
                for row in rows:
                    item = dict(row)
                    item["draft_preview"] = preview_recipes.get(int(row["id"]))
                    suggestion = NormalizationService.suggest_crop(item)
                    if suggestion is not None:
                        item.update(
                            {
                                "suggested_crop_width": suggestion.width,
                                "suggested_crop_height": suggestion.height,
                                "suggested_crop_x": suggestion.x,
                                "suggested_crop_y": suggestion.y,
                            }
                        )
                    items.append(item)
                self._json({"items": items})
                return
            parts = parsed.path.strip("/").split("/")
            if (
                len(parts) == 5
                and parts[:2] == ["api", "assets"]
                and parts[3] == "preview"
                and parts[4] in {"source", "normalized"}
            ):
                try:
                    asset_id = int(parts[2])
                except ValueError:
                    self._json({"error": "Invalid asset id"}, 400)
                    return
                column = (
                    "source_preview_relative_path"
                    if parts[4] == "source"
                    else "normalized_preview_relative_path"
                )
                with connect_database(database_path) as connection:
                    row = connection.execute(
                        f"SELECT {column} AS relative_path "
                        "FROM normalization_profiles WHERE asset_id = ?",
                        (asset_id,),
                    ).fetchone()
                if row is None or not row["relative_path"]:
                    self._json({"error": "Preview not created"}, 404)
                    return
                root = config.storage_root("previews").resolve()
                target = (root / Path(row["relative_path"])).resolve()
                frame_value = parse_qs(parsed.query).get("frame", [""])[0]
                if frame_value in {"1", "2", "3"}:
                    target = (
                        root
                        / f"{asset_id}-{parts[4]}-{frame_value}.jpg"
                    ).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    self._json({"error": "Invalid preview path"}, 400)
                    return
                self._file(target, "image/jpeg")
                return
            if parsed.path == "/api/review":
                with connect_database(database_path) as connection:
                    rows = connection.execute(
                        """
                        SELECT r.*, a.source_relative_path, a.display_title
                        FROM review_items r
                        JOIN assets a ON a.id = r.asset_id
                        WHERE r.status = 'open'
                        ORDER BY r.created_at DESC
                        """
                    ).fetchall()
                self._json({"items": [dict(row) for row in rows]})
                return
            self._json({"error": "Not found"}, 404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/external-media" or parsed.path.startswith("/api/external-media/"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 65536:
                        raise ValueError("External media request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("Request body must be an object")
                    with closing(connect_database(database_path)) as connection:
                        service = ExternalMediaService(connection, config)
                        if parsed.path == "/api/external-media":
                            result = service.register(payload)
                        else:
                            pieces = parsed.path.strip('/').split('/')
                            if len(pieces) not in (3,4):
                                raise ValueError("Unknown external media action")
                            source_id = int(pieces[2])
                            if len(pieces)==4 and pieces[3]=='rescan':
                                source = service._source(source_id)
                                if not source['enabled']:
                                    raise ValueError("Reconnect the source before scanning")
                                connection.execute("UPDATE external_sources SET scan_status='pending' WHERE root_id=?", (source_id,))
                                connection.commit()
                                result = {'root_id': source_id}
                            elif len(pieces)==3:
                                result = service.update(source_id,payload)
                            else:
                                raise ValueError("Unknown external media action")
                        if runtime is None:
                            if parsed.path == '/api/external-media' or parsed.path.endswith('/rescan'):
                                result['scan'] = service.scan(result['root_id'])
                    if runtime is not None:
                        started = runtime.request_maintenance()
                        result['queued'] = bool(started)
                        result['maintenance_started'] = bool(started)
                        result['message'] = (
                            "Scan queued and maintenance started; refresh sources to see progress."
                            if started else
                            "The source request is saved, but maintenance did not start. It will run on the next maintenance pass; retry after shutdown is complete if needed."
                        )
                    self._json(result, 202 if runtime is not None else 200)
                except (ValueError, TypeError, OSError, RuntimeError, sqlite3.Error) as exc:
                    self._json({'error': str(exc)},400)
                return

            if parsed.path in {"/api/weather-graphics/upload", "/api/weather-graphics/generate"}:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 64 * 1024 * 1024:
                        raise ValueError("Weather artwork request is too large or empty")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("Weather artwork values must be an object")
                    from retrotv.weather_graphics import WeatherGraphicsService, sync_weather_holidays
                    sync_weather_holidays(config)
                    service = WeatherGraphicsService(config)
                    path = service.upload(payload) if parsed.path.endswith("/upload") else service.generate(payload)
                    self._json({"ok": True, "path": str(path)})
                except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path in {"/api/credentials/breeze", "/api/credentials/openai"}:
                try:
                    save_key, remove_key, configured_key = (
                        (save_openai_key, remove_openai_key, configured_openai_api_key)
                        if parsed.path.endswith("/openai") else
                        (save_breeze_key, remove_breeze_key, configured_breeze_api_key))
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Credential request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("Credential request must be an object")
                    if payload.get("action") == "remove":
                        remove_key()
                    elif payload.get("action") == "save":
                        save_key(payload.get("key"))
                    else:
                        raise ValueError("Choose save or remove")
                    self._json({"configured": bool(configured_key())})
                except (ValueError, RuntimeError, OSError) as exc:
                    self._json({"error": str(exc)}, 400)
                return

            parts = parsed.path.strip("/").split("/")
            if parsed.path == "/api/holiday-reviews/action":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 16 * 1024:
                        raise ValueError("Invalid holiday review request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("Request body must be an object")
                    asset_id = int(payload["asset_id"])
                    holiday = str(payload["holiday"])
                    action = str(payload["action"])
                    with connect_database(database_path) as connection:
                        service = HolidayReviewService(connection, config)
                        if action == "dismiss":
                            service.dismiss(asset_id, holiday)
                            result = {"asset_id": asset_id, "dismissed": holiday}
                        elif action == "move":
                            active = runtime._active_media_path() if runtime is not None else None
                            result = service.move(asset_id, holiday, str(payload.get("folder", "")),
                                                  active_path=active)
                            result["schedules_stale"] = True
                        else:
                            raise ValueError("Unknown holiday review action")
                except (ValueError, KeyError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(result)
                return
            if (len(parts) == 4 and parts[:2] == ["api", "bumper-cues"]
                    and parts[3] == "override"):
                try:
                    asset_id = int(parts[2])
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 16 * 1024:
                        raise ValueError("Bumper request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("Request body must be an object")
                    with connect_database(database_path) as connection:
                        save_bumper_override(
                            connection, asset_id,
                            str(payload.get("position") or "auto"),
                            str(payload.get("series_title") or ""),
                        )
                except (ValueError, TypeError, sqlite3.Error, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"asset_id": asset_id, "schedules_stale": True})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "channels"]
                and parts[3] == "delete"
            ):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 64 * 1024:
                        raise ValueError("Channel deletion request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("Request body must be an object")
                    with connect_database(database_path) as connection:
                        result = ChannelDeletionService(connection, config).delete(
                            unquote(parts[2]),
                            confirmation_text=str(payload.get("confirmation_text") or ""),
                            acknowledge_rescan=payload.get("acknowledge_rescan") is True,
                        )
                except (
                    ValueError,
                    TypeError,
                    OSError,
                    RuntimeError,
                    sqlite3.Error,
                    json.JSONDecodeError,
                ) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if runtime is not None:
                    runtime._refresh_pending_actions()
                self._json(result)
                return
            if parsed.path.startswith("/api/internet-media"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 2 * 1024 * 1024:
                        raise ValueError("Internet-media request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError("Request body must be an object")
                    with connect_database(database_path) as connection:
                        service = InternetMediaService(connection, config)
                        if parsed.path == "/api/internet-media/preview":
                            result = service.preview_source(
                                str(payload.get("url") or ""),
                                str(payload.get("show_title") or ""),
                            )
                        elif parsed.path == "/api/internet-media/channels":
                            result = service.configure_channel(payload)
                        elif parsed.path == "/api/internet-media/sources":
                            result = service.add_source(payload)
                        elif (
                            len(parts) == 5
                            and parts[:3] == ["api", "internet-media", "sources"]
                            and parts[4] in {"refresh", "disable"}
                        ):
                            source_id = int(parts[3])
                            if parts[4] == "refresh":
                                result = service.refresh_source(source_id)
                            else:
                                service.disable_source(source_id)
                                result = {"ok": True, "disabled": source_id}
                        elif len(parts) == 5 and parts[:3] == ["api", "internet-media", "episodes"] and parts[4] == "repair":
                            result = service.repair_episode(int(parts[3]))
                        elif len(parts) == 4 and parts[:3] == ["api", "internet-media", "episodes"]:
                            result = service.update_episode(int(parts[3]), payload)
                        elif parsed.path == "/api/internet-media/position":
                            raw_episode_id = payload.get("episode_id")
                            result = service.set_watching_position(
                                str(payload.get("channel") or ""),
                                int(raw_episode_id) if raw_episode_id not in {None, ""} else None,
                                float(payload.get("position_seconds", 0)),
                                start_date=str(payload.get("start_date") or "") or None,
                                start_at_beginning=payload.get("start_at_beginning") is True,
                            )
                        elif parsed.path == "/api/internet-media/retry":
                            episode_id = int(payload.get("episode_id"))
                            row = connection.execute(
                                "SELECT id FROM internet_preparation_jobs WHERE episode_id=?",
                                (episode_id,),
                            ).fetchone()
                            if row is None:
                                raise ValueError("Episode has no preparation job")
                            PreparationService(connection, config).retry(int(row[0]))
                            result = {"ok": True, "episode_id": episode_id}
                        elif parsed.path == "/api/internet-media/restore-point":
                            restore = RestorePointService(config)
                            inventory = restore.inventory()
                            if not inventory.get("available"):
                                raise ValueError(
                                    "The pre-feature restore point must be created before migration"
                                )
                            result = inventory
                        else:
                            self._json({"error": "Not found"}, 404)
                            return
                except (
                    ValueError,
                    TypeError,
                    OSError,
                    RuntimeError,
                    sqlite3.Error,
                    json.JSONDecodeError,
                ) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(result)
                return
            if parsed.path in {
                "/api/shadows/deletion-preview", "/api/shadows/delete"
            }:
                operation_lock = (
                    getattr(runtime, "exclusive_operation_lock", None)
                    if runtime is not None else None
                )
                acquired = False
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 1024 * 1024:
                        raise ValueError("Shadow deletion request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if parsed.path.endswith("/delete") and operation_lock is not None:
                        acquired = operation_lock.acquire(blocking=False)
                        if not acquired:
                            raise ValueError(
                                "CRTV Sim is scanning or maintaining media. Try again when it finishes."
                            )
                    with connect_database(database_path) as connection:
                        service = ShadowService(connection, config)
                        if parsed.path.endswith("/deletion-preview"):
                            result = service.deletion_preview(payload.get("shadow_ids"))
                        else:
                            result = service.delete(
                                payload.get("shadow_ids"), payload.get("token")
                            )
                    if parsed.path.endswith("/delete") and runtime is not None:
                        runtime._refresh_pending_actions()
                except (
                    ValueError, TypeError, OSError, sqlite3.Error,
                    json.JSONDecodeError,
                ) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                finally:
                    if acquired:
                        operation_lock.release()
                self._json(result)
                return
            if parsed.path == "/api/shadows":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 64 * 1024:
                        raise ValueError("Shadow request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    raw_asset_id = payload.get("asset_id")
                    asset_id = int(raw_asset_id) if raw_asset_id is not None else None
                    folder = str(payload.get("folder", "")).strip() or None
                    if (asset_id is None) == (folder is None):
                        raise ValueError("Choose one source file or source folder")
                    with connect_database(database_path) as connection:
                        result = ShadowService(connection, config).create(
                            asset_id=asset_id,
                            folder=folder,
                            channel=str(payload.get("channel", "")),
                            scope=str(payload.get("scope", "")),
                            value=str(payload.get("value", "")),
                        )
                    if runtime is not None:
                        runtime._refresh_pending_actions()
                except (
                    ValueError, TypeError, OSError, json.JSONDecodeError
                ) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(result)
                return
            if parsed.path == "/api/inventory/open-folder":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 8192:
                        raise ValueError("Folder request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if payload.get('asset_id') is not None:
                        with closing(connect_database(database_path)) as connection:
                            folder = resolve_asset_path(connection,config,int(payload['asset_id'])).parent
                    else:
                        relative = Path(str(payload.get("path", "")).strip())
                        if not str(relative) or relative.is_absolute():
                            raise ValueError("Invalid media folder")
                        media_root = config.storage_root("media").resolve()
                        folder = (media_root / relative).resolve()
                        folder.relative_to(media_root)
                    if not folder.is_dir():
                        raise ValueError("Media folder no longer exists")
                    if sys.platform == "win32":
                        os.startfile(folder)  # type: ignore[attr-defined]
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", str(folder)])
                    else:
                        subprocess.Popen(["xdg-open", str(folder)])
                except (
                    ValueError, TypeError, OSError, json.JSONDecodeError
                ) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True})
                return
            duplicate_parts = parsed.path.strip("/").split("/")
            if len(duplicate_parts) == 3 and duplicate_parts[:2] == ["api", "duplicates"]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    with connect_database(database_path) as connection:
                        DuplicateService(connection, config).review(
                            duplicate_parts[2], str(payload.get("action", ""))
                        )
                except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if runtime is not None:
                    runtime._refresh_pending_actions()
                self._json({"ok": True})
                return
            if parsed.path == "/api/settings":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 1024 * 1024:
                        raise ValueError("Settings payload is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    result = SettingsService(config).save(payload)
                    if "holidays" in payload:
                        from retrotv.weather_graphics import sync_weather_holidays
                        try:
                            sync_weather_holidays(config)
                        except OSError as exc:
                            # The TV settings have already been saved. Report a
                            # companion sync problem without claiming save failed.
                            result["message"] += f" Weather holiday sync failed: {exc}. The companion will retry when running."
                except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                changed = result.get("changed_live_settings", [])
                if runtime is not None and "player.osd_font_size" in changed:
                    with runtime.lock:
                        controller = runtime.controller
                    if controller is not None:
                        try:
                            controller.player.set_osd_font_size(
                                int(config.data["player"]["osd_font_size"])
                            )
                        except (PlaybackError, OSError):
                            result["message"] += " Restart the TV player to apply the channel-label size."
                if runtime is not None and "guide.text_size_percent" in changed:
                    runtime.request_guide_refresh()
                self._json(result)
                return
            profile_parts = parsed.path.strip("/").split("/")
            if (
                len(profile_parts) == 4
                and profile_parts[:2] == ["api", "announcement-profiles"]
                and profile_parts[3] == "test"
            ):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 1024 * 1024:
                        raise ValueError("Announcer test payload is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    channel = unquote(profile_parts[2]).strip().casefold()
                    kind = str(payload.get("kind", "")).strip().casefold()
                    if kind not in {"text", "tts"}:
                        raise ValueError("Announcer test kind must be text or tts")
                    with connect_database(database_path) as connection:
                        channel_row = connection.execute(
                            "SELECT display_name FROM channels WHERE slug=? AND enabled=1",
                            (channel,),
                        ).fetchone()
                    if channel_row is None:
                        raise ValueError(f"Unknown enabled channel: {channel}")
                    identity_name = str(
                        payload.get("identity_name", channel_row["display_name"])
                    ).strip() or str(channel_row["display_name"])
                    settings = config.data.get("announcements", {})
                    client = OpenAIHTTPClient(
                        base_url=str(
                            settings.get("openai_base_url", "https://api.openai.com/v1")
                        )
                    )
                    if kind == "text" and not client.available:
                        raise ValueError("OpenAI API key is not configured; add it in Settings")
                    started = time.monotonic()
                    if kind == "text":
                        model = str(payload.get("script_model", "")).strip() or str(
                            settings.get("script_model", "gpt-5.6-luna")
                        )
                        maximum_words = max(
                            10, min(200, int(payload.get("maximum_words", 55)))
                        )
                        personality = str(payload.get("personality_prompt", "")).strip()
                        if not personality:
                            personality = (
                                f"You are a station announcer for {identity_name} "
                                "in the 1990s. Be lively, concise, and true to the station identity."
                            )
                        response = client.request_json(
                            "responses",
                            {
                                "model": model,
                                "input": (
                                    f"{personality}\n\nWrite only one short station identification "
                                    f"of no more than {maximum_words} words. This is a connection "
                                    "test, so do not mention a specific upcoming show or time."
                                ),
                                "max_output_tokens": 220,
                            },
                        )
                        generated = OpenAIScriptProvider._output_text(response).strip()
                        if not generated:
                            raise RuntimeError("OpenAI generated an empty announcement")
                        result = {
                            "ok": True,
                            "kind": "text",
                            "text": generated,
                            "model": model,
                        }
                    else:
                        model = str(payload.get("speech_model", "")).strip() or str(
                            settings.get("speech_model", "gpt-4o-mini-tts")
                        )
                        voice = str(payload.get("voice", "")).strip() or "alloy"
                        spoken = str(payload.get("generic_script", "")).strip()
                        if not spoken:
                            spoken = f"You're watching {identity_name} on CRTV Sim."
                        spoken = spoken[:800]
                        filename = (
                            f"announcer-test-{int(time.time())}-{secrets.token_hex(6)}.wav"
                        )
                        output = self._announcement_test_root() / filename
                        provider_name = str(payload.get("tts_provider", "openai"))
                        if provider_name not in {"openai", "breeze"}:
                            raise ValueError("Choose OpenAI or Breeze Blue")
                        test_profile = SimpleNamespace(tts_provider=provider_name, speech_model=model,
                            breeze_speech_model=str(payload.get("breeze_speech_model", "breeze-tts-2")))
                        if provider_name == "breeze":
                            model = test_profile.breeze_speech_model
                            voice = str(payload.get("breeze_voice_id", "")).strip()
                        provider_for_profile(test_profile, config, client).synthesize(
                            spoken,
                            output,
                            voice=voice,
                            instructions=str(payload.get("voice_instructions", "")),
                        )
                        result = {
                            "ok": True,
                            "kind": "tts",
                            "audio_url": f"/api/announcement-tests/{filename}",
                            "model": model,
                            "voice": voice,
                        }
                    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                except (ValueError, TypeError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(result)
                return
            if len(profile_parts) == 3 and profile_parts[:2] == ["api", "announcement-profiles"]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    channel = unquote(profile_parts[2]).strip().casefold()
                    daypart_slug = (
                        str(payload.get("daypart_slug") or "").strip().casefold()
                        or None
                    )
                    weekly_collection_slug = (
                        str(payload.get("weekly_collection_slug") or "").strip().casefold()
                        or None
                    )
                    if daypart_slug and weekly_collection_slug:
                        raise ValueError(
                            "Choose either a daypart or weekly announcer scope"
                        )
                    generation_error = None
                    with connect_database(database_path) as connection:
                        service = AnnouncementProfileService(connection, config)
                        profile = service.update(
                            channel, payload, daypart_slug, weekly_collection_slug
                        )
                        output = None
                        if payload.get("action") == "generate-generic":
                            try:
                                client = OpenAIHTTPClient(
                                    base_url=str(
                                        config.data.get("announcements", {}).get(
                                            "openai_base_url", "https://api.openai.com/v1"
                                        )
                                    )
                                )
                                output = service.generate_generic(
                                    channel,
                                    provider_for_profile(profile, config, client),
                                    daypart_slug,
                                    weekly_collection_slug,
                                )
                            except (ValueError, RuntimeError, OSError) as exc:
                                generation_error = str(exc)
                        status = service.generic_status(
                            channel, daypart_slug, weekly_collection_slug
                        )
                except (ValueError, TypeError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({
                    "ok": True,
                    "channel": channel,
                    "generic": status,
                    "output": str(output) if output else None,
                    "saved": True,
                    "generation_error": generation_error,
                })
                return
            if parsed.path == "/api/graphics/generate":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 64 * 1024:
                        raise ValueError("AI graphic request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    action = str(payload.get("action", "")).strip().casefold()
                    drafts = self._ai_graphic_draft_root()
                    if action in {"install", "variation"}:
                        if action == "variation":
                            graphic_key = str(payload.get("graphic_id", ""))
                            raw_id = graphic_key[2:] if graphic_key.startswith("g-") else graphic_key
                            if not raw_id.isdecimal():
                                raise ValueError("Choose an existing AI-generated graphic")
                            with connect_database(database_path) as connection:
                                source_row = connection.execute("SELECT ga.*,c.slug AS channel_slug FROM graphic_assets ga JOIN channels c ON c.id=ga.channel_id WHERE ga.id=? AND ga.provenance='ai' AND ga.validation_status='valid' AND c.enabled=1", (int(raw_id),)).fetchone()
                                if source_row is None or source_row["purpose"] not in {"up-next", "interstitials"}:
                                    raise ValueError("Choose an existing AI-generated Up Next or interstitial graphic")
                                source_path = (config.storage_root("media") / source_row["relative_path"]).resolve()
                                if not source_path.is_relative_to(config.storage_root("media").resolve()) or not source_path.is_file():
                                    raise ValueError("Source graphic is unavailable")
                                payload.update(channel=source_row["channel_slug"], purpose=source_row["purpose"],
                                    holiday=source_row["holiday_slug"] or "", daypart=source_row["daypart_slug"] or "", weekly=source_row["weekly_collection_slug"] or "")

                        channel = str(payload.get("channel", "")).strip().casefold()
                        purpose = str(payload.get("purpose", "")).strip().casefold()
                        holiday = str(payload.get("holiday", "")).strip().casefold() or None
                        daypart = str(payload.get("daypart", "")).strip().casefold() or None
                        weekly = str(payload.get("weekly", "")).strip().casefold() or None
                        art_note = str(payload.get("art_note", "")).strip()
                        if len(art_note) > 1000:
                            raise ValueError("Art direction is limited to 1,000 characters")
                        with connect_database(database_path) as connection:
                            service = GraphicsService(connection, config)
                            known = connection.execute(
                                "SELECT id,display_name FROM channels WHERE slug=? AND enabled=1", (channel,)
                            ).fetchone()
                            if known is None:
                                raise ValueError("Choose an enabled channel")
                            if purpose not in {"up-next", "interstitials"}:
                                raise ValueError("Choose an up-next or interstitial graphic")
                            if holiday and holiday not in service._holidays():
                                raise ValueError("Invalid holiday graphics destination")
                            known_dayparts = {
                                str(item.get("slug", "")).strip().casefold()
                                for item in config.data.get("scheduling", {}).get("dayparts", [])
                                if isinstance(item, dict)
                            }
                            if daypart and daypart not in known_dayparts:
                                raise ValueError("Invalid daypart graphics destination")
                            if weekly and not weekly.replace("-", "").isalnum():
                                raise ValueError("Invalid weekly-special destination")
                            folder = (source_path.parent if action == "variation" else service.destination_folder(
                                channel, purpose, holiday_slug=holiday,
                                daypart_slug=daypart, weekly_collection_slug=weekly,
                            ))
                            channel_name = str(known["display_name"])
                            lab = GraphicsLab(config)
                            generated = lab.generate({
                                "method": "composition-blueprint",
                                "template_set": "composition-blueprint",
                                "channel_name": channel_name,
                                "art_direction": art_note,
                                "purpose": purpose,
                                "quality": "low",
                                "composite_logo": False,
                            })
                            source = lab.root / str(generated["id"]) / "background-640x480.png"
                            if not source.is_file():
                                raise RuntimeError("AI graphic was generated but could not be prepared")
                            folder.mkdir(parents=True, exist_ok=True)
                            from retrotv.graphics_lab import png_dimensions
                            if png_dimensions(source) != (640, 480):
                                raise RuntimeError("Generated graphic has invalid dimensions")
                            target = folder / (f"ai-variation-{secrets.token_hex(12)}.png" if action == "variation" else "ai-station.png")
                            partial = target.with_suffix(".partial.png")
                            partial.write_bytes(source.read_bytes())
                            os.replace(partial, target)
                            media_root = config.storage_root("media").resolve()
                            old_rows = connection.execute(
                                """
                                SELECT relative_path FROM graphic_assets
                                WHERE channel_id=? AND purpose=? AND holiday_slug IS ?
                                  AND daypart_slug IS ? AND weekly_collection_slug IS ?
                                  AND provenance='ai'
                                """,
                                (int(known["id"]), purpose, holiday, daypart, weekly),
                            ).fetchall()
                            for old_row in ([] if action == "variation" else old_rows):
                                relative_path = str(old_row["relative_path"])
                                old_path = (media_root / Path(relative_path)).resolve()
                                if old_path != target.resolve() and old_path.is_relative_to(media_root):
                                    old_path.unlink(missing_ok=True)
                                    connection.execute(
                                        "DELETE FROM graphic_assets WHERE relative_path=?", (relative_path,)
                                    )
                            service.scan_catalog()
                        shutil.rmtree(lab.root / str(generated["id"]), ignore_errors=True)
                        self._json({"ok": True, "path": str(target), "model": generated["model"]})
                        return
                    if action == "preview":
                        channel = str(payload.get("channel", "")).strip().casefold()
                        purpose = str(payload.get("purpose", "")).strip().casefold()
                        holiday = str(payload.get("holiday", "")).strip().casefold() or None
                        daypart = str(payload.get("daypart", "")).strip().casefold() or None
                        weekly = str(payload.get("weekly", "")).strip().casefold() or None
                        art_note = str(payload.get("art_note", "")).strip()
                        if len(art_note) > 1000:
                            raise ValueError("Art direction is limited to 1,000 characters")
                        with connect_database(database_path) as connection:
                            service = GraphicsService(connection, config)
                            known = connection.execute(
                                "SELECT display_name FROM channels WHERE slug=? AND enabled=1", (channel,)
                            ).fetchone()
                            if known is None:
                                raise ValueError("Choose an enabled channel")
                            if purpose not in {"up-next", "interstitials"}:
                                raise ValueError("Choose an up-next or interstitial graphic")
                            if holiday and holiday not in service._holidays():
                                raise ValueError("Invalid holiday graphics destination")
                            known_dayparts = {
                                str(item.get("slug", "")).strip().casefold()
                                for item in config.data.get("scheduling", {}).get("dayparts", [])
                                if isinstance(item, dict)
                            }
                            if daypart and daypart not in known_dayparts:
                                raise ValueError("Invalid daypart graphics destination")
                            if weekly and not weekly.replace("-", "").isalnum():
                                raise ValueError("Invalid weekly-special destination")
                            target_folder = service.destination_folder(
                                channel, purpose, holiday_slug=holiday,
                                daypart_slug=daypart, weekly_collection_slug=weekly,
                            )
                            channel_name = str(known["display_name"])
                        guide = (
                            f'Include one prominent, legible, stylized 1990s television station '
                            f'wordmark that spells "{channel_name}" exactly. Treat that wordmark as '
                            "the channel logo and integrate it into the upper or outer part of the composition. "
                            "Do not add other readable words, ratings, schedules, borders, or UI elements. "
                            "CRTV Sim will add program information over the lower third, so keep that area clean "
                            "and high contrast. Keep the main visual interest outside that lower-third safe area."
                        )
                        scope = ", ".join(value for value in [
                            channel_name, weekly and f"weekly special {weekly}",
                            holiday and f"{holiday} seasonal version", daypart and f"{daypart} daypart",
                            "up next" if purpose == "up-next" else "station interstitial",
                        ] if value)
                        prompt = (
                            f"Design a polished retro television {scope} background in a 4:3 composition. "
                            f"{guide} "
                            + (f"Additional art direction: {art_note}" if art_note else "Use an era-neutral broadcast design appropriate to the station.")
                        )
                        client = OpenAIHTTPClient()
                        if not client.available:
                            raise ValueError("OpenAI API key is not configured; add it in Settings")
                        response = client.request_json(
                            "images/generations",
                            {
                                "model": str(config.data.get("graphics", {}).get("ai_image_model", "gpt-image-1")),
                                "prompt": prompt,
                                "size": "1024x1024",
                                "quality": "low",
                                "output_format": "png",
                                "background": "opaque",
                            },
                        )
                        data = response.get("data") if isinstance(response, dict) else None
                        encoded = data[0].get("b64_json") if isinstance(data, list) and data and isinstance(data[0], dict) else None
                        if not isinstance(encoded, str):
                            raise RuntimeError("OpenAI did not return a PNG image")
                        raw = base64.b64decode(encoded, validate=True)
                        token = secrets.token_hex(16)
                        source = drafts / f"{token}.source.png"
                        draft = drafts / f"{token}.png"
                        source.write_bytes(raw)
                        try:
                            process = subprocess.run(
                                [str(config.ffmpeg), "-y", "-i", str(source), "-vf",
                                 "scale=640:480",
                                 "-frames:v", "1", str(draft)],
                                capture_output=True, text=True, timeout=90, check=False,
                            )
                            if process.returncode != 0 or not draft.is_file():
                                detail = (process.stderr or process.stdout or "image conversion failed").strip()[-800:]
                                raise RuntimeError(f"AI graphic could not be prepared: {detail}")
                        finally:
                            source.unlink(missing_ok=True)
                        (drafts / f"{token}.json").write_text(json.dumps({
                            "channel": channel, "purpose": purpose, "holiday": holiday,
                            "daypart": daypart, "weekly": weekly,
                            "folder": str(target_folder),
                        }), encoding="utf-8")
                        self._json({"ok": True, "draft_id": token,
                                    "preview_url": f"/api/graphics/ai-draft/{token}"})
                        return
                    if action == "accept":
                        token = str(payload.get("draft_id", "")).strip().casefold()
                        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
                            raise ValueError("Invalid AI graphic draft")
                        draft = drafts / f"{token}.png"
                        metadata = drafts / f"{token}.json"
                        if not draft.is_file() or not metadata.is_file():
                            raise ValueError("AI graphic draft expired; generate it again")
                        destination = json.loads(metadata.read_text(encoding="utf-8"))
                        with connect_database(database_path) as connection:
                            service = GraphicsService(connection, config)
                            folder = service.destination_folder(
                                str(destination["channel"]), str(destination["purpose"]),
                                holiday_slug=destination.get("holiday"), daypart_slug=destination.get("daypart"),
                                weekly_collection_slug=destination.get("weekly"),
                            )
                            folder.mkdir(parents=True, exist_ok=True)
                            target = folder / f"ai-{token[:12]}.png"
                            temporary = target.with_suffix(".partial.png")
                            temporary.write_bytes(draft.read_bytes())
                            os.replace(temporary, target)
                            service.scan_catalog()
                        draft.unlink(missing_ok=True)
                        metadata.unlink(missing_ok=True)
                        self._json({"ok": True, "path": str(target)})
                        return
                    raise ValueError("Unknown AI graphic action")
                except (ValueError, TypeError, RuntimeError, OSError, subprocess.SubprocessError,
                        json.JSONDecodeError, binascii.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path == "/api/graphics/manage":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 8 * 1024:
                        raise ValueError("Graphic management request is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if str(payload.get("action", "")).strip().casefold() != "delete-ai":
                        raise ValueError("Unknown graphic management action")
                    key = str(payload.get("graphic_id", "")).strip()
                    kind, _, raw_id = key.partition("-")
                    if kind != "g" or not raw_id.isdecimal():
                        raise ValueError("Choose a generated graphic")
                    with connect_database(database_path) as connection:
                        row = connection.execute(
                            "SELECT relative_path,provenance FROM graphic_assets WHERE id=?", (int(raw_id),)
                        ).fetchone()
                        if row is None or str(row["provenance"]) != "ai":
                            raise ValueError("Only generated graphics can be deleted here")
                        media_root = config.storage_root("media").resolve()
                        target = (media_root / Path(str(row["relative_path"]))).resolve()
                        if not target.is_relative_to(media_root):
                            raise ValueError("Generated graphic path is outside the media library")
                        target.unlink(missing_ok=True)
                        connection.execute("DELETE FROM graphic_assets WHERE id=?", (int(raw_id),))
                        GraphicsService(connection, config).scan_catalog()
                    self._json({"ok": True})
                except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path in {"/api/graphics/upload", "/api/graphics/placeholder"}:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 384 * 1024 * 1024:
                        raise ValueError("Graphic upload is too large")
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    channel = str(payload.get("channel", "")).strip().casefold()
                    purpose = str(payload.get("purpose", "")).strip().casefold()
                    holiday = str(payload.get("holiday", "")).strip().casefold() or None
                    daypart = str(payload.get("daypart", "")).strip().casefold() or None
                    weekly = str(payload.get("weekly", "")).strip().casefold() or None
                    with connect_database(database_path) as connection:
                        service = GraphicsService(connection, config)
                        known = connection.execute(
                            "SELECT id FROM channels WHERE slug=? AND enabled=1", (channel,)
                        ).fetchone()
                        if known is None:
                            raise ValueError("Choose an enabled channel")
                        if holiday and holiday not in service._holidays():
                            raise ValueError("Invalid holiday graphics destination")
                        known_dayparts = {
                            str(item.get("slug", "")).strip().casefold()
                            for item in config.data.get("scheduling", {}).get("dayparts", [])
                        }
                        if daypart and daypart not in known_dayparts:
                            raise ValueError("Invalid daypart graphics destination")
                        if weekly and not weekly.replace("-", "").isalnum():
                            raise ValueError("Invalid weekly-special destination")
                        if parsed.path.endswith("placeholder"):
                            if weekly:
                                raise ValueError("Weekly-special graphics do not use generated placeholders")
                            target = service.regenerate_placeholder(
                                channel, purpose, holiday, daypart
                            )
                        else:
                            filename = Path(str(payload.get("filename", ""))).name
                            suffix = Path(filename).suffix.casefold()
                            if suffix not in GRAPHIC_EXTENSIONS | AUDIO_EXTENSIONS:
                                raise ValueError("Unsupported graphic format")
                            if purpose not in {"up-next", "interstitials", "channel-music"}:
                                raise ValueError("Invalid graphics destination")
                            is_audio = suffix in AUDIO_EXTENSIONS
                            if purpose == "channel-music" and not is_audio:
                                raise ValueError("The all-graphics destination accepts audio files only")
                            if purpose == "channel-music" and holiday:
                                raise ValueError("Root channel music cannot have a holiday destination")
                            replacement_row = None
                            replacement_key = str(payload.get("replace_graphic_id", "")).strip()
                            if replacement_key:
                                kind, _, raw_id = replacement_key.partition("-")
                                if kind != "g" or not raw_id.isdecimal():
                                    raise ValueError("Choose a generated graphic to replace")
                                replacement_row = connection.execute(
                                    """
                                    SELECT id,relative_path,channel_id,purpose,holiday_slug,daypart_slug,
                                           weekly_collection_slug,provenance
                                    FROM graphic_assets WHERE id=?
                                    """, (int(raw_id),)
                                ).fetchone()
                                if replacement_row is None or str(replacement_row["provenance"]) != "ai":
                                    raise ValueError("Only generated graphics can be replaced here")
                                if (
                                    int(replacement_row["channel_id"]) != int(known["id"])
                                    or str(replacement_row["purpose"]) != purpose
                                    or replacement_row["holiday_slug"] != holiday
                                    or replacement_row["daypart_slug"] != daypart
                                    or replacement_row["weekly_collection_slug"] != weekly
                                ):
                                    raise ValueError("Replacement file must use the generated graphic's exact scope")
                            content = base64.b64decode(
                                str(payload.get("content_base64", "")), validate=True
                            )
                            if purpose == "channel-music":
                                root = service._channel_root(channel) / "graphics"
                                if daypart:
                                    root = root / daypart
                                folder = root
                            else:
                                folder = service.destination_folder(
                                    channel, purpose, holiday_slug=holiday,
                                    daypart_slug=daypart, weekly_collection_slug=weekly,
                                )
                            folder.mkdir(parents=True, exist_ok=True)
                            target = folder / filename
                            counter = 2
                            while target.exists():
                                target = folder / f"{Path(filename).stem}-{counter}{suffix}"
                                counter += 1
                            target.write_bytes(content)
                            if replacement_row is not None:
                                media_root = config.storage_root("media").resolve()
                                old_path = (media_root / Path(str(replacement_row["relative_path"]))).resolve()
                                if not old_path.is_relative_to(folder.resolve()):
                                    raise ValueError("Generated graphic path is outside its replacement folder")
                                old_path.unlink(missing_ok=True)
                                connection.execute(
                                    "DELETE FROM graphic_assets WHERE id=?", (int(replacement_row["id"]),)
                                )
                            service.scan_catalog()
                    self._json({"ok": True, "path": str(target)})
                except (ValueError, TypeError, OSError, json.JSONDecodeError, binascii.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path.startswith("/api/runtime/"):
                if runtime is None:
                    self._json({"error": "Application runtime is not active"}, 503)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    action = parsed.path.rsplit("/", 1)[-1]
                    if action != "shutdown" and runtime.stop_event.is_set():
                        self._json(
                            {
                                "error": (
                                    "CRTV Sim is shutting down. Do not remove the drive "
                                    "until safe shutdown completes."
                                )
                            },
                            409,
                        )
                        return
                    if action == "start":
                        runtime.start_tv(str(payload.get("channel", "")).strip() or None)
                        message = "Television is starting."
                    elif action == "stop":
                        runtime.stop_tv()
                        message = "Television stopped."
                    elif action == "tune":
                        channel = str(payload.get("channel", "")).strip()
                        if not channel:
                            raise ValueError("Choose a channel")
                        runtime.tune(channel)
                        message = f"Tuning to {channel}."
                    elif action == "normalization-review":
                        runtime.request_normalization_review(int(payload.get("asset_id", 0)))
                        message = "Playback will briefly stop; a verified original will be restored if available, then normalization review will reopen."
                    elif action == "maintenance":
                        started = runtime.request_maintenance()
                        message = (
                            "Maintenance started."
                            if started
                            else "Maintenance is already running."
                        )
                    elif action == "guide-refresh":
                        started = runtime.request_guide_refresh(
                            force=payload.get("force") is True
                        )
                        message = (
                            "Guide refresh started."
                            if started
                            else "A Guide refresh is already running."
                        )
                    elif action == "generated-cleanup":
                        started = runtime.request_generated_cleanup()
                        message = (
                            "Generated-file cleanup started."
                            if started
                            else "Generated-file cleanup is already running."
                        )
                    elif action == "database-backup":
                        started = runtime.request_database_backup()
                        message = (
                            "Verified database backup started."
                            if started
                            else "A database backup is already running."
                        )
                    elif action == "database-housekeeping":
                        started = runtime.request_database_housekeeping()
                        message = (
                            "Database housekeeping started."
                            if started
                            else "Stop the TV and wait for other maintenance to finish first."
                        )
                    elif action == "internet-cache-empty":
                        started = runtime.request_internet_cache_cleanup()
                        message = (
                            "Managed Internet Media cache cleanup started."
                            if started
                            else "Wait for the current download or cache cleanup to finish."
                        )
                    elif action == "database-compact":
                        started = runtime.request_database_compaction()
                        message = (
                            "Database compaction started."
                            if started
                            else "Stop the TV and wait for all background work to finish first."
                        )
                    elif action == "maintenance-window-skip":
                        skipped = runtime.skip_current_maintenance_window(start_tv=True)
                        message = (
                            "This maintenance window was skipped and television is starting."
                            if skipped
                            else "No maintenance window is currently active."
                        )
                    elif action == "normalization-batch":
                        started = runtime.request_normalization_batch()
                        message = (
                            "Approved-media conversion started."
                            if started
                            else "Approved-media conversion is already running."
                        )
                    elif action == "audio-analysis":
                        started = runtime.request_audio_analysis()
                        message = (
                            "Audio loudness analysis started."
                            if started
                            else "Audio loudness analysis is already running."
                        )
                    elif action == "audio-normalization":
                        started = runtime.request_audio_normalization()
                        message = (
                            "Approved audio normalization started."
                            if started
                            else "Audio normalization is already running."
                        )
                    elif action == "weather-restart":
                        started = runtime.restart_weather_companion()
                        message = (
                            "RetroWeather restart requested."
                            if started
                            else "RetroWeather is running independently and was left running."
                        )
                    elif action == "shutdown":
                        result = runtime.shutdown()
                        if not result["safe_to_remove"]:
                            self._json(
                                {
                                    "error": result["error"]
                                    or "CRTV Sim could not complete safe shutdown",
                                    "shutdown": result,
                                },
                                500,
                            )
                            return
                        message = (
                            "CRTV SIM IS FULLY STOPPED — all writing is finished. "
                            "It is now safe to turn off the computer. To unplug the external "
                            "drive, use Windows Eject/Safely Remove Hardware and disconnect "
                            "it after Windows confirms."
                        )
                        self._json(
                            {"ok": True, "message": message, "shutdown": result}
                        )
                        return
                    else:
                        self._json({"error": "Unknown runtime action"}, 404)
                        return
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True, "message": message})
                return
            block_parts = parsed.path.strip("/").split("/")
            if (
                len(block_parts) == 4
                and block_parts[:2] == ["api", "blocks"]
                and block_parts[3] in {"cues", "profile"}
            ):
                try:
                    asset_id = int(block_parts[2])
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    with connect_database(database_path) as connection:
                        service = BlockService(connection)
                        if block_parts[3] == "cues":
                            cues = payload.get("cues")
                            if not isinstance(cues, list):
                                raise ValueError("Cue sheet must be a JSON list")
                            saved = service.replace_cues(asset_id, cues)
                            response = {
                                "asset_id": asset_id,
                                "cue_count": len(saved),
                            }
                        else:
                            raw_probability = payload.get("probability_override")
                            probability = (
                                None
                                if raw_probability in (None, "")
                                else float(raw_probability)
                            )
                            service.update_profile(
                                asset_id,
                                general_title=str(payload.get("general_title", "")),
                                boundary_policy=str(payload.get("boundary_policy", "")),
                                probability_override=probability,
                            )
                            response = {"asset_id": asset_id, "saved": True}
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(response)
                return
            if parsed.path == "/api/schedule/generate":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    channel = str(payload.get("channel", "fox")).strip() or "fox"
                    target = date.fromisoformat(str(payload.get("date", "")))
                    reroll = payload.get("reroll") is True
                    with connect_database(database_path) as connection:
                        builder = ScheduleBuilder(connection, config)
                        refreshed: list[date] = []
                        locked: list[date] = []
                        if reroll:
                            result, refreshed, locked = builder.reroll_day(
                                channel,
                                target,
                                reroll_token=secrets.token_hex(16),
                            )
                        else:
                            result = builder.generate_day(channel, target)
                except (ValueError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(
                    {
                        "generation_id": result.generation_id,
                        "channel": result.channel_slug,
                        "date": result.schedule_date.isoformat(),
                        "entries": result.entry_count,
                        "programs": result.program_count,
                        "continuity_seconds": result.continuity_seconds,
                        "validation": result.validation_status,
                        "detail": result.validation_detail,
                        "rerolled": reroll,
                        "recurring_schedules_refreshed": len(refreshed),
                        "recurring_schedules_locked": len(locked),
                    }
                )
                return
            if parsed.path == "/api/schedule/preferences":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    channel = str(payload.get("channel", "")).strip()
                    if not channel:
                        raise ValueError("Choose a channel")
                    recurring = payload.get("recurring_enabled")
                    weekends = payload.get("unique_weekends")
                    presentation = payload.get("presentation_format", "standard")
                    if not isinstance(recurring, bool) or not isinstance(weekends, bool):
                        raise ValueError("Recurring schedule settings must be true or false")
                    if not isinstance(presentation, str):
                        raise ValueError("Presentation format must be text")
                    with connect_database(database_path) as connection:
                        preferences = ScheduleBuilder(
                            connection, config
                        ).update_schedule_preferences(
                            channel,
                            recurring_enabled=recurring,
                            unique_weekends=weekends,
                            presentation_format=presentation,
                        )
                except (ValueError, json.JSONDecodeError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"channel": channel, "schedules_stale": True, **preferences})
                return
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "assets"]:
                self._json({"error": "Not found"}, 404)
                return
            try:
                asset_id = int(parts[2])
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "Invalid request"}, 400)
                return
            if parts[3] == "commercial-review":
                try:
                    action = str(payload.get("action", ""))
                    with connect_database(database_path) as connection:
                        service = CommercialReviewService(connection, config)
                        if action in {"approve", "disable"}:
                            service.decide(asset_id, "approved" if action == "approve" else "disabled")
                            response = {"asset_id": asset_id, "decision": action,
                                        "schedules_stale": True}
                        elif action == "move":
                            destination = service.move_to_category(
                                asset_id, str(payload.get("category", ""))
                            )
                            response = {"asset_id": asset_id, "moved_to": destination,
                                        "schedules_stale": True}
                        else:
                            raise ValueError("Unknown commercial review action")
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(response)
                return
            if parts[3] == "breakpoint":
                action = payload.get("action")
                try:
                    with connect_database(database_path) as connection:
                        service = BreakpointAnalyzer(connection, config)
                        if action == "analyze":
                            result = service.analyze_asset(asset_id)
                        elif action == "manual":
                            result = service.set_manual(
                                asset_id, int(payload.get("offset_ms")),
                                int(payload.get("target_index", 1)),
                            )
                        elif action == "reset":
                            result = service.reset_manual(asset_id, int(payload.get("target_index")))
                        else:
                            raise ValueError("Unknown breakpoint action")
                    response = {
                        "asset_id": result.asset_id,
                        "status": result.status,
                        "offset_ms": result.offset_ms,
                        "method": result.method,
                        "confidence": result.confidence,
                    }
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(response)
                return

            if parts[3] == "audio-normalization":
                action = str(payload.get("action", ""))
                try:
                    with connect_database(database_path) as connection:
                        service = AudioNormalizationService(connection, config)
                        if action == "analyze":
                            result = service.analyze_asset(asset_id)
                            response = {
                                "asset_id": asset_id,
                                "analysis_status": result.status,
                                "measured_i": result.measured_i,
                                "needs_correction": result.needs_correction,
                            }
                        elif action in {"retry", "skip"}:
                            service.resolve_review(asset_id, action)
                            response = {"asset_id": asset_id, "action": action}
                        else:
                            raise ValueError("Unknown audio normalization action")
                except (
                    ValueError,
                    RuntimeError,
                    OSError,
                    subprocess.SubprocessError,
                ) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(response)
                return

            if parts[3] == "normalization":
                action = payload.get("action")
                try:
                    with connect_database(database_path) as connection:
                        service = NormalizationService(connection, config)
                        if action == "analyze":
                            result = service.analyze_asset(asset_id)
                            response = {
                                "asset_id": asset_id,
                                "analysis_status": result.status,
                                "detected_mode": result.detected_mode,
                                "confidence": result.confidence,
                            }
                        elif action == "source-preview":
                            source = service.create_source_preview(asset_id)
                            response = {
                                "asset_id": asset_id,
                                "source": source.name,
                            }
                        elif action == "draft-preview":
                            mode = str(payload.get("mode", ""))
                            if mode not in MODES or mode == "auto":
                                raise ValueError("Choose a specific correction to preview")
                            crop = None
                            raw_crop = payload.get("crop")
                            if raw_crop is not None:
                                if not isinstance(raw_crop, dict):
                                    raise ValueError("Crop must contain W, H, X, and Y")
                                try:
                                    crop = Crop(
                                        int(raw_crop["width"]),
                                        int(raw_crop["height"]),
                                        int(raw_crop["x"]),
                                        int(raw_crop["y"]),
                                    )
                                except (KeyError, TypeError, ValueError) as exc:
                                    raise ValueError(
                                        "Crop must contain whole-number W, H, X, and Y"
                                    ) from exc
                            result = service.create_corrected_preview(
                                asset_id,
                                mode,
                                crop=crop,
                                frame_count=(
                                    3 if int(payload.get("frame_count", 1)) == 3 else 1
                                ),
                            )
                            response = {
                                "asset_id": asset_id,
                                "token": result["token"],
                                "mode": result["mode"],
                                "crop": result["crop"],
                                "source": Path(result["source"]).name,
                                "normalized": Path(result["normalized"]).name,
                                "frame_count": result["frame_count"],
                                "frames": result["frames"],
                            }
                        elif action == "approve-preview":
                            mode = service.approve_preview(
                                asset_id, str(payload.get("token", ""))
                            )
                            response = {
                                "asset_id": asset_id,
                                "analysis_status": "analyzed",
                                "override_mode": mode,
                            }
                        elif action == "approve-preview-folder":
                            response = service.approve_folder_preview(
                                asset_id, str(payload.get("token", ""))
                            )
                        elif action == "override":
                            mode = str(payload.get("mode", ""))
                            if mode not in MODES:
                                raise ValueError("Invalid web override mode")
                            crop = None
                            raw_crop = payload.get("crop")
                            if raw_crop is not None:
                                if not isinstance(raw_crop, dict):
                                    raise ValueError("Crop must contain W, H, X, and Y")
                                try:
                                    crop = Crop(
                                        int(raw_crop["width"]),
                                        int(raw_crop["height"]),
                                        int(raw_crop["x"]),
                                        int(raw_crop["y"]),
                                    )
                                except (KeyError, TypeError, ValueError) as exc:
                                    raise ValueError(
                                        "Crop must contain whole-number W, H, X, and Y"
                                    ) from exc
                            service.set_override(asset_id, mode, crop=crop)
                            response = {"asset_id": asset_id, "override_mode": mode}
                        elif action == "approve":
                            mode = service.approve_review(asset_id)
                            response = {
                                "asset_id": asset_id,
                                "analysis_status": "analyzed",
                                "override_mode": mode,
                            }
                        elif action == "approve-folder":
                            mode = str(payload.get("mode", ""))
                            if mode not in MODES:
                                raise ValueError("Invalid folder override mode")
                            crop = None
                            raw_crop = payload.get("crop")
                            if raw_crop is not None:
                                if not isinstance(raw_crop, dict):
                                    raise ValueError("Crop must contain W, H, X, and Y")
                                try:
                                    crop = Crop(
                                        int(raw_crop["width"]),
                                        int(raw_crop["height"]),
                                        int(raw_crop["x"]),
                                        int(raw_crop["y"]),
                                    )
                                except (KeyError, TypeError, ValueError) as exc:
                                    raise ValueError(
                                        "Crop must contain whole-number W, H, X, and Y"
                                    ) from exc
                            response = service.approve_folder_review(
                                asset_id, mode, crop=crop
                            )
                        elif action == "preview":
                            profile = connection.execute(
                                """
                                SELECT analysis_status
                                FROM normalization_profiles
                                WHERE asset_id = ?
                                """,
                                (asset_id,),
                            ).fetchone()
                            if profile is None or profile["analysis_status"] == "pending":
                                service.analyze_asset(asset_id)
                            source, normalized = service.create_preview(asset_id)
                            response = {
                                "asset_id": asset_id,
                                "source": source.name,
                                "normalized": normalized.name,
                            }
                        elif action == "normalize":
                            raise ValueError(
                                "Standalone playback corrections are no longer supported; "
                                "use Replace + backup so validated staging is installed and removed"
                            )
                        elif action == "replace":
                            if payload.get("confirm_replace") is not True:
                                raise ValueError("Replacement requires confirmation")
                            result = PromotionService(
                                connection, config
                            ).replace_with_backup(asset_id)
                            response = {
                                "asset_id": asset_id,
                                "proxy_status": "promoted",
                                "source": str(result.source_path),
                                "backup": str(result.backup_path),
                            }
                        else:
                            self._json(
                                {"error": "Unknown normalization action"}, 400
                            )
                            return
                except (
                    ValueError,
                    RuntimeError,
                    OSError,
                    subprocess.SubprocessError,
                ) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(response)
                return

            if parts[3] != "review":
                self._json({"error": "Not found"}, 404)
                return
            action = payload.get("action")
            if action == "forget":
                try:
                    with connect_database(database_path) as connection:
                        detached = forget_missing_asset_record(
                            connection, config, asset_id
                        )
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(
                    {
                        "asset_id": asset_id,
                        "forgotten": True,
                        "detached_schedule_entries": detached,
                    }
                )
                return
            mapping = {
                "approve": ("ready", 1),
                "ignore": ("ignored", 0),
                "disable": ("open", 0),
            }
            if action not in mapping:
                self._json({"error": "Unknown review action"}, 400)
                return
            review_status, enabled = mapping[action]
            now = utc_now()
            with connect_database(database_path) as connection:
                if connection.execute(
                    """SELECT 1 FROM commercial_duration_reviews
                       WHERE asset_id=? AND decision='pending'""", (asset_id,)
                ).fetchone():
                    self._json({"error": "Use the Long commercials review actions for this file"}, 400)
                    return
                cursor = connection.execute(
                    """
                    UPDATE assets SET review_status = ?,
                        enabled_for_scheduling = ?
                    WHERE id = ?
                    """,
                    (review_status, enabled, asset_id),
                )
                if cursor.rowcount == 0:
                    self._json({"error": "Asset not found"}, 404)
                    return
                connection.execute(
                    """
                    UPDATE review_items SET status = 'resolved', resolved_at = ?
                    WHERE asset_id = ? AND status = 'open'
                    """,
                    (now, asset_id),
                )
                connection.commit()
            self._json(
                {
                    "asset_id": asset_id,
                    "review_status": review_status,
                    "enabled_for_scheduling": bool(enabled),
                }
            )

        def log_message(self, format: str, *args: object) -> None:
            print(f"[web] {self.address_string()} - {format % args}")

    return Handler


def serve(
    config: AppConfig,
    *,
    host: str,
    port: int,
    runtime=None,
    before_serve=None,
) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(config, runtime))
    label = "application dashboard" if runtime is not None else "library interface"
    print(f"CRTV Sim {label}: http://{host}:{port}")
    stop_monitor = None
    try:
        if before_serve is not None:
            before_serve()
        if runtime is not None:
            def stop_server_when_runtime_stops():
                runtime.stop_event.wait()
                runtime.shutdown_complete_event.wait()
                # Let the request handler finish sending the final safe/error
                # response before the local server disappears.
                time.sleep(2.0)
                server.shutdown()

            stop_monitor = threading.Thread(
                target=stop_server_when_runtime_stops,
                name="retrotv-web-stop-monitor",
                daemon=True,
            )
            stop_monitor.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

