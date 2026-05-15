from __future__ import annotations

import logging

from reviewd.models import (
    CLI,
    SEVERITY_ORDER,
    AutoApproveConfig,
    Finding,
    GlobalConfig,
    PRInfo,
    ProjectConfig,
    ReviewResult,
    Severity,
)
from reviewd.providers.base import GitProvider
from reviewd.state import StateDB

logger = logging.getLogger(__name__)

TASK_MARKER = '[reviewd]'

SEVERITY_EMOJI = {
    Severity.CRITICAL: '\U0001f534',
    Severity.SUGGESTION: '\U0001f7e1',
    Severity.NITPICK: '\U0001f535',
    Severity.GOOD: '\U0001f7e2',
}


def _format_finding_summary(finding: Finding) -> str:
    loc = ''
    if finding.file:
        loc = f' — `{finding.file}`'
        if finding.line:
            loc += f' (line {finding.line})'
    return f'- **{finding.title}**{loc}\n  {finding.issue}'


# TODO: support multi-line suggestions (end_line) — needs correct line range in provider API calls
def _format_inline_comment(finding: Finding) -> str:
    emoji = SEVERITY_EMOJI.get(finding.severity, '')
    parts = [f'{emoji} **{finding.title}**', finding.issue]
    if finding.fix:
        parts.append(f'```suggestion\n{finding.fix}\n```')
    return '\n\n'.join(parts)


_MAX_TALLY_DOTS = 3


def _format_inline_tally(inline_findings: list[Finding]) -> str:
    """Compact emoji tally of inline findings, e.g. '🔴🔴 🟡🟡🟡+2 — posted as inline comments'."""
    grouped: dict[Severity, int] = {}
    for f in inline_findings:
        grouped[f.severity] = grouped.get(f.severity, 0) + 1

    parts = []
    for severity in [Severity.CRITICAL, Severity.SUGGESTION, Severity.NITPICK]:
        count = grouped.get(severity, 0)
        if count == 0:
            continue
        emoji = SEVERITY_EMOJI[severity]
        shown = min(count, _MAX_TALLY_DOTS)
        part = emoji * shown
        if count > _MAX_TALLY_DOTS:
            part += f'+{count - _MAX_TALLY_DOTS}'
        parts.append(part)

    if not parts:
        return ''
    return ' '.join(parts) + ' — posted as inline comments'


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f'{m}m {s}s'
    return f'{s}s'


def _format_summary_comment(
    pr: PRInfo,
    result: ReviewResult,
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    model: str | None = None,
    approved: bool = False,
    approve_blocked_reason: str | None = None,
) -> str:
    cli_name = cli.value.capitalize()
    title = global_config.review_title.replace('{cli}', cli_name)
    model_label = model or cli_name
    lines = [f'## {title}', '']

    # Tally of findings posted as inline comments (not shown in summary)
    inline_findings = [f for f in result.findings if id(f) in inline_ids]
    if inline_findings:
        lines.append(_format_inline_tally(inline_findings))
        lines.append('')

    if project_config.show_overview and result.overview:
        lines.extend([result.overview, ''])

    if result.tests_passed is not None:
        status = 'passed' if result.tests_passed else 'FAILED'
        lines.append(f'**Tests:** {status}')
        lines.append('')

    # Findings with inline comments appear only inline, not in the summary
    summary_findings = [f for f in result.findings if id(f) not in inline_ids]

    grouped: dict[Severity, list[Finding]] = {}
    for f in summary_findings:
        grouped.setdefault(f.severity, []).append(f)

    for severity in [Severity.CRITICAL, Severity.SUGGESTION, Severity.NITPICK, Severity.GOOD]:
        findings = grouped.get(severity, [])
        if not findings:
            continue
        emoji = SEVERITY_EMOJI[severity]
        lines.append(f'### {emoji} {severity.value.capitalize()} ({len(findings)})')
        lines.append('')
        for finding in findings:
            lines.append(_format_finding_summary(finding))
        lines.append('')

    if result.summary:
        lines.append(f'**Bottom line:** {result.summary}')
        lines.append('')

    if approved and result.approve_reason:
        lines.append(f'**Auto-approve rationale:** {result.approve_reason}')
        lines.append('')

    if approve_blocked_reason:
        lines.append(f'**Auto-approve blocked:** AI recommended approval, but {approve_blocked_reason}.')
        lines.append('')

    duration_str = f' in {_format_duration(result.duration_seconds)}' if result.duration_seconds else ''
    footer = global_config.footer.replace('{duration}', duration_str).replace('{model}', model_label)
    lines.append(f'*{footer}*')
    if not pr.is_local:
        lines.append('*Replies to this comment are not monitored.*')

    return '\n'.join(lines)


