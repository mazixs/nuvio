# Nuvio documentation

Documents are grouped by purpose. The first five sections describe how Nuvio works and is operated today; [History](#history) holds dated records that are kept so past mistakes are not repeated. A new contributor should read the [README](../README.md), then [Deployment](guides/deployment.md) and [Contributing](guides/contributing.md), then [Architecture](reference/architecture.md).

| Folder | What it holds |
|---|---|
| [`guides/`](guides/) | How to install, configure, and operate the bot |
| [`reference/`](reference/) | How the system is built: architecture, state machine, error codes, product scope |
| [`runbooks/`](runbooks/) | What to do when something breaks |
| [`decisions/`](decisions/) | Architecture decision records (ADRs) |
| [`research/`](research/) | Dated measurements and source research behind decisions |
| [`history/`](history/README.md) | Dated audits, plans, and specifications |

## Guides

- [Deployment](guides/deployment.md) - installation, Docker, systemd, and the WebUI.
- [Configuration](guides/configuration.md) - environment variables, cookies, and limits.
- [Media cache and delivery](guides/cache-and-delivery.md) - retention settings, album reuse, delivery evidence, migration, and rollback.
- [Feedback and moderation](guides/feedback-and-moderation.md) - user reports, WebUI review, and access restrictions.
- [Contributing](guides/contributing.md) - local environment, tests, and code conventions.

## Reference

- [Architecture](reference/architecture.md) - modules, data flow, design patterns, and SQLite WAL.
- [FSM architecture](reference/fsm-architecture.md) - state machines, bottlenecks, and prioritized improvements (analysis dated 2026-05-29).
- [Error codes](reference/error-codes.md) - code format, platform prefixes, categories, and log lookup.
- [Product requirements](reference/PRD.md) - product scope, user flows, and success criteria (snapshot dated 2026-07-23).

## Runbooks

- [Common issues](runbooks/common-issues.md) - startup, platforms, files, and cache.
- [YouTube download runbook](runbooks/youtube-download-runbook.md) - incident analysis, reliable probes, diagnosis, and yt-dlp pin updates.

## Decisions

- [ADR-001: TikTok audio and HEVC compatibility on iOS](decisions/adr-001-tiktok-audio-hevc-compatibility.md)
- [ADR-002: Video compatibility with Telegram on iOS](decisions/adr-002-ios-video-compatibility.md)

## Research

- [Delivery latency, disk writes, and network paths](research/latency-disk-network-research.md) - measurements behind the fast paths and direct URL delivery (2026-07-25).
- [Cache, delivery, and error-handling research](research/cache-and-delivery-research-2026-10-06.md) - Telegram contracts, comparable projects, and evidence boundaries (2026-10-06).

## Work in progress

These plans are still open. They live in [History](history/README.md) like every other plan, and their status line says which stages are done.

- [Network resilience plan](history/plans/network-resilience-2026-10-16.md) - recovery after long outages; grounded in the [network outage audit](history/audits/network-outage-2026-10-15.md).
- [Cache and delivery change plan](history/plans/cache-and-delivery-changes-2026-10-06.md) - implementation phases, acceptance gates, and rollback; see also the [audit](history/audits/cache-and-delivery-audit-2026-10-06.md) and the [acceptance record](history/audits/cache-and-delivery-acceptance-2026-10-06.md).

## History

[History](history/README.md) lists every dated audit, plan, and specification in chronological order. Nothing there is deleted: outdated documents stay as a record of what was tried, measured, and decided. Check the date and status at the top of a document before treating it as current.
