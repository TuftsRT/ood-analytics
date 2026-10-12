# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-12

First public release.

### Added

- Collector (`ood-usage-collect`) and systemd timer that read the Apache access logs every 15 minutes and keep daily aggregates in SQLite. Follows rename rotation, `copytruncate`, and `.gz`/`.bz2`/`.xz` compression without double counting.
- Usage page: logins, sessions, and active users by day, week, month, or year, with presets, calendar years, and custom ranges.
- Interactive app launches and failed launches, by app.
- Interactive app wall time, core hours, and GPU hours from Slurm accounting, with clusters and `sacct` detected from the OnDemand cluster configs.
- Use of Files, Shell, Active Jobs, Job Composer, and other Passenger apps.
- Filtering the usage page to a single interactive app or tool, with its own trend charts and top users; the filter is kept in the URL.
- Day-of-week by hour-of-day activity heatmap in each viewer's own time zone.
- Issues page: server and client errors by area, failed personal web server starts, slow pages and response-time percentiles (with `%D` in the log format), and the people most affected.
- Most active users, and CSV downloads of the time series and per-user totals; per-user reporting can be turned off with `show_top_users`.
- Warning banner when the collector hasn't run in over a day.
- Print layout that keeps charts with their headings and always uses the light palette.
- Smoke test and GitHub Actions CI on Rocky Linux 8 and 9.
- AppVerse metadata (`appverse.yml`) and app icon.

[Unreleased]: https://github.com/TuftsRT/ood-analytics/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/TuftsRT/ood-analytics/releases/tag/v1.0.0
