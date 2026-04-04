from . import models
from . import wizards
from . import controllers

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Post-init hook: fix legacy crons and migrate elit_product_code."""
    cron_incremental = env.ref(
        "elit_product_integration.cron_elit_incremental",
        raise_if_not_found=False,
    )
    if cron_incremental and cron_incremental.active:
        cron_incremental.write({
            "active": False,
            "name": "ELIT: Sincronización Incremental (LEGACY - Desactivado)",
        })
        _logger.info("Deactivated legacy cron_elit_incremental")

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
