from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import settings
from common.snapshots import (
    SnapshotError,
    collect_dashboard_views,
    list_snapshots,
    load_snapshot,
    write_snapshot,
)
from .data import get_dashboard_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("visualizer.main")

app = FastAPI(title="Carbonshift Live Visualizer")


class SnapshotExportRequest(BaseModel):
    """Optional label and source for exporting the currently displayed data."""

    label: str | None = Field(default=None, max_length=100)
    source_snapshot_id: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/data")
def api_data(
    qos_profile_id: str | None = Query(default=None),
    snapshot_id: str | None = Query(default=None),
) -> JSONResponse:
    if snapshot_id is not None:
        try:
            snapshot, _ = load_snapshot(settings.snapshots_dir, snapshot_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Snapshot not found") from exc
        views = snapshot["views"]
        if qos_profile_id is None:
            data = views["all"]
        else:
            data = views["by_profile"].get(qos_profile_id)
            if data is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"QoS profile {qos_profile_id!r} is not available in this snapshot",
                )
        return JSONResponse(data)

    data = get_dashboard_data(qos_profile_id=qos_profile_id)
    return JSONResponse(data)


@app.get("/api/snapshots")
def api_snapshots() -> JSONResponse:
    return JSONResponse(list_snapshots(settings.snapshots_dir))


