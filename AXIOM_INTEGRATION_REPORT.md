# Axiom Integration Research Report

## Verdict

No supported automatic Axiom data collection path was found.

ArloBit should not integrate Axiom by scraping private frontend endpoints, browser sessions, cookies, or reverse-engineered WebSockets. Until Axiom publishes an official API, WebSocket, webhook, export, or documented partner interface, Axiom should remain an audited-but-disabled research source.

## Sources Reviewed

- Official docs root: `https://docs.axiom.trade/`
- Wallet Tracking docs: `https://docs.axiom.trade/wallet-tracking/monitor-wallets`
- Trader Scan docs: `https://docs.axiom.trade/trader-scan`

The official docs describe product features such as Wallet Tracking and Trader Scan. Trader Scan exposes wallet-level activity, bought/sold amounts, current balance, realized PnL, and hold duration in the Axiom UI. The docs do not document a machine API, WebSocket, webhook, export endpoint, API key flow, or developer authentication model.

## Implementation Decision

Implemented a research-source audit layer only:

- `axiom_source_audits`: stores the current integration status and reviewed source notes.
- `axiom_signals`: reserved SQLite table for future supported Axiom signals.
- `python -m arlobit.axiom --audit`: records the current source audit.
- `python -m arlobit.axiom --report`: reports source status and signal quality.

No Axiom signals are fabricated. `axiom_signals` remains empty until a supported ingestion path exists.

## Current Status

Status: `unsupported_no_official_machine_api_found`

Automatic collection: disabled.

Reason: no official API/WebSocket/export was found in the reviewed public documentation.

## Future Trigger To Revisit

Re-open this integration only if one of these becomes available:

- official Axiom API docs
- official WebSocket docs
- official webhook/export feature
- documented partner access
- explicit permission and documentation from Axiom support

## Trading Impact

None.

No scanner logic, filters, scoring, execution, signing, or trading behaviour was changed.
