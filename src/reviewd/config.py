from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path

import yaml

from reviewd.models import (
    CLI,
    SEVERITY_ORDER,
    AutoApproveConfig,
    GithubConfig,
    GlobalConfig,
    ProjectConfig,
    RepoConfig,
)
from reviewd.providers.base import GitProvider

ENV_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


def _resolve_env_vars(value: str) -> str:
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f'Environment variable {var_name} is not set')
        return env_value

    return ENV_VAR_PATTERN.sub(replacer, value)


def _parse_bitbucket_tokens(data: dict) -> dict[str, str]:
    return {str(workspace): _resolve_env_vars(str(token)) for workspace, token in data.items()}


def _parse_github_config(data: dict) -> GithubConfig:
    return GithubConfig(
        token=_resolve_env_vars(str(data['token'])),
    )


def _parse_cli(value: str, repo_name: str | None = None) -> CLI:
    value = str(value).strip()
    if ' ' in value:
        context = f' for repo "{repo_name}"' if repo_name else ''
        raise ValueError(
            f'Invalid cli value{context}: "{value}". '
            f'Use cli_args for extra arguments (e.g. cli_args: ["{value.split(maxsplit=1)[1]}"])'
        )
    return CLI(value)


def _parse_auto_approve(data: dict) -> AutoApproveConfig:
    return AutoApproveConfig(
        enabled=data.get('enabled', False),
        max_diff_lines=data.get('max_diff_lines'),
        max_severity=data.get('max_severity'),
        max_findings=data.get('max_findings'),
        rules=data.get('rules'),
        show_blocked_reason=data.get('show_blocked_reason', True),
    )


def _merge_auto_approve(
    global_aa: AutoApproveConfig | None,
    project_aa: AutoApproveConfig | None,
    legacy_approve_if_no_critical: bool = False,
) -> AutoApproveConfig:
    # Backward compat: approve_if_no_critical → auto_approve equivalent
    if legacy_approve_if_no_critical and global_aa is None and project_aa is None:
        return AutoApproveConfig(enabled=True, max_severity='suggestion')

    if global_aa is None and project_aa is None:
        return AutoApproveConfig()

    g = global_aa or AutoApproveConfig()
    p = project_aa or AutoApproveConfig()

    # enabled must be true in at least one source
    enabled = g.enabled or p.enabled

    # Per-project takes min() of numeric thresholds (stricter wins)
    max_diff_lines = None
    if g.max_diff_lines is not None and p.max_diff_lines is not None:
        max_diff_lines = min(g.max_diff_lines, p.max_diff_lines)
    else:
        max_diff_lines = g.max_diff_lines if p.max_diff_lines is None else p.max_diff_lines

    max_findings = None
    if g.max_findings is not None and p.max_findings is not None:
        max_findings = min(g.max_findings, p.max_findings)
    else:
        max_findings = g.max_findings if p.max_findings is None else p.max_findings

    # Lowest severity wins (stricter)
    max_severity = None
    if g.max_severity is not None and p.max_severity is not None:
        g_ord = SEVERITY_ORDER.get(g.max_severity, 3)
        p_ord = SEVERITY_ORDER.get(p.max_severity, 3)
        max_severity = g.max_severity if g_ord <= p_ord else p.max_severity
    else:
        max_severity = g.max_severity if p.max_severity is None else p.max_severity

    # Concatenate rules
    parts = []
    if g.rules:
        parts.append(g.rules.strip())
    if p.rules:
        parts.append(p.rules.strip())
    rules = '\n'.join(parts) if parts else None

    # Project can disable showing blocked reason; if either explicitly sets False, honor it
    show_blocked_reason = g.show_blocked_reason and p.show_blocked_reason

    return AutoApproveConfig(
        enabled=enabled,
        max_diff_lines=max_diff_lines,
        max_severity=max_severity,
        max_findings=max_findings,
        rules=rules,
        show_blocked_reason=show_blocked_reason,
    )


CONFIG_NAME = '.reviewd.yaml'


def load_repo_config_from_path(repo_path: Path, global_cli: CLI, global_model: str | None) -> RepoConfig | None:
    project_config_file = repo_path / CONFIG_NAME
    if not project_config_file.exists():
        return None

    try:
        with open(project_config_file) as pf:
            project_data = yaml.safe_load(pf) or {}
    except Exception:
        return None

    project_repo_data = project_data.get('repo')
    if not project_repo_data and 'repos' in project_data:
        if isinstance(project_data['repos'], list) and len(project_data['repos']) > 0:
            project_repo_data = project_data['repos'][0]
        elif isinstance(project_data['repos'], dict):
            project_repo_data = project_data['repos']

    if not project_repo_data or not isinstance(project_repo_data, dict):
        return None

    repo_data = {'path': str(repo_path), 'name': repo_path.name, **project_repo_data}
    if 'provider' not in repo_data:
        return None

    repo_gh = None
    if 'github' in repo_data:
        repo_gh = _parse_github_config(repo_data['github'])

    repo_cli = _parse_cli(repo_data['cli'], repo_data['name']) if 'cli' in repo_data else global_cli

    return RepoConfig(
        name=repo_data['name'],
        path=str(repo_path),
        provider=repo_data['provider'],
        repo_slug=repo_data.get('repo_slug'),
        workspace=repo_data.get('workspace'),
        github=repo_gh,
        cli=repo_cli,
        model=repo_data.get('model', global_model),
    )


