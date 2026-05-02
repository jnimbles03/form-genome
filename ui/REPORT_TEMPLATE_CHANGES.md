# Report Template Fixes - Summary

## Changes Applied to report-template.html

### 1. Dynamic Organization Name (Issue #1)
**Lines affected: 5, 51, 140**

- **Line 5 (Title)**: Changed from hardcoded "GSA Forms" to dynamic
  - Before: `<title>GSA Forms — Citizen Friction & Full Results (Optimized)</title>`
  - After: `<title id="page-title">Form Analysis — Citizen Friction & Full Results (Optimized)</title>`
  - The title is now updated dynamically by JavaScript based on the most common organization in the data

- **Line 51 (Subtitle)**: Made organization reference dynamic
  - Before: `This report spotlights the moments that make <strong>GSA</strong> forms hard to complete`
  - After: `This report spotlights the moments that make <strong id="org-name">these</strong> forms hard to complete`

- **Line 140 (Footer)**: Made footer organization reference dynamic
  - Before: `Generated from GSA Forms analysis • Designed for clarity & empathy`
  - After: `Generated from <span id="footer-org">Form</span> analysis • Designed for clarity & empathy`

### 2. Dynamic Statistics Calculation (Issue #2)
**Lines affected: 53-58, 66, 71, 76, 81**

Replaced all hardcoded statistics with dynamic placeholders that are calculated from `window.__REPORT_ROWS__`:

- **Lines 53-58 (Main Stats Section)**:
  - Total forms: `<div class="num" id="stat-total">0</div>`
  - Avg complexity: `<div class="num" id="stat-avg-complexity">0</div>`
  - PII percentage: `<div class="num" id="stat-pii">0%</div>`
  - Attachments percentage: `<div class="num" id="stat-attachments">0%</div>`
  - Notary percentage: `<div class="num" id="stat-notary">0%</div>`
  - Conditional percentage: `<div class="num" id="stat-conditional">0%</div>`

- **Lines 66, 71, 76, 81 (Badges in "What makes this hard" section)**:
  - Added IDs: `badge-attachments`, `badge-notary`, `badge-conditional`, `badge-pii`
  - All badges now update dynamically with calculated percentages

### 3. Default Sort by Complexity Descending (Issue #3)
**Line affected: 150**

- Before: `let sortState = { col: null, dir: 1, numeric: false };`
- After: `let sortState = { col: 3, dir: -1, numeric: true };`
- Table now sorts by column 3 (complexity) in descending order (-1) by default

### 4. Complexity Score Numbers on Bars (Issue #4)
**Line affected: 166**

- Before: `const compBar = (typeof comp === 'number' && !isNaN(comp)) ? \`<span class="mini"><span style="width:${Math.min(100, Math.max(0, comp))}%"></span></span>\` : '';`
- After: Added numerical score after the bar: `<span style="font-size:13px;color:#666;margin-left:6px;">${comp}</span>`
- Complexity bars now show the actual number (e.g., "34") next to the visual bar

### 5. New JavaScript Functions (Lines ~230-302)

Added comprehensive `calculateStats()` function that:
- Calculates total forms count
- Calculates average complexity score
- Calculates percentages for PII, attachments, notary, and conditional logic
- Updates all stat displays and badges dynamically
- Extracts organization name from most common entity in the data
- Updates page title, subtitle, and footer with organization name

The function is called on page load to populate all dynamic fields.

Also added initialization code to set the default sort indicator on the complexity column header.

## Data Flow

1. Backend injects data via `<script id="rows-data">` with JSON containing:
   ```json
   {
     "summary": {...},  // This is now ignored, calculated from rows instead
     "rows": [...]
   }
   ```

2. On page load, `calculateStats()` runs and:
   - Iterates through all rows to calculate statistics
   - Updates all `#stat-*` and `#badge-*` elements
   - Extracts most common organization name
   - Updates `#org-name`, `#footer-org`, and `#page-title`

3. Table initializes with default sort (complexity descending) and shows sorted data

## Testing Recommendations

1. Test with GSA data (should show "General Services Administration" or similar)
2. Test with Fidelity data (should show "Fidelity" or appropriate entity name)
3. Verify statistics match actual data calculations
4. Verify table sorts by complexity descending on initial load
5. Verify complexity scores appear next to bars in the table

## Backup

A backup of the original file is saved as `report-template.html.backup`
