"""The ``Run Control`` sheet: validation, ranges, and what it may not decide."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from migration_reconciliation.errors import ConfigurationError
from migration_reconciliation.models import ErrorCode
from migration_reconciliation.workbook.run_control import OutputMode, RunControl, read_run_control


def control_of(path: Path) -> RunControl:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return read_run_control(workbook)
    finally:
        workbook.close()


def test_the_shipped_defaults_parse(make_recon_workbook: Any, test_row: Any) -> None:
    control = control_of(make_recon_workbook([test_row()]))

    assert control.template_version == "2.0"
    assert control.query_timeout_seconds == 120
    assert control.continue_on_test_error is True
    assert control.stop_on_critical_config_error is True
    assert control.output_mode is OutputMode.NEW_FILE
    assert control.dry_run is False
    assert control.require_read_only_sql is True


def test_an_absent_sheet_falls_back_to_safe_defaults(tmp_path: Path, test_row: Any) -> None:
    from migration_reconciliation.workbook.payments_template import build_workbook

    workbook = build_workbook([test_row()])
    del workbook["Run Control"]
    path = tmp_path / "no-control.xlsx"
    workbook.save(path)
    workbook.close()

    control = control_of(path)

    assert control.defaulted is True
    assert control.output_mode is OutputMode.NEW_FILE
    assert control.require_read_only_sql is True


def test_an_unsupported_template_version_stops_the_run(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook(
        [test_row()], run_control=run_control_settings(Template_Version="9.9")
    )

    with pytest.raises(ConfigurationError) as caught:
        control_of(path)

    assert caught.value.code == ErrorCode.UNSUPPORTED_TEMPLATE_VERSION


@pytest.mark.parametrize("value", [-5, 100000, "soon"])
def test_a_timeout_outside_the_allowed_range_is_refused(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any, value: object
) -> None:
    path = make_recon_workbook(
        [test_row()], run_control=run_control_settings(Query_Timeout_Seconds=value)
    )

    with pytest.raises(ConfigurationError, match="Query_Timeout_Seconds"):
        control_of(path)


def test_a_timeout_of_zero_means_no_limit(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    """0 is how the sheet asks for an unlimited query, so it must parse."""
    path = make_recon_workbook(
        [test_row()], run_control=run_control_settings(Query_Timeout_Seconds=0)
    )

    assert control_of(path).query_timeout_seconds == 0


def test_yes_and_no_are_read_as_booleans(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook(
        [test_row()],
        run_control=run_control_settings(Continue_On_Test_Error="No", Dry_Run="Yes"),
    )

    control = control_of(path)

    assert control.continue_on_test_error is False
    assert control.dry_run is True


def test_a_boolean_setting_rejects_anything_else(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook([test_row()], run_control=run_control_settings(Dry_Run="perhaps"))

    with pytest.raises(ConfigurationError, match="Dry_Run"):
        control_of(path)


def test_read_only_sql_cannot_be_switched_off_from_the_workbook(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    """A security requirement is not a workbook setting."""
    path = make_recon_workbook(
        [test_row()], run_control=run_control_settings(Require_Read_Only_SQL="No")
    )

    control = control_of(path)

    assert control.require_read_only_sql is True
    assert any("Require_Read_Only_SQL" in warning for warning in control.warnings)


def test_parallel_workers_are_capped_at_one_with_a_warning(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook(
        [test_row()], run_control=run_control_settings(Max_Parallel_Workers=8)
    )

    control = control_of(path)

    assert control.max_parallel_workers == 1
    assert any("Max_Parallel_Workers" in warning for warning in control.warnings)


def test_an_unsupported_output_mode_is_refused(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook([test_row()], run_control=run_control_settings(Output_Mode="EMAIL"))

    with pytest.raises(ConfigurationError, match="Output_Mode"):
        control_of(path)


def test_an_unknown_setting_is_reported_rather_than_obeyed(
    make_recon_workbook: Any, test_row: Any, run_control_settings: Any
) -> None:
    path = make_recon_workbook([test_row()], run_control=run_control_settings(Allow_Writes="Yes"))

    control = control_of(path)

    assert any("Allow_Writes".casefold().replace("_", "") in w for w in control.warnings)


def test_a_dry_run_override_leaves_everything_else_alone() -> None:
    control = RunControl(query_timeout_seconds=45, dry_run=False)

    overridden = control.with_overrides(dry_run=True)

    assert overridden.dry_run is True
    assert overridden.query_timeout_seconds == 45
    assert control.dry_run is False
