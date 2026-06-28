import test from 'node:test';
import assert from 'node:assert/strict';

import {
  categoryTotals,
  computeMetrics,
  filterRows,
  groupRows,
  normalizeRows,
  parseCsv,
} from '../src/data.js';

const csv = `date,daily_fee,cumulative_fee,cat_sports,cat_economics,cat_perps
2025-12-29,100.00,100.00,80,20,0
2025-12-30,50.50,150.50,10,30,10.5
2026-01-03,200.00,350.50,0,200,0
2026-02-02,300.00,650.50,100,100,100
`;

test('parses CSV text with numeric values', () => {
  const parsed = parseCsv(csv);
  assert.equal(parsed.length, 4);
  assert.equal(parsed[1].date, '2025-12-30');
  assert.equal(parsed[1].daily_fee, 50.5);
  assert.equal(parsed[1].cat_perps, 10.5);
});

test('normalizes rows with missing cumulative totals and category columns', () => {
  const rows = normalizeRows([
    { date: '2026-01-02', daily_fee: 12, cat_weather: 5 },
    { date: '2026-01-01', daily_fee: 8, cat_weather: 2 },
  ]);
  assert.deepEqual(rows.map((row) => row.date), ['2026-01-01', '2026-01-02']);
  assert.equal(rows[0].cumulative_fee, 8);
  assert.equal(rows[1].cumulative_fee, 20);
  assert.equal(rows[1].categories.weather, 5);
});

test('filters rows by named windows', () => {
  const rows = normalizeRows(parseCsv(csv));
  assert.equal(filterRows(rows, 'first-trade').length, 4);
  assert.deepEqual(filterRows(rows, 'last-month').map((row) => row.date), ['2026-01-03', '2026-02-02']);
  assert.deepEqual(filterRows(rows, 'last-6-months').map((row) => row.date), rows.map((row) => row.date));
});

test('groups rows daily weekly and monthly', () => {
  const rows = normalizeRows(parseCsv(csv));
  assert.equal(groupRows(rows, 'daily').length, 4);

  const weekly = groupRows(rows, 'weekly');
  assert.deepEqual(weekly.map((row) => row.date), ['2025-12-29', '2026-02-02']);
  assert.equal(weekly[0].daily_fee, 350.5);
  assert.equal(weekly[0].cumulative_fee, 350.5);

  const monthly = groupRows(rows, 'monthly');
  assert.deepEqual(monthly.map((row) => row.date), ['2025-12-01', '2026-01-01', '2026-02-01']);
  assert.equal(monthly[0].daily_fee, 150.5);
  assert.equal(monthly[2].cumulative_fee, 650.5);
});

test('computes headline fee metrics', () => {
  const rows = normalizeRows(parseCsv(csv));
  const metrics = computeMetrics(rows);
  assert.equal(metrics.totalFees, 650.5);
  assert.equal(metrics.averageDailyFees, 162.625);
  assert.equal(metrics.highestFeeDay.date, '2026-02-02');
  assert.equal(metrics.activeFeeDays, 4);
  assert.ok(metrics.trailing30Annualized > 0);
});

test('sums categories across filtered rows', () => {
  const rows = normalizeRows(parseCsv(csv));
  assert.deepEqual(categoryTotals(rows), [
    ['economics', 350],
    ['sports', 190],
    ['perps', 110.5],
  ]);
});
