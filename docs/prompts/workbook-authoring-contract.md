# Prompt B — The Workbook Authoring Contract

Use this prompt when you want an assistant to **author a new reconciliation
workbook from scratch**, or to answer "what is allowed in this column?".
It defines the contract; it does not fix anything.

Everything below was verified against this repository's source, not inferred.

---

## PASTE FROM HERE

You are authoring a reconciliation test-case workbook for the Migration
Reconciliation Framework. Follow this contract exactly. Do not invent columns,
values, or behaviours outside it. If a rule and my request conflict, say so
rather than guessing.

### 0. Pick the execution path first — this decides everything else

The framework has **two separate paths** with different header and enum rules.
Ask me which one applies before writing anything if it is not stated.

| | Path A — `run` | Path B — `execute` |
|---|---|---|
| Command | `reconcile run --profile p.toml` | `reconcile execute wb.xlsx --schema s.toml` |
| Contract source | Hard-coded in `workbook/columns.py` | The `--schema` TOML DSL |
| Headers | Fixed, `Underscore_Case` | Whatever the schema declares |
| Comparison values | `UPPERCASE` (11 of them) | `lowercase` (3 of them) |
| Takes `--schema`? | No | Yes, required |

**Path A is the real contract** and is what `payments_reconciliation_automation_toml_v1.xlsx`
is built for. The rest of this document describes Path A. Use Path B only when
the sheet's headers cannot be changed.

### 1. Sheet layout

- Test cases live on a sheet named **`Test Cases`**.
- The header row is **found by scanning for a row containing `Test_ID`** — it
  does not have to be row 1. In the reference workbook it is row 4, with rows
  1–3 a title banner. Data begins on the next row.
- Header matching folds **case and inner whitespace only**. `test_id` and
  `Test  ID` both match `Test_ID`; **`Test Case ID` does not** — underscores and
  extra words are significant.
- These sheets are read for settings: `Run Control`, `Observation Rules`,
  `Comparison Types`, `Run History`.
- These are documentation and are **never parsed for instructions**:
  `Executor Contract`, `Conversion Notes`, `Connections`.
- All other sheets are preserved untouched.

### 2. Definition columns — the human fills these; the executor only reads them

All 20, in order:

`Test_ID`, `Enabled`, `Domain`, `Flow`, `Test_Name`, `Execution_Scope`,
`Comparison_Type`, `Result_Type`, `Source_Profile_Section`, `Source_Object`,
`Source_SQL`, `Target_Profile_Section`, `Target_Object`, `Target_SQL`,
`Expected_Value`, `Absolute_Tolerance`, `Percentage_Tolerance`, `Severity`,
`Owner`, `Tags`

**Nine are mandatory.** If any is missing the run stops before opening a
connection, rather than producing a sheet of error rows:

`Test_ID`, `Enabled`, `Execution_Scope`, `Comparison_Type`, `Result_Type`,
`Source_Profile_Section`, `Source_SQL`, `Target_Profile_Section`, `Target_SQL`

### 3. Output columns — the executor writes these; never maintain them by hand

All 14: `Source_Result`, `Target_Result`, `Actual_Value`, `Variance`,
`Variance_Percentage`, `Status`, `Observation`, `Source_Duration_ms`,
`Target_Duration_ms`, `Executed_At_UTC`, `Run_ID`, `Error_Code`, `Error_Detail`,
`Evidence_Path`

**Three are mandatory:** `Status`, `Observation`, `Error_Code`.

Leave every output cell empty when authoring. Nothing outside these 14 columns
(plus optional `Platform`) is ever written to the sheet, so a run can never
modify the test it was asked to perform.

### 4. Allowed values

**`Execution_Scope`** — `SOURCE_TARGET` | `SOURCE_ONLY` | `TARGET_ONLY`

