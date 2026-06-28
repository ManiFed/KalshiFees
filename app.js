import {
  categoryTotals,
  computeMetrics,
  filterRows,
  formatCurrency,
  formatDateLabel,
  groupRows,
  normalizeRows,
  parseCsv,
} from './src/data.js';

const state = {
  cadence: 'daily',
  window: 'first-trade',
  rows: [],
  dataState: 'loading',
  lastUpdated: '',
};

const labels = {
  daily: 'Daily',
  weekly: 'Weekly',
  monthly: 'Monthly',
  'first-trade': 'since the first trade',
  'last-year': 'over the last year',
  'last-6-months': 'over the last 6 months',
  'last-month': 'over the last month',
};

const periodLabels = {
  daily: 'day',
  weekly: 'week',
  monthly: 'month',
};

const palette = {
  accent: '#2ee6a8',
  accentSoft: 'rgba(46, 230, 168, 0.16)',
  accentFill: 'rgba(46, 230, 168, 0.32)',
  ink: '#f4f1ea',
  muted: '#8f877c',
  grid: 'rgba(255, 255, 255, 0.08)',
  bars: ['#2ee6a8', '#5b9fd4', '#d47f8f', '#d4a94a', '#9a84d4', '#7fb364', '#c49262'],
};

const logoImage = new Image();
logoImage.src = './assets/supercycle-logo.png';
logoImage.addEventListener('load', () => {
  if (state.rows.length) render();
});

const elements = {
  cadenceControl: document.querySelector('#cadenceControl'),
  windowControl: document.querySelector('#windowControl'),
  cadenceMenu: document.querySelector('#cadenceMenu'),
  windowMenu: document.querySelector('#windowMenu'),
  dataStatus: document.querySelector('#dataStatus'),
  uploadLink: document.querySelector('#uploadLink'),
  fileInput: document.querySelector('#fileInput'),
  totalFees: document.querySelector('#totalFees'),
  totalDelta: document.querySelector('#totalDelta'),
  avgLabel: document.querySelector('#avgLabel'),
  avgFees: document.querySelector('#avgFees'),
  avgDelta: document.querySelector('#avgDelta'),
  runRate: document.querySelector('#runRate'),
  runRateDelta: document.querySelector('#runRateDelta'),
  highestDay: document.querySelector('#highestDay'),
  highestDate: document.querySelector('#highestDate'),
  revenueSubtitle: document.querySelector('#revenueSubtitle'),
  categoryTrendLabel: document.querySelector('#categoryTrendLabel'),
  revenueChart: document.querySelector('#revenueChart'),
  categoryChart: document.querySelector('#categoryChart'),
  runRateChart: document.querySelector('#runRateChart'),
  mixChart: document.querySelector('#mixChart'),
};

loadData();
bindMenus();

async function loadData() {
  setDataStatus('loading', 'Loading fee data…');
  try {
    const response = await fetch('./kalshi_fee_daily.csv', { cache: 'no-store' });
    if (!response.ok) throw new Error('CSV not found');
    const text = await response.text();
    const parsed = normalizeRows(parseCsv(text));
    if (!parsed.length) throw new Error('CSV is empty');
    state.rows = parsed;
    state.dataState = 'live';
    state.lastUpdated = parsed.at(-1).date;
    setDataStatus('live', `Live fee data through ${formatDateLabel(state.lastUpdated)}`);
    elements.uploadLink.hidden = true;
  } catch {
    state.rows = [];
    state.dataState = 'error';
    state.lastUpdated = '';
    setDataStatus(
      'error',
      'Fee data unavailable — waiting for collector output',
    );
    elements.uploadLink.hidden = false;
  }
  render();
}

function setDataStatus(kind, message) {
  elements.dataStatus.textContent = message;
  elements.dataStatus.className = `status-pill ${kind}`;
}

