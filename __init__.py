from . import models
from . import wizards
from . import controllers

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Post-init hook: only fix code on legacy crons so behaviour is correct.
    Does not overwrite interval/active/name to preserve user customizations.
    """
    # Ensure legacy incremental cron stays inactive (one-time migration)
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
