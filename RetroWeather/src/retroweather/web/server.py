from __future__ import annotations

import json
import os
import copy
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from retroweather import __version__
from retroweather.assets import scan_asset_library
from retroweather.config import (
    DEFAULT_SETTINGS,
    AppPaths,
    ConfigError,
    load_settings,
    save_settings,
)
from retroweather.locations import load_selected_location, save_selected_location
from retroweather.models import NormalizedWeatherSnapshot, SnapshotValidationError
from retroweather.planning import PlanError, build_forecast_plan
from retroweather.providers.open_meteo import (
    GEOCODING_ATTRIBUTION,
    LocationQueryError,
    OpenMeteoGeocodingClient,
    ProviderRequestError,
    ProviderResponseError,
)
from retroweather.providers.open_meteo_weather import OpenMeteoForecastClient
from retroweather.rendering import (
    RenderError,
    inventory,
    load_active_channel,
)
from retroweather.rendering.music import music_settings
from retroweather.rendering.design import COLOR_PATTERN, appearance_settings
from retroweather.weather import build_weather_status
from retroweather.web.jobs import ChannelJobManager, JobBusyError
from retroweather.web.scheduler import RefreshScheduler


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RetroWeather</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #081426; color: #edf6ff; }
    main { width: min(980px, calc(100% - 32px)); margin: 32px auto 48px; }
    h1 { letter-spacing: .08em; text-transform: uppercase; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; }
    .card { padding: 20px; border: 1px solid #315173; border-radius: 10px; background: #10243b; }
    .section-title { margin: 34px 0 12px; font-size: 1.2rem; color: #edf6ff; scroll-margin-top: 82px; }
    .section-note { margin: -6px 0 14px; color: #9fc4e8; }
    .section-nav { position: sticky; top: 0; z-index: 5; display: flex; gap: 6px; overflow-x: auto; margin: 20px 0; padding: 8px; border: 1px solid #315173; border-radius: 9px; background: #081426ee; backdrop-filter: blur(8px); }
    .section-nav a { flex: 0 0 auto; padding: 8px 11px; border-radius: 6px; color: #cde7ff; text-decoration: none; font-size: .84rem; }
    .section-nav a:hover, .section-nav a:focus { background: #315173; }
    .setup-progress { margin: 18px 0; }
    .checklist { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 14px; }
    .check { padding: 12px; border: 1px solid #315173; border-radius: 7px; background: #0b1d31; }
    .check.done { border-color: #3e9f72; }
    .check strong { display: block; margin-bottom: 4px; }
    .settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
    #appearance-form, #automation-form { display: block; }
    .field { display: grid; gap: 6px; }
    .field label { color: #b8d8ec; font-size: .86rem; font-weight: 650; }
    .field small { color: #82a9c2; }
    .field input, .field select { width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid #52769d; border-radius: 6px; background: #081426; color: #edf6ff; }
    .field input[type="color"] { min-height: 42px; padding: 4px; }
    .form-message { min-height: 1.3em; margin-top: 12px; color: #9fc4e8; }
    .form-message.success { color: #7ee2a8; }
    .form-message.error { color: #ffb4a8; }
    .provider-card { display: flex; justify-content: space-between; gap: 18px; align-items: center; padding: 14px; border: 1px solid #315173; border-radius: 7px; background: #0b1d31; }
    .home-card { grid-column: 1 / -1; }
    .location { margin: 22px 0; }
    .label { color: #9fc4e8; font-size: .8rem; text-transform: uppercase; letter-spacing: .08em; }
    .value { margin-top: 8px; font-size: 1.15rem; overflow-wrap: anywhere; }
    .ready { color: #7ee2a8; }
    form { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    input { flex: 1 1 280px; padding: 10px 12px; border: 1px solid #52769d; border-radius: 6px; background: #081426; color: #edf6ff; }
    button { padding: 10px 16px; border: 0; border-radius: 6px; background: #63b3ed; color: #07111f; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .55; cursor: wait; }
    #location-results { list-style: none; padding: 0; margin: 14px 0 0; }
    #location-results li { display: flex; gap: 12px; justify-content: space-between; align-items: center; padding: 12px 0; border-top: 1px solid #315173; }
    .weather { margin: 22px 0; }
    .timeline { margin: 22px 0; }
    .timeline-bar { display: flex; min-height: 58px; margin-top: 16px; overflow: hidden; border: 1px solid #52769d; border-radius: 7px; }
    .timeline-segment { display: grid; place-items: center; min-width: 34px; padding: 5px; border-right: 1px solid #081426; background: #315173; color: #edf6ff; font-size: .72rem; text-align: center; }
    .timeline-segment:nth-child(even) { background: #23425f; }
    .timeline-segment:last-child { border-right: 0; }
    .channel-preview { margin: 22px 0; }
    .asset-library { margin: 22px 0; }
    .asset-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-top: 14px; }
    .asset-stat { padding: 10px; border: 1px solid #315173; border-radius: 6px; background: #0b1d31; }
    .asset-valid { color: #7ee2a8; }
    .asset-invalid { color: #ffb4a8; }
    .music-controls { display: grid; grid-template-columns: auto minmax(180px, 1fr) auto; align-items: center; gap: 10px 14px; margin-top: 16px; padding: 14px; border: 1px solid #315173; border-radius: 7px; background: #0b1d31; }
    .music-controls input[type="checkbox"] { flex: none; width: 18px; margin: 0; padding: 0; }
    .music-controls input[type="range"] { width: 100%; margin: 0; padding: 0; }
    .music-controls .music-message { grid-column: 1 / -1; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    .secondary { background: #315173; color: #edf6ff; }
    .job-panel { margin-top: 16px; padding: 14px; border: 1px solid #315173; border-radius: 7px; background: #0b1d31; }
    progress { width: 100%; height: 15px; margin: 10px 0 4px; accent-color: #63b3ed; }
    .job-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 12px; }
    details.inspector { margin-top: 16px; }
    summary { cursor: pointer; color: #9fc4e8; font-weight: 700; }
    pre { max-height: 420px; overflow: auto; padding: 14px; border-radius: 7px; background: #06101d; color: #cde7ff; font-size: .78rem; white-space: pre-wrap; overflow-wrap: anywhere; }
    video { display: block; width: min(640px, 100%); aspect-ratio: 4 / 3; margin: 16px auto 0; background: #050a12; border: 1px solid #52769d; }
    .weather-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
    .weather-current { font-size: 2rem; margin: 14px 0 4px; }
    .table-wrap { overflow-x: auto; margin-top: 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 9px 10px; border-bottom: 1px solid #315173; text-align: left; white-space: nowrap; }
    th { color: #9fc4e8; font-size: .76rem; text-transform: uppercase; letter-spacing: .06em; }
    h2 { margin: 22px 0 8px; font-size: 1rem; text-transform: uppercase; letter-spacing: .08em; color: #9fc4e8; }
    [hidden] { display: none !important; }
    .muted { color: #9fc4e8; }
    .warning { margin-top: 14px; padding: 11px 13px; border: 1px solid #d8a64c; border-radius: 6px; background: #3b2b10; color: #ffe1a6; }
    .success-notice { margin-top: 14px; padding: 11px 13px; border: 1px solid #3e9f72; border-radius: 6px; background: #123626; color: #a9f5ca; }
    @media (max-width: 640px) { .settings-grid, .checklist { grid-template-columns: 1fr; } main { width: min(100% - 20px, 980px); } }
    footer { margin-top: 28px; color: #9fc4e8; }
  </style>
</head>
<body>
<main>
  <h1>RetroWeather</h1>
  <p>Your local forecast channel setup, preview, media, and automation controls.</p>
  <nav class="section-nav" aria-label="Dashboard sections">
    <a href="#setup">Setup</a><a href="#current-weather">Current weather</a>
    <a href="#channel-preview-section">Channel</a><a href="#appearance-media">Appearance &amp; media</a>
    <a href="#automation">Automation</a><a href="#diagnostics">Diagnostics</a>
  </nav>
  <section class="card setup-progress" aria-labelledby="setup-progress-title">
    <div class="label" id="setup-progress-title">Getting started</div>
    <div class="value" id="setup-progress-summary">Checking your channel…</div>
    <div class="checklist">
      <div class="check" id="check-location"><strong>1. Choose location</strong><span>Select your city or postal code.</span></div>
      <div class="check" id="check-weather"><strong>2. Fetch weather</strong><span>Download current and forecast data.</span></div>
      <div class="check" id="check-channel"><strong>3. Create channel</strong><span>Render the first playable broadcast.</span></div>
    </div>
    <div class="actions"><button id="setup-build" type="button" disabled>Fetch weather + create channel</button></div>
    <div class="form-message" id="setup-message">Choose a location to begin.</div>
  </section>
  <h2 class="section-title" id="setup">Setup</h2>
  <p class="section-note">Choose the forecast location and basic channel identity. No configuration-file editing is required.</p>
  <section class="card location">
    <div class="label">Weather location</div>
    <div class="value" id="selected-location">No location selected</div>
    <form id="location-form">
      <input id="location-query" minlength="2" required placeholder="City or postal code" aria-label="City or postal code">
      <button id="location-search" type="submit">Search</button>
    </form>
    <div class="muted" id="location-message">Search results provided by Open-Meteo and GeoNames.</div>
    <ul id="location-results"></ul>
  </section>
  <section class="card">
    <div class="label">Channel basics</div>
    <form id="general-settings-form" class="settings-grid">
      <div class="field"><label for="channel-name">Channel display name</label><input id="channel-name" maxlength="40" required><small>Used on station screens and in the manifest.</small></div>
      <div class="field"><label for="units-system">Weather units</label><select id="units-system"><option value="imperial">Imperial (°F, mph)</option><option value="metric">Metric (°C, km/h)</option></select><small>Applies after the next weather fetch.</small></div>
      <div class="actions"><button type="submit">Save channel settings</button><button class="secondary" id="general-reset" type="button">Restore defaults</button></div>
    </form>
    <div class="form-message" id="general-settings-message"></div>
  </section>
  <h2 class="section-title" id="current-weather">Current weather</h2>
  <p class="section-note">Live provider data remains visible even if a later refresh fails.</p>
  <section class="card weather">
    <div class="weather-head">
      <div><div class="label">Live weather</div><div class="value" id="weather-location">No weather fetched</div></div>
      <button id="weather-fetch" type="button">Fetch weather now</button>
    </div>
    <div id="weather-empty" class="muted">Select a location, then fetch its forecast.</div>
    <div id="weather-warning" class="warning" hidden></div>
    <div id="weather-content" hidden>
      <div class="weather-current" id="weather-current"></div>
      <div class="muted" id="weather-details"></div>
      <h2>Hourly outlook</h2>
      <div class="table-wrap"><table><thead><tr><th>Time</th><th>Temp</th><th>Rain</th><th>Wind</th></tr></thead><tbody id="hourly-rows"></tbody></table></div>
      <h2>Seven-day forecast</h2>
      <div class="table-wrap"><table><thead><tr><th>Day</th><th>High</th><th>Low</th><th>Rain</th></tr></thead><tbody id="daily-rows"></tbody></table></div>
      <div class="muted" id="weather-attribution"></div>
    </div>
  </section>
  <h2 class="section-title" id="channel-preview-section">Channel preview</h2>
  <p class="section-note">Build, validate, and inspect the exact Weather channel CRTV Sim plays.</p>
  <section class="card timeline">
    <div class="label">Planned channel loop</div>
    <div class="value" id="plan-summary">Waiting for weather data</div>
    <div class="timeline-bar" id="plan-timeline" hidden></div>
    <div class="table-wrap" id="plan-table-wrap" hidden><table><thead><tr><th>Starts</th><th>Duration</th><th>Guide title</th></tr></thead><tbody id="plan-rows"></tbody></table></div>
  </section>
  <section class="card channel-preview">
    <div class="weather-head">
      <div><div class="label">Published channel</div><div class="value" id="channel-summary">No rendered channel yet</div></div>
    </div>
    <div class="muted" id="channel-message">Rendering creates and validates the complete two-minute broadcast.</div>
    <div class="actions">
      <button id="channel-render" type="button">Render now</button>
      <button id="channel-fetch-render" type="button">Fetch + render</button>
      <button class="secondary" id="output-open" type="button">Open output folder</button>
    </div>
    <div class="job-panel">
      <div class="label">Render progress</div>
      <div class="value" id="job-stage">Ready</div>
      <progress id="job-progress" max="100" value="0"></progress>
      <div class="muted" id="job-percent">0%</div>
      <div class="job-grid">
        <div><div class="label">Active generation</div><div id="active-generation">None</div></div>
        <div><div class="label">Validation result</div><div id="validation-result">No completed validation yet</div></div>
        <div><div class="label">Last successful publication</div><div id="last-publication">None</div></div>
        <div><div class="label">Active soundtrack</div><div id="active-music">Silence</div></div>
        <div><div class="label">Automatic refresh</div><div id="scheduler-state">Starting</div></div>
        <div><div class="label">Next weather fetch</div><div id="scheduler-fetch">Calculating</div></div>
        <div><div class="label">Next channel render</div><div id="scheduler-render">Calculating</div></div>
      </div>
      <div class="warning" id="job-error" hidden></div>
    </div>
    <video id="channel-video" controls preload="metadata" hidden></video>
    <details class="inspector" id="theme-inspector">
      <summary>Inspect screen theme assignments</summary>
      <div class="table-wrap"><table><thead><tr><th>Screen</th><th>Requested theme</th><th>Selected artwork</th></tr></thead><tbody id="theme-rows"></tbody></table></div>
    </details>
    <details class="inspector" id="normalized-inspector">
      <summary>Inspect normalized weather data</summary>
      <pre id="normalized-json">Loading normalized snapshot…</pre>
    </details>
  </section>
  <h2 class="section-title" id="appearance-media">Appearance &amp; media</h2>
  <p class="section-note">Adjust the forecast design and manage backgrounds, holiday borders, and soundtrack assets.</p>
  <section class="card">
    <div class="label">Forecast appearance</div>
    <form id="appearance-form">
      <div class="settings-grid">
        <div class="field"><label for="panel-opacity">Panel opacity <span id="panel-opacity-value">58%</span></label><input id="panel-opacity" type="range" min="25" max="90" step="1"><small>Higher values improve contrast; lower values reveal more background.</small></div>
        <div class="field"><label for="text-scale">Text scale <span id="text-scale-value">100%</span></label><input id="text-scale" type="range" min="85" max="115" step="1"><small>Long text still fits automatically.</small></div>
        <div class="field"><label for="safe-margin">TV safe margin</label><input id="safe-margin" type="number" min="20" max="40" step="1"><small>Extra edge clearance for CRT overscan.</small></div>
        <div class="field"><label for="palette-accent">Accent color</label><input id="palette-accent" type="color"></div>
        <div class="field"><label for="palette-condition">Condition highlight</label><input id="palette-condition" type="color"></div>
        <div class="field"><label for="palette-header">Header color</label><input id="palette-header" type="color"></div>
      </div>
      <div class="actions"><button type="submit">Save appearance</button><button class="secondary" id="appearance-reset" type="button">Restore appearance defaults</button></div>
    </form>
    <div class="form-message" id="appearance-message">Changes apply to the next render.</div>
  </section>
  <section class="card asset-library">
    <div class="weather-head">
      <div><div class="label">Asset library</div><div class="value" id="asset-summary">Scanning media folders...</div></div>
      <button class="secondary" id="media-open" type="button">Open media folder</button>
    </div>
    <div class="muted" id="asset-message">Supported files are detected automatically; no server restart is required.</div>
    <div class="asset-stats" id="asset-stats"></div>
    <div class="settings-grid" id="holiday-overlay-readiness" aria-label="Seasonal overlay readiness"></div>
    <p class="section-note">Upload or generate backgrounds and holiday layers in CRTV Sim's Library → Weather Graphics section. Linked holiday dates follow CRTV Sim Settings.</p>
    <div class="music-controls">
      <input id="music-enabled" type="checkbox">
      <label for="music-enabled">Enable soundtrack</label>
      <span></span>
      <label for="music-volume">Music volume</label>
      <input id="music-volume" type="range" min="0" max="100" step="1" value="28">
      <span id="music-volume-value">28%</span>
      <button id="music-save" type="button">Save music settings</button>
      <div class="muted music-message" id="music-message">Changes apply to the next render.</div>
    </div>
    <details class="inspector">
      <summary>Inspect backgrounds, music, and holiday assets</summary>
      <div class="table-wrap"><table><thead><tr><th>File</th><th>Role</th><th>Type</th><th>Dimensions / duration</th><th>Status</th></tr></thead><tbody id="asset-rows"></tbody></table></div>
    </details>
  </section>
  <h2 class="section-title" id="automation">Automation &amp; provider</h2>
  <p class="section-note">Control update timing. RetroWeather keeps the last valid channel during outages.</p>
  <section class="card">
    <div class="provider-card"><div><div class="label">Weather provider</div><div class="value">Open-Meteo</div><div class="muted">No account or API key required.</div></div><div class="ready">Configured</div></div>
    <form id="automation-form">
      <div class="settings-grid">
        <div class="field"><label for="refresh-automatic">Automatic updates</label><select id="refresh-automatic"><option value="true">On</option><option value="false">Off</option></select></div>
        <div class="field"><label for="refresh-data">Fetch weather every (minutes)</label><input id="refresh-data" type="number" min="5" max="180" step="1"></div>
        <div class="field"><label for="refresh-render">Render channel every (minutes)</label><input id="refresh-render" type="number" min="5" max="240" step="1"></div>
        <div class="field"><label for="refresh-stale">Warn when data is older than (minutes)</label><input id="refresh-stale" type="number" min="15" max="1440" step="1"></div>
      </div>
      <div class="actions"><button type="submit">Save automation</button><button class="secondary" id="automation-reset" type="button">Restore automation defaults</button></div>
    </form>
    <div class="form-message" id="automation-message"></div>
  </section>
  <h2 class="section-title" id="diagnostics">Diagnostics</h2>
  <p class="section-note">Technical health and storage details for troubleshooting.</p>
  <section class="grid">
    <div class="card"><div class="label">Application</div><div class="value ready" id="ready">Loading…</div></div>
    <div class="card"><div class="label">Weather data</div><div class="value" id="weather">Loading…</div></div>
    <div class="card"><div class="label">Published channel</div><div class="value" id="channel">Loading…</div></div>
    <div class="card"><div class="label">Condition backgrounds</div><div class="value" id="artwork">Loading…</div></div>
    <div class="card home-card"><div class="label">Home</div><div class="value" id="home">Loading…</div></div>
  </section>
  <footer id="version"></footer>
</main>
<script>
function showSelected(selection) {
  document.getElementById('selected-location').textContent = selection
    ? `${selection.location.display_name} · ${selection.location.timezone}`
    : 'No location selected';
}
function weatherUnits(snapshot) {
  return snapshot.units === 'imperial'
    ? {temp: '°F', wind: 'mph', precipitation: 'in', visibility: 'mi'}
    : {temp: '°C', wind: 'km/h', precipitation: 'mm', visibility: 'km'};
}
function number(value, suffix, digits = 0) {
  return value == null ? '—' : `${Number(value).toFixed(digits)}${suffix}`;
}
function showWeather(snapshot) {
  const units = weatherUnits(snapshot);
  document.getElementById('weather-empty').hidden = true;
  document.getElementById('weather-content').hidden = false;
  document.getElementById('weather-location').textContent = snapshot.location.display_name;
  document.getElementById('weather-current').textContent = `${number(snapshot.current.temperature, units.temp)} · ${snapshot.current.condition_text}`;
  document.getElementById('weather-details').textContent = `Feels like ${number(snapshot.current.apparent_temperature, units.temp)} · Humidity ${number(snapshot.current.humidity, '%')} · Wind ${number(snapshot.current.wind_speed, ` ${units.wind}`, 1)} · Updated ${new Date(snapshot.metadata.fetched_at).toLocaleString()}`;
  const hourly = document.getElementById('hourly-rows'); hourly.replaceChildren();
  for (const period of snapshot.hourly.slice(0, 12)) {
    const row = document.createElement('tr');
    for (const text of [new Date(period.starts_at).toLocaleTimeString([], {hour:'numeric'}), number(period.temperature, units.temp), number(period.precipitation_probability, '%'), number(period.wind_speed, ` ${units.wind}`, 1)]) {
      const cell = document.createElement('td'); cell.textContent = text; row.appendChild(cell);
    }
    hourly.appendChild(row);
  }
  const daily = document.getElementById('daily-rows'); daily.replaceChildren();
  for (const period of snapshot.daily) {
    const row = document.createElement('tr');
    const day = new Date(`${period.date}T12:00:00`).toLocaleDateString([], {weekday:'short', month:'short', day:'numeric'});
    for (const text of [day, number(period.high_temperature, units.temp), number(period.low_temperature, units.temp), number(period.precipitation_probability, '%')]) {
      const cell = document.createElement('td'); cell.textContent = text; row.appendChild(cell);
    }
    daily.appendChild(row);
  }
  document.getElementById('weather-attribution').textContent = snapshot.metadata.attribution.join(' · ');
}
function showWeatherStatus(status) {
  const warning = document.getElementById('weather-warning');
  const messages = [];
  if (status.location_mismatch) messages.push('The saved forecast belongs to the previously selected location. Fetch again to update it.');
  if (status.stale && status.age_seconds != null) messages.push(`Forecast data is stale (${Math.floor(status.age_seconds / 60)} minutes old).`);
  if (status.state === 'failed' && status.error) messages.push(`Last refresh failed: ${status.error}`);
  if (status.snapshot_error) messages.push(`Stored forecast is invalid: ${status.snapshot_error}`);
  warning.textContent = messages.join(' ');
  warning.hidden = messages.length === 0;
  if (status.available && status.age_seconds != null) {
    document.getElementById('weather').textContent = `Snapshot available · ${Math.floor(status.age_seconds / 60)} min old`;
  }
}
async function loadWeather() {
  const response = await fetch('/api/weather');
  if (response.status === 404) return;
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || 'Unable to load weather');
  showWeather(payload);
}
async function loadPlan() {
  const response = await fetch('/api/plan');
  if (response.status === 404) return;
  const plan = await response.json();
  if (!response.ok) throw new Error(plan.error || 'Unable to build channel plan');
  document.getElementById('plan-summary').textContent = `${plan.segments.length} segments · ${Math.floor(plan.duration_seconds / 60)}:${String(plan.duration_seconds % 60).padStart(2, '0')} total`;
  const timeline = document.getElementById('plan-timeline'); timeline.replaceChildren(); timeline.hidden = false;
  const rows = document.getElementById('plan-rows'); rows.replaceChildren(); document.getElementById('plan-table-wrap').hidden = false;
  for (const segment of plan.segments) {
    const block = document.createElement('div'); block.className = 'timeline-segment'; block.style.flexGrow = segment.duration_seconds; block.title = `${segment.title}: ${segment.duration_seconds} seconds`; block.textContent = segment.title; timeline.appendChild(block);
    const row = document.createElement('tr');
    const seconds = segment.start_seconds % 60;
    const offset = `${Math.floor(segment.start_seconds / 60)}:${seconds < 10 ? '0' : ''}${seconds}`;
    for (const text of [offset, `${segment.duration_seconds}s`, segment.title]) {
      const cell = document.createElement('td'); cell.textContent = text; row.appendChild(cell);
    }
    rows.appendChild(row);
  }
}
async function loadChannel() {
  const response = await fetch('/api/channel');
  if (response.status === 404) return;
  const channel = await response.json();
  if (!response.ok) throw new Error(channel.error || 'Unable to load published channel');
  document.getElementById('channel-summary').textContent = `${channel.channel.display_name} · ${Number(channel.duration_seconds).toFixed(1)} seconds`;
  const themes = [...new Set(channel.segments.map(segment => segment.condition_theme))];
  const seasonal = channel.render_metadata && channel.render_metadata.seasonal;
  const seasonalLabel = seasonal && seasonal.theme
    ? ` · Seasonal: ${seasonal.theme[0].toUpperCase() + seasonal.theme.slice(1)}${seasonal.overlay ? ` (${seasonal.overlay})` : ' palette only'}`
    : '';
  document.getElementById('channel-message').textContent = `Published ${new Date(channel.generated_at).toLocaleString()} · Themes: ${themes.join(', ')}${seasonalLabel}`;
  const themeRows = document.getElementById('theme-rows'); themeRows.replaceChildren();
  for (const segment of channel.segments) {
    const row = document.createElement('tr');
    for (const text of [segment.title, segment.condition_theme, segment.background || 'Built-in fallback']) {
      const cell = document.createElement('td'); cell.textContent = text; row.appendChild(cell);
    }
    themeRows.appendChild(row);
  }
  document.getElementById('active-generation').textContent = channel.generation_id;
  const music = channel.render_metadata && channel.render_metadata.music;
  document.getElementById('active-music').textContent = music && music.file
    ? `${music.file} · ${Math.round(Number(music.volume) * 100)}% · ${music.source}`
    : 'Silence';
  const video = document.getElementById('channel-video');
  video.src = `/channel/video?generation=${encodeURIComponent(channel.generation_id)}`;
  video.hidden = false;
  document.getElementById('channel').textContent = 'Published';
}
async function loadNormalized() {
  const response = await fetch('/api/weather');
  const payload = await response.json();
  document.getElementById('normalized-json').textContent = response.ok
    ? JSON.stringify(payload, null, 2)
    : (payload.error || 'No normalized weather snapshot is available.');
}
let handledGeneration = null;
let handledCompletion = null;
let jobPoll = null;
let musicSettingsDirty = false;
let dashboardSettingsDirty = false;
let lastDashboardStatus = null;
function setFormMessage(id, message, kind = '') {
  const element = document.getElementById(id);
  element.textContent = message; element.className = `form-message ${kind}`.trim();
}
function colorValue(value) { return `#${String(value || '0x000000').slice(-6)}`; }
function showDashboardSettings(settings) {
  if (!settings || dashboardSettingsDirty) return;
  document.getElementById('channel-name').value = settings.channel_name;
  document.getElementById('units-system').value = settings.units;
  const appearance = settings.appearance;
  document.getElementById('panel-opacity').value = Math.round(appearance.panel_opacity * 100);
  document.getElementById('panel-opacity-value').textContent = `${Math.round(appearance.panel_opacity * 100)}%`;
  document.getElementById('text-scale').value = Math.round(appearance.text_scale * 100);
  document.getElementById('text-scale-value').textContent = `${Math.round(appearance.text_scale * 100)}%`;
  document.getElementById('safe-margin').value = appearance.safe_margin;
  document.getElementById('palette-accent').value = colorValue(appearance.palette.accent);
  document.getElementById('palette-condition').value = colorValue(appearance.palette.condition);
  document.getElementById('palette-header').value = colorValue(appearance.palette.header);
  document.getElementById('refresh-automatic').value = String(settings.refresh.automatic);
  document.getElementById('refresh-data').value = settings.refresh.data_minutes;
  document.getElementById('refresh-render').value = settings.refresh.render_minutes;
  document.getElementById('refresh-stale').value = settings.refresh.stale_after_minutes;
}
function showSetupProgress(status) {
  const hasLocation = Boolean(status.selected_location);
  const hasWeather = Boolean(status.weather_snapshot);
  const hasChannel = Boolean(status.published_channel);
  for (const [id, done] of [['check-location', hasLocation], ['check-weather', hasWeather], ['check-channel', hasChannel]]) {
    document.getElementById(id).classList.toggle('done', done);
  }
  const complete = hasLocation && hasWeather && hasChannel;
  document.getElementById('setup-progress-summary').textContent = complete
    ? 'Your Weather channel is ready for daily use.'
    : `${Number(hasLocation) + Number(hasWeather) + Number(hasChannel)} of 3 setup steps complete`;
  const button = document.getElementById('setup-build');
  button.disabled = !hasLocation;
  button.textContent = hasChannel ? 'Fetch weather + rebuild channel' : 'Fetch weather + create channel';
  setFormMessage('setup-message', complete
    ? 'RetroWeather will continue updating automatically.'
    : (!hasLocation ? 'Choose a location to begin.' : 'Create the channel now; you can keep using the dashboard while it renders.'),
    complete ? 'success' : '');
}
function showChannelJob(job) {
  const running = job.state === 'running';
  document.getElementById('job-stage').textContent = job.stage;
  document.getElementById('job-progress').value = job.progress;
  document.getElementById('job-percent').textContent = `${job.progress}%`;
  document.getElementById('validation-result').textContent = job.validation_result || (running ? 'Pending' : 'No completed validation yet');
  document.getElementById('last-publication').textContent = job.last_successful_publication
    ? `${new Date(job.last_successful_publication).toLocaleString()}${job.last_successful_generation ? ` · ${job.last_successful_generation}` : ''}`
    : 'None';
  const error = document.getElementById('job-error');
  error.textContent = job.error || '';
  error.hidden = !job.error;
  document.getElementById('channel-render').disabled = running;
  document.getElementById('channel-fetch-render').disabled = running;
  document.getElementById('weather-fetch').disabled = running;
  document.getElementById('setup-build').disabled = running || !Boolean(lastDashboardStatus && lastDashboardStatus.selected_location);
}
async function loadChannelJob() {
  const response = await fetch('/api/channel/job');
  const job = await response.json();
  if (!response.ok) throw new Error(job.error || 'Unable to load render status');
  showChannelJob(job);
  if (job.state === 'running') {
    clearTimeout(jobPoll);
    jobPoll = setTimeout(() => loadChannelJob().catch(showJobError), 750);
  } else if (job.state === 'success' && job.finished_at && handledCompletion !== job.finished_at) {
    handledCompletion = job.finished_at;
    if (job.generation_id) handledGeneration = job.generation_id;
    await Promise.all([loadStatus(), loadWeather(), loadPlan(), loadChannel(), loadNormalized(), loadAssets()]);
  }
  return job;
}
function showJobError(error) {
  const panel = document.getElementById('job-error');
  panel.textContent = error.message || String(error); panel.hidden = false;
}
async function startChannelJob(action) {
  const route = action === 'fetch-render' ? '/api/channel/fetch-render' : '/api/channel/render';
  const response = await fetch(route, {method:'POST'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || 'Unable to start channel job');
  showChannelJob(payload);
  handledGeneration = null;
  handledCompletion = null;
  await loadChannelJob();
}
function showScheduler(scheduler) {
  if (!scheduler) return;
  const labels = {
    sleeping: 'On · waiting', job_running: `On · ${scheduler.pending_action || 'job'} running`,
    backing_off: 'On · retry backoff', waiting_for_location: 'On · select a location',
    disabled: 'Off', scheduler_error: 'Error', stopped: 'Stopped'
  };
  document.getElementById('scheduler-state').textContent = labels[scheduler.state] || scheduler.state;
  document.getElementById('scheduler-fetch').textContent = scheduler.next_fetch_at ? new Date(scheduler.next_fetch_at).toLocaleString() : '—';
  document.getElementById('scheduler-render').textContent = scheduler.next_render_at ? new Date(scheduler.next_render_at).toLocaleString() : '—';
}
async function loadStatus() {
  const response = await fetch('/api/status');
  const status = await response.json();
  if (!response.ok) throw new Error(status.error || 'Unable to load application status');
  document.getElementById('ready').textContent = status.initialized ? 'Ready' : 'Not initialized';
  document.getElementById('weather').textContent = status.weather_snapshot ? 'Snapshot available' : 'No snapshot yet';
  document.getElementById('channel').textContent = status.published_channel ? 'Published' : 'Not rendered yet';
  const artworkCount = Object.values(status.artwork_inventory).reduce((total, count) => total + count, 0);
  document.getElementById('artwork').textContent = artworkCount ? `${artworkCount} background asset${artworkCount === 1 ? '' : 's'} available` : 'Using built-in backgrounds';
  document.getElementById('home').textContent = status.home;
  document.getElementById('version').textContent = `RetroWeather ${status.version}`;
  if (status.music_settings && !musicSettingsDirty) {
    document.getElementById('music-enabled').checked = status.music_settings.enabled;
    const musicVolume = Math.round(Number(status.music_settings.volume) * 100);
    document.getElementById('music-volume').value = musicVolume;
    document.getElementById('music-volume-value').textContent = `${musicVolume}%`;
  }
  lastDashboardStatus = status;
  showDashboardSettings(status.dashboard_settings);
  showSetupProgress(status);
  showSelected(status.selected_location);
  showWeatherStatus(status.weather_status);
  showScheduler(status.scheduler);
  if (status.selected_location && !status.weather_snapshot) {
    document.getElementById('weather-empty').textContent = 'Fetch current conditions and the seven-day forecast.';
  }
  return status;
}
function showAssets(inventory) {
  const summary = inventory.summary;
  document.getElementById('asset-summary').textContent = `${summary.total} supported file${summary.total === 1 ? '' : 's'} - ${summary.eligible} valid library asset${summary.eligible === 1 ? '' : 's'}`;
  const warningParts = [];
  if (summary.invalid) warningParts.push(`${summary.invalid} invalid`);
  if (summary.unsupported_files) warningParts.push(`${summary.unsupported_files} unsupported`);
  document.getElementById('asset-message').textContent = warningParts.length
    ? `${warningParts.join(' - ')}. Invalid files remain visible and are marked ineligible.`
    : 'All detected files are valid. Backgrounds and music are available to the current renderer.';
  const stats = document.getElementById('asset-stats'); stats.replaceChildren();
  for (const [label, value] of [['Still images', summary.still_images], ['Motion', summary.motion_backgrounds], ['Music', summary.music], ['Holiday', summary.holiday_assets]]) {
    const item = document.createElement('div'); item.className = 'asset-stat';
    const heading = document.createElement('div'); heading.className = 'label'; heading.textContent = label;
    const count = document.createElement('div'); count.className = 'value'; count.textContent = value;
    item.append(heading, count); stats.appendChild(item);
  }
  const holidayCards = document.getElementById('holiday-overlay-readiness'); holidayCards.replaceChildren();
  for (const [season, holiday] of Object.entries(inventory.holidays || {})) {
    const matching = inventory.assets.filter(asset => asset.role === `holiday_${season}_artwork` && asset.valid);
    const card = document.createElement('div'); card.className = 'provider-card';
    const label = document.createElement('div'); label.textContent = `${season} border · ${holiday.start}–${holiday.end}${holiday.enabled === false ? ' (disabled)' : ''}`;
    const target = document.createElement('div'); card.append(label, target); holidayCards.append(card);
    target.textContent = matching.length ? `Ready (${matching.length})` : 'Not installed';
    target.className = matching.length ? 'ready' : 'muted';
  }
  const rows = document.getElementById('asset-rows'); rows.replaceChildren();
  for (const asset of inventory.assets) {
    const row = document.createElement('tr');
    const dimensions = asset.media_type === 'audio'
      ? (asset.duration_seconds == null ? '--' : `${asset.duration_seconds.toFixed(1)}s`)
      : `${asset.width || '?'}x${asset.height || '?'}${asset.duration_seconds == null ? '' : ` - ${asset.duration_seconds.toFixed(1)}s`}`;
    const status = asset.usable_now ? 'Used by current renderer' : (asset.valid ? (asset.eligible ? 'Valid - future rendering step' : 'Reference only') : `Invalid: ${asset.error || 'inspection failed'}`);
    for (const text of [asset.relative_path, asset.role.replaceAll('_', ' '), asset.media_type, dimensions, status]) {
      const cell = document.createElement('td'); cell.textContent = text; row.appendChild(cell);
    }
    row.lastChild.className = asset.valid ? 'asset-valid' : 'asset-invalid';
    rows.appendChild(row);
  }
}
async function loadAssets() {
  const response = await fetch('/api/assets');
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || 'Unable to scan asset library');
  showAssets(payload);
  return payload;
}
loadStatus().catch(error => { document.getElementById('ready').textContent = `Status error: ${error}`; });
loadWeather().catch(error => { document.getElementById('weather-empty').textContent = error.message; });
loadPlan().catch(error => { document.getElementById('plan-summary').textContent = error.message; });
loadChannel().catch(error => { document.getElementById('channel-message').textContent = error.message; });
loadNormalized().catch(error => { document.getElementById('normalized-json').textContent = error.message; });
loadChannelJob().catch(showJobError);
loadAssets().catch(error => { document.getElementById('asset-message').textContent = error.message; });
setInterval(() => {
  loadStatus().catch(() => {});
  loadChannelJob().catch(() => {});
  loadAssets().catch(() => {});
}, 5000);
for (const selector of ['#general-settings-form input', '#general-settings-form select', '#appearance-form input', '#automation-form input', '#automation-form select']) {
  for (const field of document.querySelectorAll(selector)) field.addEventListener('input', () => { dashboardSettingsDirty = true; });
}
document.getElementById('panel-opacity').addEventListener('input', event => { document.getElementById('panel-opacity-value').textContent = `${event.target.value}%`; });
document.getElementById('text-scale').addEventListener('input', event => { document.getElementById('text-scale-value').textContent = `${event.target.value}%`; });
async function saveDashboardSettings(section, payload, messageId) {
  setFormMessage(messageId, 'Saving…');
  try {
    const response = await fetch('/api/dashboard/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({section, ...payload})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Unable to save settings');
    dashboardSettingsDirty = false; showDashboardSettings(result.settings);
    setFormMessage(messageId, 'Saved. Changes apply to the next fetch or render.', 'success');
    await loadStatus();
  } catch (error) { setFormMessage(messageId, error.message, 'error'); }
}
async function resetDashboardSettings(section, messageId) {
  setFormMessage(messageId, 'Restoring defaults…');
  try {
    const response = await fetch('/api/dashboard/settings/reset', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({section})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Unable to restore defaults');
    dashboardSettingsDirty = false; showDashboardSettings(result.settings);
    setFormMessage(messageId, 'Defaults restored. Render again to apply visual changes.', 'success');
    await loadStatus();
  } catch (error) { setFormMessage(messageId, error.message, 'error'); }
}
document.getElementById('general-settings-form').addEventListener('submit', event => {
  event.preventDefault();
  saveDashboardSettings('general', {channel_name:document.getElementById('channel-name').value, units:document.getElementById('units-system').value}, 'general-settings-message');
});
document.getElementById('appearance-form').addEventListener('submit', event => {
  event.preventDefault();
  saveDashboardSettings('appearance', {
    panel_opacity:Number(document.getElementById('panel-opacity').value) / 100,
    safe_margin:Number(document.getElementById('safe-margin').value),
    text_scale:Number(document.getElementById('text-scale').value) / 100,
    palette:{header:document.getElementById('palette-header').value, accent:document.getElementById('palette-accent').value, condition:document.getElementById('palette-condition').value}
  }, 'appearance-message');
});
document.getElementById('automation-form').addEventListener('submit', event => {
  event.preventDefault();
  saveDashboardSettings('automation', {
    automatic:document.getElementById('refresh-automatic').value === 'true',
    data_minutes:Number(document.getElementById('refresh-data').value),
    render_minutes:Number(document.getElementById('refresh-render').value),
    stale_after_minutes:Number(document.getElementById('refresh-stale').value)
  }, 'automation-message');
});
document.getElementById('general-reset').addEventListener('click', () => resetDashboardSettings('general', 'general-settings-message'));
document.getElementById('appearance-reset').addEventListener('click', () => resetDashboardSettings('appearance', 'appearance-message'));
document.getElementById('automation-reset').addEventListener('click', () => resetDashboardSettings('automation', 'automation-message'));
document.getElementById('setup-build').addEventListener('click', () => startChannelJob('fetch-render').catch(error => setFormMessage('setup-message', error.message, 'error')));
document.getElementById('channel-render').addEventListener('click', () => startChannelJob('render').catch(showJobError));
document.getElementById('channel-fetch-render').addEventListener('click', () => startChannelJob('fetch-render').catch(showJobError));
document.getElementById('output-open').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/output/open', {method:'POST'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Unable to open output folder');
    document.getElementById('channel-message').textContent = `Opened ${payload.path}`;
  } catch (error) { showJobError(error); }
});
document.getElementById('media-open').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/media/open', {method:'POST'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Unable to open media folder');
    document.getElementById('asset-message').textContent = `Opened ${payload.path}`;
  } catch (error) { document.getElementById('asset-message').textContent = error.message; }
});
document.getElementById('music-volume').addEventListener('input', event => {
  musicSettingsDirty = true;
  document.getElementById('music-volume-value').textContent = `${event.target.value}%`;
});
document.getElementById('music-enabled').addEventListener('change', () => { musicSettingsDirty = true; });
document.getElementById('music-save').addEventListener('click', async () => {
  const button = document.getElementById('music-save');
  const message = document.getElementById('music-message');
  button.disabled = true;
  try {
    const response = await fetch('/api/music/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        enabled: document.getElementById('music-enabled').checked,
        volume: Number(document.getElementById('music-volume').value) / 100
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Unable to save music settings');
    musicSettingsDirty = false;
    message.textContent = 'Music settings saved. Render again to apply them.';
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
document.getElementById('weather-fetch').addEventListener('click', async () => {
  const button = document.getElementById('weather-fetch');
  const empty = document.getElementById('weather-empty');
  button.disabled = true; button.textContent = 'Fetching…'; empty.textContent = 'Contacting Open-Meteo…';
  try {
    const response = await fetch('/api/weather/fetch', {method:'POST'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Weather fetch failed');
    showChannelJob(payload); handledCompletion = null; await loadChannelJob();
  } catch (error) {
    const warning = document.getElementById('weather-warning');
    warning.hidden = false; warning.textContent = `${error.message} The last valid forecast remains on screen when available.`;
    button.disabled = false;
  }
  finally { button.textContent = 'Fetch weather now'; }
});
document.getElementById('location-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('location-search');
  const message = document.getElementById('location-message');
  const list = document.getElementById('location-results');
  button.disabled = true; list.replaceChildren(); message.textContent = 'Searching…';
  try {
    const query = document.getElementById('location-query').value;
    const response = await fetch(`/api/locations/search?q=${encodeURIComponent(query)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Location search failed');
    message.textContent = payload.results.length ? payload.attribution : 'No matching locations found.';
    for (const result of payload.results) {
      const item = document.createElement('li');
      const description = document.createElement('span');
      description.textContent = `${result.display_name} · ${result.timezone}`;
      const select = document.createElement('button');
      select.type = 'button'; select.textContent = 'Select';
      select.addEventListener('click', async () => {
        select.disabled = true; message.textContent = `Selecting ${result.display_name}…`;
        try {
          const selectedResponse = await fetch('/api/location', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({provider_location_id: result.provider_location_id})
          });
          const selected = await selectedResponse.json();
          if (!selectedResponse.ok) throw new Error(selected.error || 'Location selection failed');
          showSelected(selected.selection); message.textContent = `Selected ${result.display_name}. You can now create the channel.`;
          list.replaceChildren();
          await loadStatus();
        } catch (error) { message.textContent = error.message; select.disabled = false; }
      });
      item.append(description, select); list.appendChild(item);
    }
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
</script>
</body>
</html>
"""


def build_status(
    paths: AppPaths,
    scheduler: RefreshScheduler | None = None,
) -> dict[str, Any]:
    configured = load_settings(paths) if paths.settings.is_file() else {}
    selected = load_selected_location(paths) if paths.settings.is_file() else None
    weather_status = build_weather_status(paths) if paths.settings.is_file() else None
    assets = scan_asset_library(paths.media)
    return {
        "schema_version": 1,
        "version": __version__,
        "initialized": paths.settings.is_file(),
        "home": str(paths.home),
        "settings": str(paths.settings),
        "weather_snapshot": weather_status.available if weather_status else False,
        "published_channel": (paths.output / "current.json").is_file(),
        "media_root": str(paths.media),
        "artwork_inventory": inventory(paths.media),
        "asset_summary": assets["summary"],
        "music_settings": music_settings(configured.get("music")),
        "dashboard_settings": _dashboard_settings(configured),
        "output_root": str(paths.output),
        "selected_location": selected.to_dict() if selected else None,
        "weather_status": weather_status.to_dict() if weather_status else None,
        "scheduler": scheduler.status() if scheduler else None,
    }


def _dashboard_settings(configured: dict[str, Any]) -> dict[str, Any]:
    channel = configured.get("channel") if isinstance(configured.get("channel"), dict) else {}
    units = configured.get("units") if isinstance(configured.get("units"), dict) else {}
    refresh = configured.get("refresh") if isinstance(configured.get("refresh"), dict) else {}
    appearance = appearance_settings(configured)
    return {
        "channel_name": str(channel.get("display_name", "RetroWeather")),
        "units": str(units.get("system", "imperial")),
        "appearance": {
            "panel_opacity": appearance.panel_opacity,
            "panel_border_opacity": appearance.panel_border_opacity,
            "safe_margin": appearance.safe_margin,
            "text_scale": appearance.text_scale,
            "palette": {
                key: getattr(appearance.palette, key)
                for key in appearance.palette.__dataclass_fields__
            },
        },
        "refresh": {
            "automatic": bool(refresh.get("automatic", True)),
            "data_minutes": float(refresh.get("data_minutes", 15)),
            "render_minutes": float(refresh.get("render_minutes", 30)),
            "stale_after_minutes": float(refresh.get("stale_after_minutes", 90)),
        },
    }


def _dashboard_number(
    payload: dict[str, Any], key: str, minimum: float, maximum: float
) -> float:
    raw = payload.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{key} must be a number.")
    value = float(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}.")
    return value


def create_server(
    paths: AppPaths,
    host: str | None = None,
    port: int | None = None,
    geocoder: OpenMeteoGeocodingClient | None = None,
    weather_client: OpenMeteoForecastClient | None = None,
    job_manager: ChannelJobManager | None = None,
    scheduler: RefreshScheduler | None = None,
    output_opener: Callable[[Path], None] | None = None,
) -> ThreadingHTTPServer:
    settings = load_settings(paths)
    application = settings["application"]
    bind_host = host if host is not None else str(application["host"])
    bind_port = port if port is not None else int(application["port"])
    refresh = settings.get("refresh")
    timeout = refresh.get("provider_timeout_seconds", 20) if isinstance(refresh, dict) else 20
    active_geocoder = geocoder or OpenMeteoGeocodingClient(timeout=float(timeout))
    active_weather_client = weather_client or OpenMeteoForecastClient(timeout=float(timeout))
    active_jobs = job_manager or ChannelJobManager(paths, active_weather_client)
    active_scheduler = scheduler or RefreshScheduler(paths, active_jobs)
    active_output_opener = output_opener or open_output_folder

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/":
                self._send_bytes(
                    200,
                    DASHBOARD_HTML.encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if route == "/api/status":
                payload = json.dumps(
                    build_status(paths, active_scheduler), indent=2
                ).encode("utf-8")
                self._send_bytes(200, payload, "application/json; charset=utf-8")
                return
            if route == "/api/assets":
                assets = scan_asset_library(paths.media)
                configured = load_settings(paths)
                assets["holidays"] = configured.get("crtvsim_holidays", configured.get("holidays", {
                    "halloween": {"start": "10-01", "end": "10-31"},
                    "christmas": {"start": "12-01", "end": "12-25"},
                }))
                self._send_json(200, assets)
                return
            if route == "/api/locations/search":
                parameters = parse_qs(parsed.query)
                query = parameters.get("q", [""])[0]
                try:
                    results = active_geocoder.search(query, count=10)
                    self._send_json(
                        200,
                        {
                            "results": [item.to_dict() for item in results],
                            "attribution": GEOCODING_ATTRIBUTION,
                        },
                    )
                except LocationQueryError as exc:
                    self._send_json(400, {"error": str(exc)})
                except (ProviderRequestError, ProviderResponseError) as exc:
                    self._send_json(502, {"error": str(exc)})
                return
            if route == "/api/weather":
                if not paths.latest_snapshot.is_file():
                    self._send_json(404, {"error": "No weather snapshot is available yet."})
                    return
                try:
                    snapshot = NormalizedWeatherSnapshot.load(paths.latest_snapshot)
                    self._send_json(200, snapshot.to_dict())
                except (OSError, SnapshotValidationError) as exc:
                    self._send_json(500, {"error": f"Stored weather is invalid: {exc}"})
                return
            if route == "/api/plan":
                if not paths.latest_snapshot.is_file():
                    self._send_json(404, {"error": "No weather snapshot is available yet."})
                    return
                try:
                    snapshot = NormalizedWeatherSnapshot.load(paths.latest_snapshot)
                    plan = build_forecast_plan(snapshot, settings)
                    self._send_json(200, plan.to_dict())
                except (OSError, SnapshotValidationError, PlanError) as exc:
                    self._send_json(500, {"error": f"Unable to build channel plan: {exc}"})
                return
            if route == "/api/channel":
                try:
                    manifest, manifest_path = load_active_channel(paths)
                    response = dict(manifest)
                    metadata_path = manifest_path.parent / "render-metadata.json"
                    if metadata_path.is_file():
                        try:
                            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                            if isinstance(metadata, dict):
                                response["render_metadata"] = metadata
                        except (OSError, json.JSONDecodeError):
                            pass
                    self._send_json(200, response)
                except RenderError as exc:
                    self._send_json(404, {"error": str(exc)})
                return
            if route == "/api/channel/job":
                self._send_json(200, active_jobs.status())
                return
            if route == "/api/scheduler":
                self._send_json(200, active_scheduler.status())
                return
            if route == "/channel/video":
                try:
                    manifest, manifest_path = load_active_channel(paths)
                    relative_video = manifest.get("video")
                    if not isinstance(relative_video, str) or Path(relative_video).is_absolute() or ".." in Path(relative_video).parts:
                        raise RenderError("Active video path is unsafe.")
                    video = (manifest_path.parent / relative_video).resolve()
                    if manifest_path.parent.resolve() not in video.parents:
                        raise RenderError("Active video escapes its version directory.")
                    self._send_file(video, "video/mp4")
                except RenderError as exc:
                    self._send_json(404, {"error": str(exc)})
                return
            self._send_bytes(404, b"Not found\n", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            route = urlparse(self.path).path
            if route == "/api/shutdown":
                self._send_json(202, {"ok": True, "message": "RetroWeather is shutting down."})
                threading.Thread(
                    target=self.server.shutdown,
                    name="retroweather-web-shutdown",
                    daemon=True,
                ).start()
                return
            if route == "/api/dashboard/settings":
                try:
                    payload = self._read_json_body()
                    updated = load_settings(paths)
                    section = payload.get("section")
                    if section == "general":
                        name = str(payload.get("channel_name", "")).strip()
                        if not 1 <= len(name) <= 40:
                            raise ValueError("Channel name must be between 1 and 40 characters.")
                        units = payload.get("units")
                        if units not in {"imperial", "metric"}:
                            raise ValueError("Units must be imperial or metric.")
                        updated.setdefault("channel", {})["display_name"] = name
                        updated.setdefault("units", {})["system"] = units
                    elif section == "appearance":
                        current = updated.setdefault("appearance", {})
                        current["panel_opacity"] = _dashboard_number(payload, "panel_opacity", 0.25, 0.90)
                        current["safe_margin"] = int(_dashboard_number(payload, "safe_margin", 20, 40))
                        current["text_scale"] = _dashboard_number(payload, "text_scale", 0.85, 1.15)
                        colors = payload.get("palette")
                        if not isinstance(colors, dict):
                            raise ValueError("Palette must be an object.")
                        palette = current.setdefault("palette", {})
                        for key in ("header", "accent", "condition"):
                            value = str(colors.get(key, "")).strip()
                            if not COLOR_PATTERN.fullmatch(value):
                                raise ValueError(f"{key} must be a six-digit color.")
                            palette[key] = "0x" + value[-6:].lower()
                    elif section == "automation":
                        automatic = payload.get("automatic")
                        if not isinstance(automatic, bool):
                            raise ValueError("automatic must be true or false.")
                        refresh = updated.setdefault("refresh", {})
                        refresh["automatic"] = automatic
                        refresh["data_minutes"] = _dashboard_number(payload, "data_minutes", 5, 180)
                        refresh["render_minutes"] = _dashboard_number(payload, "render_minutes", 5, 240)
                        refresh["stale_after_minutes"] = _dashboard_number(payload, "stale_after_minutes", 15, 1440)
                    else:
                        raise ValueError("Unknown dashboard settings section.")
                    save_settings(paths, updated)
                    self._send_json(200, {"settings": _dashboard_settings(updated)})
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._send_json(400, {"error": str(exc)})
                except (OSError, ConfigError) as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            if route == "/api/dashboard/settings/reset":
                try:
                    payload = self._read_json_body()
                    section = payload.get("section")
                    if section not in {"general", "appearance", "automation"}:
                        raise ValueError("Unknown dashboard settings section.")
                    updated = load_settings(paths)
                    if section == "general":
                        updated.setdefault("channel", {})["display_name"] = DEFAULT_SETTINGS["channel"]["display_name"]
                        updated["units"] = copy.deepcopy(DEFAULT_SETTINGS["units"])
                    elif section == "appearance":
                        updated["appearance"] = copy.deepcopy(DEFAULT_SETTINGS["appearance"])
                    else:
                        for key in ("automatic", "data_minutes", "render_minutes", "stale_after_minutes"):
                            updated.setdefault("refresh", {})[key] = DEFAULT_SETTINGS["refresh"][key]
                    save_settings(paths, updated)
                    self._send_json(200, {"settings": _dashboard_settings(updated)})
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._send_json(400, {"error": str(exc)})
                except (OSError, ConfigError) as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            if route in {"/api/channel/render", "/api/channel/fetch-render"}:
                action = "fetch-render" if route.endswith("fetch-render") else "render"
                try:
                    self._send_json(202, active_jobs.start(action))
                except JobBusyError as exc:
                    self._send_json(409, {"error": str(exc)})
                except (ValueError, OSError) as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            if route == "/api/output/open":
                try:
                    paths.output.mkdir(parents=True, exist_ok=True)
                    active_output_opener(paths.output)
                    self._send_json(200, {"path": str(paths.output)})
                except OSError as exc:
                    self._send_json(500, {"error": f"Unable to open output folder: {exc}"})
                return
            if route == "/api/media/open":
                try:
                    paths.media.mkdir(parents=True, exist_ok=True)
                    active_output_opener(paths.media)
                    self._send_json(200, {"path": str(paths.media)})
                except OSError as exc:
                    self._send_json(500, {"error": f"Unable to open media folder: {exc}"})
                return
            if route == "/api/music/settings":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("Request body is missing or too large.")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
                        raise ValueError("enabled must be true or false.")
                    volume = payload.get("volume")
                    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 1:
                        raise ValueError("volume must be between 0 and 1.")
                    updated = load_settings(paths)
                    current_music = music_settings(updated.get("music"))
                    current_music.update({"enabled": payload["enabled"], "volume": float(volume)})
                    updated["music"] = current_music
                    save_settings(paths, updated)
                    self._send_json(200, {"music": current_music})
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._send_json(400, {"error": str(exc)})
                except (OSError, ConfigError) as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            if route == "/api/weather/fetch":
                try:
                    self._send_json(202, active_jobs.start("fetch"))
                except JobBusyError as exc:
                    self._send_json(409, {"error": str(exc)})
                except (ValueError, OSError) as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            if route != "/api/location":
                self._send_bytes(404, b"Not found\n", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_384:
                    raise LocationQueryError("Request body is missing or too large.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise LocationQueryError("Request body must be a JSON object.")
                identifier = payload.get("provider_location_id")
                result = active_geocoder.get(identifier)
                selection = result.to_selection()
                save_selected_location(paths, selection)
                self._send_json(200, {"selection": selection.to_dict()})
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "Request body must be valid JSON."})
            except (LocationQueryError, ConfigError) as exc:
                self._send_json(400, {"error": str(exc)})
            except (ProviderRequestError, ProviderResponseError) as exc:
                self._send_json(502, {"error": str(exc)})

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            self._send_bytes(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_384:
                raise ValueError("Request body is missing or too large.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            return payload

        def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                raise RenderError("Published channel video is missing.")
            size = path.stat().st_size
            start, end = 0, size - 1
            status = 200
            range_header = self.headers.get("Range")
            if range_header:
                if not range_header.startswith("bytes=") or "," in range_header:
                    self.send_error(416)
                    return
                first, _, last = range_header[6:].partition("-")
                try:
                    start = int(first) if first else 0
                    end = int(last) if last else size - 1
                except ValueError:
                    self.send_error(416)
                    return
                if start < 0 or end < start or start >= size:
                    self.send_error(416)
                    return
                end = min(end, size - 1)
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((bind_host, bind_port), DashboardHandler)
    server.refresh_scheduler = active_scheduler  # type: ignore[attr-defined]
    return server


def serve(paths: AppPaths, host: str | None = None, port: int | None = None) -> None:
    server = create_server(paths, host=host, port=port)
    scheduler = server.refresh_scheduler  # type: ignore[attr-defined]
    scheduler.start()
    address, selected_port = server.server_address[:2]
    print(f"RetroWeather dashboard: http://{address}:{selected_port}/")
    try:
        server.serve_forever()
    finally:
        scheduler.stop()
        server.server_close()


def open_output_folder(path: Path) -> None:
    resolved = path.resolve()
    if os.name == "nt":
        os.startfile(str(resolved))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(resolved)])
    else:
        subprocess.Popen(["xdg-open", str(resolved)])