function bindMenus() {
  bindMenu(elements.cadenceControl, elements.cadenceMenu);
  bindMenu(elements.windowControl, elements.windowMenu);

  elements.cadenceMenu.addEventListener('click', (event) => {
    const cadence = event.target.dataset.cadence;
    if (!cadence) return;
    state.cadence = cadence;
    closeMenus();
    render();
  });

  elements.windowMenu.addEventListener('click', (event) => {
    const windowName = event.target.dataset.window;
    if (!windowName) return;
    state.window = windowName;
    closeMenus();
    render();
  });

  elements.fileInput.addEventListener('change', async (event) => {
    const [file] = event.target.files;
    if (!file) return;
    state.rows = normalizeRows(parseCsv(await file.text()));
    state.dataState = 'live';
    state.lastUpdated = state.rows.at(-1)?.date || '';
    setDataStatus('live', `Loaded ${file.name}`);
    elements.uploadLink.hidden = true;
    render();
  });

  window.addEventListener('resize', debounce(render, 120));
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.menu') && !event.target.closest('.title-control')) closeMenus();
  });
}

function bindMenu(control, menu) {
  control.addEventListener('click', () => {
    const wasHidden = menu.hidden;
    closeMenus();
    if (wasHidden) {
      const rect = control.getBoundingClientRect();
      menu.style.left = `${Math.min(rect.left, window.innerWidth - 240)}px`;
      menu.style.top = `${rect.bottom + 8}px`;
      menu.hidden = false;
      control.setAttribute('aria-expanded', 'true');
    }
  });
}

function closeMenus() {
  elements.cadenceMenu.hidden = true;
  elements.windowMenu.hidden = true;
  elements.cadenceControl.setAttribute('aria-expanded', 'false');
  elements.windowControl.setAttribute('aria-expanded', 'false');
}

function render() {
  elements.cadenceControl.textContent = labels[state.cadence];
  elements.windowControl.textContent = labels[state.window];
  elements.revenueSubtitle.textContent = `${labels[state.cadence]} fees and cumulative fees over time`;

  if (!state.rows.length) {
    renderEmpty();
    return;
  }

  const filteredRows = filterRows(state.rows, state.window);
  const groupedRows = groupRows(filteredRows, state.cadence);
  const metrics = computeMetrics(filteredRows);
  const groupedMetrics = computeMetrics(groupedRows);
  const categories = categoryTotals(filteredRows);
  const topCategory = categories[0]?.[0];

  elements.totalFees.textContent = formatCurrency(metrics.totalFees);
  elements.totalDelta.textContent = metrics.firstDate
    ? `${formatDateLabel(metrics.firstDate)} → ${formatDateLabel(metrics.lastDate)}`
    : 'No loaded history';
  elements.avgLabel.textContent = `${labels[state.cadence]} average fees`;
  elements.avgFees.textContent = formatCurrency(groupedMetrics.averageDailyFees);
  elements.avgDelta.textContent = `Per ${periodLabels[state.cadence]} in window`;
  elements.runRate.textContent = `${formatCurrency(metrics.trailing30Annualized)}/yr`;
  elements.runRateDelta.textContent = `${formatCurrency(metrics.trailing30Fees)} fees in latest 30 days`;
  elements.highestDay.textContent = formatCurrency(metrics.highestFeeDay?.daily_fee || 0);
  elements.highestDate.textContent = metrics.highestFeeDay
    ? formatDateLabel(metrics.highestFeeDay.date)
    : 'No data';
  elements.categoryTrendLabel.textContent = topCategory
    ? `${titleCase(topCategory)} fees in the selected window`
    : 'No category columns in the loaded CSV';

  drawRevenueChart(elements.revenueChart, groupedRows, state.cadence);
  drawCategoryTrendChart(elements.categoryChart, groupedRows, categories);
  drawRunRateChart(elements.runRateChart, groupRows(filteredRows, 'monthly'));
  drawCategoryChart(elements.mixChart, categories);
}

function renderEmpty() {
  const empty = '—';
  elements.totalFees.textContent = empty;
  elements.avgFees.textContent = empty;
  elements.runRate.textContent = empty;
  elements.highestDay.textContent = empty;
  elements.totalDelta.textContent = 'Run scripts/kalshi_fee_calculator.py to generate kalshi_fee_daily.csv';
  elements.avgDelta.textContent = 'No fee series loaded';
  elements.runRateDelta.textContent = 'Collector updates hourly on GitHub Actions';
  elements.highestDate.textContent = 'No data';

  for (const canvas of [
    elements.revenueChart,
    elements.categoryChart,
    elements.runRateChart,
    elements.mixChart,
  ]) {
    drawEmptyPanel(canvas, 'Fee data not loaded yet');
  }
}

