# Shipping for orders

Orders get a shipping cost, a carrier and a tracking number, and carriers can report their progress.

- **Zones.** A destination is `domestic` (US), `remote` (US ZIP codes starting 967, 968 and 995-999),
  `near` (CA and MX) or `international`. A country that is not in the table is `international`.
- **Rates.** Each carrier has a price table per zone: tiers of increasing weight limits, and the first
  tier that covers the parcel weight applies. The prices of different zones are independent of each
  other. `orderdesk --rates FILE` replaces the built-in table.
- **Cost of an order.** Shipping is the rate of the order's carrier and zone plus a handling fee of
  1.50 per parcel. An order is split into the fewest parcels of at most 20 kg. Orders whose net amount
  (subtotal minus discount) is 75.00 or more ship for free. Staff can override the price with
  `orderdesk ship ID --carrier C --override AMOUNT`; the override replaces everything above, and an
  override of 0 waives the shipping. Unknown carriers are rejected.
- **Totals.** The order total is net amount plus tax plus shipping. Shipping is not taxed. The total
  shown by `show` and `list`, the daily report revenue and the CSV export all use it; the CSV gets the
  columns `carrier` and `tracking` after `status`, and `shipping` after `discount`.
- **Status.** `new` goes to `packed` or `cancelled`, `packed` to `shipped` or `cancelled`, `shipped`
  to `delivered`. An order needs a tracking number before it ships. Every step is recorded in the
  order's history.
- **Carrier events.** `orderdesk track ID FILE` applies a JSON list of carrier events
  (`tracking`, `status`, `at`) to the order and prints how many were applied and how many were
  rejected. Rejected events are: events for another tracking number, events that would be an illegal
  status step, and events that are malformed (missing fields, an unknown status, a bad timestamp).
- Orders saved before this change have none of the new fields and must still load.
