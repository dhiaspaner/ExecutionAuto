# Migration Reconciliation

A reusable framework for running data-migration reconciliation test cases that
live in an Excel workbook.

Each test case carries a query for the legacy **source**, a query for the
migrated **target**, or one of the two. Every query returns a single scalar. The
framework compares what comes back, decides the outcome in Python, and writes it
into a **new, timestamped copy** of the workbook. The original is never modified.

Both engines are supported on either side: SQL Server through `pyodbc`, Oracle
through `oracledb`. SQL is passed to the driver verbatim and never translated
between dialects.

New domains, entities and templates are added by editing Excel and TOML. They do
not require a Python change.

There are three ways to run it:

| Entry point | What it does |
| --- | --- |
| `reconcile run` | **The reconciliation template.** A TOML profile holds the connections; the workbook's `Test Cases`, `Run Control` and `Observation Rules` sheets hold everything else. Start here. |
| `reconcile execute` | The offline command. Schema-driven, with scripted results from a TOML file and no database at all. Use it to validate a workbook or rehearse a run. |
| `run_reconciliation.py` | The original interactive runner. Asks fifteen questions, connects to a real source and target, and executes a fixed-column workbook. |

> None of them accepts a password as an argument, reads a credential from a file
> or an environment variable, or writes one anywhere. See [SECURITY.md](SECURITY.md).

---

## Quick start: the reconciliation template

Run these from the repository root. Every command is prefixed with `uv run`, so
there is no environment to activate; the first one installs the project if it is
not installed yet.

```powershell
# 1. Install the project. Needed once, and after every git pull.
uv sync

# 2. Get a workbook whose columns match this executor exactly.
#    Add --force to replace one you already made.
uv run reconcile make-workbook ".\Payments.xlsx"

# 3. Copy the example profile. It is yours to edit and never holds a password.
Copy-Item ".\config\reconciliation_profile.example.toml" ".\run_profile.toml"

# 4. Validate everything, opening no database at all.
#    --workbook wins over [workbook] path, so this works before you edit anything.
uv run reconcile run --profile ".\run_profile.toml" --workbook ".\Payments.xlsx" --dry-run

# 5. Install the driver for the databases you are about to open.
uv add pyodbc          # SQL Server, either side
uv add oracledb        # only if a side is Oracle

# 6. Run it for real against the servers the profile names.
uv run reconcile run --profile ".\run_profile.toml" --workbook ".\Payments.xlsx"
```

On macOS or Linux the same commands work with `cp` in place of `Copy-Item` and
forward slashes in the paths.

Steps 1–4 need no driver, no server and no credential, so they work on any
machine. Step 4 is the one to repeat while you are setting up: it reads the
workbook, validates every enabled test, reports what it would connect to, and
stops.

