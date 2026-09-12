# Migration Reconciliation

A reusable framework for running data-migration reconciliation test cases that
live in an Excel workbook.

Each test case carries two queries — one for the legacy **source** (Oracle or
SQL Server) and one for the migrated **target** (always SQL Server). Both return
a single scalar. The framework compares them, decides PASS / FAIL / ERROR, and
writes the outcome into a **new, timestamped copy** of the workbook. The
original is never modified.

New domains, entities and templates are added by editing Excel and TOML. They do
not require a Python change.

> **Milestone 1 is offline.** No database driver is imported, no credential is
> read, and no network call is made. Queries run against scripted results from a
> TOML file so the whole pipeline can be exercised and reviewed before anything
> touches a real system. See [SECURITY.md](SECURITY.md).

---

## What the framework does

| Stage | Behaviour |
| --- | --- |
| Schema | A TOML file maps stable semantic fields (`source_sql`, `status`, …) to the header text of one specific workbook. |
| Read | Columns are found by header name, never by column letter. Disabled rows are skipped; malformed rows are reported without stopping the run. |
| Guard | Each query must be a single read-only `SELECT` / `WITH` statement. |
| Execute | Each query returns exactly one row and one column. Nothing else crosses the boundary. |
| Compare | `equal`, `expected_zero` or `numeric_tolerance`, computed in Python. |
| Write | Results go into a new `*_results_<timestamp>.xlsx`. Other sheets are preserved. |

---

## Windows development setup

Production runs happen on Windows; the framework is pure Python and also runs on
macOS and Linux for development.

### 1. Install `uv`

`uv` is the project and dependency manager. It creates the virtual environment,
resolves and pins dependencies in `uv.lock`, and runs commands inside that
environment — so every machine gets the identical set of packages, and nothing is
installed into the system Python.

```powershell
winget install --id=astral-sh.uv -e
```

### 2. Install the project

```powershell
git clone <repository-url>
cd migration-reconciliation
uv sync
```

`uv sync` creates `.venv\` and installs the locked dependencies (`openpyxl`, plus
`pytest` and `ruff` for development). Python 3.12 or newer is required; `uv`
fetches a suitable interpreter if the machine has none.

You do **not** need to activate the environment — prefix commands with `uv run`.

### 3. Confirm the install

```powershell
uv run reconcile --version
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The full suite passes with no database, no credentials, no internet access, no
Oracle Client and no ODBC driver installed.

---

## Validate a schema

A schema describes one workbook layout. Validating it opens no workbook and
connects to nothing.

```powershell
uv run reconcile validate-schema `
    --schema ".\config\workbook_schema.example.toml"
```

This checks the schema version, the sheet geometry, that every mandatory
semantic field is present and correctly readable/writable, that no two fields
claim the same Excel header, that enum values are ones the framework supports,
and that the output filename pattern cannot overwrite the input workbook.

## Validate a workbook

```powershell
uv run reconcile validate-template `
    ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.example.toml"
```

This confirms the sheet exists, every required column is present, test-case ids
are unique, and each row parses. It connects to no database. It exits `1` if any
row is malformed, listing the row numbers.

## Run the offline demonstration

```powershell
uv run reconcile execute `
    ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.example.toml" `
    --fake-results ".\tests\fixtures\fake_results.toml"
```

`--fake-results` supplies the scalar each query "returns", replacing the database
entirely. The shipped fixture deliberately produces a mixed result — passes, a
count mismatch, a tolerance breach, a source error and a NULL — so the reporting
and exit codes are visible:

```text
  PASS    TC-PAY-001     row 2        0 ms  Source and target numeric results are equal.
  FAIL    TC-PAY-004     row 5        0 ms  Mismatch: source 4201 != target 4198.
  ERROR   TC-PAY-007     row 8        0 ms  DatabaseExecutionError: ORA-00942: ...
  ERROR   TC-PAY-009     row 10       0 ms  target result is NULL
  ------------------------------------------------------------------------
  4 passed, 2 failed, 2 errors, 1 skipped (8 executed in 16 ms)
```

Useful variations:

```powershell
# One named test case
uv run reconcile execute ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.example.toml" `
    --case "TC-PAY-008" `
    --fake-results ".\tests\fixtures\fake_results.toml"

# A two-case pilot
uv run reconcile execute ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.example.toml" `
    --limit 2 `
    --fake-results ".\tests\fixtures\fake_results.toml"

# Stop at the first problem, and put the output somewhere specific
uv run reconcile execute ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.example.toml" `
    --fail-fast --output-dir ".\out" `
    --fake-results ".\tests\fixtures\fake_results.toml"
```

