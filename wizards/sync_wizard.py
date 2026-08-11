from odoo import _, api, models, fields


class ElitSyncWizard(models.TransientModel):
    _name = "elit.sync.wizard"
    _description = "Wizard to synchronize ELIT products"

    sync_type = fields.Selection(
        [
            (
                "full",
                "Catálogo completo (activa lotes: productos nuevos + precio/stock)",
            ),
            (
                "incremental",
                "Precio y stock (activa lote de actualización)",
            ),
        ],
        default="incremental",
        required=True,
        help="No ejecuta un loop monolítico: activa los crons de lotes "
        "(una página API por ejecución) para evitar timeouts del worker.",
    )
    date_from = fields.Datetime(
        string="From Date (optional)",
        help="Solo aplica al modo legacy full-loop (acción de servidor).",
    )
    offset_start = fields.Integer(
        string="Initial Offset (to resume)",
        default=1,
        help="Reservado para reanudación manual del modo legacy full-loop.",
    )

    def action_sync(self):
        """Activate trigger+batch crons instead of a monolithic full-loop sync."""
        processor = self.env["elit.sync.processor"]
        if self.sync_type == "full":
            processor._action_request_elit_new_products_sync()
            processor._action_request_elit_price_stock_sync()
            message = _(
                "Sync ELIT catálogo completo solicitado: se activaron los lotes "
                "de productos nuevos y de precio/stock. Seguí el progreso en los logs."
            )
        else:
            processor._action_request_elit_price_stock_sync()
            message = _(
                "Sync ELIT precio/stock solicitado: se activó el cron de lotes. "
                "Seguí el progreso en los logs."
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
        """Activate price/stock batch sync (safe for scheduled use)."""
        self.env["elit.sync.processor"]._action_request_elit_price_stock_sync()
