# Migration Reconciliation Framework — Agent Instructions

## Project purpose

This repository contains a secure Python framework for executing data-migration reconciliation test cases defined in Excel workbooks.

The framework:

* Reads source and target SQL queries from Excel.
* Supports Oracle or SQL Server as the source database.
* Always uses SQL Server as the target database.
* Executes queries that return one scalar result.
* Compares source and target results.
* Writes the outcome to a new timestamped Excel workbook.
* Supports multiple domains and entities without domain-specific Python implementations.

## Technology decisions

Use:

* Python 3.12+
* `uv` for project and dependency management
* `pyodbc` for SQL Server
* `oracledb` for Oracle
* `openpyxl` for Excel
* `pytest` for tests
* `ruff` for linting and formatting
* Python type hints
* `dataclasses`, enums and protocols where appropriate

Do not introduce:

* SQLAlchemy or another ORM
* pandas unless explicitly approved
* web frameworks
* web interfaces
* Docker during the initial implementation
* asynchronous or parallel query execution
* external HTTP services or AI APIs

## Mandatory security rules

### Never access a real database automatically

The agent must never:

* Connect to a real SQL Server or Oracle database.
* Test credentials.
* Execute production SQL.
* Run integration tests against a database.
* Ask the user to paste passwords into chat.
* Search the machine for saved database credentials.

A real database connection may only be attempted when an authorized person explicitly requests it and provides an approved local configuration.

### Authentication and credential handling

Version 1 supports two authentication modes:

* `interactive`
* `integrated`

Environment-variable authentication is not required in version 1.

#### Interactive authentication

Use `interactive` when the database requires a username and password.

The application must:

* Ask for the username at runtime.
* Ask for the password using Python's `getpass`.
* Hide the password while the user enters it.
* Request credentials only once per logical connection during a run.
* Reuse the established database connection for applicable test cases.
* Keep credentials only in process memory.
* Close database connections after execution.
* Remove credential references when they are no longer needed.

The application must never:

* Accept a password through a command-line argument such as `--password`.
* Store passwords in Python code.
* Store passwords in Excel workbooks.
* Store passwords in TOML configuration.
* Store passwords in environment variables.
* Store passwords in Git.
* Display passwords on screen.
* Write passwords to logs or result workbooks.
* Include passwords in exception messages.
* Print complete connection strings.

Use a hidden prompt:

```python
from getpass import getpass

username = input("Database username: ").strip()
password = getpass("Database password: ")
```

### Database permissions

Production execution requires read-only database accounts.

The framework’s SQL validation is an additional safeguard and must not be presented as a replacement for database permissions.

### Data protection

The framework may store only scalar reconciliation results, including:

* counts
* totals
* mismatch counts
* duplicate counts
* status
* duration
* run ID
* timestamp
* sanitized errors

It must not export or log:

* raw payment records
* customer details
* employee details
* transaction records
* attachments
* personally identifiable information
* multiple database rows returned by a query

If a query returns more than one row or more than one column, stop that test case and mark it as `ERROR`.

### Network restrictions

Do not add:

* HTTP requests
* telemetry
* analytics
* cloud uploads
* email integration
* third-party APIs
* AI API integration

All execution and outputs must remain within the client-controlled environment.

## SQL rules

Source and target SQL are already written in their corresponding database dialects.

The framework must:

* Pass SQL to the selected driver without translation.
* Never convert Oracle SQL to SQL Server SQL.
* Allow only one query statement.
* Accept only read-only queries starting with `SELECT` or `WITH`.
* Ignore leading whitespace and SQL comments when checking the statement.
* Reject empty SQL.
* Reject obvious DML, DDL and procedural commands.
* Reject multiple executable statements.
* Apply the configured query timeout.
* Require exactly one row and one column.
* Close cursors and connections after success or failure.

Rejected operations include:

* `INSERT`
* `UPDATE`
* `DELETE`
* `MERGE`
* `DROP`
* `ALTER`
* `CREATE`
* `TRUNCATE`
* `EXEC`
* `EXECUTE`
* stored-procedure invocation
* transaction-control commands

SQL inspection is conservative and is not a complete SQL parser. Read-only accounts remain mandatory.

## Architecture rules

Maintain these boundaries:

```text
CLI
 ├── Workbook reader and validator
 ├── Reconciliation runner
 │    ├── Database executor interface
 │    ├── Comparison strategies
 │    └── Result models
 └── Workbook result writer
```

### Database adapters

Use a shared interface or protocol such as:

```python
class QueryExecutor(Protocol):
    def test_connection(self) -> ConnectionIdentity: ...

    def execute_scalar(
        self,
        sql: str,
        timeout_seconds: int,
    ) -> ScalarValue: ...

    def close(self) -> None: ...
```

Provide separate adapters for:

* SQL Server using `pyodbc`
* Oracle using `oracledb`
* offline tests using a fake executor

The runner must depend on the interface, not directly on database drivers.

### Domain independence

Do not create classes such as:

* `PaymentRunner`
* `FinesRunner`
* `CompanyRunner`

Domains, entities, SQL queries and comparison rules belong in Excel or configuration.

Add Python code only for reusable capabilities such as:

* a new database type
* a new comparison strategy
* a new workbook schema version
* shared validation behavior

### No ORM

Do not use an ORM.

The framework executes reconciliation SQL already prepared for each database. It does not manage business entities, relationships or application persistence.

## Workbook schema DSL

The framework must not hard-code Excel sheet names, header rows, data-row positions or display column names.

Implement a declarative, versioned workbook schema DSL using TOML.

The DSL maps stable framework semantic fields to template-specific Excel headers.

Examples of stable semantic fields include:

- `test_case_id`
- `domain`
- `entity`
- `enabled`
- `source_type`
- `source_connection`
- `target_connection`
- `source_sql`
- `target_sql`
- `comparison_rule`
- `tolerance`
- `timeout_seconds`
- `source_result`
- `target_result`
- `variance`
- `status`
- `remarks`
- `executed_at`
- `duration_ms`
- `run_id`
- `error_side`
- `error_code`

Each field definition may specify:

- Excel header
- data type
- required status
- read permission
- write permission
- default value
- allowed enum values
- Excel display format

The DSL must also define:

- worksheet name
- header row
- first data row
- output filename pattern
- whether unrelated worksheets are preserved

Requirements:

- Validate the DSL before connecting to databases.
- Reject unsupported schema versions.
- Reject duplicate Excel-header mappings.
- Reject unknown field types.
- Reject missing mandatory semantic fields.
- Prevent writing to fields declared as read-only.
- Never evaluate Python, shell code or arbitrary expressions from the DSL.
- Never include credentials or connection secrets in the DSL.
- Never allow the output path to overwrite the original workbook.
- Allow a different workbook template to be supported by creating another schema file without modifying Python code.

The CLI must accept:

`--schema path/to/workbook_schema.toml`

Add a `validate-schema` command that validates the DSL without opening a workbook or connecting to a database.

Add a `validate-template` command that validates a workbook against the selected DSL without connecting to a database.

## Workbook rules

Read columns by header name, not fixed Excel column letters or row positions.

Expected definition columns:

* `ID`
* `Domain`
* `Entity`
* `Enabled`
* `Source Type`
* `Source Connection`
* `Target Connection`
* `Reconciliation Type`
* `Comparison Rule`
* `Tolerance`
* `Source SQL`
* `Target SQL`
* `Timeout Seconds`
* `Expected Result`
* `Severity`

Expected result columns:

* `Source Results`
* `Target Results`
* `Variance`
* `Status`
* `Remarks`
* `Executed At`
* `Run ID`
* `Duration`
* `Error Side`

Validate:

* required headers
* unique test-case IDs
* supported source database types
* comparison rules
* connection names
* required queries
* timeouts
* tolerance values

Never overwrite the source workbook.

Every execution must produce a new file, for example:

```text
Payment_Domain_Data_MigrationV2.0_results_20260912_093000.xlsx
```

Preserve unrelated worksheets and workbook content.

Calculate variance and status in Python. Do not depend on Excel recalculating formulas.

## Comparison rules

Initially support:

### `equal`

Pass when normalized source and target scalar values are equal.

### `expected_zero`

Pass when the reconciliation or mismatch result is zero.

### `numeric_tolerance`

Pass when:

```text
abs(source_result - target_result) <= tolerance
```

Do not silently convert incompatible values. Invalid comparisons must produce a clear, sanitized error.

## Error handling

A failure in one test case should not normally stop the complete run.

For every error, capture:

* test-case ID
* error side: Source, Target, Workbook or Comparison
* sanitized message
* timestamp
* execution duration
* `ERROR` status

Never include credentials, complete connection strings, sensitive query output or full SQL in errors.

Support an explicit `--fail-fast` option for controlled troubleshooting.

## CLI requirements

Provide these commands:

```bash
reconcile validate-template workbook.xlsx
reconcile test-connections workbook.xlsx
reconcile execute workbook.xlsx --limit 2
reconcile execute workbook.xlsx --case TC-PAY-008
reconcile execute workbook.xlsx
```

Rules:

* `validate-template` must never connect to a database.
* `test-connections` must not execute workbook test queries.
* `execute` must always create a new timestamped output workbook.
* `--limit` supports a safe pilot execution.
* `--case` executes one selected test.
* Disabled cases must be skipped.
* Failures and errors must result in a non-zero CLI exit code.

## Testing rules

All ordinary tests must work without:

* database credentials
* database servers
* internet access
* Oracle Client
* sensitive workbooks

Use fake executors and driver mocks.

Test at least:

* workbook parsing
* missing columns
* duplicate IDs
* disabled cases
* unsafe SQL rejection
* multiple SQL statement rejection
* zero returned rows
* multiple returned rows
* multiple returned columns
* comparison rules
* null handling
* source failure
* target failure
* timeout behavior
* credential redaction
* connection cleanup
* timestamped output
* original workbook remaining unchanged
* `--case`
* `--limit`

Integration tests must:

* live under `tests/integration`
* be skipped by default
* require an explicit environment flag
* never contain credentials
* never be executed automatically by the agent

## Commands

Use `uv` commands:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Do not install packages globally.

Do not modify the user’s system Python installation.

## Agent working process

Before making changes:

1. Inspect the repository.
2. Read this file completely.
3. Review existing code and tests.
4. Check the Git working tree.
5. Preserve all unrelated user changes.
6. Present a short implementation plan.

While working:

1. Implement one small vertical slice at a time.
2. Add or update tests with each behavior.
3. Use fake connections during development.
4. Avoid unnecessary abstractions.
5. Do not hard-code workbook row numbers or domain names.
6. Do not add dependencies without explaining why.
7. Do not connect to a database.

After making changes:

1. Run unit tests.
2. Run Ruff checks.
3. Report which files changed.
4. Report test and lint results.
5. Describe assumptions and remaining limitations.
6. Stop before any action requiring real credentials or database access.

## Current implementation priority

Begin with the offline workflow:

1. Project scaffold
2. Models
3. Workbook validation
4. Workbook reader
5. Fake database executor
6. Comparison strategies
7. Reconciliation runner
8. Workbook result writer
9. CLI
10. Unit tests

Do not begin real database execution until the offline workflow is reviewed and approved.
