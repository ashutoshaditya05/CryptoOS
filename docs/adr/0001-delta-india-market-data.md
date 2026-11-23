# Delta India Bitcoin market data

TradeOS starts with one market, Delta Exchange India’s `BTC_INR` spot product. INR is the quote currency. The adapter checks the exchange’s product catalog before importing data and requires the configured product to have the `spot` contract type. [Delta spot API](https://docs.delta.exchange/#spot-trading)

The first adapter calls Delta’s public REST API directly with HTTPX. This preserves Delta’s symbol, explicit time bounds, and decimal prices at the adapter boundary. CCXT’s Delta adapter defaults to the global API host and requires a host override for Delta India. The ingestion code accepts any provider that implements the `FetchCandles` contract. [CCXT Delta integration](https://github.com/ccxt/ccxt/blob/master/wiki/comparisons/ccxt-vs-delta-api.md)

The SQLite manifest indexes immutable Parquet batches. A writer checks for duplicates under a SQLite write transaction, writes the file, then publishes its manifest entry. Readers only access files listed in the manifest. After a crash between the file rename and the SQLite commit, an unreferenced file can remain on disk. Backups need both the SQLite database and the Parquet directory.
