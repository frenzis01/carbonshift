from __future__ import annotations

import logging
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .config import settings
from .data import get_dashboard_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("visualizer.main")

app = FastAPI(title="Carbonshift Live Visualizer")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/data")
async def api_data() -> JSONResponse:
    data = get_dashboard_data()
    return JSONResponse(data)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML_TEMPLATE


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Carbonshift Visualizer</title>
  <!-- Plotly.js -->
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {
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
      background-color: #334155;
      border-color: #475569;
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
    select {
      background-color: var(--card-bg);
      color: var(--text-main);
      border: 1px solid var(--card-border);
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 0.85rem;
      cursor: pointer;
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
      <button class="btn" onclick="fetchData()">⟳ Refresh</button>
    </div>
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
        <div class="chart-desc">Stacked requests by flavour with capacity tier thresholds and carbon intensity overlay (identical to notebook display)</div>
      </div>
    </div>
    <div id="assignment-plot" class="plot-wrapper"></div>
  </section>

  <!-- Plot 2: Average Error Over Slots -->
  <section class="chart-container">
    <div class="chart-header">
      <div>
        <div class="chart-title">Error Tracking Over Time Slots</div>
        <div class="chart-desc">Average actual error % per scheduled slot against declared threshold and global error avg</div>
      </div>
    </div>
    <div id="error-plot" class="plot-wrapper"></div>
  </section>

  <!-- Plot 3: Input Requests & Carbon Forecast Wave -->
  <section class="chart-container">
    <div class="chart-header">
      <div>
        <div class="chart-title">Input Load & Carbon Intensity Wave</div>
        <div class="chart-desc">Arrived requests by slot vs forecasted and observed carbon intensity</div>
      </div>
    </div>
    <div id="input-plot" class="plot-wrapper"></div>
  </section>

  <!-- Detailed Breakdowns -->
  <section class="breakdown-grid">
    <div class="card">
      <div class="card-label">Execution Time By Flavour</div>
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
          <tr><td>Global Error Avg</td><td id="info-global-error">-</td></tr>
          <tr><td>Error Threshold</td><td id="info-error-threshold">-</td></tr>
          <tr><td>Horizon Total Slots</td><td id="info-total-slots">-</td></tr>
          <tr><td>Completed Requests</td><td id="info-completed-reqs">-</td></tr>
          <tr><td>Pending Requests</td><td id="info-pending-reqs">-</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <script>
    let rawData = null;
    let selectedRange = 'all';
    let resampleBin = 1;
    let autoRefresh = true;
    let refreshTimer = null;

    const FLAVOUR_COLORS = {
      'Fast': '#1f77b4',
      'Balanced': '#2ca02c',
      'Accurate': '#ff7f0e'
    };

    async function fetchData() {
      try {
        const resp = await fetch('/api/data');
        if (!resp.ok) return;
        rawData = await resp.json();
        renderAll();
      } catch (e) {
        console.error("Fetch error:", e);
      }
    }

    function toggleAutoRefresh() {
      autoRefresh = !autoRefresh;
      const btn = document.getElementById('toggle-refresh-btn');
      const badge = document.getElementById('live-badge');
      if (autoRefresh) {
        btn.textContent = 'Pause';
        btn.className = 'btn btn-toggle';
        badge.textContent = '● Live Polling';
        badge.style.color = 'var(--accent-blue)';
        if (!refreshTimer) refreshTimer = setInterval(fetchData, 2000);
      } else {
        btn.textContent = 'Resume';
        btn.className = 'btn btn-toggle paused';
        badge.textContent = '⏸ Paused';
        badge.style.color = 'var(--accent-red)';
        clearInterval(refreshTimer);
        refreshTimer = null;
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
        return { slots: fSlots, arrays: fArrays };
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

      return { slots: bSlots, arrays: bArrays };
    }

    function renderAll() {
      if (!rawData) return;
      renderKPIs(rawData.indicators);
      renderAssignmentPlot(rawData.assignment_plot, rawData.indicators);
      renderErrorPlot(rawData.error_plot);
      renderInputPlot(rawData.input_plot);
    }

    function renderKPIs(ind) {
      document.getElementById('kpi-current-slot').textContent = `Slot ${ind.current_slot}`;
      document.getElementById('kpi-global-slot').textContent = ind.global_slot ? `Global Epoch Slot: ${ind.global_slot}` : `Local Horizon: ${ind.total_slots}`;
      document.getElementById('kpi-assigned-count').textContent = ind.scheduled_requests;
      document.getElementById('kpi-assigned-breakdown').textContent = `${ind.completed_requests} completed, ${ind.pending_requests} pending`;
      
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
      for (const [flv, st] of Object.entries(ind.by_flavour || {})) {
        const dotColor = FLAVOUR_COLORS[flv] || '#7f7f7f';
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><span class="flavour-dot" style="background:${dotColor};"></span>${flv}</td>
          <td>${st.count}</td>
          <td>${st.avg_exec_sec ? (st.avg_exec_sec * 1000).toFixed(1) + ' ms' : '-'}</td>
          <td>${st.baseline_avg_sec ? (st.baseline_avg_sec * 1000).toFixed(1) + ' ms' : '-'}</td>
        `;
        tbody.appendChild(tr);
      }

      document.getElementById('info-global-error').textContent = ind.global_error_avg !== null ? `${ind.global_error_avg}%` : '-';
      document.getElementById('info-error-threshold').textContent = `${ind.max_error_threshold}%`;
      document.getElementById('info-total-slots').textContent = ind.total_slots;
      document.getElementById('info-completed-reqs').textContent = ind.completed_requests;
      document.getElementById('info-pending-reqs').textContent = ind.pending_requests;
    }

    function renderAssignmentPlot(plot, ind) {
      const filtered = filterAndResample(
        plot.slots,
        [plot.fast, plot.balanced, plot.accurate, plot.carbon_intensity_forecast, plot.carbon_intensity_actual],
        resampleBin
      );

      const slots = filtered.slots;
      const [fast, balanced, accurate, ciForecast, ciActual] = filtered.arrays;

      const traces = [
        {
          x: slots,
          y: fast,
          name: 'Fast',
          type: 'bar',
          marker: { color: FLAVOUR_COLORS['Fast'] },
        },
        {
          x: slots,
          y: balanced,
          name: 'Balanced',
          type: 'bar',
          marker: { color: FLAVOUR_COLORS['Balanced'] },
        },
        {
          x: slots,
          y: accurate,
          name: 'Accurate',
          type: 'bar',
          marker: { color: FLAVOUR_COLORS['Accurate'] },
        },
        {
          x: slots,
          y: ciForecast,
          name: 'Carbon Intensity (Forecast)',
          type: 'scatter',
          mode: 'lines',
          line: { color: '#d62728', width: 2, dash: 'dot' },
          yaxis: 'y2'
        },
        {
          x: slots,
          y: ciActual,
          name: 'Carbon Intensity (Observed)',
          type: 'scatter',
          mode: 'lines+markers',
          marker: { size: 5, color: '#ef4444' },
          line: { color: '#ef4444', width: 2.2 },
          yaxis: 'y2'
        }
      ];

      // Capacity tier shape lines
      const shapes = [];
      const annotations = [];
      if (plot.capacity_tiers && plot.capacity_tiers.length > 0) {
        for (const tier of plot.capacity_tiers) {
          if (tier.max_requests) {
            shapes.push({
              type: 'line',
              xref: 'paper',
              x0: 0,
              x1: 1,
              y0: tier.max_requests,
              y1: tier.max_requests,
              line: { color: '#94a3b8', width: 1.2, dash: 'dash' }
            });
            annotations.push({
              xref: 'paper',
              x: 0.99,
              y: tier.max_requests,
              xanchor: 'right',
              yanchor: 'bottom',
              text: `cap <= ${tier.max_requests} (x${tier.multiplier})`,
              showarrow: false,
              font: { size: 10, color: '#94a3b8' }
            });
          }
        }
      }

      const layout = {
        barmode: 'stack',
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: '#f8fafc', family: 'inherit' },
        margin: { l: 50, r: 50, t: 30, b: 60 },
        legend: { orientation: 'h', x: 0, y: 1.15 },
        xaxis: {
          title: 'Scheduled Slot',
          gridcolor: '#334155',
          rangeslider: { visible: true, thickness: 0.05, bgcolor: '#1e293b' }
        },
        yaxis: {
          title: 'Requests Scheduled',
          gridcolor: '#334155',
          zerolinecolor: '#475569'
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
      const filtered = filterAndResample(plot.slots, [plot.error_avg], resampleBin);
      const slots = filtered.slots;
      const [errorAvg] = filtered.arrays;

      const traces = [
        {
          x: slots,
          y: errorAvg,
          name: 'Avg Error %',
          type: 'scatter',
          mode: 'lines+markers',
          marker: { size: 6, color: '#38bdf8' },
          line: { color: '#38bdf8', width: 2 }
        }
      ];

      const shapes = [];
      const annotations = [];

      if (plot.max_error_threshold) {
        shapes.push({
          type: 'line',
          xref: 'paper',
          x0: 0,
          x1: 1,
          y0: plot.max_error_threshold,
          y1: plot.max_error_threshold,
          line: { color: '#f87171', width: 1.5, dash: 'dash' }
        });
        annotations.push({
          xref: 'paper',
          x: 0.99,
          y: plot.max_error_threshold,
          xanchor: 'right',
          yanchor: 'bottom',
          text: `Threshold: ${plot.max_error_threshold}%`,
          showarrow: false,
          font: { size: 10, color: '#f87171' }
        });
      }

      if (plot.global_error_avg) {
        shapes.push({
          type: 'line',
          xref: 'paper',
          x0: 0,
          x1: 1,
          y0: plot.global_error_avg,
          y1: plot.global_error_avg,
          line: { color: '#a78bfa', width: 1.2, dash: 'dot' }
        });
        annotations.push({
          xref: 'paper',
          x: 0.01,
          y: plot.global_error_avg,
          xanchor: 'left',
          yanchor: 'bottom',
          text: `Global Avg: ${plot.global_error_avg}%`,
          showarrow: false,
          font: { size: 10, color: '#a78bfa' }
        });
      }

      const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: '#f8fafc', family: 'inherit' },
        margin: { l: 50, r: 50, t: 30, b: 60 },
        legend: { orientation: 'h', x: 0, y: 1.15 },
        xaxis: {
          title: 'Scheduled Slot',
          gridcolor: '#334155',
          rangeslider: { visible: true, thickness: 0.05, bgcolor: '#1e293b' }
        },
        yaxis: {
          title: 'Average Error (%)',
          gridcolor: '#334155',
          zerolinecolor: '#475569'
        },
        shapes: shapes,
        annotations: annotations
      };

      const config = { responsive: true, displayModeBar: true, scrollZoom: true };
      Plotly.react('error-plot', traces, layout, config);
    }

    function renderInputPlot(plot) {
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
          marker: { color: '#0ea5e9', opacity: 0.75 }
        },
        {
          x: slots,
          y: ciForecast,
          name: 'Forecast CI',
          type: 'scatter',
          mode: 'lines',
          line: { color: '#d62728', width: 2, dash: 'dot' },
          yaxis: 'y2'
        },
        {
          x: slots,
          y: ciActual,
          name: 'Observed CI',
          type: 'scatter',
          mode: 'lines+markers',
          marker: { size: 4, color: '#ef4444' },
          line: { color: '#ef4444', width: 2 },
          yaxis: 'y2'
        }
      ];

      const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: '#f8fafc', family: 'inherit' },
        margin: { l: 50, r: 50, t: 30, b: 60 },
        legend: { orientation: 'h', x: 0, y: 1.15 },
        xaxis: {
          title: 'Arrival Slot',
          gridcolor: '#334155',
          rangeslider: { visible: true, thickness: 0.05, bgcolor: '#1e293b' }
        },
        yaxis: {
          title: 'Requests Arrived',
          gridcolor: '#334155',
          zerolinecolor: '#475569'
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

    // Initial Fetch & Start Timer
    fetchData();
    refreshTimer = setInterval(fetchData, 2000);
  </script>
</body>
</html>
"""