### Output files

Each run writes `<input stem>_results_<yyyymmdd_hhmmss>.xlsx` next to the input,
or into `--output-dir`. An existing file is never overwritten: if two runs land
in the same second, the second filename also carries the run id
(`..._results_20260912_104504_fa24b2a748ee.xlsx`).

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Everything passed, or validation succeeded. |
| `1` | The run completed but at least one case FAILED or ERRORED. |
| `2` | The run could not start: bad arguments, schema or workbook. |
| `130` | Interrupted with Ctrl+C. |

---

## Create a schema for another Excel template

No Python changes are needed — this is the point of the design.

1. **Copy the example.**

   ```powershell
   Copy-Item .\config\workbook_schema.example.toml .\config\fines_schema.toml
   ```

2. **Point it at your sheet.**

   ```toml
   profile_name = "fines-domain"

   [workbook]
   test_case_sheet = "Fines Recon"   # your sheet name
   header_row = 3                    # the row your headers are on
   first_data_row = 5                # the first row of real data
   ```

3. **Set each `header` to the text in your workbook.** The semantic field name in
   brackets stays the same; only the header changes.

   ```toml
   [fields.test_case_id]
   header = "Scenario Ref"   # was "Test Case ID"
   type = "string"
   required = true
   read = true
   write = false
   ```

   Header matching ignores case and surrounding whitespace. Columns may appear in
   any order.

4. **Keep every mandatory field.** Readable: `test_case_id`, `source_type`,
   `source_connection`, `target_connection`, `source_sql`, `target_sql`.
   Writable: `source_result`, `target_result`, `variance`, `status`, `remarks`,
   `executed_at`, `run_id`, `duration_ms`, `error_side`.

   Optional fields — `domain`, `entity`, `reconciliation_type`, `enabled`,
   `comparison_rule`, `tolerance`, `timeout_seconds`, `expected_result`,
   `severity`, `error_code` — may be dropped if your template lacks them.
   Dropped input fields fall back to their declared `default`.

5. **Validate before you rely on it.**

   ```powershell
   uv run reconcile validate-schema --schema ".\config\fines_schema.toml"
   uv run reconcile validate-template ".\workbooks\fines.xlsx" --schema ".\config\fines_schema.toml"
   ```

To generate a blank workbook that already matches a schema:

```powershell
uv run reconcile make-template ".\workbooks\fines_template.xlsx" `
    --schema ".\config\fines_schema.toml"
```

A schema file is data. It cannot execute Python, shell commands, Excel formulas
or SQL, and it must never contain a credential.

---

## Comparison rules

| Rule | Passes when |
| --- | --- |
| `equal` | The normalized source and target values are equal. Numbers compare numerically (`100`, `100.0` and `"100"` agree); text compares case-sensitively after trimming. |
| `expected_zero` | **Both** the source and target results are numeric zero. Intended for mismatch/orphan-count queries where each side independently answers "how many rows are wrong?". If only one side is meaningful, put the same query in both columns. |
| `numeric_tolerance` | `abs(source - target) <= tolerance`, using the row's `Tolerance` value. |

**NULL never reconciles**, under any rule — not even NULL against NULL. An empty
scalar nearly always means the query matched nothing, which is exactly the case
that must not be recorded as a pass. Wrap aggregates so they return a number:
`COALESCE(SUM(x), 0)` on SQL Server, `NVL(SUM(x), 0)` on Oracle.

Comparing values of different kinds (text against a number, a date against text)
is reported as `ERROR` on the `COMPARISON` side rather than being coerced.

---

## Database adapters are not enabled yet

There is no `pyodbc` and no `oracledb` in this milestone, and
`reconcile test-connections` deliberately refuses to run:

```text
reconcile: test-connections is not available in offline mode: no database
adapter is registered in this milestone.
```

`config/connections.example.toml` documents the shape a connection registry will
take — host, database, driver and authentication **mode** only. It has no
password key, and will not get one: credentials are prompted for at runtime with
`getpass` and kept in memory for the life of the run.

Enabling real connectivity is a separate, reviewed piece of work. See
[ARCHITECTURE.md](ARCHITECTURE.md) for where the adapters plug in and
[SECURITY.md](SECURITY.md) for the rules they must satisfy.

---

## Repository layout

```text
config/     Workbook schema DSL and the connection-registry example
templates/  A generated example workbook matching the example schema
src/        The framework (see ARCHITECTURE.md)
tests/      Offline unit tests; tests/integration is an opt-in placeholder
AGENTS.md   Mandatory rules for anyone (human or agent) changing this repo
```
