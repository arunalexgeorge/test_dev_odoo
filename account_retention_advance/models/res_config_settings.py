from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    retention_account_id = fields.Many2one(related='company_id.retention_account_id', readonly=False)
    retention_percent = fields.Float(related='company_id.retention_percent', readonly=False)
    retention_journal_id = fields.Many2one(related='company_id.retention_journal_id', readonly=False)
