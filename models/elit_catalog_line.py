# Copyright 2026 Be OnlyOne
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ElitCatalogLine(models.Model):
    _name = "elit.catalog.line"
    _description = "ELIT catalog staging line"
    _order = "id"

    run_id = fields.Many2one(
        "elit.catalog.run",
        required=True,
        ondelete="cascade",
        index=True,
    )
    codigo = fields.Char(required=True, index=True)
    payload = fields.Text(
        required=True,
        help="Raw JSON of one ELIT API product dict.",
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    product_tmpl_id = fields.Many2one(
        "product.template",
        ondelete="set null",
        index=True,
    )
    error_message = fields.Char()

    _sql_constraints = [
        (
            "run_codigo_uniq",
            "unique(run_id, codigo)",
            "An ELIT code can appear only once per catalog run.",
        ),
    ]
