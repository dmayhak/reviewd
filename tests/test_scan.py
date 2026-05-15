from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reviewd.cli import main
from reviewd.models import PRInfo, ReviewResult


@patch('reviewd.cli.subprocess.run')
@patch('reviewd.cli._resolve_repo')
@patch('reviewd.cli.load_global_config')
@patch('reviewd.cli._ensure_global_config')
@patch('reviewd.cli.get_current_branch')
@patch('reviewd.cli.get_base_branch')
@patch('reviewd.cli.get_diff_lines')
@patch('reviewd.cli.review_pr')
@patch('reviewd.cli.load_project_config')
@patch('reviewd.cli.StateDB')
def test_scan_command_basic(
    mock_statedb,
    mock_load_project_config,
    mock_review_pr,
    mock_get_diff_lines,
    mock_get_base_branch,
    mock_get_current_branch,
    mock_ensure_config,
    mock_load_global_config,
    mock_resolve_repo,
    mock_run,
    global_config,
    project_config,
):
    """Scan command detects branches, runs local review and prints dry-run output."""
    mock_resolve_repo.return_value = 'my-repo'
    mock_load_global_config.return_value = global_config
    mock_get_current_branch.return_value = 'feature-branch'
    mock_get_base_branch.return_value = 'main'
    mock_get_diff_lines.return_value = 10
    mock_load_project_config.return_value = project_config

    # Mock git rev-parse --verify origin/main to succeed
    mock_run_result = MagicMock()
    mock_run_result.returncode = 0
    mock_run.return_value = mock_run_result

    mock_review_pr.return_value = ReviewResult(
        overview='Looks good',
        findings=[],
        summary='No issues',
        approve=True,
    )

    runner = CliRunner()
    result = runner.invoke(main, ['scan'])

    assert result.exit_code == 0
    assert 'Scanning: feature-branch \u2192 origin/main' in result.output
    assert 'DRY RUN' in result.output

    # Verify review_pr was called with local PRInfo
    args, kwargs = mock_review_pr.call_args
    pr_arg = args[1]
    assert isinstance(pr_arg, PRInfo)
    assert pr_arg.is_local is True
    assert pr_arg.source_branch == 'feature-branch'
    assert pr_arg.destination_branch == 'origin/main'


@patch('reviewd.cli.subprocess.run')
@patch('reviewd.cli._resolve_repo')
@patch('reviewd.cli.load_global_config')
@patch('reviewd.cli._ensure_global_config')
@patch('reviewd.cli.get_current_branch')
@patch('reviewd.cli.get_base_branch')
@patch('reviewd.cli.get_diff_lines')
@patch('reviewd.cli.load_project_config')
def test_scan_no_changes(
    mock_load_project_config,
    mock_get_diff_lines,
    mock_get_base_branch,
    mock_get_current_branch,
    mock_ensure_config,
    mock_load_global_config,
    mock_resolve_repo,
    mock_run,
    global_config,
):
    """Scan command exits early if no changes detected."""
    mock_resolve_repo.return_value = 'my-repo'
    mock_load_global_config.return_value = global_config
    mock_get_current_branch.return_value = 'feature-branch'
    mock_get_base_branch.return_value = 'main'
    mock_get_diff_lines.return_value = 0

    mock_run_result = MagicMock()
    mock_run_result.returncode = 0
    mock_run.return_value = mock_run_result

    runner = CliRunner()
    result = runner.invoke(main, ['scan'])

    assert result.exit_code == 0
    assert 'No changes detected' in result.output
