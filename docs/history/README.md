# History

Dated records of audits, plans, and specifications. They are kept so the project does not repeat the same mistakes: what was measured, what was planned, and why a design changed.

These documents are snapshots. Each one states its date and, usually, its status near the top. Commands, versions, line numbers, and file paths inside them describe the repository at the time of writing, so they may no longer work. For current behavior use the [guides](../guides/), [reference](../reference/), and [runbooks](../runbooks/); [`../index.md`](../index.md) lists plans that are still open.

Layout:

- [`audits/`](audits/) - findings from code and operations reviews, the incident report, and runnable fixtures in [`audits/fixtures/`](audits/fixtures/).
- [`plans/`](plans/) - implementation plans. The five July 2026 plans were written for an agentic coding workflow, so their opening banners mention a tool plugin; the steps and checkboxes are preserved as written.
- [`specs/`](specs/) - design specifications that preceded implementation.

Paths in older documents point to the layout before the October 2026 reorganization: `docs/technical/` is now split into [`reference/`](../reference/), [`runbooks/`](../runbooks/), [`decisions/`](../decisions/), and [`research/`](../research/); `docs/troubleshooting/` is in `runbooks/`; `docs/development/` is in `guides/`; `docs/audits/`, `docs/plans/`, and `docs/superpowers/` are under `history/`.

## Audits

- 2026-09-23 - [yt-dlp audit, updates, and diagnostics](audits/ytdlp-and-update-audit-2026-09-23.md)
- 2026-09-24 - [Code quality and data cleanup audit](audits/code-quality-cleanup-audit-2026-09-24.md)
- 2026-09-24 - [VK live archives and download speed](audits/vk-live-and-download-speed-2026-09-24.md)
- 2026-09-24 - [YouTube audio language and iOS video processing](audits/youtube-audio-language-and-ios-2026-09-24.md)
- 2026-09-24 - [Implementation status after the audits](audits/implementation-status-2026-09-24.md) - what was fixed from the four audits above.
- 2026-10-06 - [Cache and delivery audit](audits/cache-and-delivery-audit-2026-10-06.md)
- 2026-10-06 - [Cache and delivery implementation acceptance](audits/cache-and-delivery-acceptance-2026-10-06.md)
- 2026-10-15 - [Network outage and local Bot API experiments](audits/network-outage-2026-10-15.md) (recorded 2026-10-16)

## Plans

- 2026-07-23 - [GitHub Actions DevOps hardening](plans/2026-07-23-github-actions-devops-hardening.md)
- 2026-07-23 - [Local Telegram Bot API](plans/2026-07-23-local-telegram-bot-api.md)
- 2026-07-23 - [Quality, CI/CD, and FSM hardening](plans/2026-07-23-quality-ci-fsm-hardening.md)
- 2026-07-25 - [Cache coverage and request deduplication](plans/2026-07-25-cache-coverage-and-request-dedup.md)
- 2026-07-29 - [Concurrent update processing and live status](plans/2026-07-29-concurrent-updates-and-live-status.md)
- 2026-09-27 - [Photo albums and post descriptions](plans/2026-09-27-photo-albums-and-descriptions.md)
- 2026-10-06 - [Cache and delivery change plan](plans/cache-and-delivery-changes-2026-10-06.md) (open)
- 2026-10-16 - [Network resilience plan](plans/network-resilience-2026-10-16.md) (open)

## Specs

- 2026-07-23 - [Dependencies, configuration, and local Telegram Bot API](specs/2026-07-23-dependencies-env-local-telegram-api-design.md)
- 2026-07-23 - [Instagram Highlight `/s/...` links](specs/2026-07-23-instagram-highlight-share-design.md)
- 2026-07-25 - [Disk-free delivery and format menu redesign](specs/2026-07-25-delivery-and-menu-redesign.md)
- 2026-07-29 - [User load queue and fair progress](specs/2026-07-29-download-queue-and-progress-design.md)
