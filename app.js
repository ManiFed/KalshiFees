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
  usingSample: false,
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

const colors = ['#20d895', '#4877aa', '#b95f70', '#b88738', '#7660a8', '#6d8f4e', '#9b6a42'];

const elements = {
  cadenceControl: document.querySelector('#cadenceControl'),
  windowControl: document.querySelector('#windowControl'),
  cadenceMenu: document.querySelector('#cadenceMenu'),
  windowMenu: document.querySelector('#windowMenu'),
  dataNotice: document.querySelector('#dataNotice'),
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
  revenueChart: document.querySelector('#revenueChart'),
  categoryChart: document.querySelector('#categoryChart'),
  runRateChart: document.querySelector('#runRateChart'),
  mixChart: document.querySelector('#mixChart'),
};

loadData();
bindMenus();

async function loadData() {
  try {
    const response = await fetch('./kalshi_fee_daily.csv', { cache: 'no-store' });
    if (!response.ok) throw new Error('No local CSV found');
    const text = await response.text();
    state.rows = normalizeRows(parseCsv(text));
    state.usingSample = false;
  } catch {
    state.rows = normalizeRows(generateSampleRows());
    state.usingSample = true;
  }
  render();
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
    state.usingSample = false;
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
      menu.style.left = `${Math.min(rect.left, window.innerWidth - 230)}px`;
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
  const filteredRows = filterRows(state.rows, state.window);
  const groupedRows = groupRows(filteredRows, state.cadence);
  const metrics = computeMetrics(filteredRows);
  const groupedMetrics = computeMetrics(groupedRows);

  elements.cadenceControl.textContent = labels[state.cadence];
  elements.windowControl.textContent = labels[state.window];
  elements.dataNotice.hidden = !state.usingSample;
  elements.revenueSubtitle.textContent = `${labels[state.cadence]} fee and cumulative fee over time`;

  elements.totalFees.textContent = formatCurrency(metrics.totalFees);
  elements.totalDelta.textContent = metrics.firstDate ? `From ${formatDateLabel(metrics.firstDate)} to ${formatDateLabel(metrics.lastDate)}` : 'No loaded history';
  elements.avgLabel.textContent = `${labels[state.cadence]} Average Fees`;
  elements.avgFees.textContent = formatCurrency(groupedMetrics.averageDailyFees);
  elements.avgDelta.textContent = `Average per ${periodLabels[state.cadence]}`;
  elements.runRate.textContent = `${formatCurrency(metrics.trailing30Annualized)}/yr`;
  elements.runRateDelta.textContent = `${formatCurrency(metrics.trailing30Fees)} in latest month`;
  elements.highestDay.textContent = formatCurrency(metrics.highestFeeDay?.daily_fee || 0);
  elements.highestDate.textContent = metrics.highestFeeDay ? formatDateLabel(metrics.highestFeeDay.date) : 'No data';

  drawRevenueChart(elements.revenueChart, groupedRows, state.cadence);
  drawCategoryTrendChart(elements.categoryChart, groupedRows, categoryTotals(filteredRows));
  drawRunRateChart(elements.runRateChart, groupRows(filteredRows, 'monthly'));
  drawCategoryChart(elements.mixChart, categoryTotals(filteredRows));
}

function drawRevenueChart(canvas, rows, cadence) {
  const chart = setupCanvas(canvas);
  const { ctx, width, height, dpr } = chart;
  const pad = { top: 18, right: 92, bottom: 70, left: 72 };
  const plot = bounds(width, height, pad);
  const maxFee = Math.max(...rows.map((row) => row.daily_fee), 1);
  const maxCumulative = Math.max(...rows.map((row) => row.cumulative_fee), 1);

  drawGrid(ctx, plot, 5, (value) => formatCurrency(value * maxFee));
  const barWidth = Math.max(1, plot.width / Math.max(rows.length, 1) * 0.72);

  rows.forEach((row, index) => {
    const x = xAt(plot, index, rows.length);
    const h = (row.daily_fee / maxFee) * plot.height;
    ctx.fillStyle = 'rgba(32, 216, 149, 0.22)';
    ctx.fillRect(x - barWidth / 2, plot.bottom - h, barWidth, h);
  });

  drawLine(ctx, rows.map((row, index) => ({
    x: xAt(plot, index, rows.length),
    y: plot.bottom - (row.cumulative_fee / maxCumulative) * plot.height,
  })), '#242424', 2.4 * dpr);

  drawAxisLabels(ctx, plot, rows, cadence);
  drawRightAxis(ctx, plot, maxCumulative);
  drawWatermark(ctx, plot);
}

function drawCategoryTrendChart(canvas, rows, categories) {
  const chart = setupCanvas(canvas);
  const { ctx, width, height, dpr } = chart;
  const pad = { top: 18, right: 24, bottom: 70, left: 72 };
  const plot = bounds(width, height, pad);
  const topCategory = categories[0]?.[0];
  const values = rows.map((row) => topCategory ? (row.categories?.[topCategory] || 0) : row.daily_fee);
  const max = Math.max(...values, 1);
  drawGrid(ctx, plot, 5, (value) => formatCurrency(value * max));
  const points = values.map((value, index) => ({
    x: xAt(plot, index, values.length),
    y: plot.bottom - (value / max) * plot.height,
  }));
  drawArea(ctx, points, plot, 'rgba(32, 216, 149, 0.12)');
  drawLine(ctx, points, '#20d895', 2 * dpr);
  drawAxisLabels(ctx, plot, rows, state.cadence);
  drawWatermark(ctx, plot);
}

