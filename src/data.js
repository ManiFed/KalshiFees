const MS_PER_DAY = 24 * 60 * 60 * 1000;

export function parseCsv(text) {
  const lines = String(text || '')
    .replace(/^\uFEFF/, '')
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0);

  if (lines.length < 2) return [];

  const headers = splitCsvLine(lines[0]).map((header) => header.trim());
  return lines.slice(1).map((line) => {
    const cells = splitCsvLine(line);
    return headers.reduce((row, header, index) => {
      const raw = (cells[index] || '').trim();
      row[header] = header === 'date' || header === 'month' ? raw : toNumber(raw);
      return row;
    }, {});
  });
}

export function normalizeRows(rows) {
  let cumulative = 0;
  return rows
    .filter((row) => row.date || row.month)
    .map((row) => {
      const date = row.date || `${row.month}-01`;
      const dailyFee = toNumber(row.daily_fee ?? row.fee);
      const categories = Object.entries(row).reduce((result, [key, value]) => {
        if (key.startsWith('cat_')) {
          result[key.replace(/^cat_/, '')] = toNumber(value);
        }
        return result;
      }, {});

      return {
        date,
        daily_fee: dailyFee,
        cumulative_fee: row.cumulative_fee == null ? null : toNumber(row.cumulative_fee),
        categories,
      };
    })
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((row) => {
      cumulative += row.daily_fee;
      return {
        ...row,
        cumulative_fee: row.cumulative_fee == null ? cumulative : row.cumulative_fee,
      };
    });
}

export function filterRows(rows, windowName) {
  if (!rows.length || windowName === 'first-trade') return rows.slice();

  const lastDate = parseDate(rows.at(-1).date);
  const days = {
    'last-month': 31,
    'last-6-months': 183,
    'last-year': 365,
  }[windowName];

  if (!days) return rows.slice();

  const cutoff = new Date(lastDate.getTime() - days * MS_PER_DAY);
  return rows.filter((row) => parseDate(row.date) >= cutoff);
}

export function groupRows(rows, cadence) {
  if (cadence === 'daily') return rows.map(cloneRow);

  const buckets = new Map();
  for (const row of rows) {
    const key = cadence === 'weekly' ? weekStart(row.date) : monthStart(row.date);
    const bucket = buckets.get(key) || {
      date: key,
      daily_fee: 0,
      cumulative_fee: 0,
      categories: {},
    };
    bucket.daily_fee += row.daily_fee;
    for (const [category, value] of Object.entries(row.categories || {})) {
      bucket.categories[category] = (bucket.categories[category] || 0) + value;
    }
    buckets.set(key, bucket);
  }

  let cumulative = 0;
  return [...buckets.values()]
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((bucket) => {
      cumulative += bucket.daily_fee;
      return { ...bucket, cumulative_fee: cumulative };
    });
}

export function computeMetrics(rows) {
  if (!rows.length) {
    return {
      totalFees: 0,
      averageDailyFees: 0,
      trailing30Fees: 0,
      trailing30Annualized: 0,
      highestFeeDay: null,
      activeFeeDays: 0,
      firstDate: '',
      lastDate: '',
    };
  }

  const totalFees = rows.reduce((sum, row) => sum + row.daily_fee, 0);
  const highestFeeDay = rows.reduce((best, row) => (row.daily_fee > best.daily_fee ? row : best), rows[0]);
  const trailingRows = filterRows(rows, 'last-month');
  const trailing30Fees = trailingRows.reduce((sum, row) => sum + row.daily_fee, 0);

  return {
    totalFees,
    averageDailyFees: totalFees / rows.length,
    trailing30Fees,
    trailing30Annualized: trailing30Fees * (365 / Math.max(trailingRows.length, 1)),
    highestFeeDay,
    activeFeeDays: rows.filter((row) => row.daily_fee > 0).length,
    firstDate: rows[0].date,
    lastDate: rows.at(-1).date,
  };
}

export function categoryTotals(rows) {
  const totals = new Map();
  for (const row of rows) {
    for (const [category, value] of Object.entries(row.categories || {})) {
      totals.set(category, (totals.get(category) || 0) + value);
    }
  }
  return [...totals.entries()]
    .filter(([, value]) => value > 0)
    .sort((a, b) => b[1] - a[1]);
}

export function formatCurrency(value, options = {}) {
  const abs = Math.abs(value);
  const compact =
    options.compact ||
    (abs >= 1_000_000_000 ? 'B' : abs >= 1_000_000 ? 'M' : abs >= 1_000 ? 'K' : '');
  const divisor = compact === 'B' ? 1_000_000_000 : compact === 'M' ? 1_000_000 : compact === 'K' ? 1_000 : 1;
  const digits = divisor === 1 ? 0 : abs / divisor >= 100 ? 0 : 1;
  return `$${(value / divisor).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: options.fixed ? digits : 0,
  })}${compact}`;
}

export function formatDateLabel(dateString, cadence = 'daily') {
  const date = parseDate(dateString);
  if (cadence === 'monthly') {
    return date.toLocaleDateString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' });
  }
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: '2-digit', timeZone: 'UTC' });
}

function splitCsvLine(line) {
  const cells = [];
  let current = '';
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    const next = line[index + 1];
    if (char === '"' && next === '"') {
      current += '"';
      index += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if (char === ',' && !quoted) {
      cells.push(current);
      current = '';
    } else {
      current += char;
    }
  }
  cells.push(current);
  return cells;
}

function toNumber(value) {
  if (value == null || value === '') return 0;
  const parsed = Number(String(value).replace(/[$,]/g, ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function cloneRow(row) {
  return {
    ...row,
    categories: { ...(row.categories || {}) },
  };
}

function parseDate(dateString) {
  return new Date(`${dateString}T00:00:00Z`);
}

function weekStart(dateString) {
  const date = parseDate(dateString);
  const day = date.getUTCDay();
  const offset = day === 0 ? 6 : day - 1;
  date.setUTCDate(date.getUTCDate() - offset);
  return date.toISOString().slice(0, 10);
}

function monthStart(dateString) {
  return `${dateString.slice(0, 7)}-01`;
}
