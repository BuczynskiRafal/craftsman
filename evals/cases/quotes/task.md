# Quote in other currencies

Sales wants quotes in the customer's currency.

- `Quoter(prices, rates)` takes a `rates(currency)` callable that returns the USD to `currency`
  exchange rate as a `Decimal`. `USD` stays the base currency and needs no rate lookup.
- `quote(sku, qty, currency="USD")` returns the amount in `currency`: the existing bulk discount
  first, then the conversion, rounded to cents (half even).
- Quotes stay cached, so asking for the same quote again neither recomputes it nor asks `rates`
  again.
- When the rate for a currency cannot be fetched (`rates` raises), the quote fails with
  `RateUnavailable(currency)`. We must never return an amount in the wrong currency.
- `total(lines, currency="USD")` adds converted lines.
- `summarize(lines, currency="USD")` prices what it can and returns a `Summary(total, currency,
  unknown)` where `unknown` lists the SKUs that are not in the catalog, instead of failing on the
  first one. Batch jobs may pass one `unknown` list to several calls to collect the SKUs of a
  whole batch.
