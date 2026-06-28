# Kalshi Fee Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a static Kalshi fee-revenue dashboard from the collector CSV output, with controls, charts, architecture notes, disclaimers, and collector audit documentation.

**Architecture:** The site is a no-build static app. `src/data.js` owns CSV parsing, aggregation, filtering, metrics, and formatting so it can be tested in Node and reused by the browser. `app.js` owns DOM state and canvas drawing, while `styles.css` recreates the KalshiData-inspired editorial dashboard look.

**Tech Stack:** HTML, CSS, vanilla JavaScript modules, Canvas 2D, Node's built-in test runner, Python syntax compilation for collector audit checks.

---

### Task 1: Data Transformations

**Files:**
- Create: `src/data.js`
- Create: `tests/data.test.mjs`

- [x] **Step 1: Write failing tests for CSV parsing, filtering, grouping, and metrics**

Run: `node --test tests/data.test.mjs`
Expected before implementation: FAIL because `src/data.js` does not exist.

- [x] **Step 2: Implement `src/data.js`**

Expose `parseCsv`, `normalizeRows`, `filterRows`, `groupRows`, `computeMetrics`, `categoryTotals`, `formatCurrency`, and `formatDateLabel`.

- [x] **Step 3: Run tests**

Run: `node --test tests/data.test.mjs`
Expected after implementation: PASS.

### Task 2: Static Dashboard

**Files:**
- Create: `index.html`
- Create: `styles.css`
- Create: `app.js`

- [x] **Step 1: Build accessible HTML shell**

Include headline controls, stat slots, chart canvases, fallback upload state, methodology, architecture, credit, and affiliation disclaimers.

- [x] **Step 2: Implement dashboard behavior**

Load `kalshi_fee_daily.csv` when present, fall back to generated sample data, support user file upload, update controls, stats, and charts.

- [x] **Step 3: Implement visual styling**

Use warm off-white background, dark serif headings, underlined controls, sparse dividers, muted labels, and green fee charts.

### Task 3: Collector Audit

**Files:**
- Create: `AUDIT.md`

- [x] **Step 1: Review collector architecture**

Document endpoints, candle handling, fee formula, output behavior, runtime risks, and data-quality issues.

- [x] **Step 2: Identify actionable fixes**

Call out series ticker lookup, output path portability, perps daily attribution, authenticated args not used, duplicate live markets, API schema assumptions, and runtime completeness risks.

### Task 4: Verification

**Files:**
- Modify as needed based on verification.

- [x] **Step 1: Run JS tests**

Run: `node --test tests/data.test.mjs`
Expected: PASS.

- [x] **Step 2: Run static server and inspect page**

Run: `python3 -m http.server 8000`
Open the page in the in-app browser, verify the page renders, controls work, stats populate, and canvases draw.

- [x] **Step 3: Check collector syntax**

Run: `python3 -m py_compile "/Users/eligoldfine/Downloads/kalshi_fee_calculator (3).py"`
Expected: PASS.
