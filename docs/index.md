# Nuvio documentation

## Guides

- [Deployment](guides/deployment.md) - installation, Docker, systemd, and the WebUI.
- [Feedback and moderation](guides/feedback-and-moderation.md) - user reports, WebUI review, and access restrictions.
- [Media cache and delivery](guides/cache-and-delivery.md) - retention settings, album reuse, delivery evidence, migration, and rollback.
- [Configuration](guides/configuration.md) - environment variables, cookies, and limits.

## Technical reference

- [Product requirements](PRD.md) - product scope, user flows, and success criteria.
- [Architecture](technical/architecture.md) - modules, data flow, design patterns, and SQLite WAL.
- [FSM architecture](technical/fsm-architecture.md) - state machines, bottlenecks, and prioritized improvements.
- [YouTube download runbook](technical/youtube-download-runbook.md) - incident analysis, reliable probes, diagnosis, and yt-dlp pin updates.
- [Error codes](error-codes.md) - code format, platform prefixes, categories, and log lookup.
- [Cache and delivery research](technical/cache-and-delivery-research-2026-10-06.md) - Telegram contracts, comparable projects, and evidence boundaries.

## Current audits and plans

- [Cache and delivery implementation acceptance](audits/cache-and-delivery-acceptance-2026-10-06.md) - local evidence, runtime verification, and pending external checks.
- [Cache and delivery audit](audits/cache-and-delivery-audit-2026-10-06.md) - dated findings and reproducible local experiments.
- [Cache and delivery change plan](plans/cache-and-delivery-changes-2026-10-06.md) - implementation phases, acceptance gates, and rollback.

## Development

- [Contributing](development/contributing.md) - local environment, tests, and code conventions.

## Troubleshooting

- [Common issues](troubleshooting/common-issues.md) - startup, platforms, files, and cache.

## Historical material

- [Audits](audits/) - dated findings and implementation snapshots.
- [Design decisions and research](technical/) - ADRs and technical investigations.
- [Plans and specifications](superpowers/) - historical implementation plans and feature specifications.