def load_global_config(path: str | Path | None = None) -> GlobalConfig:
    if path is None:
        config_home = os.environ.get('XDG_CONFIG_HOME', '~/.config')
        path = Path(config_home).expanduser() / 'reviewd' / 'config.yaml'
    else:
        path = Path(path).expanduser()

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise SystemExit(f'Invalid YAML in {path}: {e}') from e

    if not isinstance(data, dict):
        raise SystemExit(f'Invalid config in {path}: expected a YAML mapping, got {type(data).__name__}')

    global_bb = _parse_bitbucket_tokens(data['bitbucket']) if 'bitbucket' in data else {}

    global_gh = None
    if 'github' in data:
        global_gh = _parse_github_config(data['github'])

    global_cli = _parse_cli(data.get('cli', 'claude'))
    global_model = data.get('model')

    repos = []
    for i, base_repo_data in enumerate(data.get('repos', [])):
        if 'path' not in base_repo_data:
            raise SystemExit(f'Repo #{i + 1} in {path} is missing required field "path"')

        repo_path = Path(base_repo_data['path']).expanduser()

        # Load per-project overrides if any
        project_config_file = repo_path / CONFIG_NAME
        project_data = {}
        if project_config_file.exists():
            try:
                with open(project_config_file) as pf:
                    project_data = yaml.safe_load(pf) or {}
            except yaml.YAMLError as e:
                raise SystemExit(f'Invalid YAML in {project_config_file}: {e}') from e

        project_repo_data = project_data.get('repo')
        if not project_repo_data and 'repos' in project_data:
            if isinstance(project_data['repos'], list) and len(project_data['repos']) > 0:
                project_repo_data = project_data['repos'][0]
            elif isinstance(project_data['repos'], dict):
                project_repo_data = project_data['repos']

        repo_data = {**base_repo_data}
        if project_repo_data and isinstance(project_repo_data, dict):
            repo_data.update(project_repo_data)

        if 'name' not in repo_data:
            repo_data['name'] = repo_path.name
        if 'provider' not in repo_data:
            raise SystemExit(f'Repo in {repo_path} is missing required field "provider"')

        repo_gh = None
        if 'github' in repo_data:
            repo_gh = _parse_github_config(repo_data['github'])

        repo_cli = _parse_cli(repo_data['cli'], repo_data['name']) if 'cli' in repo_data else global_cli
        repos.append(
            RepoConfig(
                name=repo_data['name'],
                path=str(repo_path),
                provider=repo_data['provider'],
                repo_slug=repo_data.get('repo_slug'),
                workspace=repo_data.get('workspace'),
                github=repo_gh,
                cli=repo_cli,
                model=repo_data.get('model', global_model),
            )
        )

    # Discover repo in current directory if .reviewd.yaml has repo definition
    cwd = Path.cwd()
    if not any(Path(r.path).resolve() == cwd.resolve() for r in repos):
        cwd_repo = load_repo_config_from_path(cwd, global_cli, global_model)
        if cwd_repo:
            repos.append(cwd_repo)

    default_data_home = os.environ.get('XDG_DATA_HOME', '~/.local/share')
    state_db = data.get('state_db', f'{default_data_home}/reviewd/state.db')
    state_db = str(Path(_resolve_env_vars(state_db)).expanduser())

    log_file = data.get('log_file')
    if log_file is not None:
        log_file = str(Path(_resolve_env_vars(str(log_file))).expanduser())

    global_aa = _parse_auto_approve(data['auto_approve']) if 'auto_approve' in data else None

    return GlobalConfig(
        repos=repos,
        bitbucket=global_bb,
        github=global_gh,
        state_db=state_db,
        cli=global_cli,
        model=global_model,
        cli_args=data.get('cli_args', []),
        cli_defaults={CLI(k): v for k, v in data.get('cli_defaults', {}).items()},
        instructions=data.get('instructions'),
        auto_approve=global_aa,
        inline_comments_for=data.get('inline_comments_for'),
        skip_title_patterns=data.get('skip_title_patterns', ['[no-review]', '[wip]', '[no-claudiu]']),
        skip_authors=data.get('skip_authors', []),
        poll_interval_seconds=data.get('poll_interval_seconds', 60),
        max_concurrent_reviews=data.get('max_concurrent_reviews', 4),
        review_title=data.get('review_title', "review'd by {cli}"),
        footer=data.get('footer', GlobalConfig.footer),
        log_file=log_file,
    )