function drawCategoryChart(canvas, categories) {
  const { ctx, width, height } = setupCanvas(canvas);
  const pad = { top: 24, right: 18, bottom: 42, left: 130 };
  const plot = bounds(width, height, pad);
  const data = categories.slice(0, 7);
  const max = Math.max(...data.map(([, value]) => value), 1);
  const gap = 12;
  const barHeight = Math.max(18, (plot.height - gap * Math.max(data.length - 1, 0)) / Math.max(data.length, 1));

  ctx.font = font(15);
  ctx.textBaseline = 'middle';
  data.forEach(([category, value], index) => {
    const y = plot.top + index * (barHeight + gap);
    ctx.fillStyle = '#776b5d';
    ctx.textAlign = 'right';
    ctx.fillText(titleCase(category), plot.left - 14, y + barHeight / 2);
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(plot.left, y, (value / max) * plot.width, barHeight);
    ctx.fillStyle = '#2c241c';
    ctx.textAlign = 'left';
    ctx.fillText(formatCurrency(value), plot.left + (value / max) * plot.width + 8, y + barHeight / 2);
  });

  if (!data.length) drawEmpty(ctx, width, height);
}

function drawRunRateChart(canvas, rows) {
  const { ctx, width, height, dpr } = setupCanvas(canvas);
  const pad = { top: 18, right: 20, bottom: 58, left: 72 };
  const plot = bounds(width, height, pad);
  const recent = rows.slice(-18);
  const max = Math.max(...recent.map((row) => row.daily_fee), 1);
  drawGrid(ctx, plot, 4, (value) => formatCurrency(value * max));
  const points = recent.map((row, index) => ({
    x: xAt(plot, index, recent.length),
    y: plot.bottom - (row.daily_fee / max) * plot.height,
  }));
  drawArea(ctx, points, plot, 'rgba(32, 216, 149, 0.14)');
  drawLine(ctx, points, '#20d895', 2 * dpr);
  drawAxisLabels(ctx, plot, recent, 'monthly');
  drawWatermark(ctx, plot);
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
  ctx.strokeStyle = 'rgba(119, 107, 93, 0.22)';
  ctx.lineWidth = 1;
  ctx.font = font(14);
  ctx.fillStyle = '#776b5d';
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
  ctx.font = font(14);
  ctx.fillStyle = '#776b5d';
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

function drawWatermark(ctx, plot) {
  ctx.save();
  ctx.globalAlpha = 0.14;
  ctx.fillStyle = '#2c241c';
  ctx.font = '700 31px Georgia, "Times New Roman", serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('kalshifees', plot.left + plot.width / 2, plot.top + plot.height / 2);
  ctx.restore();
}

function drawAxisLabels(ctx, plot, rows, cadence) {
  if (!rows.length) return;
  const count = Math.min(8, rows.length);
  ctx.font = font(13);
  ctx.fillStyle = '#776b5d';
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

function drawEmpty(ctx, width, height) {
  ctx.fillStyle = '#776b5d';
  ctx.font = font(16);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('No category columns found', width / 2, height / 2);
}

function xAt(plot, index, length) {
  if (length <= 1) return plot.left + plot.width / 2;
  return plot.left + (index / (length - 1)) * plot.width;
}

function font(size) {
  return `${size}px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
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

function generateSampleRows() {
  const rows = [];
  const start = new Date('2021-06-27T00:00:00Z');
  const end = new Date('2026-06-28T00:00:00Z');
  let cumulative = 0;
  for (let time = start.getTime(), index = 0; time <= end.getTime(); time += 24 * 60 * 60 * 1000, index += 1) {
    const date = new Date(time).toISOString().slice(0, 10);
    const growth = Math.pow(index / 1828, 3.8);
    const seasonality = 0.72 + Math.sin(index / 24) * 0.18 + Math.cos(index / 61) * 0.1;
    const eraLift = date >= '2025-05-01' ? 2.25 : 1;
    const sportsSpike = date >= '2024-10-01' && date <= '2025-02-15' ? 2.1 : 1;
    const daily = Math.max(0, 5500 + 760000 * growth * seasonality * eraLift * sportsSpike);
    const perps = date >= '2026-05-29' ? daily * 0.1 : 0;
    const sports = daily * (date >= '2024-10-01' ? 0.42 : 0.18);
    const economics = daily * 0.34;
    const politics = daily * (date < '2024-11-10' && date > '2024-06-01' ? 0.32 : 0.08);
    const weather = daily * 0.09;
    const crypto = Math.max(0, daily - sports - economics - politics - weather);
    cumulative += daily + perps;
    rows.push({
      date,
      daily_fee: Math.round((daily + perps) * 100) / 100,
      cumulative_fee: Math.round(cumulative * 100) / 100,
      cat_sports: Math.round(sports * 100) / 100,
      cat_economics: Math.round(economics * 100) / 100,
      cat_politics: Math.round(politics * 100) / 100,
      cat_weather: Math.round(weather * 100) / 100,
      cat_crypto: Math.round(crypto * 100) / 100,
      cat_perps: Math.round(perps * 100) / 100,
    });
  }
  return rows;
}
