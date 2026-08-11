# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Share existing ELIT supplier pricelists across companies.

    Supplierinfo created before multi-company support got the cron user's
    company assigned by default. Clear company_id so every company can see
    them (new syncs set it explicitly from Settings).
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    partner_id_raw = (
        env["ir.config_parameter"].get_param("elit.partner_id", "") or ""
    ).strip()
    try:
        partner_id = int(partner_id_raw) if partner_id_raw else 0
    except (TypeError, ValueError):
        partner_id = 0
    if not partner_id:
        # Same fallback the sync uses when no partner is configured
        partner = env["res.partner"].search([("name", "ilike", "ELIT")], limit=1)
        partner_id = partner.id if partner else 0
    if not partner_id:
        _logger.info(
            "elit_product_integration migration: no ELIT partner found, "
            "nothing to update."
        )
        return
    cr.execute(
        """
        UPDATE product_supplierinfo
        SET company_id = NULL
        WHERE partner_id = %s AND company_id IS NOT NULL
        """,
        (partner_id,),
    )
    _logger.info(
        "elit_product_integration migration: cleared company_id on %d "
        "supplierinfo rows (partner_id=%s).",
        cr.rowcount,
        partner_id,
    )