function drawRevenueChart(canvas, rows, cadence) {
  const chart = setupCanvas(canvas);
  const { ctx, width, height, dpr } = chart;
  const pad = { top: 20, right: 88, bottom: 72, left: 78 };
  const plot = bounds(width, height, pad);
  const maxFee = Math.max(...rows.map((row) => row.daily_fee), 1);
  const maxCumulative = Math.max(...rows.map((row) => row.cumulative_fee), 1);

  drawGrid(ctx, plot, 5, (value) => formatCurrency(value * maxFee));
  const barWidth = Math.max(2, plot.width / Math.max(rows.length, 1) * 0.68);

  rows.forEach((row, index) => {
    const x = xAt(plot, index, rows.length);
    const h = (row.daily_fee / maxFee) * plot.height;
    ctx.fillStyle = palette.accentFill;
    ctx.fillRect(x - barWidth / 2, plot.bottom - h, barWidth, h);
  });

  drawLine(ctx, rows.map((row, index) => ({
    x: xAt(plot, index, rows.length),
    y: plot.bottom - (row.cumulative_fee / maxCumulative) * plot.height,
  })), palette.ink, 2.6 * dpr);

  drawAxisLabels(ctx, plot, rows, cadence);
  drawRightAxis(ctx, plot, maxCumulative);
  drawLogoOverlay(ctx, plot);
}

function drawCategoryTrendChart(canvas, rows, categories) {
  const chart = setupCanvas(canvas);
  const { ctx, width, height, dpr } = chart;
  const pad = { top: 20, right: 24, bottom: 72, left: 78 };
  const plot = bounds(width, height, pad);
  const topCategory = categories[0]?.[0];
  const values = rows.map((row) => (topCategory ? (row.categories?.[topCategory] || 0) : row.daily_fee));
  const max = Math.max(...values, 1);
  drawGrid(ctx, plot, 5, (value) => formatCurrency(value * max));
  const points = values.map((value, index) => ({
    x: xAt(plot, index, values.length),
    y: plot.bottom - (value / max) * plot.height,
  }));
  drawArea(ctx, points, plot, palette.accentSoft);
  drawLine(ctx, points, palette.accent, 2.4 * dpr);
  drawAxisLabels(ctx, plot, rows, state.cadence);
  drawLogoOverlay(ctx, plot);
}

function drawCategoryChart(canvas, categories) {
  const { ctx, width, height } = setupCanvas(canvas);
  const pad = { top: 24, right: 18, bottom: 42, left: 132 };
  const plot = bounds(width, height, pad);
  const data = categories.slice(0, 7);
  const max = Math.max(...data.map(([, value]) => value), 1);
  const gap = 14;
  const barHeight = Math.max(20, (plot.height - gap * Math.max(data.length - 1, 0)) / Math.max(data.length, 1));

  ctx.font = font(14);
  ctx.textBaseline = 'middle';
  data.forEach(([category, value], index) => {
    const y = plot.top + index * (barHeight + gap);
    ctx.fillStyle = palette.muted;
    ctx.textAlign = 'right';
    ctx.fillText(titleCase(category), plot.left - 14, y + barHeight / 2);
    ctx.fillStyle = palette.bars[index % palette.bars.length];
    ctx.fillRect(plot.left, y, (value / max) * plot.width, barHeight);
    ctx.fillStyle = palette.ink;
    ctx.textAlign = 'left';
    ctx.fillText(formatCurrency(value), plot.left + (value / max) * plot.width + 10, y + barHeight / 2);
  });

  if (!data.length) {
    drawEmptyPanel(canvas, 'No category fee columns found');
    return;
  }
  drawLogoOverlay(ctx, plot);
}

function drawRunRateChart(canvas, rows) {
  const { ctx, width, height, dpr } = setupCanvas(canvas);
  const pad = { top: 20, right: 20, bottom: 58, left: 78 };
  const plot = bounds(width, height, pad);
  const recent = rows.slice(-18);
  const max = Math.max(...recent.map((row) => row.daily_fee), 1);
  drawGrid(ctx, plot, 4, (value) => formatCurrency(value * max));
  const points = recent.map((row, index) => ({
    x: xAt(plot, index, recent.length),
    y: plot.bottom - (row.daily_fee / max) * plot.height,
  }));
  drawArea(ctx, points, plot, palette.accentSoft);
  drawLine(ctx, points, palette.accent, 2.4 * dpr);
  drawAxisLabels(ctx, plot, recent, 'monthly');
  drawLogoOverlay(ctx, plot);
}

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, rect.width) * dpr;
  const height = Number(canvas.getAttribute('height')) * dpr;
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, width, height);
  ctx.scale(dpr, dpr);
  return { ctx, width: width / dpr, height: height / dpr, dpr };
}

