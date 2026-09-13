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

There are two ways to run it:

| Entry point | What it does |
| --- | --- |
| `reconcile` | The offline command. Schema-driven, with scripted results from a TOML file and no database at all. Use it to validate a workbook or rehearse a run. |
| `run_reconciliation.py` | The interactive runner. Asks fifteen questions, connects to a real source and target, and executes the workbook. |

> Neither one accepts a password as an argument, reads a credential from a file
> or an environment variable, or writes one anywhere. See [SECURITY.md](SECURITY.md).

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

## Run against real databases

`run_reconciliation.py` executes a workbook against a live source and target.
It takes no connection arguments at all: it asks, one question at a time, and
will not move on until each answer is valid.

```powershell
uv run python run_reconciliation.py
```

### 1. Install the driver you need

`pyodbc` is used for every SQL Server connection; `oracledb` only if a source is
Oracle. Both are imported lazily, so the one you do not use never has to be
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
run_reconciliation.py   The interactive runner: fifteen questions, two live databases
config/                 Workbook schema DSL, plus the run-profile template
templates/              A generated example workbook matching the example schema
src/                    The framework (see ARCHITECTURE.md)
tests/                  Offline unit tests; tests/integration is an opt-in placeholder
AGENTS.md               Mandatory rules for anyone (human or agent) changing this repo
```
