import re
from datetime import date

from odoo import api, fields, models
from odoo.tools import frozendict

RETENTION_ADVANCE_TYPES = [
    ('retention', "Retention"),
    ('advance_recovery', "Advance Recovery"),
    ('retention_release', "Retention Release"),
]


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    # Retention journal item of an invoice / credit note. Not a payment term: it must not be part of the amount due.
    display_type = fields.Selection(
        selection_add=[('retention', "Retention")],
        ondelete={'retention': 'set product'},
    )
    retention_advance_type = fields.Selection(
        selection=RETENTION_ADVANCE_TYPES,
        string="Retention/Advance",
        index='btree_not_null',
        readonly=True,
        help="Technical role of the journal item in the retention / advance recovery flow.",
    )
    retention_origin_move_id = fields.Many2one(
        comodel_name='account.move',
        string="Retention Origin Invoice",
        index='btree_not_null',
        readonly=True,
        help="Invoice whose retention is released (release entries) or reversed (credit notes).",
    )
    retention_key = fields.Binary(compute='_compute_retention_key', exportable=False)

    # -------------------------------------------------------------------------
    # COMPUTE METHODS
    # -------------------------------------------------------------------------

    @api.depends('date_maturity', 'display_type')
    def _compute_retention_key(self):
        for line in self:
            if line.display_type == 'retention':
                line.retention_key = frozendict({
                    'move_id': line.move_id.id,
                    'retention_advance_type': 'retention',
                    'date_maturity': fields.Date.to_date(line.date_maturity),
                })
            else:
                line.retention_key = False

    @api.depends('retention_advance_type')
    def _compute_term_key(self):
        super()._compute_term_key()
        for line in self:
            if line.term_key and line.retention_advance_type == 'advance_recovery':
                line.term_key = frozendict({**line.term_key, 'retention_advance_type': 'advance_recovery'})

    def _compute_name(self):
        super()._compute_name()
        for line in self:
            if line.move_id.inalterable_hash:
                continue
            label = line._ra_get_label()
            if label:
                line.name = label
            elif (
                line.display_type == 'payment_term'
                and line.name
                and len(line.move_id.invoice_payment_term_id.line_ids) > 1
                and line.move_id.line_ids.filtered(lambda aml: aml.retention_advance_type == 'advance_recovery')
            ):
                # Odoo numbers installments over all payment term items: don't count the advance recovery.
                installments = (line.move_id.line_ids | line).filtered(
                    lambda aml: aml.display_type == 'payment_term' and not aml.retention_advance_type
                ).sorted(lambda aml: aml.date_maturity or date.max)
                if line in installments:
                    line.name = re.sub(r'#\d+$', f'#{installments._ids.index(line.id) + 1}', line.name)

    def _compute_tax_ids(self):
        retention_lines = self.filtered(lambda line: line.display_type == 'retention')
        super(AccountMoveLine, self - retention_lines)._compute_tax_ids()
        retention_lines.tax_ids = False

    def _compute_account_id(self):
        super()._compute_account_id()
        for line in self.filtered(lambda line: line.retention_advance_type == 'advance_recovery'):
            account = line.move_id._ra_get_advance_account()
            if account:
                line.account_id = account

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _ra_get_label(self):
        self.ensure_one()
        move = self.move_id
        is_refund = move.move_type == 'out_refund'
        if self.display_type == 'retention':
            if is_refund:
                return self.env._("Retention reversed")
            return self.env._("Retention %(percent)s%%", percent=f"{move.retention_percent:g}")
        if self.display_type == 'payment_term' and self.retention_advance_type == 'advance_recovery':
            payment = move.advance_payment_id
            if is_refund:
                return self.env._("Advance recovery reversed %(payment)s", payment=payment.name or '')
            return self.env._("Advance recovery %(payment)s", payment=payment.name or '')
        return False