def _sync_critical_task(provider, pr: PRInfo, result: ReviewResult, project_config: ProjectConfig):
    try:
        tasks = provider.list_tasks(pr.repo_slug, pr.pr_id)
        for task in tasks:
            if TASK_MARKER in task.get('content', {}).get('raw', ''):
                provider.delete_task(pr.repo_slug, pr.pr_id, task['id'])
        has_critical = any(f.severity == Severity.CRITICAL for f in result.findings)
        if has_critical:
            message = f'{TASK_MARKER} {project_config.critical_task_message}'
            provider.create_task(pr.repo_slug, pr.pr_id, message)
    except Exception:
        logger.exception('Failed to sync critical task on PR #%d', pr.pr_id)


def _check_auto_approve_gates(
    aa: AutoApproveConfig,
    result: ReviewResult,
    diff_lines: int | None,
) -> str | None:
    """Returns a blocking reason string, or None if auto-approve should proceed."""
    if not aa.enabled:
        return 'Auto-approve is disabled'

    if aa.max_diff_lines is not None and diff_lines is not None and diff_lines > aa.max_diff_lines:
        return f'diff too large ({diff_lines} > {aa.max_diff_lines})'

    if aa.max_findings is not None:
        issue_count = sum(1 for f in result.findings if f.severity != Severity.GOOD)
        if issue_count > aa.max_findings:
            return f'too many findings ({issue_count} > {aa.max_findings})'

    if aa.max_severity is not None:
        max_allowed = SEVERITY_ORDER.get(aa.max_severity, 3)
        for f in result.findings:
            f_order = SEVERITY_ORDER.get(f.severity.value, 3)
            if f_order > max_allowed:
                return f'finding severity {f.severity.value} exceeds max {aa.max_severity}'

    if not result.approve:
        return 'AI did not approve'

    return None


def _resolve_auto_approve(
    aa: AutoApproveConfig,
    result: ReviewResult,
    diff_lines: int | None,
) -> tuple[bool, str | None]:
    """Returns (approved, blocked_reason_to_show).

    blocked_reason_to_show is set only when the AI recommended approval
    but a config gate prevented it and show_blocked_reason is enabled.
    """
    if not aa.enabled:
        return False, None

    blocked = _check_auto_approve_gates(aa, result, diff_lines)
    if not blocked:
        return True, None

    # AI wanted to approve but a gate stopped it
    show_reason = aa.show_blocked_reason and result.approve and blocked != 'AI did not approve'
    return False, blocked if show_reason else None


def _post_review_impl(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    inline_findings: list[Finding],
    inline_ids: set[int],
    cli: CLI = CLI.CLAUDE,
    model: str | None = None,
    diff_lines: int | None = None,
):
    logger.info('Posting review: %d inline + summary comment', len(inline_findings))

    old_comment_ids = state_db.get_comment_ids(pr.repo_slug, pr.pr_id)
    if old_comment_ids:
        logger.info('Deleting %d old comments on PR #%d', len(old_comment_ids), pr.pr_id)
        deleted = 0
        for cid in old_comment_ids:
            try:
                provider.delete_comment(pr.repo_slug, pr.pr_id, cid)
                state_db.remove_comment(pr.repo_slug, pr.pr_id, cid)
                deleted += 1
            except Exception as e:
                logger.warning('Failed to delete comment %s: %s', cid, e)
        logger.info('Deleted %d comments', deleted)

    aa = project_config.auto_approve
    approved, approve_blocked_reason = _resolve_auto_approve(aa, result, diff_lines)

    summary_text = _format_summary_comment(
        pr,
        result,
        inline_ids,
        global_config,
        project_config,
        cli,
        model=model,
        approved=approved,
        approve_blocked_reason=approve_blocked_reason,
    )

    posted_cids = []
    from reviewd.colors import RED, RESET

    for finding in inline_findings:
        text = _format_inline_comment(finding)
        try:
            cid = provider.post_comment(
                pr.repo_slug, pr.pr_id, text, file_path=finding.file, line=finding.line, source_commit=pr.source_commit
            )
            if cid:
                posted_cids.append(cid)
        except Exception as e:
            logger.error('%sFailed to post inline comment on %s:%s%s\n%s', RED, finding.file, finding.line, RESET, e)

    try:
        cid = provider.post_comment(pr.repo_slug, pr.pr_id, summary_text)
        if cid:
            posted_cids.append(cid)
    except Exception as e:
        logger.error('%sFailed to post summary comment%s\n%s', RED, RESET, e)

    for cid in posted_cids:
        state_db.record_comment(pr.repo_slug, pr.pr_id, cid)

    # Approve
    if approved:
        try:
            logger.info('Auto-approving PR #%d', pr.pr_id)
            provider.approve_pr(pr.repo_slug, pr.pr_id)
        except Exception as e:
            logger.error('%sFailed to auto-approve PR%s\n%s', RED, RESET, e)

    # Critical tasks (BitBucket only for now)
    if project_config.critical_task:
        _sync_critical_task(provider, pr, result, project_config)