**`Comparison_Type`** — exactly one of:
`EQUAL`, `EQUAL_ABS_TOLERANCE`, `EQUAL_PCT_TOLERANCE`, `EXPECTED_EQUAL`,
`EXPECTED_ZERO`, `LESS_THAN_OR_EQUAL`, `GREATER_THAN_OR_EQUAL`, `NON_ZERO`,
`BOOLEAN_TRUE`, `TEXT_CASE_INSENSITIVE_EQUAL`, `NO_COMPARISON`

**`Result_Type`** — `INTEGER` | `NUMBER` | `TEXT` | `BOOLEAN` | `DATETIME`

**`Source_Profile_Section` / `Target_Profile_Section`** — only `source` or
`target`. These are **names of sections in the run-profile TOML**, never
servers, connection strings, or credentials.

**`Enabled`** — `Yes` | `No`

### 5. Cross-field rules — these are where most sheets fail

1. **Two-sided comparisons require `SOURCE_TARGET`.**
   `EQUAL`, `EQUAL_ABS_TOLERANCE`, `EQUAL_PCT_TOLERANCE` read both sides, so
   pairing them with `SOURCE_ONLY` or `TARGET_ONLY` is rejected.

2. **`Comparison_Type` constrains `Result_Type`:**
   - Numeric (`EQUAL_ABS_TOLERANCE`, `EQUAL_PCT_TOLERANCE`, `EXPECTED_ZERO`,
     `LESS_THAN_OR_EQUAL`, `GREATER_THAN_OR_EQUAL`, `NON_ZERO`)
     → `INTEGER` or `NUMBER` only
   - `BOOLEAN_TRUE` → `BOOLEAN` only
   - `TEXT_CASE_INSENSITIVE_EQUAL` → `TEXT` only
   - `NO_COMPARISON` → any type
   - `EQUAL` / `EXPECTED_EQUAL` → any type

3. **`Expected_Value` is mandatory** for `EXPECTED_EQUAL`,
   `LESS_THAN_OR_EQUAL`, `GREATER_THAN_OR_EQUAL` — and also for
   `TEXT_CASE_INSENSITIVE_EQUAL` when the scope is not `SOURCE_TARGET`.

4. **Scope decides which SQL side must be present *and which must be empty*.**
   `SOURCE_ONLY` needs `Source_SQL` and requires `Target_SQL` **and**
   `Target_Profile_Section` to be blank. A populated unused side is an error,
   not a harmless extra.

5. **`NO_COMPARISON` never reports a pass.** It records the value as
   `PROFILED`. Use it for profiling, not for a test you want to succeed.

### 6. SQL rules

- Exactly **one statement**, starting with `SELECT` or `WITH`. A single
  trailing semicolon is accepted.
- Must return **exactly one row and one column**. More rows or columns stops
  that test as `ERROR` — this is a data-protection rule, not a style rule.
- Rejected: `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `DROP`, `ALTER`, `CREATE`,
  `TRUNCATE`, `EXEC`/`EXECUTE`, stored procedures, transaction control.
- **SQL is passed to the driver untranslated.** Write each side in its own
  dialect, matching the database type of the profile section it names. Oracle
  constants need `FROM DUAL`; a bare `SELECT 0` is T-SQL and will fail on Oracle.
- Read-only database accounts remain mandatory. SQL inspection is a
  conservative safeguard, not a parser, and is not a substitute for permissions.

### 7. Security — absolute

Never place in any cell, in any sheet: a password, a full connection string, a
token, a secret, or personally identifying data. The workbook selects a named
profile section; all connection detail lives in the run-profile TOML, and the
password is prompted at runtime and kept only in memory.

The workbook may **describe** what should be done. It may never issue a command,
and no cell is ever evaluated as code, shell, or a formula.

### 8. Before you hand the workbook back

State explicitly:
- Every assumption you made about a column I did not specify.
- Any row where you had to guess a `Comparison_Type` / `Result_Type` pairing.
- Confirmation that all output columns are empty.

Then tell me to verify with:

```
reconcile validate-template <workbook.xlsx> --schema config/workbook_schema.default.toml
```

## PASTE TO HERE