function bounds(width, height, pad) {
  return {
    left: pad.left,
    top: pad.top,
    right: width - pad.right,
    bottom: height - pad.bottom,
    width: width - pad.left - pad.right,
    height: height - pad.top - pad.bottom,
  };
}

function drawGrid(ctx, plot, count, labeler) {
  ctx.strokeStyle = palette.grid;
  ctx.lineWidth = 1;
  ctx.font = font(13);
  ctx.fillStyle = palette.muted;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let index = 0; index <= count; index += 1) {
    const ratio = index / count;
    const y = plot.bottom - ratio * plot.height;
    ctx.beginPath();
    ctx.moveTo(plot.left, y);
    ctx.lineTo(plot.right, y);
    ctx.stroke();
    ctx.fillText(labeler(ratio), plot.left - 12, y);
  }
}

function drawRightAxis(ctx, plot, max) {
  ctx.font = font(13);
  ctx.fillStyle = palette.muted;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  for (let index = 0; index <= 4; index += 1) {
    const ratio = index / 4;
    const y = plot.bottom - ratio * plot.height;
    ctx.fillText(formatCurrency(ratio * max), plot.right + 12, y);
  }
}

function drawLine(ctx, points, color, width) {
  if (points.length < 2) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.stroke();
}

function drawArea(ctx, points, plot, color) {
  if (points.length < 2) return;
  ctx.fillStyle = color;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, plot.bottom);
    ctx.lineTo(point.x, point.y);
  });
  ctx.lineTo(points.at(-1).x, plot.bottom);
  ctx.closePath();
  ctx.fill();
}

function drawAxisLabels(ctx, plot, rows, cadence) {
  if (!rows.length) return;
  const count = Math.min(8, rows.length);
  ctx.font = font(12);
  ctx.fillStyle = palette.muted;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let index = 0; index < count; index += 1) {
    const rowIndex = Math.round((index / Math.max(count - 1, 1)) * (rows.length - 1));
    const x = Math.max(plot.left + 34, Math.min(plot.right - 12, xAt(plot, rowIndex, rows.length)));
    ctx.save();
    ctx.translate(x, plot.bottom + 30);
    ctx.rotate(-Math.PI / 4);
    ctx.fillText(formatDateLabel(rows[rowIndex].date, cadence), 0, 0);
    ctx.restore();
  }
}

function drawLogoOverlay(ctx, plot) {
  if (!logoImage.complete || !logoImage.naturalWidth) return;
  const aspect = logoImage.naturalWidth / logoImage.naturalHeight;
  const maxWidth = plot.width * 0.42;
  const width = Math.min(maxWidth, 220);
  const height = width / aspect;
  const x = plot.left + (plot.width - width) / 2;
  const y = plot.top + (plot.height - height) / 2;
  ctx.save();
  ctx.globalAlpha = 0.16;
  ctx.drawImage(logoImage, x, y, width, height);
  ctx.restore();
}

function drawEmptyPanel(canvas, message) {
  const { ctx, width, height } = setupCanvas(canvas);
  const plot = bounds(width, height, { top: 20, right: 20, bottom: 40, left: 20 });
  ctx.fillStyle = palette.muted;
  ctx.font = font(15);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(message, width / 2, height / 2);
  drawLogoOverlay(ctx, plot);
}

function xAt(plot, index, length) {
  if (length <= 1) return plot.left + plot.width / 2;
  return plot.left + (index / (length - 1)) * plot.width;
}

function font(size) {
  return `${size}px ${getComputedStyle(document.body).fontFamily}`;
}

function titleCase(text) {
  return text.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

function debounce(fn, wait) {
  let id;
  return (...args) => {
    clearTimeout(id);
    id = setTimeout(() => fn(...args), wait);
  };
}