_config_logger = logging.getLogger(__name__)


_GIT_ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}


def _sync_project_config(repo: Path):
    """Auto-pull if .reviewd.yaml changed on remote and working copy is clean."""
    # Fetch latest
    subprocess.run(['git', 'fetch', '--quiet'], cwd=repo, capture_output=True, env=_GIT_ENV, timeout=120)

    # Check if local working copy is clean
    status = subprocess.run(
        ['git', 'status', '--porcelain'],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.returncode != 0 or status.stdout.strip():
        return

    # Check if HEAD is behind remote
    behind = subprocess.run(
        ['git', 'rev-list', '--count', 'HEAD..@{u}'],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if behind.returncode != 0 or behind.stdout.strip() == '0':
        return

    result = subprocess.run(
        ['git', 'pull', '--ff-only'],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=120,
    )
    if result.returncode == 0:
        _config_logger.info('Auto-pulled %s', repo.name)
    else:
        _config_logger.error('Failed to pull %s: %s', repo.name, result.stderr.strip())


def _read_project_config_data(repo_path: str | Path) -> dict:
    repo = Path(repo_path)
    config_path = repo / CONFIG_NAME

    _sync_project_config(repo)

    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}

    return {}


def load_project_config(repo_path: str | Path, global_config: GlobalConfig) -> ProjectConfig:
    data = _read_project_config_data(repo_path)

    # Merge instructions: global + per-project
    parts = []
    if global_config.instructions:
        parts.append(global_config.instructions.strip())
    if data.get('instructions'):
        parts.append(data['instructions'].strip())
    # Backwards compat: support old guidelines/explore fields
    if data.get('guidelines'):
        parts.append(data['guidelines'].strip())
    if data.get('explore'):
        parts.append(data['explore'].strip())
    instructions = '\n\n'.join(parts) if parts else None

    project_aa = _parse_auto_approve(data['auto_approve']) if 'auto_approve' in data else None
    legacy = data.get('approve_if_no_critical', False)
    auto_approve = _merge_auto_approve(global_config.auto_approve, project_aa, legacy)

    return ProjectConfig(
        instructions=instructions,
        test_commands=data.get('test_commands', []),
        inline_comments_for=data.get('inline_comments_for', global_config.inline_comments_for or ['critical']),
        max_inline_comments=data.get('max_inline_comments'),
        skip_severities=data.get('skip_severities', []),
        show_overview=data.get('show_overview', False),
        min_diff_lines=data.get('min_diff_lines', 0),
        min_diff_lines_update=data.get('min_diff_lines_update', 5),
        review_cooldown_minutes=data.get('review_cooldown_minutes', 0),
        auto_approve=auto_approve,
        critical_task=data.get('critical_task', False),
        critical_task_message=data.get('critical_task_message', ProjectConfig.critical_task_message),
    )


def resolve_bitbucket_config(global_config: GlobalConfig, repo_config: RepoConfig) -> tuple[str, str]:
    workspace = repo_config.workspace
    if not workspace:
        raise ValueError(f'Repo "{repo_config.name}" is a bitbucket repo but has no workspace specified')
    token = global_config.bitbucket.get(workspace)
    if not token:
        raise ValueError(f'No bitbucket auth_token found for workspace "{workspace}" (repo "{repo_config.name}")')
    return workspace, token


def resolve_github_config(global_config: GlobalConfig, repo_config: RepoConfig) -> GithubConfig:
    if repo_config.github is not None:
        return repo_config.github
    if global_config.github is not None:
        return global_config.github
    raise ValueError(f'No github config found for repo "{repo_config.name}"')


# Providers wrap a long-lived httpx.Client. Recreating them per call leaks
# pooled keepalive sockets in long-running daemons (FD exhaustion under
# launchd's default 256-FD soft limit). Cache by credential identity so each
# workspace/token reuses one client for the process lifetime.
_provider_cache: dict[tuple, GitProvider] = {}
_provider_cache_lock = threading.Lock()


def get_provider(global_config: GlobalConfig, repo_config: RepoConfig) -> GitProvider:
    if repo_config.provider == 'github':
        from reviewd.providers.github import GithubProvider

        config = resolve_github_config(global_config, repo_config)
        key = ('github', config.token)
        with _provider_cache_lock:
            cached = _provider_cache.get(key)
            if cached is None:
                cached = GithubProvider(config)
                _provider_cache[key] = cached
        return cached

    from reviewd.providers.bitbucket import BitbucketProvider

    workspace, token = resolve_bitbucket_config(global_config, repo_config)
    key = ('bitbucket', workspace, token)
    with _provider_cache_lock:
        cached = _provider_cache.get(key)
        if cached is None:
            cached = BitbucketProvider(workspace, token)
            _provider_cache[key] = cached
    return cached