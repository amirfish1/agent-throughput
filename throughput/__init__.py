"""Unified, local session-usage database (the "Throughput" DB).

One normalized ``sessions`` row per logical session across Claude Code, Codex
and Kimi, plus a per-API-call ``usage_events`` table, a versioned
``price_rates`` table and cost views. Source session stores are only ever read.

Stdlib-only, no side effects at import. Entry point: ``throughput`` (or
``python3 -m throughput``). See ``README.md``.
"""
