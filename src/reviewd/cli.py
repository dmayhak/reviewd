from __future__ import annotations

import importlib.metadata
import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path

import click

from reviewd.colors import BOLD_RED, CLEAR_LINE, CYAN, DIM, GREEN, RED, RESET, YELLOW
from reviewd.config import get_provider, load_global_config
from reviewd.daemon import review_single_pr, run_poll_loop
from reviewd.models import CLI, GlobalConfig
from reviewd.state import StateDB

try:
    VERSION = importlib.metadata.version('reviewd')
except importlib.metadata.PackageNotFoundError:
    VERSION = '0.0.0-dev'

CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / 'reviewd'
CONFIG_PATH = CONFIG_DIR / 'config.yaml'


def _apply_cli_override(config: GlobalConfig, cli: str | None):
    if cli is None:
        return
    cli_enum = CLI(cli)
    config.cli = cli_enum
    for repo in config.repos:
        repo.cli = cli_enum


PROGRESS_LOG_LEVEL = 22
logging.addLevelName(PROGRESS_LOG_LEVEL, 'PROGRESS')

REVIEW_LOG_LEVEL = 25
logging.addLevelName(REVIEW_LOG_LEVEL, 'REVIEW')


class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: DIM,
        PROGRESS_LOG_LEVEL: CYAN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
        REVIEW_LOG_LEVEL: GREEN,
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, '')
        record.levelname = f'{color}{record.levelname:<8}{RESET}'
        if color:
            record.msg = f'{color}{record.msg}{RESET}'
        # Clear any in-place status line before writing the log line
        return CLEAR_LINE + super().format(record)


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter('%(asctime)s %(levelname)s %(name)s — %(message)s', datefmt='%H:%M:%S'))
    logging.root.addHandler(handler)
    logging.root.setLevel(level)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 7


