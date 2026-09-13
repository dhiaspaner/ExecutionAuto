# Security

This framework runs against production-adjacent databases and handles workbooks
that describe real client data. The rules below are requirements, not
preferences. `AGENTS.md` binds anyone — human or agent — changing this
repository to them.

---

## 1. Offline-first development

Two entry points exist, and only one of them can reach a database:

* `reconcile` is **offline**. It answers every query from a TOML fixture
  (`--fake-results`), and `create_executor_factory()` raises rather than falling
  back to a real server. `reconcile test-connections` still exits non-zero.
* `run_reconciliation.py` connects, using the answers given to its wizard.

Neither `pyodbc` nor `oracledb` is a hard dependency: both are imported lazily,
inside the adapter that needs one, so the framework imports and the whole test
suite passes with no credentials, no servers, no internet, no Oracle Client and
no ODBC driver installed. Every adapter test runs against a fake driver.

Development and review therefore still happen with zero exposure.

## 2. Connections are deliberate, never incidental

A connection is opened only when a person runs `run_reconciliation.py` and
answers its questions. There is no configuration file, environment variable or
default that can cause one on its own.

A real connection may only be attempted when:

1. An authorized person explicitly asks for it,
2. read-only accounts exist on both source and target, and
3. that person supplies the connection details at the prompt or in a profile.

Agents must never initiate a connection, test credentials, run integration
tests against a database, or search the machine for stored credentials.

### Authentication

`run_reconciliation.py` offers two modes per connection:

| Mode | What is typed | What travels |
| --- | --- | --- |
| `windows` | Nothing | `Trusted_Connection=yes`; the signed-in Windows account authenticates. SQL Server only. |
| `password` | Username, then a hidden `getpass` prompt | `UID` and `PWD`, held in process memory for the run only. |

Windows authentication is preferred wherever the servers accept it: no
credential is typed, stored or handled by this program at all.

### Run profiles

`--profile file.toml` pre-answers connection questions. A profile may carry a
server, port, database, authentication mode and username. It may **not** carry a
password: the keys `password`, `pwd`, `passwd`, `secret`, `token` and
`credential` are rejected outright, so a credential placed there fails the run
instead of sitting on disk. Every value a profile supplies is echoed to the
console, so nothing is applied invisibly.

## 3. Client-controlled execution

Everything runs on the client's machine, inside the client's network:

* No HTTP requests, telemetry, analytics or crash reporting.
* No cloud upload, email integration or third-party API.
* No AI or LLM integration of any kind.
* No MCP servers or external tooling.

The only artifacts produced are local files: a result workbook next to the input
(or in `--output-dir`) and console output.

## 4. Scalar-only results

Every reconciliation query must return **exactly one row and one column**. A
result with zero rows, several rows or several columns stops that test case and
records `ERROR`; the data is discarded, never stored and never displayed.

This is what keeps payment records, customer and employee details, transaction
rows and other personal data out of the framework. The only values that reach a
result workbook are counts, totals, mismatch counts, status, duration, run id,
timestamp and sanitized error text.

Enforcement is shared: `single_scalar()` in `database/base.py` is the helper
every adapter — fake and, later, real — routes its result set through.

## 5. Read-only production accounts

**The database account is the real control.** Accounts used for reconciliation
must be granted `SELECT` only, on both the source and the target.

## 6. Interactive credentials, entered hidden

`interactive` authentication (`authentication = "password"`) does this today:

* prompts for the username at runtime, or takes it from a profile;
* reads the password with `getpass`, so it is never echoed;
* prompts **once per connection per run**, reusing that connection for every
  test case;
* keeps the password in process memory only, for the lifetime of the run;
* closes both connections when the run ends, including after a failure.

A password is never: accepted as a command-line argument, written into
Python, stored in a workbook, stored in TOML, read from an environment variable,
committed to Git, printed, logged, written to a result workbook, or included in
an exception message. Complete connection strings are never printed.

`--password` does not exist. A test asserts the CLI exposes no such option. Do
not add one.

## 7. Windows Integrated Authentication

For SQL Server, answering `windows` at the authentication question connects as
the signed-in Windows account: nothing is prompted, nothing is stored, and the
connection string carries `Trusted_Connection=yes` in place of `UID` and `PWD`.
Use it **where the DBA has approved it**.

If integrated authentication fails, the run stops with a classified error. It
**never** silently falls back to prompting — a silent downgrade hides a
misconfiguration and trains people to type credentials at unexpected moments.

Oracle always uses `password` authentication; a profile requesting `windows` for
an Oracle source is rejected. A secret manager may be added later for unattended
execution; environment-variable authentication is not part of version 1.

## 8. No credentials in arguments, files, variables or logs

Anything that could carry a secret passes through `security/redaction.py` before
it is displayed, logged or written to a cell. It masks:

* connection-string pairs (`PWD=`, `UID=`, `Data Source=`, `Server=`, …),
* JSON-style `"password": "…"` pairs,
* `user/password@host` credentials as used by Oracle EasyConnect,
* whole ODBC connection strings recognised by their `DRIVER={…}` clause,

and truncates the result so a verbose driver payload cannot flood a workbook
cell. Driver exceptions routinely echo the connection string that produced them,
so **every** exception written to a result or the console is sanitized —
`sanitize_error()` keeps the exception type and masks the message body.

Error messages also never include the full SQL of a query. `sql_fingerprint()`
provides a non-reversible `sha256:…` reference when correlation is needed.

## 9. The SQL guard is a safety net, not a boundary

`security/sql_guard.py` accepts a single statement that begins with `SELECT` or
`WITH` (after leading whitespace and comments) and rejects empty SQL, multiple
statements, and obvious `INSERT` / `UPDATE` / `DELETE` / `MERGE` / `DROP` /
`ALTER` / `CREATE` / `TRUNCATE` / `EXEC` / `EXECUTE` / transaction-control /
privilege / administrative keywords. It also rejects `SELECT … INTO`, which
writes a table in T-SQL.

It works by stripping comments and blanking string literals and quoted
identifiers, then scanning what remains — so a semicolon or keyword inside data
is not mistaken for code.

**It is deliberately not a SQL parser.** It can be defeated by sufficiently
exotic SQL, and it cannot reason about what a view, synonym or function does. It
exists to catch mistakes — a pasted `UPDATE`, a stray second statement — before
they reach a driver.

It is not a substitute for read-only accounts, and must never be presented as
one.

## 10. Result workbooks still deserve care

A result workbook contains counts and totals for real client data, and it
inherits every input column from the workbook it was copied from. Treat outputs
as client data: `*_results_*.xlsx` and `out/` are git-ignored so results cannot
be committed by accident.

---

## Reporting a problem

If you find a way to make this framework connect somewhere it should not, execute
something that is not a read-only query, or emit a credential into a file, log or
cell, report it to the project owner before opening a pull request.
