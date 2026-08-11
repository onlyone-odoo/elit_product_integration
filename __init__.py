from . import models
from . import wizards
from . import controllers

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Post-init hook: migrate elit_product_code from supplierinfo."""
    env.cr.execute("""
        UPDATE product_template pt
        SET elit_product_code = COALESCE(
            (SELECT si.product_code
             FROM product_supplierinfo si
             JOIN res_partner rp ON rp.id = si.partner_id
             WHERE si.product_tmpl_id = pt.id
               AND rp.name ILIKE '%%ELIT%%'
               AND si.product_code IS NOT NULL
               AND si.product_code != ''
             LIMIT 1),
            pt.default_code
        )
        WHERE pt.is_elit_product = True
          AND (pt.elit_product_code IS NULL OR pt.elit_product_code = '')
    """)
    migrated = env.cr.rowcount
    if migrated:
        _logger.info("Migrated elit_product_code for %d products", migrated)

    env.cr.execute("""
        UPDATE product_template
        SET allow_out_of_stock_order = TRUE
        WHERE is_elit_product = TRUE
          AND (allow_out_of_stock_order IS NOT TRUE)
    """)
    oos = env.cr.rowcount
    if oos:
        _logger.info(
            "Set allow_out_of_stock_order for %d ELIT product templates", oos
        )
