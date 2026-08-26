# Copyright 2026 Be OnlyOne
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestElitSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.processor = self.env["elit.sync.processor"]

    def test_code_matches_prefix_and_hyphen(self):
        self.assertTrue(
            self.processor._elit_code_matches("LOGMOU910005795", "910-005795")
        )
        self.assertTrue(self.processor._elit_code_matches("910-005794", "910005794"))
        self.assertFalse(
            self.processor._elit_code_matches("LOGMOUG203", "910-005794")
        )
        self.assertFalse(
            self.processor._elit_code_matches("9100057950", "910-005795")
        )

    def test_map_templates_by_alfa_and_producto(self):
        tmpl = self.env["product.template"].create(
            {
                "name": "Mouse C/Cable Logitech G203 Blanco",
                "detailed_type": "product",
                "default_code": "LOGMOUG203",
                "elit_product_code": "LOGMOUG203",
                "is_elit_product": True,
            }
        )
        mapped = self.processor._elit_map_templates_by_api_codes(
            ["LOGMOUG203", "910-005794"]
        )
        self.assertEqual(mapped.get("LOGMOUG203"), tmpl)

    def test_pagination_ignores_total_equal_to_page_size(self):
        data = {"paginador": {"total": 100, "offset": 0}}
        self.assertFalse(
            self.processor._elit_pagination_done(data, 0, 100, 100)
        )
        data_big = {"paginador": {"total": 15000, "offset": 0}}
        self.assertFalse(
            self.processor._elit_pagination_done(data_big, 0, 100, 100)
        )
        self.assertTrue(
            self.processor._elit_pagination_done(data_big, 14900, 100, 100)
        )
        self.assertTrue(self.processor._elit_pagination_done({}, 0, 100, 0))
        self.assertTrue(self.processor._elit_pagination_done({}, 0, 100, 40))

    def test_legacy_trigger_alias_does_not_reset_catalog_run(self):
        """Old new-products trigger now resumes the unified catalog cycle."""
        Run = self.env["elit.catalog.run"]
        run = Run.create({"state": "ingest", "offset": 500})
        self.processor._action_request_elit_new_products_sync()
        self.assertEqual(run.offset, 500)
        self.assertEqual(run.state, "ingest")
        self.assertEqual(Run.search_count([("state", "=", "ingest")]), 1)