def _attach_file_logging(log_file: str | None):
    if not log_file:
        return
    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
    )
    handler.setFormatter(
        logging.Formatter('%(asctime)s %(levelname)-8s %(name)s — %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    )
    logging.root.addHandler(handler)


def _resolve_verbose(ctx, local_verbose: bool) -> bool:
    verbose = ctx.obj['verbose'] or local_verbose
    if verbose:
        logging.root.setLevel(logging.DEBUG)
    return verbose


@click.group(invoke_without_command=True)
@click.option('--config', 'config_path', default=None, help='Path to global config file')
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.version_option(version=VERSION, message=f'reviewd v{VERSION}')
@click.pass_context
def main(ctx, config_path: str | None, verbose: bool):
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config_path
    ctx.obj['verbose'] = verbose
    _setup_logging(verbose)

    if ctx.invoked_subcommand is None:
        path = Path(config_path).expanduser() if config_path else CONFIG_PATH
        if not path.exists():
            click.echo(f'reviewd v{VERSION}')
            ctx.invoke(init)
        else:
            click.echo(ctx.get_help())


UPDATE_CHECK_CACHE = Path(os.environ.get('XDG_CACHE_HOME', '~/.cache')).expanduser() / 'reviewd' / 'latest_version'
UPDATE_CHECK_INTERVAL = 6 * 3600  # seconds


def _check_for_updates():
    try:
        import time

        now = time.time()
        latest = None

        if UPDATE_CHECK_CACHE.exists():
            stat = UPDATE_CHECK_CACHE.stat()
            if now - stat.st_mtime < UPDATE_CHECK_INTERVAL:
                latest = UPDATE_CHECK_CACHE.read_text().strip()

        if latest is None:
            import httpx

            resp = httpx.get('https://pypi.org/pypi/reviewd/json', timeout=2)
            latest = resp.json()['info']['version']
            UPDATE_CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
            UPDATE_CHECK_CACHE.write_text(latest)

        installed = tuple(int(x) for x in VERSION.split('.'))
        remote = tuple(int(x) for x in latest.split('.'))
        if remote > installed:
            exe = sys.executable
            if 'uv/tools' in exe or 'uv\\tools' in exe:
                cmd = 'uv tool upgrade reviewd'
            elif 'pipx' in exe:
                cmd = 'pipx upgrade reviewd'
            else:
                cmd = 'pip install --upgrade reviewd'
            click.echo(f'{YELLOW}Update available: v{VERSION} \u2192 v{latest}  ({cmd}){RESET}')
    except Exception:
        pass


def _ensure_global_config(config_path: str | None) -> Path:
    path = Path(config_path).expanduser() if config_path else CONFIG_PATH
    if not path.exists():
        from reviewd.wizard import run_wizard

        click.echo(f'No config found at {path}. Starting setup wizard...')
        run_wizard()
        if not path.exists():
            raise SystemExit(1)
    return path


@main.command()
@click.option('--sample', is_flag=True, help='Write annotated sample config (non-interactive, for VPS/CI)')
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.pass_context
def init(ctx, sample: bool, verbose: bool):
    """Interactive setup wizard — configure repos, credentials, and AI CLI."""
    _resolve_verbose(ctx, verbose)
    from reviewd.wizard import SAMPLE_CONFIG, run_wizard

    if sample:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(SAMPLE_CONFIG)
        click.echo(f'Created sample config at {CONFIG_PATH}')
        click.echo('Edit it to add your tokens and repos.')
        return

    if CONFIG_PATH.exists():
        click.echo(f'Global config already exists at {CONFIG_PATH}. \u2713')
        if not click.confirm('Re-run setup wizard?', default=False):
            return

    run_wizard()


@main.command()
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print reviews without posting')
@click.option('--review-existing', is_flag=True, help='Review unreviewed open PRs on startup')
@click.option('--cli', type=click.Choice(['claude', 'gemini', 'codex']), default=None, help='Override AI CLI')
@click.option('--concurrency', type=int, default=None, help='Max concurrent reviews (default: 4)')
@click.pass_context
def watch(ctx, verbose: bool, dry_run: bool, review_existing: bool, cli: str | None, concurrency: int | None):
    """Start the daemon — polls for new PRs and reviews them."""
    verbose = _resolve_verbose(ctx, verbose)
    _check_for_updates()
    _ensure_global_config(ctx.obj['config_path'])
    config = load_global_config(ctx.obj['config_path'])
    _attach_file_logging(config.log_file)
    _apply_cli_override(config, cli)
    if concurrency is not None:
        config.max_concurrent_reviews = concurrency
    run_poll_loop(config, dry_run=dry_run, review_existing=review_existing, verbose=verbose)


def _resolve_repo(config: GlobalConfig, repo_arg: str) -> str:
    # Treat '.' as the path to the current working directory
    if repo_arg == '.':
        repo_arg = str(Path.cwd())

    # Check if the argument is a valid directory path
    path = Path(repo_arg).expanduser().resolve()
    if path.is_dir():
        # See if there's a configured repo that matches this path exactly
        for r in config.repos:
            if Path(r.path).expanduser().resolve() == path:
                return r.name
        
        # If it wasn't matched but is a directory, just return its folder name
        # It's possible we dynamically discovered it, so its name would match its folder name.
        return path.name

    return repo_arg


def review_pr_cmd(ctx, repo: str, pr_id: int, verbose: bool, dry_run: bool, force: bool, cli: str | None, interactive_prompt: bool = False):
    # Retrieve 'verbose' from the context dict instead of assigning it as an attribute
    verbose_val = ctx.obj.get('verbose', False) or verbose
    _resolve_verbose(ctx, verbose_val)
    _ensure_global_config(ctx.obj['config_path'])
    config = load_global_config(ctx.obj['config_path'])
    _apply_cli_override(config, cli)
    
    repo_name = _resolve_repo(config, repo)
    review_single_pr(config, repo_name, pr_id=pr_id, dry_run=dry_run, force=force, interactive_prompt=interactive_prompt)


@main.command()
@click.argument('repo', metavar='<repo_name_or_path>')
@click.argument('pr_id', type=int)
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print review without posting')
@click.option('--force', is_flag=True, help='Review even if already reviewed (bypasses cooldown/skip)')
@click.option('--cli', type=click.Choice(['claude', 'gemini', 'codex']), default=None, help='Override AI CLI')
@click.pass_context
def pr(ctx, repo: str, pr_id: int, verbose: bool, dry_run: bool, force: bool, cli: str | None):
    """One-shot review of a specific PR. You can provide a repo name, a local path, or '.' to match the current directory's repo."""
    review_pr_cmd(ctx, repo, pr_id, verbose, dry_run, force, cli, interactive_prompt=True)


def _interactive_select(options: list[tuple[str, str]]) -> str | None:
    """Interactively select an option using fzf if available, else a numbered list."""
    if not options:
        return None

    import shutil
    has_fzf = shutil.which('fzf') is not None

    if has_fzf:
        input_text = '\n'.join(display for display, _ in options)
        try:
            result = subprocess.run(
                ['fzf', '--prompt=Select a PR to review> '],
                input=input_text,
                text=True,
                capture_output=True,
            )
            if result.returncode == 0:
                selected_display = result.stdout.strip()
                for display, value in options:
                    if display == selected_display:
                        return value
        except Exception:
            pass
        return None

    # Fallback: simple text prompt
    for i, (display, _) in enumerate(options, 1):
        click.echo(f'{i}) {display}')

    while True:
        try:
            choice = input('\nEnter number to select a PR (or empty to cancel): ').strip()
            if not choice:
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][1]
            click.echo('Invalid selection.')
        except ValueError:
            click.echo('Please enter a valid number.')
        except (KeyboardInterrupt, EOFError):
            return None


@main.command(name='ls')
@click.argument('repo', metavar='[repo_name_or_path]', required=False)
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='If a PR is selected, preview the review without posting')
@click.option('--force', is_flag=True, help='If a PR is selected, review even if already reviewed (bypasses cooldown/skip)')
@click.pass_context
def ls_repos(ctx, repo: str | None, verbose: bool, dry_run: bool, force: bool):
    """List watched repos and their open PRs. Select one to review."""
    _resolve_verbose(ctx, verbose)
    _ensure_global_config(ctx.obj['config_path'])
    config = load_global_config(ctx.obj['config_path'])
    state_db = StateDB(config.state_db)

    # Determine which repos to list
    target_repos = config.repos
    if repo is None:
        # Default to '.' if it's a valid repo, otherwise list all configured repos
        cwd_repo_name = _resolve_repo(config, '.')
        cwd_repo = next((r for r in config.repos if r.name == cwd_repo_name), None)
        if cwd_repo:
            target_repos = [cwd_repo]
    else:
        # Explicit repo passed
        repo_name = _resolve_repo(config, repo)
        explicit_repo = next((r for r in config.repos if r.name == repo_name), None)
        if explicit_repo:
            target_repos = [explicit_repo]
        else:
            available = ', '.join(r.name for r in config.repos) or '(none)'
            click.echo(f'Repo "{repo_name}" not found. Available: {available}', err=True)
            raise SystemExit(1)

    pr_options: list[tuple[str, str]] = []

    try:
        for repo_config in target_repos:
            try:
                provider = get_provider(config, repo_config)
                prs = provider.list_open_prs(repo_config.slug)
                if not prs:
                    continue
                for pr in prs:
                    reviewed = state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit)
                    marker = '\u2713' if reviewed else '\u2022'

                    # Create formatted string for display
                    display = f'[{repo_config.name}] #{pr.pr_id} {pr.title} ({pr.author}) {marker}'
                    # The value contains the actual repo name and PR ID separated by a space
                    value = f'{repo_config.name} {pr.pr_id}'
                    pr_options.append((display, value))
            except Exception as e:
                click.echo(f'  Error loading {repo_config.name}: {e}', err=True)
    finally:
        state_db.close()
    
    if not pr_options:
        click.echo('No open PRs found.')
        return

    # Prompt user to interactively select a PR
    selected_value = _interactive_select(pr_options)
    if not selected_value:
        return

    # Parse selection and invoke 'pr' command
    sel_repo, sel_pr_id_str = selected_value.split(' ', 1)
    sel_pr_id = int(sel_pr_id_str)

    dry_run_flag = " --dry-run" if dry_run else ""
    force_flag = " --force" if force else ""
    click.echo(f'\n{CYAN}Running: reviewd pr {sel_repo} {sel_pr_id}{dry_run_flag}{force_flag}{RESET}\n')

    # Invoke directly instead of via click's context to avoid context argument binding issues
    review_pr_cmd(ctx, repo=sel_repo, pr_id=sel_pr_id, verbose=verbose, dry_run=dry_run, force=force, cli=None, interactive_prompt=True)


@main.command()
@click.argument('repo', metavar='<repo_name_or_path>')
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--limit', default=20, help='Number of recent reviews to show')
@click.pass_context
def status(ctx, repo: str, verbose: bool, limit: int):
    """Show review history for a repo. You can provide a repo name, a local path, or '.' to match the current directory."""
    _resolve_verbose(ctx, verbose)
    _ensure_global_config(ctx.obj['config_path'])
    config = load_global_config(ctx.obj['config_path'])
    state_db = StateDB(config.state_db)
    try:
        repo_name = _resolve_repo(config, repo)

        history = state_db.get_review_history(repo_name, limit=limit)
        if not history:
            click.echo(f'No review history for {repo_name}')
            return
        for row in history:
            status_str = row['status']
            pr = row['pr_id']
            commit = row['source_commit'][:8]
            ts = row['created_at']
            err = row.get('error_message', '')
            line = f'PR #{pr}  {commit}  {status_str:<10}  {ts}'
            if err:
                line += f'  error: {err}'
            click.echo(line)
    finally:
        state_db.close()