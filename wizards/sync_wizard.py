from odoo import api, models, fields


class ElitSyncWizard(models.TransientModel):
    _name = "elit.sync.wizard"
    _description = "Wizard to synchronize ELIT products"

    sync_type = fields.Selection(
        [
            ("full", "Full Synchronization"),
            ("incremental", "Incremental Synchronization"),
        ],
        default="incremental",
        required=True,
    )
    date_from = fields.Datetime(string="From Date (optional)")
    offset_start = fields.Integer(
        string="Initial Offset (to resume)",
        default=1,
        help="If the sync was interrupted, enter the last successful offset + 100 to resume",
    )

    def action_sync(self):
        """Launch the batch sync process."""
        self.env["elit.sync.processor"].sync_products(
            sync_type=self.sync_type,
            date_from=self.date_from,
            offset_start=self.offset_start if self.sync_type == "full" else None,
        )
        message = f"ELIT {self.sync_type} sync launched in background (check logs for progress)."
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
        """Called by daily cron – now launches batch."""
        self.env["elit.sync.processor"].sync_products(sync_type="incremental")
