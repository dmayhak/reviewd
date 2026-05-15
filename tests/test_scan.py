from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reviewd.cli import main
from reviewd.models import PRInfo, RepoConfig, ReviewResult


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


@patch('reviewd.wizard.init_local_repo')
@patch('reviewd.cli.load_repo_config_from_path')
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
def test_scan_prompts_to_init_local_repo(
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
    mock_load_repo_config,
    mock_init_local,
    global_config,
    project_config,
):
    """Scan command prompts to create .reviewd.yaml if not configured."""
    mock_load_global_config.return_value = global_config
    mock_resolve_repo.return_value = 'other-repo'  # Not in global config
    
    # First call returns None, second call returns a config (simulating success)
    mock_load_repo_config.side_effect = [None, RepoConfig(name='other-repo', path='/tmp/other', provider='github')]
    mock_init_local.return_value = True
    
    mock_get_current_branch.return_value = 'main'
    mock_get_base_branch.return_value = 'main'
    mock_get_diff_lines.return_value = 5
    mock_load_project_config.return_value = project_config
    
    mock_run_result = MagicMock()
    mock_run_result.returncode = 0
    mock_run.return_value = mock_run_result

    mock_review_pr.return_value = ReviewResult(overview='OK', findings=[], summary='OK', approve=True)

    runner = CliRunner()
    result = runner.invoke(main, ['scan'])

    assert result.exit_code == 0
    assert mock_init_local.called
    assert mock_load_repo_config.call_count == 2
    assert 'Scanning: main \u2192 origin/main' in result.output
