# Prompt A — Fix an existing sheet to fit the contract

Use this when you already have a workbook (like
`wps_domain_reconciliation_lean_v1.xlsx`) and need it reshaped to what the
executor accepts. Pair it with **Prompt B**, which defines the target contract.

---

## PASTE FROM HERE

You are remediating an existing reconciliation workbook so it satisfies the
Migration Reconciliation Framework's `Test Cases` contract.

**Inputs I am giving you:**
- The workbook to fix: `<PATH>`
- The reference workbook that already conforms:
  `payments_reconciliation_automation_toml_v1.xlsx`
- The contract definition (Prompt B / `docs/prompts/workbook-authoring-contract.md`)

### Rules of engagement — read before touching anything

1. **Never edit my workbook in place.** Produce a new file. The original is
   evidence and must stay byte-identical.
2. **Never invent SQL, connection names, or test intent.** If a value cannot be
   derived from what is already in the sheet, leave it blank and list it as an
   open question. A plausible guess in a reconciliation test is worse than a
   gap, because it produces a green result nobody checked.
3. **Preserve every unrelated sheet and column.** Extra columns the contract
   does not know about are harmless — keep them. The executor ignores them.
4. **Never overwrite a column that already holds human-authored content.** If a
   contract output column collides with an existing populated column, add the
   output column separately and tell me about the collision.
5. Do not put a credential, connection string, or token in any cell.

### Step 1 — Report before you change

Produce a mapping table before editing. For every column in my sheet:

| my header | maps to contract field | confidence | note |
|---|---|---|---|

Then list, separately:
- **Renames** — a column that exists under different header text. Safe.
- **Genuinely absent** — a mandatory field with no source column at all.
- **Value conflicts** — a column that maps, but whose *values* fall outside the
  allowed set.
- **Collisions** — an output column whose header already holds authored data.

Stop here and wait for my decision on anything in the last three groups.

### Step 2 — Apply only what I approved

Header text must match the contract exactly, including underscores. Matching
folds case and inner whitespace only, so `Test Case ID` will **not** match
`Test_ID` — the underscore and the word count are significant.

Leave all output columns empty. Do not pre-fill `Status` with anything.

### Step 3 — Verify and report honestly

Run:
```
reconcile validate-template <new_workbook.xlsx> --schema config/workbook_schema.default.toml
```

Report the real output, including the `invalid rows` count. **A workbook whose
headers pass but whose rows fail is not fixed.** In the reference workbook,
62 of the 234 data rows still fail on empty profile-section cells — header conformance
and row conformance are separate problems, and you must report both.

---

## Worked example — the WPS sheet

My sheet `wps_domain_reconciliation_lean_v1.xlsx` has headers on **row 1**
(169 data rows, 19 columns) in space-separated Title Case. Apply the mapping
below.

**Safe renames — header text only:**

| WPS header | contract header |
|---|---|
| `Test Case ID` | `Test_ID` |
| `Source SQL` | `Source_SQL` |
| `Target SQL` | `Target_SQL` |
| `Source Result` | `Source_Result` |
| `Target Result` | `Target_Result` |
| `Expected Result` | `Expected_Value` |
| `Migration Flow` | `Flow` |
| `Description` | `Test_Name` |
| `Target Table` | `Target_Object` |
| `Source View` | `Source_Object` |

`Severity`, `Variance`, and `Status` already match exactly.

**Must be added — no source column exists:**

`Execution_Scope`, `Comparison_Type`, `Result_Type`,
`Source_Profile_Section`, `Target_Profile_Section`, `Observation`,
`Error_Code`, `Executed_At_UTC`, `Run_ID`

For the profile sections, the reference workbook uses the literal values
`source` and `target` — these name TOML sections, not servers. Ask me to
confirm before filling them; do not assume every row uses both sides.

**Three conflicts you must not resolve on your own:**

1. **`Source Platform` = `Mixed` on 85 of 169 rows.** There is no `Mixed`
   database. Those rows' source SQL is `SELECT 0 AS expected_count;` — valid
   T-SQL, invalid Oracle (no `FROM DUAL`). Propose a resolution; do not pick one.
   Where a fix means widening an allowed-value set, extend the existing enum
   rather than adding a parallel column.

2. **`Remarks` holds 126 authored notes.** The contract's `Observation` column
   is executor-written. Do not map `Remarks` to `Observation` — that destroys
   126 human notes on the first run. Add `Observation` as a new column.

3. **`Applicability`** (`Applicable` ×168, `N/A` ×1) is the natural source for
   `Enabled`, but it is text, not `Yes`/`No`. `TC-WPS-042` is the `N/A` row: it
   has blank SQL on both sides and is the only row our SQL guard rejects.
   Confirm it should become `Enabled = No`.

**One thing already fine:** 336 of 338 queries pass the read-only SQL guard.
Trailing semicolons are accepted. The only rejections are the empty pair on
`TC-WPS-042`. Do not "fix" the SQL.

## PASTE TO HERE
