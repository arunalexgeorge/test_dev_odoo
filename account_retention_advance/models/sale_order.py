from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    retention_percent = fields.Float(
        string="Retention %",
        digits=(16, 2),
        compute='_compute_retention_percent', store=True, readonly=False, precompute=True,
        help="Retention withheld on each progress invoice of this project.",
    )
    advance_recovery_percent = fields.Float(
        string="Advance Recovery %",
        digits=(16, 2),
        help="Part of the work done recovered from the advance on each progress invoice.",
    )
    expected_handover_date = fields.Date(
        string="Expected Handover Date",
        help="Due date of the retention withheld on the invoices of this project.",
    )
    advance_payment_ids = fields.One2many(
        'account.payment', 'advance_order_id', string="Advance Payments",
        groups='account.group_account_invoice,account.group_account_readonly',
    )
    advance_payment_count = fields.Integer(
        compute='_compute_advance_payment_count',
        groups='account.group_account_invoice,account.group_account_readonly',
    )

    @api.depends('company_id')
    def _compute_retention_percent(self):
        for order in self:
            order.retention_percent = order.company_id.retention_percent

    @api.depends('advance_payment_ids')
    def _compute_advance_payment_count(self):
        for order in self:
            order.advance_payment_count = len(order.advance_payment_ids)

    def _ra_get_default_advance(self):
        """Oldest advance of the project (or of the customer, not linked to a project) with a balance left."""
        self.ensure_one()
        domain = [
            ('partner_type', '=', 'customer'),
            ('payment_type', '=', 'inbound'),
            ('state', 'in', ('in_process', 'paid')),
            ('move_id', '!=', False),
            ('company_id', '=', self.company_id.id),
            ('advance_residual', '>', 0.0),
        ]
        # sudo: salespeople create invoices from their orders without access to payments.
        Payment = self.env['account.payment'].sudo()
        return (
            Payment.search(domain + [('advance_order_id', '=', self.id)], order='date, id', limit=1)
            or Payment.search(domain + [
                ('is_advance_payment', '=', True),
                ('advance_order_id', '=', False),
                ('partner_id', 'child_of', self.partner_id.commercial_partner_id.id),
            ], order='date, id', limit=1)
        )

    def _prepare_invoice(self):
        vals = super()._prepare_invoice()
        vals.update({
            'retention_percent': self.retention_percent,
            'retention_due_date': self.expected_handover_date,
        })
        if self.advance_recovery_percent:
            advance = self._ra_get_default_advance()
            if advance:
                vals.update({
                    'advance_payment_id': advance.id,
                    'advance_recovery_method': 'percent',
                    'advance_recovery_percent': self.advance_recovery_percent,
                })
        return vals

    def action_view_advance_payments(self):
        self.ensure_one()
        return self.advance_payment_ids._get_records_action(
            name=self.env._("Advance Payments"),
            context={
                'default_payment_type': 'inbound',
                'default_partner_type': 'customer',
                'default_partner_id': self.partner_id.commercial_partner_id.id,
                'default_is_advance_payment': True,
                'default_advance_order_id': self.id,
            },
        )
