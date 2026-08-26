# Copyright 2026 Be OnlyOne
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.elit_product_integration.models.sync_processor import (
    ELIT_NEW_PRODUCTS_OFFSET_KEY,
    ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY,
)


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

    def test_trigger_does_not_reset_offset_when_cycle_active(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param(
            ELIT_NEW_PRODUCTS_REQUESTED_DATE_KEY,
            fields.Datetime.to_string(fields.Datetime.now()),
        )
        ICP.set_param(ELIT_NEW_PRODUCTS_OFFSET_KEY, "500")
        self.processor._action_request_elit_new_products_sync()
        self.assertEqual(ICP.get_param(ELIT_NEW_PRODUCTS_OFFSET_KEY), "500")
