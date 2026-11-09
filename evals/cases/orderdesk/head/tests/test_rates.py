import json
import tempfile
import unittest
from pathlib import Path

from orderdesk.rates import RateError, RateTable, Tier, default_table, load_table
from orderdesk.zones import DOMESTIC, INTERNATIONAL, NEAR, REMOTE, delivery_days, zone_for


class ZoneTests(unittest.TestCase):
    def test_countries(self) -> None:
        self.assertEqual(zone_for("US", "73301"), DOMESTIC)
        self.assertEqual(zone_for("ca", "M5V 2T6"), NEAR)
        self.assertEqual(zone_for("MX", "01000"), NEAR)

    def test_unknown_countries_are_international(self) -> None:
        self.assertEqual(zone_for("DE", "10115"), INTERNATIONAL)
        self.assertEqual(zone_for("", ""), INTERNATIONAL)

    def test_hawaii_and_alaska_are_remote(self) -> None:
        self.assertEqual(zone_for("US", "96795"), REMOTE)
        self.assertEqual(zone_for("US", " 99501"), REMOTE)
        self.assertEqual(zone_for("US", "90210"), DOMESTIC)

    def test_delivery_days_grow_with_distance(self) -> None:
        self.assertLess(delivery_days("US", "73301"), delivery_days("US", "96795"))
        self.assertLess(delivery_days("US", "96795"), delivery_days("DE", "10115"))


class RateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = default_table()

    def test_first_tier_that_covers_the_weight_applies(self) -> None:
        self.assertEqual(self.table.quote("post", DOMESTIC, 500), 450)
        self.assertEqual(self.table.quote("post", DOMESTIC, 501), 700)

    def test_international_costs_more_than_domestic(self) -> None:
        domestic = default_table().quote("post", DOMESTIC, 1200)
        abroad = default_table().quote("post", INTERNATIONAL, 1200)
        self.assertGreater(abroad, domestic)

    def test_heavy_and_unknown_parcels_are_rejected(self) -> None:
        with self.assertRaises(RateError):
            self.table.quote("post", DOMESTIC, 20_001)
        with self.assertRaises(RateError):
            self.table.quote("pigeon", DOMESTIC, 100)
        with self.assertRaises(RateError):
            self.table.quote("post", DOMESTIC, -1)

    def test_tier_limits_must_increase(self) -> None:
        with self.assertRaises(RateError):
            RateTable({"x": {DOMESTIC: [Tier(1000, 5), Tier(500, 9)]}})
        with self.assertRaises(RateError):
            RateTable({"x": {"moon": [Tier(1000, 5)]}})

    def test_carriers_and_limits(self) -> None:
        self.assertEqual(self.table.carriers(), ["express", "post"])
        self.assertEqual(self.table.max_weight_g("post"), 20_000)

    def test_load_table_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rates.json"
            path.write_text(json.dumps({"van": {"domestic": [[1000, 300], [5000, 800]]}}), encoding="utf-8")
            table = load_table(path)
        self.assertEqual(table.quote("van", DOMESTIC, 2000), 800)
        self.assertEqual(table.max_weight_g("van"), 5000)


if __name__ == "__main__":
    unittest.main()
