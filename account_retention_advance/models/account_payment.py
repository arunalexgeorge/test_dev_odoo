from odoo import api, fields, models


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    is_advance_payment = fields.Boolean(
        string="Advance Payment",
        tracking=True,
        help="Advance received from the customer before the work starts. It is recovered on the progress invoices "
             "and is not proposed as an outstanding credit on other invoices.",
    )
    advance_order_id = fields.Many2one(
        comodel_name='sale.order',
        string="Project",
        tracking=True,
        index='btree_not_null',
        check_company=True,
        domain="[('partner_id', 'child_of', partner_id)]",
        help="Sales order (project) this advance belongs to.",
    )
    advance_residual = fields.Monetary(
        string="Advance Balance",
        compute='_compute_advance_residual', store=True,
        currency_field='currency_id',
        help="Part of the payment not yet matched with invoices.",
    )
    advance_invoice_ids = fields.One2many('account.move', 'advance_payment_id', string="Recovering Invoices")
    advance_invoice_count = fields.Integer(compute='_compute_advance_invoice_count')

    @api.depends('move_id.line_ids.amount_residual', 'move_id.line_ids.amount_residual_currency', 'move_id.state')
    def _compute_advance_residual(self):
        for payment in self:
            lines = payment._ra_get_advance_lines()
            if payment.currency_id == payment.company_currency_id:
                payment.advance_residual = -sum(lines.mapped('amount_residual'))
            else:
                payment.advance_residual = -sum(lines.mapped('amount_residual_currency'))

    @api.depends('advance_invoice_ids')
    def _compute_advance_invoice_count(self):
        for payment in self:
            payment.advance_invoice_count = len(payment.advance_invoice_ids.filtered(lambda move: move.move_type == 'out_invoice'))

    def _ra_get_advance_lines(self):
        """Posted receivable credit items of the payments: their residual is the advance still available."""
        return self.move_id.line_ids.filtered(
            lambda line: line.parent_state == 'posted'
            and line.account_id.account_type == 'asset_receivable'
            and line.balance < 0.0
        )

    def action_post(self):
        # Advances must be recoverable right away: force a journal entry (receivable credit) even when the
        # payment method has no outstanding receipts account.
        for payment in self.filtered(
            lambda pay: pay.is_advance_payment and pay.payment_type == 'inbound'
            and not pay.move_id and not pay.outstanding_account_id
        ):
            payment.outstanding_account_id = payment._get_outstanding_account(payment.payment_type)
        return super().action_post()

    def action_view_advance_invoices(self):
        self.ensure_one()
        return self.advance_invoice_ids._get_records_action(name=self.env._("Recovering Invoices"))