def post_review(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    cli: CLI = CLI.CLAUDE,
    model: str | None = None,
    dry_run: bool = False,
    post: bool = False,
    diff_lines: int | None = None,
):
    # Deduplicate findings by file + line + title
    seen: set[tuple] = set()
    unique_findings = []
    for f in result.findings:
        key = (f.file, f.line, f.title)
        if key not in seen:
            seen.add(key)
            unique_findings.append(f)
        else:
            logger.debug('Skipping duplicate finding: %s:%s %s', f.file, f.line, f.title)
    # Filter out skipped severities
    skip = {s for s in project_config.skip_severities}
    if skip:
        unique_findings = [f for f in unique_findings if f.severity.value not in skip]
        logger.info('Filtered out %s severities, %d findings remain', skip, len(unique_findings))

    result = ReviewResult(
        overview=result.overview,
        findings=unique_findings,
        summary=result.summary,
        tests_passed=result.tests_passed,
        approve=result.approve,
        approve_reason=result.approve_reason,
        duration_seconds=result.duration_seconds,
    )

    inline_severities = {s for s in project_config.inline_comments_for}
    inline_findings = [f for f in result.findings if f.severity.value in inline_severities and f.file and f.line]

    max_inline = project_config.max_inline_comments
    if max_inline is not None and len(inline_findings) > max_inline:
        logger.info(
            'Inline comments (%d) exceed max (%d), skipping all inline',
            len(inline_findings),
            max_inline,
        )
        inline_findings = []

    inline_ids = {id(f) for f in inline_findings}

    if dry_run:
        # True dry-run: print only, no DB log, no prompt
        _print_dry_run(
            pr,
            result,
            inline_findings,
            inline_ids,
            global_config,
            project_config,
            cli,
            model=model,
            diff_lines=diff_lines,
            skip_confirm=True,
        )
        return

    if post:
        # Auto-post and log to DB
        _post_review_impl(
            provider,
            state_db,
            pr,
            result,
            project_config,
            global_config,
            inline_findings,
            inline_ids,
            cli,
            model=model,
            diff_lines=diff_lines,
        )
        return

    # Default: Preview + Prompt. Log to DB either way.
    should_post = _print_dry_run(
        pr,
        result,
        inline_findings,
        inline_ids,
        global_config,
        project_config,
        cli,
        model=model,
        diff_lines=diff_lines,
        skip_confirm=False,
    )
    
    if should_post:
        _post_review_impl(
            provider,
            state_db,
            pr,
            result,
            project_config,
            global_config,
            inline_findings,
            inline_ids,
            cli,
            model=model,
            diff_lines=diff_lines,
        )
    else:
        # Mark as reviewed even if we didn't post the comment
        state_db.start_review(pr.repo_slug, pr.pr_id, pr.source_commit)
        state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit)


def _print_dry_run(
    pr: PRInfo,
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    model: str | None = None,
    diff_lines: int | None = None,
    skip_confirm: bool = False,
) -> bool:
    print('\n' + '=' * 60)
    print('REVIEW PREVIEW — the following would be posted:' if not skip_confirm else 'DRY RUN — results:')
    print('=' * 60)

    if inline_findings:
        print(f'\n--- Inline Comments ({len(inline_findings)}) ---')
        for f in inline_findings:
            print(f'\n  File: {f.file}:{f.line}')
            print(f'  {_format_inline_comment(f)}')

    aa = project_config.auto_approve
    approved = False
    approve_blocked_reason = None
    if aa.enabled:
        approved, approve_blocked_reason = _resolve_auto_approve(aa, result, diff_lines)
        if not approved:
            print(f'\n--- Auto-Approve: BLOCKED ({approve_blocked_reason or "AI did not approve"}) ---')

    print('\n--- Summary Comment ---')
    print(
        _format_summary_comment(
            pr,
            result,
            inline_ids,
            global_config,
            project_config,
            cli,
            model=model,
            approved=approved,
            approve_blocked_reason=approve_blocked_reason,
        )
    )

    print('\n==================== REVIEW SUMMARY ====================')
    if pr.is_local:
        if result.approve:
            print(f'✅ AI Recommendation: APPROVE')
        else:
            print(f'❌ AI Recommendation: DO NOT APPROVE')
    elif aa.enabled:
        if approved:
            print(f'✅ Auto-Approve: WOULD APPROVE PR')
        else:
            print(f'❌ Auto-Approve: BLOCKED ({approve_blocked_reason or "AI did not approve"})')
    else:
        print('ℹ️ Auto-Approve is disabled for this project.')
        if result.approve:
            print(f'  (The AI recommended approval, but auto-approve is turned off)')
        else:
            print(f'  (The AI did not recommend approval)')
            
    if not pr.is_local:
        print(f'💬 Comments: {len(inline_findings)} inline + 1 summary comment.')
    print('=========================================================\n')

    if skip_confirm:
        return False

    import click
    from reviewd.colors import YELLOW, RESET

    prompt = f'{YELLOW}Post this review?'
    if aa.enabled and approved:
        prompt = f'{YELLOW}This PR would be approved. Post comments and approve?'
        
    prompt += f'{RESET}'
    
    try:
        return click.confirm(prompt, default=False)
    except (click.Abort, EOFError, KeyboardInterrupt):
        return False