`pyodbc` also needs the **ODBC Driver 18 for SQL Server** installed on the
machine — see [Run against real databases](#run-against-real-databases). Without
it, step 6 reports every test as `BLOCKED` with `CONNECTION_FAILED` and a
message naming what is missing, rather than failing obscurely.

### Then edit the profile

Open `run_profile.toml` and change two things:

1. `[workbook] path` — where your workbook actually is. Once it is right you can
   drop `--workbook` from the commands above.
2. `[source]` and `[target]` — the servers and databases to reconcile.

The profile is the only place a connection is configured:

```toml
version = "1.0"

[workbook]
# Optional. Until it is set, pass --workbook or answer the one question asked.
path = "C:/Users/PC/Downloads/Payments.xlsx"
sheet = "Test Cases"

[source]
type = "sqlserver"
server = "localhost"
port = 1433
authentication = "windows"
trust_server_certificate = true
database = "webservice"

[target]
type = "sqlserver"
server = "localhost"
port = 1433
database = "PaymentRecon_Target_Local"
trust_server_certificate = true
authentication = "windows"
```

Every key is optional: what is missing is asked for, one question at a time, and
what is present is echoed on screen so you can see what the run assumed. A
`[workbook] path` that no longer exists is a warning and a question, not a
failure — a shared profile outliving a moved workbook is ordinary.

Under `--non-interactive` nothing is asked at all, so everything needed must be
in the file, and every connection must use `authentication = "windows"`. There
is no non-interactive source for a password, by design.

### The `run` flags

| Flag | Effect |
| --- | --- |
| `--profile PATH` | **Required.** The TOML profile. |
| `--workbook PATH` | Overrides `[workbook] path`. A path given here must exist. |
| `--sheet NAME` | Overrides `[workbook] sheet`. Defaults to `Test Cases`. |
| `--dry-run` | Validate everything; open no database, run no SQL, write no file. |
| `--non-interactive` | Never prompt. Fail instead of asking. |
| `--output-dir PATH` | Where the result workbook goes. Defaults to beside the input. |
| `--no-write` | Run and report, but produce no result workbook. |

Exit codes: `0` clean, `1` failures or errors (for a dry run, `1` means a test is
misconfigured), `2` the run could not start, `130` interrupted.

**A profile never contains a password.** Any key that looks like a secret —
`password`, `pwd`, `secret`, `token`, `access_token`, `api_key` and their
relatives — is rejected at any depth in the file rather than ignored. Under
`authentication = "password"` the username lives in TOML and the password is
typed at runtime behind a hidden prompt, held in memory for the run, and never
written to a file, a log or the workbook.

### Who decides what

| Source | Decides |
| --- | --- |
| The TOML profile | Workbook location, and every database connection. |
| `Test Cases` | Queries, scopes, comparisons, tolerances, expected values. |
| `Run Control` | Timeout, output mode, truncation, dry run, error handling. |
| `Observation Rules` | The **wording** of an outcome. Never the outcome. |
| `Comparison Types` | Documentation and validation metadata only. |
| `Executor Contract`, `Conversion Notes`, `Connections` | Documentation only — never parsed for instructions, actions or connection settings. |

### Execution scopes

`Execution_Scope` decides which databases are opened at all.

| Scope | Requires | Must be blank | Opens |
| --- | --- | --- | --- |
| `SOURCE_TARGET` | `Source_Profile_Section`, `Source_SQL`, `Target_Profile_Section`, `Target_SQL` | — | `[source]` and `[target]` |
| `SOURCE_ONLY` | `Source_Profile_Section`, `Source_SQL` | `Target_Profile_Section`, `Target_SQL` | `[source]` only |
| `TARGET_ONLY` | `Target_Profile_Section`, `Target_SQL` | `Source_Profile_Section`, `Source_SQL` | `[target]` only |

A workbook of `TARGET_ONLY` tests never asks a source question and never opens a
source session. `Enabled = No` is stronger still: the row is marked `DISABLED`,
runs nothing, needs no connection, and is not counted in any total.

### Comparison types

All eleven are Python functions in a fixed registry. A workbook selects one by
name; it can never supply one, and no formula or expression from a spreadsheet
is evaluated anywhere.

| Type | Passes when |
| --- | --- |
| `EQUAL` | Source and target are equal after normalization. |
| `EQUAL_ABS_TOLERANCE` | `abs(target - source) <= Absolute_Tolerance`. |
| `EQUAL_PCT_TOLERANCE` | The percentage variance against the source is within `Percentage_Tolerance`. |
| `EXPECTED_EQUAL` | The executed value equals `Expected_Value`. |
| `EXPECTED_ZERO` | The executed value is zero. |
| `LESS_THAN_OR_EQUAL` | The executed value is at most `Expected_Value`. |
| `GREATER_THAN_OR_EQUAL` | The executed value is at least `Expected_Value`. |
| `NON_ZERO` | The executed value is not zero. |
| `BOOLEAN_TRUE` | The executed value is TRUE. |
| `TEXT_CASE_INSENSITIVE_EQUAL` | Text matches ignoring case. |
| `NO_COMPARISON` | Never passes — records the value as `PROFILED`. |

When the source is zero, `EQUAL_PCT_TOLERANCE` passes only if the target is zero
too. Anything else needs an `Absolute_Tolerance` saying explicitly how much
drift from nothing is acceptable; otherwise it fails with
`ZERO_SOURCE_BASELINE` rather than dividing by zero or guessing.

Under `SOURCE_TARGET`, a one-sided comparison such as `EXPECTED_ZERO` is applied
to **both** executed values and both must satisfy it. Asserting on one of two
executed values would let the other fail unread.

### Statuses and error codes

Eight statuses, and never more: `PASS`, `FAIL`, `PROFILED`, `ERROR`, `BLOCKED`,
`CONFIG ERROR`, `NOT EXECUTED`, `DISABLED`. The specific reason lives in
`Error_Code` and the platform that decided it, so a new failure mode never needs
a new status:

```text
Status = ERROR         Platform = TARGET      Error_Code = QUERY_TIMEOUT
Status = CONFIG ERROR  Platform = WORKBOOK    Error_Code = MISSING_SOURCE_SQL
Status = FAIL          Platform = COMPARISON  Error_Code = VALUE_MISMATCH
```

A run is never reported as an overall `PASS` while an enabled test is still
`NOT EXECUTED`.

### Observations

Wording is selected deterministically from `(Status, Platform, Error_Code)`:
exact match, then `(status, platform, ANY)`, then `(status, ANY, error_code)`,
then `(status, ANY, ANY)`, then the built-in Python fallback. There is no
priority column, and two enabled rules matching the same key are rejected when
the sheet is read. Templates may use controlled placeholders such as
`{test_id}`, `{source_result}`, `{variance}` and `{timeout_seconds}`; only known
names are replaced, and nothing is evaluated.

### What a run writes

The executor updates only the execution-output columns —
`Source_Result`, `Target_Result`, `Actual_Value`, `Variance`,
`Variance_Percentage`, `Status`, `Observation`, `Source_Duration_ms`,
`Target_Duration_ms`, `Executed_At_UTC`, `Run_ID`, `Error_Code`, `Error_Detail`,
`Evidence_Path` — plus one appended `Run History` row. Definition columns are
never modified. Formatting, formulas, validation and unrelated sheets are
preserved. The save goes to a temporary file in the destination folder and is
moved into place only once it has succeeded, so a crash cannot leave a
half-written workbook. Under the default `Output_Mode = NEW_FILE` the input
workbook is left byte-for-byte as it was.

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
    --schema ".\config\workbook_schema.default.toml"
```

This checks the schema version, the sheet geometry, that every mandatory
semantic field is present and correctly readable/writable, that no two fields
claim the same Excel header, that enum values are ones the framework supports,
and that the output filename pattern cannot overwrite the input workbook.

## Validate a workbook

```powershell
uv run reconcile validate-template `
    ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.default.toml"
```

This confirms the sheet exists, every required column is present, test-case ids
are unique, and each row parses. It connects to no database. It exits `1` if any
row is malformed, listing the row numbers.

## Run the offline demonstration

```powershell
uv run reconcile execute `
    ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.default.toml" `
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
    --schema ".\config\workbook_schema.default.toml" `
    --case "TC-PAY-008" `
    --fake-results ".\tests\fixtures\fake_results.toml"

# A two-case pilot
uv run reconcile execute ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.default.toml" `
    --limit 2 `
    --fake-results ".\tests\fixtures\fake_results.toml"

# Stop at the first problem, and put the output somewhere specific
uv run reconcile execute ".\templates\reconciliation_template.xlsx" `
    --schema ".\config\workbook_schema.default.toml" `
    --fail-fast --output-dir ".\out" `
    --fake-results ".\tests\fixtures\fake_results.toml"
```

`--output-dir` must already exist — create it once with `mkdir out`.

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

1. **Copy the shipped schema.**

   ```powershell
   Copy-Item .\config\workbook_schema.default.toml .\config\fines_schema.toml
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
   header = "Scenario Ref"   # was "Test_ID"
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

## Run against real databases

Two entry points connect to live databases, and **section 1 below applies to
both**:

* `reconcile run` — the reconciliation template, with connections from a TOML
  profile. See [Quick start](#quick-start-the-reconciliation-template).
* `run_reconciliation.py` — the original runner, described in the rest of this
  section. It takes no connection arguments at all: it asks, one question at a
  time, and will not move on until each answer is valid.

```powershell
uv run python run_reconciliation.py
```

### 1. Install the driver you need

`pyodbc` is used for every SQL Server connection; `oracledb` only for an Oracle
one. Both are imported lazily, so the one you do not use never has to be
installed.

```powershell
uv add pyodbc
uv add oracledb    # only for an Oracle source
```

On Windows, `pyodbc` also needs Microsoft's ODBC driver:

```powershell
winget install --id=Microsoft.msodbcsql.18 -e
```

The newest installed driver is selected automatically, from `ODBC Driver 18`
down to the legacy `SQL Server` driver. `oracledb` runs in thin mode and needs
no Oracle Client.

### 2. Prepare the workbook

The sheet is found by the name you choose at question 2, so it can be called
anything. The **column headers are fixed** — they are constants in the script,
not a schema file:

| Column | Required | Purpose |
| --- | --- | --- |
| `ID` | yes | Test case identifier, unique within the sheet. |
| `Source SQL` | yes | Read-only query for the source database, in its own dialect. |
| `Target SQL` | yes | Read-only query for the target database. |
| `Source Results` | result | The source scalar. |
| `Target Results` | result | The target scalar. |
| `Variance` | result | Computed in Python, never an Excel formula. |
| `Status` | result | `PASS`, `FAIL`, `ERROR` or `SKIPPED`. |
| `Remarks` | result | Why it passed, failed or errored. |

Four more columns are used **if they exist** and defaulted if they do not:
`Enabled` (default true), `Comparison Rule` (default `equal`), `Tolerance`
(default 0) and `Timeout Seconds` (default 120). `Domain` and `Entity` are read
for reporting. Any other column is left alone, and every other worksheet is
preserved untouched.

Close the workbook in Excel first — Excel holds a lock, and a `~$name.xlsx` file
next to it is the sign that it is still open.

### 3. Answer the questions

One at a time, in this order, each re-asked until it is valid:

| # | Question | Notes |
| --- | --- | --- |
| 1 | Path to the workbook (.xlsx) | Must exist. A path pasted with quotes is fine. |
| 2 | Which sheet | Sheets are listed and numbered; answer by number or name. |
| 3 | Source database type | `sqlserver` or `oracle`. |
| 4 | Source server | Hostname or IP. |
| 5 | Source port | Enter accepts 1433, or 1521 for Oracle. |
| 6 | Source database or service name | Service name when the source is Oracle. |
| 7 | Trust the source certificate? | SQL Server only. |
| 8 | Source authentication | `windows` or `password`. SQL Server only; Oracle always uses a password. |
| 9 | Source username | Skipped under Windows authentication. |
| 10 | Source password | Hidden. Skipped under Windows authentication. |
| 11 | Target server | The target is always SQL Server. |
| 12 | Target port | Enter accepts 1433. |
| 13 | Target database name | |
| 14 | Trust the target certificate? | |
| 15 | Target authentication | `windows` or `password`. |
| 16 | Target username | Skipped under Windows authentication. |
| 17 | Target password | Hidden. Skipped under Windows authentication. |

Between 13 and 17 questions are asked, depending on the answers: an Oracle
source skips the certificate and authentication questions, and each side using
Windows authentication skips a username and a password. The counter in the
prompt shows the current total and says why it changed.

Nothing is connected to, no sheet row is read and no query runs until every
answer is in hand.

#### Windows authentication

Answering `windows` connects as the account already signed in to this machine.
No username is typed, no password is typed, and none is placed in the connection
string — it carries `Trusted_Connection=yes` instead of `UID`/`PWD`:

```text
[8/13] Source authentication (windows or password)
      > windows
      Source connection uses the signed-in Windows account; no password needed.
```

This is the safer option wherever the servers accept it, and it is what makes a
completely unattended run possible. It needs a Windows account with access to
the database, and it is SQL Server only — Oracle always asks for a username and
password.

The certificate questions (7 and 14) exist because the connection is always
encrypted. Answer `y` when the server presents a certificate this machine does
not already trust, which is usual for local and internal servers; `n` verifies
it properly.

### 3b. Or answer them from a file

A profile is a TOML file holding the answers that never change:

```powershell
uv run python run_reconciliation.py --profile .\config\run_profile.example.toml
```

[`config/run_profile.example.toml`](config/run_profile.example.toml) is a
commented template — copy it and edit. Every key is optional: what the file
answers is **echoed on screen** and skipped, what it omits is still asked.

```toml
version = "1.0"

[workbook]
path = "C:/migration/payments_domain_reconciliation_v2.xlsx"
sheet = "Payments"

[source]
type = "oracle"                  # "oracle" or "sqlserver"
server = "legacy-ora.corp.local"
port = 1521                      # omit to accept the default for the type
database = "LEGACYPAY"           # the SERVICE NAME when type = "oracle"
authentication = "password"
username = "recon_reader"

[target]                         # always SQL Server, so there is no `type` key
server = "sql-mig-01.corp.local"
port = 1433
database = "PaymentsMigrated"
trust_server_certificate = true
authentication = "windows"       # needs no username and no password
```

A run using that profile asks exactly one question — the Oracle password — and
prints what it took from the file:

```text
      [profile] Sheet: Payments
      [profile] Source server (hostname or IP): legacy-ora.corp.local
      [profile] Target authentication (windows or password): windows
      Target connection uses the signed-in Windows account; no password needed.
```

With `authentication = "windows"` on both sides, the run asks **nothing at all**.

> **A profile never contains a password.** There is no key for one, and a file
> containing `password`, `pwd`, `secret`, `token` or similar is **rejected**,
> not ignored — so putting one there fails loudly instead of silently leaving a
> credential on disk. A profile pointing at a workbook or sheet that does not
> exist falls back to asking rather than failing.

### 4. Read the pre-flight, then the results

Both connections open first and describe themselves, then the sheet is read and
summarised — enabled cases, disabled cases, invalid rows — before a single query
is sent. A run looks like this:

```text
Connecting...
  SOURCE  oracle at legacy-ora.corp.local:1521 db=LEGACYPAY user=recon_reader version=19.3.0.0.0
  TARGET  sqlserver at sql-mig-01.corp.local:1433 db=PaymentsMigrated user=svc_recon version=16.00.4125

Sheet 'Payments':
  enabled cases  : 3
  disabled cases : 0
  invalid rows   : 0

Executing...

Run 11e39e7daf1b - payments_demo.xlsx
------------------------------------------------------------------------
  PASS    TC-PAY-001     row 2       31 ms  Source and target numeric results are equal.
  PASS    TC-PAY-002     row 3       28 ms  Source and target numeric results are equal.
  FAIL    TC-PAY-003     row 4       26 ms  Mismatch: source 4201 != target 4198.
------------------------------------------------------------------------
  2 passed, 1 failed, 0 errors, 0 skipped (3 executed in 605 ms)
  Results written to: ...\payments_demo_results_20260913_104633.xlsx
  Original workbook unchanged: ...\payments_demo.xlsx
```

### Repeat runs

Four optional flags exist, because they change *what runs* rather than supply
information the script needs:

```powershell
uv run python run_reconciliation.py --case TC-PAY-008
uv run python run_reconciliation.py --limit 2
uv run python run_reconciliation.py --output-dir .\out
uv run python run_reconciliation.py --profile .\config\my_profile.toml --limit 2
```

Those four are the only flags. There is deliberately no `--source-server`, no
`--sheet-name` and no `--password`: connection details are typed in or come from
a profile, and a password is only ever typed at a hidden prompt.

### When a connection fails

Connections give up after 15 seconds, so an unreachable host fails while you are
still watching rather than hanging. The message names the likely cause instead of
echoing the driver's exception, which can contain the connection string:

| Message says | Usually means |
| --- | --- |
| the server could not be reached in time | Wrong hostname or port, VPN not connected, firewall blocking |
| the server rejected the credentials | Wrong username or password, or a locked or expired account |
| the database or service could not be opened | Wrong database or service name, or the account lacks permission |
| the database driver is missing | Install ODBC Driver 18 for SQL Server |

A failure in one test case does not stop the run: it becomes one `ERROR` row
with a sanitized message, and the remaining cases still execute.

### What the adapters will not do

* Execute anything but a single read-only `SELECT` or `WITH` statement — the
  guard runs before the query reaches the driver.
* Fetch more than two rows. A query that wrongly matches a whole table is
  rejected rather than pulled into this process.
* Translate SQL between dialects. Each query goes to its driver verbatim.
* Write a password, a connection string or any query output to the workbook, a
  log or an error message.

Read-only database accounts remain mandatory. The SQL guard is a second line of
defence, not a replacement for permissions.

---

## Repository layout

```text
run_reconciliation.py   The original interactive runner: fifteen questions, two live databases
config/                 Run profiles (both templates) and the workbook schema DSL
templates/              A generated example workbook matching the example schema
src/                    The framework (see ARCHITECTURE.md)
  execution/            Planning and running the reconciliation template
  workbook/             Sheet readers, the comparison-safe writer, template generation
  evaluation/           Result normalization and the comparison registry
  database/             Adapters, connection settings and failure classification
tests/                  Offline unit tests; tests/integration is opt-in and never automatic
AGENTS.md               Mandatory rules for anyone (human or agent) changing this repo
```