@app.get("/api/snapshots/{snapshot_id}/summary")
def api_snapshot_summary(snapshot_id: str) -> JSONResponse:
    try:
        _, summary = load_snapshot(settings.snapshots_dir, snapshot_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Snapshot not found") from exc
    return JSONResponse(summary)


@app.post("/api/snapshots")
def api_export_snapshot(request: SnapshotExportRequest) -> JSONResponse:
    clients: list[dict[str, Any]] = []
    source_snapshot_id = request.source_snapshot_id
    if source_snapshot_id is not None:
        try:
            source_snapshot, source_summary = load_snapshot(
                settings.snapshots_dir,
                source_snapshot_id,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Source snapshot not found") from exc
        views = source_snapshot["views"]
        clients = source_summary["clients"]
        source = "visualizer-copy"
    else:
        views = collect_dashboard_views(get_dashboard_data)
        source = "visualizer-live"

    try:
        record = write_snapshot(
            snapshots_dir=settings.snapshots_dir,
            views=views,
            clients=clients,
            label=request.label,
            source=source,
            source_snapshot_id=source_snapshot_id,
        )
    except SnapshotError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(record, status_code=201)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML_TEMPLATE


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Carbonshift Visualizer</title>
  <!-- Plotly.js -->
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {
      color-scheme: dark;
      --bg-color: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-blue: #38bdf8;
      --accent-green: #4ade80;
      --accent-orange: #fb923c;
      --accent-red: #f87171;
      --fast-color: #1f77b4;
      --balanced-color: #2ca02c;
      --accurate-color: #ff7f0e;
      --button-hover-bg: #334155;
      --button-hover-border: #475569;
      --plot-grid-color: #334155;
      --plot-zero-line-color: #475569;
      --plot-annotation-color: #94a3b8;
      --plot-hover-bg: #1e293b;
      --plot-slider-bg: #1e293b;
      --plot-slider-border: #334155;
    }
    :root[data-theme="light"] {
      color-scheme: light;
      --bg-color: #f1f5f9;
      --card-bg: #ffffff;
      --card-border: #cbd5e1;
      --text-main: #0f172a;
      --text-muted: #475569;
      --accent-blue: #0284c7;
      --accent-green: #16a34a;
      --accent-orange: #ea580c;
      --accent-red: #dc2626;
      --button-hover-bg: #e2e8f0;
      --button-hover-border: #94a3b8;
      --plot-grid-color: #e2e8f0;
      --plot-zero-line-color: #94a3b8;
      --plot-annotation-color: #64748b;
      --plot-hover-bg: #ffffff;
      --plot-slider-bg: #f1f5f9;
      --plot-slider-border: #cbd5e1;
    }
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    body {
      background-color: var(--bg-color);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 20px 24px 60px 24px;
      line-height: 1.5;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--card-border);
      flex-wrap: wrap;
      gap: 16px;
    }
    .header-title {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    h1 {
      font-size: 1.6rem;
      font-weight: 700;
      letter-spacing: -0.5px;
    }
    .badge {
      background-color: rgba(56, 189, 248, 0.15);
      color: var(--accent-blue);
      border: 1px solid rgba(56, 189, 248, 0.3);
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 0.8rem;
      font-weight: 600;
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    #snapshot-status {
      flex-basis: 100%;
      text-align: left;
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    #snapshot-status:empty {
      display: none;
    }
    .btn {
      background-color: var(--card-bg);
      color: var(--text-main);
      border: 1px solid var(--card-border);
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.85rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn:hover {
      background-color: var(--button-hover-bg);
      border-color: var(--button-hover-border);
    }
    .btn.active {
      background-color: var(--accent-blue);
      color: #0f172a;
      border-color: var(--accent-blue);
      font-weight: 600;
    }
    .btn-toggle {
      background-color: rgba(74, 222, 128, 0.15);
      color: var(--accent-green);
      border-color: rgba(74, 222, 128, 0.3);
    }
    .btn-toggle.paused {
      background-color: rgba(248, 113, 113, 0.15);
      color: var(--accent-red);
      border-color: rgba(248, 113, 113, 0.3);
    }
    select, input[type="text"] {
      background-color: var(--card-bg);
      color: var(--text-main);
      border: 1px solid var(--card-border);
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 0.85rem;
      cursor: pointer;
    }
    input[type="text"] {
      width: 140px;
      cursor: text;
    }

    /* KPI Cards Grid */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .card {
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
    }
    .card-label {
      font-size: 0.8rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      margin-bottom: 8px;
    }
    .card-value {
      font-size: 1.5rem;
      font-weight: 700;
      margin-bottom: 4px;
    }
    .card-subtext {
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .saving-positive {
      color: var(--accent-green);
    }
    .saving-negative {
      color: var(--accent-red);
    }

    /* Charts Section */
    .chart-container {
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 18px;
      margin-bottom: 24px;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
    }
    .chart-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }
    .chart-title {
      font-size: 1.1rem;
      font-weight: 600;
    }
    .chart-desc {
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .chart-current-metrics {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .chart-current-metrics strong {
      color: var(--text-main);
    }
    .plot-wrapper {
      width: 100%;
      min-height: 380px;
      overflow-x: auto;
    }

    /* Tables & Breakdown Grid */
    .breakdown-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      margin-top: 8px;
    }
    th, td {
      padding: 8px 12px;
      text-align: left;
      border-bottom: 1px solid var(--card-border);
    }
    th {
      color: var(--text-muted);
      font-weight: 600;
    }
    .flavour-dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      margin-right: 6px;
    }
  </style>
</head>
<body>

  <header>
    <div class="header-title">
      <h1>Carbonshift Scheduler Monitor</h1>
      <span class="badge" id="live-badge">● Live Polling</span>
    </div>

    <div class="controls">
      <label for="source-select" style="font-size:0.85rem; color:var(--text-muted);">Data:</label>
      <select id="source-select" onchange="setDataSource(this.value)">
        <option value="live">Live data</option>
        <option value="snapshot">Choose snapshot</option>
      </select>
      <select id="snapshot-select" aria-label="Saved snapshot" onchange="selectSnapshot(this.value)" disabled>
        <option value="">No snapshots available</option>
      </select>

      <label for="profile-select" style="font-size:0.85rem; color:var(--text-muted);">QoS profile:</label>
      <select id="profile-select" onchange="selectProfile(this.value)">
        <option value="">All profiles</option>
      </select>

      <input id="snapshot-label" type="text" maxlength="100" placeholder="Snapshot name" aria-label="Snapshot name">
      <button id="export-snapshot-btn" class="btn" onclick="exportSnapshot()">Export snapshot</button>
      <a id="summary-download-btn" class="btn" hidden>Download summary</a>

      <span style="font-size:0.85rem; color:var(--text-muted);">Slots:</span>
      <button class="btn time-filter" data-range="24" onclick="setTimeRange(24)">Last 24</button>
      <button class="btn time-filter" data-range="48" onclick="setTimeRange(48)">Last 48</button>
      <button class="btn time-filter" data-range="72" onclick="setTimeRange(72)">Last 72</button>
      <button class="btn time-filter active" data-range="all" onclick="setTimeRange('all')">All</button>

      <span style="font-size:0.85rem; color:var(--text-muted); margin-left: 8px;">Bin:</span>
      <select id="resample-select" onchange="updateResampling()">
        <option value="1">1 Slot (Raw)</option>
        <option value="2">2 Slots</option>
        <option value="4">4 Slots</option>
      </select>

      <button id="toggle-refresh-btn" class="btn btn-toggle" onclick="toggleAutoRefresh()">Pause</button>
      <button id="theme-toggle-btn" class="btn" onclick="toggleTheme()">Use light theme</button>
      <button class="btn" onclick="fetchData()">⟳ Refresh</button>
    </div>
    <span id="snapshot-status" role="status"></span>
  </header>

  <!-- Key Metrics Row -->
  <section class="kpi-grid">
    <div class="card">
      <div class="card-label">Current Time Slot</div>
      <div class="card-value" id="kpi-current-slot">-</div>
      <div class="card-subtext" id="kpi-global-slot">Global: -</div>
    </div>
    <div class="card">
      <div class="card-label">Assigned Requests</div>
      <div class="card-value" id="kpi-assigned-count">0</div>
      <div class="card-subtext" id="kpi-assigned-breakdown">0 pending</div>
    </div>
    <div class="card">
      <div class="card-label">Actual Carbon vs Baseline</div>
      <div class="card-value" id="kpi-actual-costs">-</div>
      <div class="card-subtext" id="kpi-actual-saving">Saving: -</div>
    </div>
    <div class="card">
      <div class="card-label">Forecasted Pending Cost</div>
      <div class="card-value" id="kpi-pending-cost">0.0 gCO₂</div>
      <div class="card-subtext">For not-yet-processed requests</div>
    </div>
    <div class="card">
      <div class="card-label">Avg Actual Exec Time</div>
      <div class="card-value" id="kpi-avg-exec-time">-</div>
      <div class="card-subtext" id="kpi-baseline-exec-time">Baseline: -</div>
    </div>
  </section>

  <!-- Plot 1: Stacked Assignment Plot -->
  <section class="chart-container">
    <div class="chart-header">
      <div>
        <div class="chart-title">Assignment Timeline & Carbon Intensity</div>
        <div class="chart-desc">Selected-profile assignments, with capacity tiers and carbon intensity shown separately.</div>
      </div>
    </div>
    <div id="assignment-plot" class="plot-wrapper"></div>
  </section>

  <!-- Plot 2: Average Error Over Slots -->
  <section class="chart-container">
    <div class="chart-header">
      <div>
        <div class="chart-title">Error Tracking by Scheduled Slot</div>
        <div class="chart-desc">Selected-profile error contributions; the all-profile average is descriptive only and has no QoS threshold.</div>
      </div>
      <div class="chart-current-metrics">
        <span id="plot-error-avg-label">Current all-profile cumulative error: <strong id="plot-global-error-avg">-</strong></span>
        <span>Current window error avg: <strong id="plot-window-error-avg">-</strong></span>
      </div>
    </div>
    <div id="error-plot" class="plot-wrapper"></div>
  </section>

  <!-- Plot 3: Input Requests & Carbon Forecast Wave -->
  <section class="chart-container">
    <div class="chart-header">
      <div>
        <div class="chart-title">Input Load & Carbon Intensity Wave</div>
        <div class="chart-desc">Arrived requests by arrival slot vs forecasted and observed carbon intensity</div>
      </div>
    </div>
    <div id="input-plot" class="plot-wrapper"></div>
  </section>

  <!-- Detailed Breakdowns -->
  <section class="breakdown-grid">
    <div class="card">
      <div class="card-label">Execution Time By Flavour (sec)</div>
      <table>
        <thead>
          <tr>
            <th>Flavour</th>
            <th>Count</th>
            <th>Avg Time</th>
            <th>Baseline Time</th>
          </tr>
        </thead>
        <tbody id="flavour-table-body">
          <tr><td colspan="4" style="text-align:center;">Loading...</td></tr>
        </tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-label">Scheduler & Horizon Details</div>
      <table>
        <tbody>
          <tr><td>Selected QoS Profile</td><td id="info-profile">All profiles</td></tr>
          <tr><td>Error Semantics</td><td id="info-error-semantics">-</td></tr>
          <tr><td>Error Window (past/future/decay)</td><td id="info-error-window">-</td></tr>
          <tr><td>Profile / All-profile Error Avg</td><td id="info-global-error">-</td></tr>
          <tr><td>Selected Profile Threshold</td><td id="info-error-threshold">Not applicable</td></tr>
          <tr><td>Horizon Total Slots</td><td id="info-total-slots">-</td></tr>
          <tr><td>Completed Requests</td><td id="info-completed-reqs">-</td></tr>
          <tr><td>In-progress Requests</td><td id="info-dispatched-reqs">-</td></tr>
          <tr><td>Failed Requests</td><td id="info-failed-reqs">-</td></tr>
          <tr><td>Pending Requests</td><td id="info-pending-reqs">-</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <script>
    let rawData = null;
    let selectedRange = 'all';
    let selectedProfileId = '';
    let dataMode = 'live';
    let selectedSnapshotId = '';
    let dataSourceGeneration = 0;
    let snapshots = [];
    let dataRequestGeneration = 0;
    let resampleBin = 1;
    let autoRefresh = true;
    let refreshTimer = null;
    let selectedTheme = 'dark';

    const FLAVOUR_COLORS = {
      'Fast': '#1f77b4',
      'Balanced': '#2ca02c',
      'Accurate': '#ff7f0e'
    };
    const CUSTOM_FLAVOUR_COLORS = [
      '#14b8a6', '#a855f7', '#eab308', '#ec4899', '#64748b', '#84cc16'
    ];

    function flavourColor(name, index) {
      return FLAVOUR_COLORS[name] || CUSTOM_FLAVOUR_COLORS[index % CUSTOM_FLAVOUR_COLORS.length];
    }

    function chartTheme() {
      const styles = getComputedStyle(document.documentElement);
      return {
        text: styles.getPropertyValue('--text-main').trim(),
        grid: styles.getPropertyValue('--plot-grid-color').trim(),
        zeroLine: styles.getPropertyValue('--plot-zero-line-color').trim(),
        annotation: styles.getPropertyValue('--plot-annotation-color').trim(),
        hoverBackground: styles.getPropertyValue('--plot-hover-bg').trim(),
        sliderBackground: styles.getPropertyValue('--plot-slider-bg').trim(),
        sliderBorder: styles.getPropertyValue('--plot-slider-border').trim(),
      };
    }

    function toggleTheme() {
      selectedTheme = selectedTheme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = selectedTheme;
      document.getElementById('theme-toggle-btn').textContent =
        selectedTheme === 'dark' ? 'Use light theme' : 'Use dark theme';
      if (rawData) renderAll();
    }

    function plotUiRevision(chartName, viewMode = '') {
      // Plotly.react rebuilds each figure every poll. Keep its revision stable
      // for this chart/profile so Plotly retains user zoom, legend visibility,
      // and other interactions instead of treating every poll as a new plot.
      // A different profile is a different view and gets its own revision.
      const profileKey = selectedProfileId || 'all-profiles';
      return `carbonshift:${chartName}:${profileKey}:${viewMode}`;
    }

    async function fetchData() {
      const requestGeneration = ++dataRequestGeneration;
      if (dataMode === 'snapshot' && !selectedSnapshotId) return;
      try {
        const url = new URL('/api/data', window.location.origin);
        if (selectedProfileId) url.searchParams.set('qos_profile_id', selectedProfileId);
        if (dataMode === 'snapshot') url.searchParams.set('snapshot_id', selectedSnapshotId);
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`Dashboard request failed with HTTP ${resp.status}`);
        const data = await resp.json();
        if (requestGeneration !== dataRequestGeneration) return;
        rawData = data;
        selectedProfileId = rawData.indicators.qos_profile_id || '';
        renderProfileSelector(rawData.active_profiles || []);
        renderAll();
        renderSourceState();
      } catch (e) {
        console.error("Fetch error:", e);
        document.getElementById('snapshot-status').textContent = String(e);
      }
    }

    async function refreshSnapshots() {
      const response = await fetch('/api/snapshots');
      if (!response.ok) throw new Error(`Snapshot list failed with HTTP ${response.status}`);
      snapshots = await response.json();
      const selector = document.getElementById('snapshot-select');
      selector.replaceChildren();

      if (snapshots.length === 0) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No snapshots available';
        selector.appendChild(option);
        selectedSnapshotId = '';
      } else {
        for (const snapshot of snapshots) {
          const option = document.createElement('option');
          option.value = snapshot.snapshot_id;
          const capturedAt = new Date(snapshot.captured_at_utc).toLocaleString();
          option.textContent = `${capturedAt} · ${snapshot.label || snapshot.source}`;
          selector.appendChild(option);
        }
        if (!snapshots.some(snapshot => snapshot.snapshot_id === selectedSnapshotId)) {
          selectedSnapshotId = snapshots[0].snapshot_id;
        }
        selector.value = selectedSnapshotId;
      }
      renderSourceState();
      if (dataMode === 'snapshot' && selectedSnapshotId) fetchData();
    }

    function setDataSource(mode) {
      if (mode !== dataMode) {
        dataSourceGeneration += 1;
        document.getElementById('snapshot-status').textContent = '';
      }
      dataMode = mode;
      if (dataMode === 'snapshot' && !selectedSnapshotId && snapshots.length > 0) {
        selectedSnapshotId = snapshots[0].snapshot_id;
      }
      document.getElementById('snapshot-select').disabled =
        dataMode !== 'snapshot' || snapshots.length === 0;
      syncRefreshTimer();
      renderSourceState();
      fetchData();
    }

    function selectSnapshot(snapshotId) {
      selectedSnapshotId = snapshotId;
      renderSourceState();
      fetchData();
    }

    function renderSourceState() {
      const snapshotMode = dataMode === 'snapshot';
      const selector = document.getElementById('snapshot-select');
      selector.disabled = !snapshotMode || snapshots.length === 0;
      const summaryLink = document.getElementById('summary-download-btn');
      summaryLink.hidden = !snapshotMode || !selectedSnapshotId;
      if (selectedSnapshotId) {
        summaryLink.href = `/api/snapshots/${encodeURIComponent(selectedSnapshotId)}/summary`;
        summaryLink.download = `${selectedSnapshotId}_summary.json`;
      }
      if (snapshotMode) {
        const selected = snapshots.find(snapshot => snapshot.snapshot_id === selectedSnapshotId);
        document.getElementById('live-badge').textContent =
          selected ? `◆ Snapshot · ${selected.label || selected.captured_at_utc}` : '◆ Choose a snapshot';
      } else {
        document.getElementById('live-badge').textContent =
          autoRefresh ? '● Live Polling' : '⏸ Paused';
      }
    }

    function renderProfileSelector(profiles) {
      const selector = document.getElementById('profile-select');
      const effectiveProfileId = rawData.indicators.qos_profile_id || '';
      selector.replaceChildren();

      const allOption = document.createElement('option');
      allOption.value = '';
      allOption.textContent = 'All profiles';
      selector.appendChild(allOption);

      for (const profile of profiles) {
        const option = document.createElement('option');
        option.value = profile.profile_id;
        option.textContent = `${profile.profile_id} · ${profile.task_kind}`;
        selector.appendChild(option);
      }
      selector.value = effectiveProfileId;
    }

    function selectProfile(profileId) {
      selectedProfileId = profileId;
      fetchData();
    }

    function toggleAutoRefresh() {
      if (dataMode !== 'live') return;
      autoRefresh = !autoRefresh;
      const btn = document.getElementById('toggle-refresh-btn');
      btn.textContent = autoRefresh ? 'Pause' : 'Resume';
      btn.className = autoRefresh ? 'btn btn-toggle' : 'btn btn-toggle paused';
      syncRefreshTimer();
      renderSourceState();
    }

    function syncRefreshTimer() {
      const shouldPoll = dataMode === 'live' && autoRefresh;
      const button = document.getElementById('toggle-refresh-btn');
      button.disabled = dataMode !== 'live';
      button.textContent = dataMode !== 'live' ? 'Polling off' : (autoRefresh ? 'Pause' : 'Resume');
      button.className = autoRefresh && dataMode === 'live' ? 'btn btn-toggle' : 'btn btn-toggle paused';
      if (shouldPoll && !refreshTimer) {
        refreshTimer = setInterval(fetchData, 2000);
      } else if (!shouldPoll && refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
      }
    }

    async function exportSnapshot() {
      const status = document.getElementById('snapshot-status');
      const button = document.getElementById('export-snapshot-btn');
      const exportGeneration = dataSourceGeneration;
      if (dataMode === 'snapshot' && !selectedSnapshotId) {
        status.textContent = 'Select a saved snapshot before exporting.';
        return;
      }
      button.disabled = true;
      status.textContent = 'Saving snapshot...';
      try {
        const response = await fetch('/api/snapshots', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            label: document.getElementById('snapshot-label').value.trim() || null,
            source_snapshot_id: dataMode === 'snapshot' ? selectedSnapshotId : null,
          }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || `Export failed with HTTP ${response.status}`);
        if (dataMode === 'snapshot' && exportGeneration === dataSourceGeneration) {
          selectedSnapshotId = result.snapshot_id;
        }
        await refreshSnapshots();
        if (exportGeneration === dataSourceGeneration) {
          status.textContent = `Saved ${result.snapshot_filename}`;
        }
      } catch (e) {
        console.error("Snapshot export error:", e);
        if (exportGeneration === dataSourceGeneration) status.textContent = String(e);
      } finally {
        button.disabled = false;
      }
    }

    function setTimeRange(range) {
      selectedRange = range;
      document.querySelectorAll('.time-filter').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.range === String(range));
      });
      renderAll();
    }

    function updateResampling() {
      resampleBin = parseInt(document.getElementById('resample-select').value) || 1;
      renderAll();
    }

    function displaySlotValue(slot) {
      if (slot === null || slot === undefined || slot === '') return slot;
      const numeric = Number(slot);
      return Number.isFinite(numeric) ? numeric + 1 : slot;
    }

    function displaySlotAxis(slots) {
      return slots.map(displaySlotValue);
    }

    function slotAxisClockTicks(slots, plot) {
      const origin = Date.parse(plot.slot_zero_start_utc || '');
      const slotMinutes = Number(plot.slot_minutes);
      if (!slots || slots.length === 0 || !Number.isFinite(origin) ||
          !Number.isFinite(slotMinutes) || slotMinutes <= 0) {
        return {};
      }

      const maxLabels = 12;
      const step = Math.max(1, Math.ceil((slots.length - 1) / (maxLabels - 1)));
      const indexes = [];
      for (let index = 0; index < slots.length; index += step) indexes.push(index);
      if (indexes[indexes.length - 1] !== slots.length - 1) indexes.push(slots.length - 1);

      const pad2 = value => String(value).padStart(2, '0');
      const tickvals = indexes.map(index => Number(slots[index]));
      const ticktext = tickvals.map(slot => {
        // Displayed slots are one-based; the provider timestamp anchors engine slot zero.
        const utc = new Date(origin + (slot - 1) * slotMinutes * 60 * 1000);
        return `${slot}<br>${pad2(utc.getUTCHours())}:${pad2(utc.getUTCMinutes())} ` +
          `${pad2(utc.getUTCDate())}/${pad2(utc.getUTCMonth() + 1)}`;
      });
      return { tickmode: 'array', tickvals, ticktext, tickfont: { size: 10 } };
    }

    function filterAndResample(slots, arrays, binSize) {
      if (!slots || slots.length === 0) return { slots: [], arrays: arrays.map(() => []) };
      
      let startIdx = 0;
      let endIdx = slots.length;

      if (selectedRange !== 'all') {
        const count = parseInt(selectedRange);
        startIdx = Math.max(0, slots.length - count);
      }

      const fSlots = slots.slice(startIdx, endIdx);
      const fArrays = arrays.map(arr => arr.slice(startIdx, endIdx));

      if (binSize <= 1 || fSlots.length === 0) {
        return { slots: displaySlotAxis(fSlots), arrays: fArrays };
      }

      // Bin aggregation
      const bSlots = [];
      const bArrays = arrays.map(() => []);

      for (let i = 0; i < fSlots.length; i += binSize) {
        const chunkSlots = fSlots.slice(i, i + binSize);
        bSlots.push(chunkSlots[0]); // representative slot label
        
        for (let aIdx = 0; aIdx < fArrays.length; aIdx++) {
          const chunkVals = fArrays[aIdx].slice(i, i + binSize);
          const validVals = chunkVals.filter(v => v !== null && v !== undefined);
          if (validVals.length === 0) {
            bArrays[aIdx].push(null);
          } else {
            const sum = validVals.reduce((acc, v) => acc + Number(v), 0);
            bArrays[aIdx].push(sum / validVals.length);
          }
        }
      }

      return { slots: displaySlotAxis(bSlots), arrays: bArrays };
    }

    function renderAll() {
      if (!rawData) return;
      renderKPIs(rawData.indicators);
      renderAssignmentPlot(rawData.assignment_plot, rawData.indicators);
      renderErrorPlot(rawData.error_plot);
      renderInputPlot(rawData.input_plot);
    }

    function renderKPIs(ind) {
      document.getElementById('kpi-current-slot').textContent = `Slot ${displaySlotValue(ind.current_slot)}`;
      const globalSlot = ind.global_slot !== null && ind.global_slot !== undefined ? displaySlotValue(ind.global_slot) : null;
      document.getElementById('kpi-global-slot').textContent = globalSlot !== null ? `Global Epoch Slot: ${globalSlot}` : `Local Horizon: ${ind.total_slots}`;
      document.getElementById('kpi-assigned-count').textContent = ind.scheduled_requests;
      document.getElementById('kpi-assigned-breakdown').textContent =
        `${ind.completed_requests} completed, ${ind.dispatched_requests} in progress, ${ind.failed_requests} failed, ${ind.pending_requests} awaiting assignment · ${ind.total_requests} total`;
      
      document.getElementById('kpi-actual-costs').textContent = `${ind.actual_carbon_cost} vs ${ind.actual_baseline_carbon_cost} gCO₂`;
      const savingEl = document.getElementById('kpi-actual-saving');
      if (ind.actual_carbon_saving_pct !== null && ind.actual_carbon_saving_pct !== undefined) {
        const isPos = ind.actual_carbon_saving_pct >= 0;
        savingEl.textContent = `Saving: ${ind.actual_carbon_saving_pct}%`;
        savingEl.className = isPos ? 'card-subtext saving-positive' : 'card-subtext saving-negative';
      } else {
        savingEl.textContent = 'Saving: -';
        savingEl.className = 'card-subtext';
      }

      document.getElementById('kpi-pending-cost').textContent = `${ind.forecasted_pending_carbon_cost} gCO₂`;
      document.getElementById('kpi-avg-exec-time').textContent = ind.overall_avg_exec_sec ? `${(ind.overall_avg_exec_sec * 1000).toFixed(1)} ms` : '-';
      document.getElementById('kpi-baseline-exec-time').textContent = ind.overall_baseline_exec_sec ? `Baseline: ${(ind.overall_baseline_exec_sec * 1000).toFixed(1)} ms` : 'Baseline: -';

      // Table & Side info
      const tbody = document.getElementById('flavour-table-body');
      tbody.innerHTML = '';
      for (const [index, [flv, st]] of Object.entries(ind.by_flavour || {}).entries()) {
        const dotColor = flavourColor(flv, index);
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><span class="flavour-dot" style="background:${dotColor};"></span>${flv}</td>
          <td>${st.count}</td>
          <td>${st.avg_exec_sec ? (st.avg_exec_sec * 1000).toFixed(1) + ' ms' : '-'}</td>
          <td>${st.baseline_avg_sec ? (st.baseline_avg_sec * 1000).toFixed(1) + ' ms' : '-'}</td>
        `;
        tbody.appendChild(tr);
      }

      const displayedError = ind.qos_profile_id ? ind.profile_error_avg : ind.global_error_avg;
      document.getElementById('info-profile').textContent = ind.qos_profile_id || 'All profiles';
      document.getElementById('info-error-semantics').textContent = ind.error_semantics || '-';
      const window = ind.error_window;
      document.getElementById('info-error-window').textContent = window
        ? `${window.past_slots} / ${window.future_slots} / ${window.past_decay_slots}`
        : '-';
      document.getElementById('info-global-error').textContent =
        displayedError !== null && displayedError !== undefined ? `${displayedError}%` : '-';
      document.getElementById('info-error-threshold').textContent =
        ind.max_error_threshold !== null && ind.max_error_threshold !== undefined
          ? `${ind.max_error_threshold}%`
          : 'Not applicable';
      document.getElementById('info-total-slots').textContent = ind.total_slots;
      document.getElementById('info-completed-reqs').textContent = ind.completed_requests;
      document.getElementById('info-dispatched-reqs').textContent = ind.dispatched_requests;
      document.getElementById('info-failed-reqs').textContent = ind.failed_requests;
      document.getElementById('info-pending-reqs').textContent = ind.pending_requests;
    }

    function renderAssignmentPlot(plot, ind) {
      const flavourCount = plot.flavours.length;
      const theme = chartTheme();
      const showGlobalSlotOccupancy = Boolean(
        selectedProfileId && plot.global_slot_occupancy_visible_by_default
      );
      const uiRevision = plotUiRevision(
        'assignments',
        showGlobalSlotOccupancy ? 'multiple-active-profiles' : 'single-active-profile',
      );
      const filtered = filterAndResample(
        plot.slots,
        [
          ...plot.flavour_counts,
          plot.global_slot_occupancy,
          plot.carbon_intensity_forecast,
          plot.carbon_intensity_actual,
        ],
        resampleBin
      );

      const slots = filtered.slots;
      const flavourSeries = filtered.arrays.slice(0, flavourCount);
      const [globalOccupancy, ciForecast, ciActual] = filtered.arrays.slice(flavourCount);
      const traces = plot.flavours.map((name, index) => ({
        x: slots,
        y: flavourSeries[index],
        name,
        type: 'bar',
        marker: { color: flavourColor(name, index) },
        hovertemplate: 'Slot %{x}<br>Requests: %{y}<extra></extra>',
      }));
      if (selectedProfileId) {
        traces.push({
          x: slots,
          y: globalOccupancy,
          name: 'Global Slot Occupancy',
          type: 'scatter',
          mode: 'lines+markers',
          // Start hidden for one active profile and shown for multiple
          // profiles. A legend click is remembered by `uirevision` after this.
          visible: showGlobalSlotOccupancy ? true : 'legendonly',
          marker: { size: 4, color: '#cbd5e1' },
          line: { color: '#cbd5e1', width: 1.5, dash: 'dash' },
          hovertemplate: 'Slot %{x}<br>Requests: %{y}<extra></extra>',
        });
      }
      traces.push(
        {
          x: slots,
          y: ciForecast,
          name: 'Carbon Intensity (Forecast)',
          type: 'scatter',
          mode: 'lines',
          line: { color: '#d62728', width: 2, dash: 'dot' },
          yaxis: 'y2',
          hovertemplate: 'Slot %{x}<br>%{y:.2f} gCO₂/kWh<extra></extra>',
        },
        {
          x: slots,
          y: ciActual,
          name: 'Carbon Intensity (Observed)',
          type: 'scatter',
          mode: 'lines+markers',
          marker: { size: 5, color: '#ef4444' },
          line: { color: '#ef4444', width: 2.2 },
          yaxis: 'y2',
          hovertemplate: 'Slot %{x}<br>%{y:.2f} gCO₂/kWh<extra></extra>',
        }
      );

      // Capacity tier shape lines: omit the implicit baseline x1 band and
      // label each subsequent tier by the request count where the next multiplier
      // starts taking effect.
      const shapes = [];
      const annotations = [];
      if (plot.capacity_tiers && plot.capacity_tiers.length > 0) {
        const tiers = plot.capacity_tiers.filter(tier => tier && typeof tier === 'object');
        if (tiers.length > 0) {
          const baselineIndex = tiers.findIndex(tier => Number(tier.multiplier) === 1);
          const startIndex = baselineIndex >= 0 ? baselineIndex + 1 : 0;

          for (let i = startIndex; i < tiers.length; i++) {
            const tier = tiers[i];
            const previousTier = i > 0 ? tiers[i - 1] : null;
            const threshold = previousTier && previousTier.max_requests != null && previousTier.max_requests !== undefined
              ? Number(previousTier.max_requests)
              : null;

            if (threshold === null || !Number.isFinite(threshold)) continue;

            shapes.push({
              type: 'line',
              xref: 'paper',
              x0: 0,
              x1: 1,
              y0: threshold,
              y1: threshold,
              line: { color: theme.annotation, width: 1.2, dash: 'dash' }
            });
            annotations.push({
              xref: 'paper',
              x: 0.99,
              y: threshold,
              xanchor: 'right',
              yanchor: 'bottom',
              text: `cap > ${threshold} (x${tier.multiplier})`,
              showarrow: false,
              font: { size: 10, color: theme.annotation }
            });
          }
        }
      }

      const layout = {
        uirevision: uiRevision,
        barmode: 'stack',
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: theme.text, family: 'inherit' },
        hoverlabel: { bgcolor: theme.hoverBackground, font: { color: theme.text } },
        margin: { l: 50, r: 50, t: 30, b: 82 },
        legend: { uirevision: uiRevision, orientation: 'h', x: 0, y: 1.15 },
        xaxis: {
          title: plot.slot_zero_start_utc ? 'Slot / UTC time' : 'Slot',
          gridcolor: theme.grid,
          ...slotAxisClockTicks(slots, plot),
          rangeslider: {
            visible: true,
            thickness: 0.05,
            bgcolor: theme.sliderBackground,
            bordercolor: theme.sliderBorder,
          }
        },
        yaxis: {
          title: 'Requests Scheduled',
          gridcolor: theme.grid,
          zerolinecolor: theme.zeroLine
        },
        yaxis2: {
          title: 'Carbon Intensity (gCO₂/kWh)',
          overlaying: 'y',
          side: 'right',
          showgrid: false,
          color: '#f87171'
        },
        shapes: shapes,
        annotations: annotations
      };

      const config = { responsive: true, displayModeBar: true, scrollZoom: true };
      Plotly.react('assignment-plot', traces, layout, config);
    }

    function renderErrorPlot(plot) {
      const theme = chartTheme();
      const uiRevision = plotUiRevision('errors');
      document.getElementById('plot-error-avg-label').firstChild.textContent =
        `Current ${plot.error_avg_label.toLowerCase()}: `;
      document.getElementById('plot-global-error-avg').textContent =
        plot.displayed_error_avg !== null && plot.displayed_error_avg !== undefined
          ? `${plot.displayed_error_avg}%`
          : '-';
      document.getElementById('plot-window-error-avg').textContent =
        plot.window_error_avg !== null && plot.window_error_avg !== undefined
          ? `${plot.window_error_avg}%`
          : '-';

      const filtered = filterAndResample(
        plot.slots,
        plot.error_by_flavour,
        resampleBin
      );
      const slots = filtered.slots;

      let historyStartIdx = 0;
      if (selectedRange !== 'all') {
        historyStartIdx = Math.max(0, plot.slots.length - parseInt(selectedRange));
      }
      const historyStartSlot = plot.slots[historyStartIdx];
      const historyEndSlot = plot.slots[plot.slots.length - 1];
      const historyIndexes = (plot.error_history_slots || [])
        .map((slot, index) => ({ slot, index }))
        .filter(({ slot }) => slot >= historyStartSlot && slot <= historyEndSlot);
      const historySlots = displaySlotAxis(historyIndexes.map(({ slot }) => slot));
      const errorHistory = historyIndexes.map(({ index }) => plot.error_history[index]);
      const windowHistory = historyIndexes.map(({ index }) => plot.window_error_history[index]);

      const traces = plot.flavours.map((name, index) => ({
        x: slots,
        y: filtered.arrays[index],
        name: `${name} (Slot Contrib)`,
        type: 'bar',
        marker: { color: flavourColor(name, index) },
        hovertemplate: 'Slot %{x}<br>Error Contribution: %{y:.2f}%<extra></extra>',
      }));
      if (historySlots.length > 0) {
        traces.push(
          {
            x: historySlots,
            y: windowHistory,
            name: 'Window Error Avg (last per slot)',
            type: 'scatter',
            mode: 'lines+markers',
            connectgaps: false,
            marker: { size: 5, color: '#eab308' },
            line: { color: '#eab308', width: 2.2 },
            hovertemplate: 'Slot %{x}<br>Window Error Avg: %{y:.2f}%<extra></extra>',
          },
          {
            x: historySlots,
            y: errorHistory,
            name: `${plot.error_avg_label} (last per slot)`,
            type: 'scatter',
            mode: 'lines+markers',
            connectgaps: false,
            marker: { size: 5, color: '#a855f7' },
            line: { color: '#a855f7', width: 2.5 },
            hovertemplate: `Slot %{x}<br>${plot.error_avg_label}: %{y:.2f}%<extra></extra>`,
          }
        );
      }

      const shapes = [];
      const annotations = [];

      const thresholds = plot.qos_profile_id
        ? [{
            profile_id: plot.qos_profile_id,
            threshold: plot.max_error_threshold,
          }]
        : (plot.profile_thresholds || []);
      const thresholdColors = ['#f87171', '#38bdf8', '#facc15', '#c084fc', '#4ade80'];
      thresholds.forEach((profile, index) => {
        if (profile.threshold === null || profile.threshold === undefined) return;
        const color = thresholdColors[index % thresholdColors.length];
        const label = plot.qos_profile_id
          ? `Threshold: ${profile.threshold}%`
          : `${profile.profile_id}: ${profile.threshold}%`;
        shapes.push({
          type: 'line',
          xref: 'paper',
          x0: 0,
          x1: 1,
          y0: profile.threshold,
          y1: profile.threshold,
          line: { color, width: 1.5, dash: 'dash' }
        });
        annotations.push({
          xref: 'paper',
          x: 0.99,
          y: profile.threshold,
          xanchor: 'right',
          yanchor: 'bottom',
          text: label,
          showarrow: false,
          font: { size: 10, color }
        });
      });

      const layout = {
        uirevision: uiRevision,
        barmode: 'stack',
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: theme.text, family: 'inherit' },
        hoverlabel: { bgcolor: theme.hoverBackground, font: { color: theme.text } },
        margin: { l: 50, r: 50, t: 30, b: 82 },
        legend: { uirevision: uiRevision, orientation: 'h', x: 0, y: 1.15 },
        xaxis: {
          title: plot.slot_zero_start_utc ? 'Slot / UTC time' : 'Slot',
          gridcolor: theme.grid,
          ...slotAxisClockTicks(slots, plot),
          rangeslider: {
            visible: true,
            thickness: 0.05,
            bgcolor: theme.sliderBackground,
            bordercolor: theme.sliderBorder,
          }
        },
        yaxis: {
          title: 'Error (%)',
          gridcolor: theme.grid,
          zerolinecolor: theme.zeroLine
        },
        shapes: shapes,
        annotations: annotations
      };

      const config = { responsive: true, displayModeBar: true, scrollZoom: true };
      Plotly.react('error-plot', traces, layout, config);
    }

    function renderInputPlot(plot) {
      const theme = chartTheme();
      const uiRevision = plotUiRevision('inputs');
      const filtered = filterAndResample(
        plot.slots,
        [plot.arrived_requests, plot.carbon_intensity_forecast, plot.carbon_intensity_actual],
        resampleBin
      );
      const slots = filtered.slots;
      const [arrived, ciForecast, ciActual] = filtered.arrays;

      const traces = [
        {
          x: slots,
          y: arrived,
          name: 'Arrived Requests',
          type: 'bar',
          marker: { color: '#0ea5e9', opacity: 0.75 },
          hovertemplate: 'Slot %{x}<br>Requests: %{y}<extra></extra>',
        },
        {
          x: slots,
          y: ciForecast,
          name: 'Forecast CI',
          type: 'scatter',
          mode: 'lines',
          line: { color: '#d62728', width: 2, dash: 'dot' },
          yaxis: 'y2',
          hovertemplate: 'Slot %{x}<br>%{y:.2f} gCO₂/kWh<extra></extra>',
        },
        {
          x: slots,
          y: ciActual,
          name: 'Observed CI',
          type: 'scatter',
          mode: 'lines+markers',
          marker: { size: 4, color: '#ef4444' },
          line: { color: '#ef4444', width: 2 },
          yaxis: 'y2',
          hovertemplate: 'Slot %{x}<br>%{y:.2f} gCO₂/kWh<extra></extra>',
        }
      ];

      const layout = {
        uirevision: uiRevision,
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: theme.text, family: 'inherit' },
        hoverlabel: { bgcolor: theme.hoverBackground, font: { color: theme.text } },
        margin: { l: 50, r: 50, t: 30, b: 82 },
        legend: { uirevision: uiRevision, orientation: 'h', x: 0, y: 1.15 },
        xaxis: {
          title: plot.slot_zero_start_utc ? 'Slot / UTC time' : 'Slot',
          gridcolor: theme.grid,
          ...slotAxisClockTicks(slots, plot),
          rangeslider: {
            visible: true,
            thickness: 0.05,
            bgcolor: theme.sliderBackground,
            bordercolor: theme.sliderBorder,
          }
        },
        yaxis: {
          title: 'Requests Arrived',
          gridcolor: theme.grid,
          zerolinecolor: theme.zeroLine
        },
        yaxis2: {
          title: 'Carbon Intensity (gCO₂/kWh)',
          overlaying: 'y',
          side: 'right',
          showgrid: false,
          color: '#f87171'
        }
      };

      const config = { responsive: true, displayModeBar: true, scrollZoom: true };
      Plotly.react('input-plot', traces, layout, config);
    }

    // The visualizer is intentionally usable without CarbonShift; live data
    // requests degrade to the empty/cached dashboard while snapshots stay local.
    refreshSnapshots().catch(error => {
      console.error("Snapshot list error:", error);
      document.getElementById('snapshot-status').textContent = String(error);
    });
    fetchData();
    syncRefreshTimer();
  </script>
</body>
</html>
"""
