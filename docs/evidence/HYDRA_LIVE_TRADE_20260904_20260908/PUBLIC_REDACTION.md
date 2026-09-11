# Public redaction boundary

This Git branch contains a reviewable redacted copy of the private Hydra live evidence bundle.

Retained:

- Hydra target weights and research lineage hashes;
- Server order batch and identifiers;
- strategy reference, limit, arrival and execution prices;
- Hydra-related order/trade quantities and cash checkpoints;
- sanitized MiniQMT-derived order/trade/funds timelines and source-log SHA-256;
- Windows client version history and compatibility patch.

Excluded:

- live account number and private account identity;
- API keys, trigger/backup tokens and WeCom webhook;
- raw MiniQMT logs, because the upstream application writes sensitive account fields into them;
- current full-account QMT positions unrelated to Hydra;
- the private live SQLite binary snapshot.

The unredacted archive remains local. Its SHA-256 before this public copy was prepared was
`4b72c0026fcfcd8578d457e73ac76c8c23954c7b0b269a1b7a62a4969ada3c5e`.
