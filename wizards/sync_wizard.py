from odoo import _, api, models, fields


class ElitSyncWizard(models.TransientModel):
    _name = "elit.sync.wizard"
    _description = "Wizard to synchronize ELIT products"

    sync_type = fields.Selection(
        [
            (
                "catalog",
                "Catálogo (ingest 1 página API + apply interno)",
            ),
        ],
        default="catalog",
        required=True,
        help="Activa el ciclo unificado: una página API por lote de ingest, "
        "luego apply interno. No recorre todo el catálogo en esta petición.",
    )
    date_from = fields.Datetime(
        string="From Date (optional)",
        help="Solo aplica al modo legacy full-loop (acción de servidor).",
    )
    offset_start = fields.Integer(
        string="Initial Offset (to resume)",
        default=0,
        help="Reservado para reanudación manual del modo legacy full-loop.",
    )

    def action_sync(self):
        """Activate the unified catalog ingest+apply crons."""
        self.env["elit.sync.processor"]._action_request_elit_catalog_sync()
        message = _(
            "Sync ELIT catálogo solicitado: ingest (1 página API por lote) "
            "y luego apply interno. Seguí el progreso en Inventario → "
            "Ajustes o en ELIT catalog runs."
        )
        self.env["bus.bus"]._sendone(
            self.env.user.partner_id,
            "simple_notification",
            {"title": "ELIT Sync", "message": message},
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": message, "type": "success"},
        }

    @api.model
    def action_sync_incremental(self):
        """Activate unified catalog cycle (safe for scheduled use)."""
        self.env["elit.sync.processor"]._action_request_elit_catalog_sync()
