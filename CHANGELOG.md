# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-04-25

### Added
- Initial release of `teracron-sdk`.
- `teracron.up()` / `teracron.down()` — zero-ceremony singleton API.
- `TeracronClient` — explicit lifecycle for advanced use cases.
- `teracron-agent` CLI for sidecar monitoring.
- Hybrid RSA-4096 OAEP + AES-256-GCM encryption (wire-compatible with Node.js SDK).
- Zero-dependency protobuf encoder (wire-compatible with Node.js SDK).
- API key format (`tcn_...`) — encodes slug + public key in a single token.
- Domain allowlisting — restricts to `*.teracron.com` by default.
- Background daemon thread — never blocks the host process.
- `atexit` handler for graceful shutdown.
- Framework examples: Flask, FastAPI, Django.
- 87 tests, 100% bandit-clean.
