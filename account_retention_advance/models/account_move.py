from datetime import date

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import formatLang, frozendict


class AccountMove(models.Model):
    _inherit = 'account.move'

    project_sale_order_id = fields.Many2one(
        comodel_name='sale.order',
        string="Project",
        compute='_compute_project_sale_order_id',
        store=True,
        recursive=True,
        index='btree_not_null',
        help="Sales order the invoice was created from. Invoices without sales order are grouped per customer.",
    )

    # ---- Retention ----
    retention_percent = fields.Float(
        string="Retention %",
        digits=(16, 2),
        compute='_compute_retention_percent', store=True, readonly=False, precompute=True,
        copy=True,
        tracking=True,
        help="Percentage of the work done (untaxed amount) withheld by the customer until handover.",
    )
    retention_due_date = fields.Date(
        string="Retention Due Date",
        copy=True,
        tracking=True,
        help="Expected handover date: due date of the retention journal item.",
    )
    retention_amount = fields.Monetary(
        string="Retention",
        compute='_compute_retention_advance_amounts', store=True, recursive=True,
        currency_field='currency_id',
    )
    retention_residual = fields.Monetary(
        string="Retention Held",
        compute='_compute_retention_residual', store=True,
        currency_field='currency_id',
        help="Retention of this invoice not yet released nor reversed by a credit note.",
    )

    # ---- Advance recovery ----
    advance_payment_id = fields.Many2one(
        comodel_name='account.payment',
        string="Advance Payment",
        copy=True,
        tracking=True,
        index='btree_not_null',
        check_company=True,
        domain="[('partner_type', '=', 'customer'), ('payment_type', '=', 'inbound'),"
               " ('state', 'in', ('in_process', 'paid')), ('move_id', '!=', False),"
               " ('partner_id', 'child_of', commercial_partner_id), ('company_id', '=', company_id),"
               " ('advance_residual', '>', 0)]",
        help="Customer advance payment from which part of this invoice is recovered.",
    )
    advance_recovery_method = fields.Selection(
        selection=[('percent', "% of Work Done"), ('fixed', "Fixed Amount")],
        string="Recovery Method",
        default='percent',
        copy=True,
    )
    advance_recovery_percent = fields.Float(string="Recovery %", digits=(16, 2), copy=True)
    advance_recovery_fixed_amount = fields.Monetary(
        string="Recovery Amount",
        currency_field='currency_id',
        copy=True,
    )
    advance_recovery_amount = fields.Monetary(
        string="Advance Recovery",
        compute='_compute_retention_advance_amounts', store=True, recursive=True,
        currency_field='currency_id',
    )
    advance_available_amount = fields.Monetary(
        string="Advance Available",
        compute='_compute_advance_available_amount',
        compute_sudo=True,
        currency_field='currency_id',
        help="Part of the selected advance that this invoice can still recover.",
    )
    amount_to_pay_now = fields.Monetary(
        string="Amount to Pay Now",
        compute='_compute_amount_to_pay_now',
        currency_field='currency_id',
        help="Invoice total less retention and advance recovery.",
    )

    # ---- Technical: dynamic retention journal item (same mechanism as needed_terms) ----
    needed_retention_lines = fields.Binary(compute='_compute_needed_retention_lines', exportable=False)
    needed_retention_dirty = fields.Boolean(compute='_compute_needed_retention_lines')

    # -------------------------------------------------------------------------
    # CONSTRAINTS
    # -------------------------------------------------------------------------

    @api.constrains('retention_percent', 'advance_recovery_percent', 'advance_recovery_fixed_amount')
    def _check_retention_advance_values(self):
        for move in self:
            if not 0.0 <= move.retention_percent <= 100.0 or not 0.0 <= move.advance_recovery_percent <= 100.0:
                raise ValidationError(self.env._("Retention and advance recovery percentages must be between 0 and 100."))
            if move.advance_recovery_fixed_amount < 0.0:
                raise ValidationError(self.env._("The advance recovery amount cannot be negative."))
            if (
                move.state == 'draft'
                and move.move_type == 'out_invoice'
                and move.retention_percent
                and not move.company_id._ra_get_retention_account()
            ):
                raise ValidationError(self.env._(
                    "Set the Retention Receivable account in the Accounting settings of %s before using retention.",
                    move.company_id.display_name,
                ))

    # -------------------------------------------------------------------------
    # COMPUTE METHODS
    # -------------------------------------------------------------------------

    @api.depends('move_type', 'invoice_line_ids.sale_line_ids', 'reversed_entry_id.project_sale_order_id')
    def _compute_project_sale_order_id(self):
        for move in self:
            if not move.is_sale_document():
                move.project_sale_order_id = False
                continue
            orders = move.invoice_line_ids.sale_line_ids.order_id
            move.project_sale_order_id = orders.sorted('id')[:1] or move.reversed_entry_id.project_sale_order_id

    @api.depends('move_type', 'company_id')
    def _compute_retention_percent(self):
        for move in self:
            if move.move_type == 'out_invoice':
                move.retention_percent = move.company_id.retention_percent
            elif move.move_type == 'out_refund':
                # Credit notes keep the percentage copied from the reversed invoice.
                move.retention_percent = move.retention_percent
            else:
                move.retention_percent = 0.0

    @api.depends(
        'move_type', 'currency_id', 'retention_percent',
        'invoice_line_ids.price_subtotal', 'invoice_line_ids.display_type', 'invoice_line_ids.is_downpayment',
        'advance_payment_id', 'advance_recovery_method', 'advance_recovery_percent', 'advance_recovery_fixed_amount',
        'reversed_entry_id.retention_amount', 'reversed_entry_id.advance_recovery_amount',
    )
    def _compute_retention_advance_amounts(self):
        for move in self:
            retention = recovery = 0.0
            currency = move.currency_id
            if not currency:
                move.retention_amount = move.advance_recovery_amount = 0.0
                continue
            if move.move_type == 'out_invoice':
                work_done = move._ra_get_work_done()
                retention = currency.round(work_done * move.retention_percent / 100.0)
                if move.advance_payment_id:
                    if move.advance_recovery_method == 'fixed':
                        recovery = currency.round(move.advance_recovery_fixed_amount)
                    else:
                        recovery = currency.round(work_done * move.advance_recovery_percent / 100.0)
            elif move.move_type == 'out_refund' and move.reversed_entry_id.move_type == 'out_invoice':
                retention, recovery = move._ra_get_refund_amounts()
            move.retention_amount = retention
            move.advance_recovery_amount = recovery

    @api.depends('state', 'line_ids.amount_residual_currency', 'line_ids.display_type')
    def _compute_retention_residual(self):
        for move in self:
            lines = move._ra_get_retention_lines()
            if move.move_type == 'out_invoice' and move.state == 'posted':
                move.retention_residual = sum(lines.mapped('amount_residual_currency'))
            else:
                move.retention_residual = 0.0

    @api.depends('advance_payment_id', 'currency_id', 'invoice_date', 'date')
    def _compute_advance_available_amount(self):
        for move in self:
            move.advance_available_amount = move._ra_get_advance_available() if move.advance_payment_id else 0.0

    @api.depends('tax_totals', 'amount_total', 'retention_amount', 'advance_recovery_amount')
    def _compute_amount_to_pay_now(self):
        for move in self:
            total = move.tax_totals['total_amount_currency'] if move.tax_totals else move.amount_total
            move.amount_to_pay_now = total - move.retention_amount - move.advance_recovery_amount

    @api.depends(
        'retention_amount', 'retention_due_date', 'invoice_date_due', 'invoice_date',
        'currency_id', 'invoice_currency_rate', 'company_id', 'move_type', 'reversed_entry_id',
    )
    def _compute_needed_retention_lines(self):
        for move in self:
            move.needed_retention_lines = {}
            move.needed_retention_dirty = True
            account = move._ra_get_expected_retention_account()
            if not move.is_sale_document() or not account or not move.currency_id or move.currency_id.is_zero(move.retention_amount):
                continue
            amount_currency, balance = move._ra_get_signed_amounts(move.retention_amount)
            key = frozendict({
                'move_id': move.id,
                'retention_advance_type': 'retention',
                'date_maturity': fields.Date.to_date(move._ra_get_retention_maturity()),
            })
            move.needed_retention_lines = {key: {
                'balance': balance,
                'amount_currency': amount_currency,
                'account_id': account.id,
                'retention_origin_move_id': move.reversed_entry_id.id if move.move_type == 'out_refund' else False,
            }}

    @api.depends('retention_amount', 'advance_recovery_amount', 'advance_payment_id', 'invoice_currency_rate')
    def _compute_needed_terms(self):
        super()._compute_needed_terms()
        for move in self.with_context(bin_size=False):
            if move.is_sale_document() and move.needed_terms:
                move.needed_terms = move._ra_split_needed_terms(move.needed_terms)

    def _get_sync_stack(self, container):
        stack, update_containers = super()._get_sync_stack(container)
        _tax_container, invoice_container, _misc_container = update_containers()
        stack.append((15, self._sync_dynamic_line(
            existing_key_fname='retention_key',
            needed_vals_fname='needed_retention_lines',
            needed_dirty_fname='needed_retention_dirty',
            line_type='retention',
            container=invoice_container,
        )))
        return stack, update_containers

    def _compute_payments_widget_to_reconcile_info(self):
        # Advance payments are consumed through advance recovery only: don't propose them as outstanding credits.
        super()._compute_payments_widget_to_reconcile_info()
        for move in self:
            widget = move.invoice_outstanding_credits_debits_widget
            if not widget or not move.is_sale_document():
                continue
            lines = self.env['account.move.line'].sudo().browse([vals['id'] for vals in widget['content']])
            advance_line_ids = set(lines.filtered(lambda line: line.move_id.origin_payment_id.is_advance_payment).ids)
            if advance_line_ids:
                content = [vals for vals in widget['content'] if vals['id'] not in advance_line_ids]
                move.invoice_outstanding_credits_debits_widget = {**widget, 'content': content} if content else False

    @api.model
    def _get_default_read_fields(self):
        return [fname for fname in super()._get_default_read_fields() if fname != 'needed_retention_lines']

    @api.onchange('partner_id')
    def _onchange_partner_id_advance_payment(self):
        if self.advance_payment_id and self.advance_payment_id.partner_id.commercial_partner_id != self.commercial_partner_id:
            self.advance_payment_id = False

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _ra_get_retention_lines(self):
        return self.line_ids.filtered(lambda line: line.display_type == 'retention')

    def _ra_get_recovery_lines(self):
        return self.line_ids.filtered(
            lambda line: line.display_type == 'payment_term' and line.retention_advance_type == 'advance_recovery'
        )

    def _ra_get_work_done(self):
        """Untaxed value of the work invoiced (live in the form, unlike amount_untaxed).

        Sales down payment lines are not work done: no retention on a down payment invoice, and the deduction of a
        down payment on a later invoice does not reduce the retention base.
        """
        self.ensure_one()
        return sum(self.invoice_line_ids.filtered(
            lambda line: line.display_type == 'product' and not line.is_downpayment
        ).mapped('price_subtotal'))

    def _ra_get_signed_amounts(self, amount):
        """Return (amount_currency, balance) of a receivable-side item of this document for ``amount``."""
        self.ensure_one()
        amount_currency = -self.direction_sign * self.currency_id.round(amount)
        balance = self.company_currency_id.round(amount_currency / (self.invoice_currency_rate or 1.0))
        return amount_currency, balance

    def _ra_get_retention_maturity(self):
        self.ensure_one()
        return self.retention_due_date or self.invoice_date_due or self.invoice_date or fields.Date.context_today(self)

    def _ra_get_expected_retention_account(self):
        """Credit notes must hit the account of the reversed invoice so both items can be matched."""
        self.ensure_one()
        if self.move_type == 'out_refund':
            origin_line = self.reversed_entry_id._ra_get_retention_lines()[:1]
            if origin_line:
                return origin_line.account_id
        return self.company_id._ra_get_retention_account()

    def _ra_get_advance_account(self):
        self.ensure_one()
        return self.advance_payment_id.sudo()._ra_get_advance_lines()[:1].account_id.with_env(self.env)

    def _ra_split_needed_terms(self, needed_terms):
        """Take retention and advance recovery out of the payment terms.

        The amount to pay now is spread over the original installments; the advance recovery becomes an extra
        receivable item (matched with the advance at posting) and the retention its own journal item.
        """
        self.ensure_one()
        currency, company_currency = self.currency_id, self.company_currency_id
        ret_cur = ret_bal = rec_cur = rec_bal = 0.0
        if not currency:
            return needed_terms
        if self._ra_get_expected_retention_account() and not currency.is_zero(self.retention_amount):
            ret_cur, ret_bal = self._ra_get_signed_amounts(self.retention_amount)
        if self.advance_payment_id and not currency.is_zero(self.advance_recovery_amount):
            rec_cur, rec_bal = self._ra_get_signed_amounts(self.advance_recovery_amount)

        # Always reset the technical fields: the sync may reuse a former recovery item for a normal installment.
        result = {
            key: {**values, 'retention_advance_type': False, 'retention_origin_move_id': False}
            for key, values in needed_terms.items()
        }
        if not ret_cur and not rec_cur:
            return result

        total_cur = sum(values['amount_currency'] for values in result.values())
        total_bal = sum(values['balance'] for values in result.values())
        pay_cur, pay_bal = total_cur - ret_cur - rec_cur, total_bal - ret_bal - rec_bal
        factor_cur = pay_cur / total_cur if total_cur else 0.0
        factor_bal = pay_bal / total_bal if total_bal else 0.0
        keys = sorted(result, key=lambda key: key.get('date_maturity') or date.min)
        remaining_cur, remaining_bal = pay_cur, pay_bal
        for index, key in enumerate(keys):
            values = result[key]
            if index == len(keys) - 1:
                values['amount_currency'], values['balance'] = remaining_cur, remaining_bal
            else:
                values['amount_currency'] = currency.round(values['amount_currency'] * factor_cur)
                values['balance'] = company_currency.round(values['balance'] * factor_bal)
                remaining_cur -= values['amount_currency']
                remaining_bal -= values['balance']
            if values.get('discount_amount_currency'):
                values['discount_amount_currency'] = currency.round(values['discount_amount_currency'] * factor_cur)
            if values.get('discount_balance'):
                values['discount_balance'] = company_currency.round(values['discount_balance'] * factor_bal)

        if rec_cur:
            key = frozendict({
                'move_id': self.id,
                'date_maturity': fields.Date.to_date(self.invoice_date or self.date or fields.Date.context_today(self)),
                'discount_date': False,
                'retention_advance_type': 'advance_recovery',
            })
            values = {
                'balance': rec_bal,
                'amount_currency': rec_cur,
                'discount_balance': 0.0,
                'discount_amount_currency': 0.0,
                'retention_origin_move_id': self.reversed_entry_id.id if self.move_type == 'out_refund' else False,
            }
            if account := self._ra_get_advance_account():
                values['account_id'] = account.id
            result[key] = values
        return result

    def _ra_get_consumed_amounts(self, exclude_moves=None):
        """Retention released or credited and advance recovery given back so far for this invoice.

        Based on posted journal items (not on reconciliation), in the invoice currency.
        """
        self.ensure_one()
        result = {'retention': 0.0, 'recovery': 0.0}
        if not self._origin.id:
            return result
        domain = [('retention_origin_move_id', '=', self._origin.id), ('parent_state', '=', 'posted')]
        if exclude_moves:
            domain.append(('move_id', 'not in', exclude_moves.ids))
        for line in self.env['account.move.line'].search(domain):
            if line.retention_advance_type == 'advance_recovery':
                result['recovery'] -= line.amount_currency
            elif line.retention_advance_type == 'retention' or (
                line.retention_advance_type == 'retention_release'
                and line.account_id.account_type != 'asset_receivable'
            ):
                result['retention'] -= line.amount_currency
        return result

    def _ra_get_refund_amounts(self):
        """Retention and advance recovery reversed by a credit note: proportional to the work credited,
        capped by what is still held / recovered on the reversed invoice."""
        self.ensure_one()
        origin = self.reversed_entry_id
        currency = self.currency_id
        origin_work = origin._ra_get_work_done()
        if currency != origin.currency_id or currency.is_zero(origin_work):
            return 0.0, 0.0
        ratio = min(self._ra_get_work_done() / origin_work, 1.0)
        consumed = origin._ra_get_consumed_amounts(exclude_moves=self._origin)
        retention = min(
            currency.round(origin.retention_amount * ratio),
            max(origin.retention_amount - consumed['retention'], 0.0),
        )
        recovery = min(
            currency.round(origin.advance_recovery_amount * ratio),
            max(origin.advance_recovery_amount - consumed['recovery'], 0.0),
        )
        return retention, recovery

    def _ra_get_advance_available(self):
        """Part of the selected advance (in invoice currency) that this invoice can still recover."""
        self.ensure_one()
        pay_lines = self.advance_payment_id._ra_get_advance_lines()
        if not pay_lines:
            return 0.0
        partials = self._origin._ra_get_recovery_lines().matched_credit_ids.filtered(
            lambda partial: partial.credit_move_id in pay_lines
        )
        if pay_lines.currency_id == self.currency_id:
            return -sum(pay_lines.mapped('amount_residual_currency')) + sum(partials.mapped('credit_amount_currency'))
        available = -sum(pay_lines.mapped('amount_residual')) + sum(partials.mapped('amount'))
        return self.company_currency_id._convert(
            available, self.currency_id, self.company_id,
            self.invoice_date or self.date or fields.Date.context_today(self),
        )

    def _ra_has_deductions(self):
        self.ensure_one()
        currency = self.currency_id
        return bool(currency) and (not currency.is_zero(self.retention_amount) or not currency.is_zero(self.advance_recovery_amount))

    # -------------------------------------------------------------------------
    # POSTING / VALIDATION
    # -------------------------------------------------------------------------

    def _ra_format(self, amount, currency=None):
        return formatLang(self.env, amount, currency_obj=currency or self.currency_id)

    def _ra_check_before_post(self):
        for move in self:
            currency = move.currency_id
            retention_lines, recovery_lines = move._ra_get_retention_lines(), move._ra_get_recovery_lines()
            if not move._ra_has_deductions() and not retention_lines and not recovery_lines:
                continue

            has_retention = not currency.is_zero(move.retention_amount)
            has_recovery = not currency.is_zero(move.advance_recovery_amount)
            account = move._ra_get_expected_retention_account()
            if has_retention and not account:
                raise UserError(self.env._(
                    "Set the Retention Receivable account in the Accounting settings before posting %s.", move.display_name))
            if has_retention and move.move_type == 'out_invoice' and not move.retention_due_date:
                raise UserError(self.env._(
                    "Enter the Retention Due Date (expected handover date) on %s.", move.display_name))

            # The journal items are generated when the invoice is saved: make sure they still match the settings.
            expected_retention = move._ra_get_signed_amounts(move.retention_amount)[0] if has_retention else 0.0
            expected_recovery = move._ra_get_signed_amounts(move.advance_recovery_amount)[0] if has_recovery else 0.0
            if (
                currency.compare_amounts(sum(retention_lines.mapped('amount_currency')), expected_retention)
                or currency.compare_amounts(sum(recovery_lines.mapped('amount_currency')), expected_recovery)
                or (retention_lines and retention_lines.account_id != account)
            ):
                raise UserError(self.env._(
                    "The retention / advance recovery journal items of %s are out of date (the settings changed after "
                    "it was saved). Change the retention or the advance recovery on the document and save it again.",
                    move.display_name,
                ))

            if move.move_type == 'out_invoice':
                if currency.compare_amounts(move.amount_to_pay_now, 0.0) < 0:
                    raise UserError(self.env._(
                        "Retention and advance recovery (%(deductions)s) are higher than the total of %(move)s (%(total)s).",
                        deductions=move._ra_format(move.retention_amount + move.advance_recovery_amount),
                        move=move.display_name,
                        total=move._ra_format(move.amount_total),
                    ))
                if has_recovery:
                    move._ra_check_advance()
                consumed = move._ra_get_consumed_amounts()
                if currency.compare_amounts(move.retention_amount, consumed['retention']) < 0:
                    raise UserError(self.env._(
                        "The retention of %(move)s cannot be lower than the part already released or credited (%(amount)s).",
                        move=move.display_name, amount=move._ra_format(consumed['retention']),
                    ))
                if currency.compare_amounts(move.advance_recovery_amount, consumed['recovery']) < 0:
                    raise UserError(self.env._(
                        "The advance recovery of %(move)s cannot be lower than the part already reversed by credit notes (%(amount)s).",
                        move=move.display_name, amount=move._ra_format(consumed['recovery']),
                    ))
            elif move.move_type == 'out_refund' and move.reversed_entry_id:
                origin = move.reversed_entry_id
                consumed = origin._ra_get_consumed_amounts(exclude_moves=move)
                available_retention = origin.retention_amount - consumed['retention']
                available_recovery = origin.advance_recovery_amount - consumed['recovery']
                if currency.compare_amounts(move.retention_amount, available_retention) > 0:
                    raise UserError(self.env._(
                        "%(move)s reverses %(amount)s of retention but only %(available)s is still held on %(origin)s.",
                        move=move.display_name, amount=move._ra_format(move.retention_amount),
                        available=move._ra_format(available_retention), origin=origin.display_name,
                    ))
                if currency.compare_amounts(move.advance_recovery_amount, available_recovery) > 0:
                    raise UserError(self.env._(
                        "%(move)s gives back %(amount)s of advance but %(origin)s only recovered %(available)s.",
                        move=move.display_name, amount=move._ra_format(move.advance_recovery_amount),
                        available=move._ra_format(available_recovery), origin=origin.display_name,
                    ))

    def _ra_check_advance(self):
        self.ensure_one()
        payment = self.advance_payment_id
        if not payment:
            raise UserError(self.env._("Select the advance payment to recover from on %s.", self.display_name))
        if (
            payment.company_id != self.company_id
            or payment.partner_id.commercial_partner_id != self.commercial_partner_id
            or payment.payment_type != 'inbound'
            or payment.partner_type != 'customer'
        ):
            raise UserError(self.env._(
                "The advance %(payment)s must be a payment received from %(partner)s in %(company)s.",
                payment=payment.display_name, partner=self.commercial_partner_id.display_name,
                company=self.company_id.display_name,
            ))
        pay_lines = payment._ra_get_advance_lines()
        if not pay_lines:
            raise UserError(self.env._(
                "The advance %s has no posted journal entry on a receivable account, so it cannot be recovered. "
                "Make sure the payment is confirmed and its payment method has an outstanding receipts account.",
                payment.display_name,
            ))
        recovery_lines = self._ra_get_recovery_lines()
        if recovery_lines.account_id != pay_lines.account_id:
            raise UserError(self.env._(
                "The advance %(payment)s is booked on %(account)s, which is not the receivable account of %(move)s.",
                payment=payment.display_name, account=pay_lines.account_id[:1].display_name, move=self.display_name,
            ))
        partials = recovery_lines.matched_credit_ids.filtered(lambda partial: partial.credit_move_id in pay_lines)
        if pay_lines.currency_id == self.currency_id:
            currency = self.currency_id
            needed = self.advance_recovery_amount
            available = -sum(pay_lines.mapped('amount_residual_currency')) + sum(partials.mapped('credit_amount_currency'))
        else:
            currency = self.company_currency_id
            needed = sum(recovery_lines.mapped('balance'))
            available = -sum(pay_lines.mapped('amount_residual')) + sum(partials.mapped('amount'))
        if currency.compare_amounts(needed, available) > 0:
            raise UserError(self.env._(
                "You cannot recover %(amount)s from %(payment)s on %(move)s: only %(available)s of this advance is left.",
                amount=self._ra_format(needed, currency), payment=payment.display_name,
                move=self.display_name, available=self._ra_format(max(available, 0.0), currency),
            ))

    def _post(self, soft=True):
        self.filtered(lambda move: move.is_sale_document())._ra_check_before_post()
        posted = super()._post(soft=soft)
        posted._ra_reconcile_after_post()
        return posted

    def _reconcile_reversed_moves(self, reverse_moves, move_reverse_cancel):
        # Match retention / advance recovery of credit notes first, otherwise Odoo would match the
        # recovery item of the credit note with the open receivable of the invoice.
        refunds = reverse_moves.filtered(lambda move: move.move_type == 'out_refund')
        refunds._ra_reconcile_refund()
        res = super()._reconcile_reversed_moves(reverse_moves, move_reverse_cancel)
        refunds._ra_settle_released_retention()
        return res

    def _ra_reconcile_after_post(self):
        for move in self:
            if move.move_type == 'out_invoice':
                move._ra_reconcile_retention()
                move._ra_reconcile_advance()
                move._ra_link_advance_to_project()
            elif move.move_type == 'out_refund':
                move._ra_reconcile_refund()
        release_lines = self.filtered(lambda move: move.move_type == 'entry').line_ids.filtered(
            lambda line: line.retention_advance_type == 'retention_release'
        )
        release_lines.retention_origin_move_id._ra_reconcile_retention()

    def _ra_reconcile_retention(self):
        """Match the retention item of posted invoices with released / credited retention."""
        for invoice in self:
            retention_line = invoice._ra_get_retention_lines()
            if invoice.state != 'posted' or invoice.move_type != 'out_invoice' or not retention_line or retention_line.reconciled:
                continue
            counterparts = self.env['account.move.line'].search([
                ('retention_origin_move_id', '=', invoice.id),
                ('parent_state', '=', 'posted'),
                ('account_id', '=', retention_line.account_id.id),
                ('reconciled', '=', False),
                ('amount_currency', '<', 0.0),
            ])
            if counterparts:
                (retention_line | counterparts).reconcile()

    def _ra_reconcile_advance(self):
        """Match the advance recovery item of posted invoices with the advance payment.

        Credit notes giving part of the recovery back take over (part of) the match with the payment, so the
        given back amount becomes available again on the advance.
        """
        AccountMoveLine = self.env['account.move.line']
        for invoice in self:
            if invoice.state != 'posted' or invoice.move_type != 'out_invoice':
                continue
            recovery_line = invoice._ra_get_recovery_lines()
            payment = invoice.advance_payment_id
            pay_lines = payment._ra_get_advance_lines() if recovery_line else AccountMoveLine
            # The advance was changed or removed while the invoice was in draft: drop the match with the former
            # advance. The sync may also have turned the former recovery item into a normal installment.
            term_lines = invoice.line_ids.filtered(lambda line: line.display_type == 'payment_term')
            self._ra_unlink_partials(term_lines.matched_credit_ids.filtered(
                lambda partial: partial.credit_move_id.move_id.origin_payment_id.is_advance_payment
                and (partial.credit_move_id not in pay_lines or partial.debit_move_id != recovery_line)
            ))
            if not recovery_line or not payment:
                continue
            refund_lines = AccountMoveLine.search([
                ('retention_origin_move_id', '=', invoice.id),
                ('retention_advance_type', '=', 'advance_recovery'),
                ('parent_state', '=', 'posted'),
            ])

            open_refund_lines = refund_lines.filtered(lambda line: not line.reconciled)
            if open_refund_lines:
                self._ra_unlink_partials(recovery_line.matched_credit_ids.filtered(lambda partial: partial.credit_move_id in pay_lines))
                (recovery_line | open_refund_lines).reconcile()
            if not recovery_line.reconciled:
                open_pay_lines = pay_lines.filtered(lambda line: not line.reconciled)
                if open_pay_lines:
                    (recovery_line | open_pay_lines).reconcile()

    @api.model
    def _ra_unlink_partials(self, partials):
        if partials:
            partials.unlink()
            # Write the matching numbers reset by the unlink now: a reconciliation done in the same transaction
            # updates them in SQL and would be overwritten by this pending value when flushed.
            self.env['account.move.line'].flush_model(['matching_number'])

    def _ra_reconcile_refund(self):
        for refund in self:
            origin = refund.reversed_entry_id
            if refund.state != 'posted' or refund.move_type != 'out_refund' or origin.move_type != 'out_invoice':
                continue
            origin._ra_reconcile_retention()
            origin._ra_reconcile_advance()

    def _ra_settle_released_retention(self):
        """A credit note that could not reverse the full retention because part of it was already released
        credits the receivable instead: match that excess with the released retention still due."""
        for refund in self:
            origin = refund.reversed_entry_id
            if refund.state != 'posted' or origin.move_type != 'out_invoice' or not origin._ra_get_work_done():
                continue
            ratio = min(refund._ra_get_work_done() / origin._ra_get_work_done(), 1.0)
            excess = refund.currency_id.round(origin.retention_amount * ratio) - refund.retention_amount
            if refund.currency_id.compare_amounts(excess, 0.0) <= 0:
                continue
            open_credit = refund.line_ids.filtered(
                lambda line: line.display_type == 'payment_term' and not line.retention_advance_type and not line.reconciled
            )
            released = self.env['account.move.line'].search([
                ('retention_origin_move_id', '=', origin.id),
                ('retention_advance_type', '=', 'retention_release'),
                ('parent_state', '=', 'posted'),
                ('account_id', 'in', open_credit.account_id.ids),
                ('reconciled', '=', False),
                ('amount_currency', '>', 0.0),
            ])
            if open_credit and released:
                (open_credit | released).reconcile()

    def _ra_link_advance_to_project(self):
        for invoice in self.filtered('advance_payment_id'):
            payment = invoice.advance_payment_id
            vals = {}
            if not payment.is_advance_payment:
                vals['is_advance_payment'] = True
            if not payment.advance_order_id and invoice.project_sale_order_id:
                vals['advance_order_id'] = invoice.project_sale_order_id.id
            if vals:
                payment.sudo().write(vals)

    def button_cancel(self):
        for invoice in self.filtered(lambda move: move.move_type == 'out_invoice'):
            consumed = invoice._ra_get_consumed_amounts()
            if not invoice.currency_id.is_zero(consumed['retention']) or not invoice.currency_id.is_zero(consumed['recovery']):
                raise UserError(self.env._(
                    "You cannot cancel %s: part of its retention was released or reversed by a credit note. "
                    "Reverse those entries first or issue a credit note.",
                    invoice.display_name,
                ))
        return super().button_cancel()

    # -------------------------------------------------------------------------
    # ACTIONS
    # -------------------------------------------------------------------------

    def action_open_retention_release(self):
        invoices = self.filtered(
            lambda move: move.move_type == 'out_invoice' and move.state == 'posted'
            and not move.currency_id.is_zero(move.retention_residual)
        )
        if not invoices:
            raise UserError(self.env._("Select posted customer invoices with retention still held."))
        if len(invoices.commercial_partner_id) > 1 or len(invoices.company_id) > 1:
            raise UserError(self.env._("Release retention for one customer and one company at a time."))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Release Retention"),
            'res_model': 'retention.release.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': invoices.commercial_partner_id.id,
                'default_company_id': invoices.company_id.id,
                'default_invoice_ids': invoices.ids,
            },
        }

    def action_view_retention_items(self):
        """Retention item of the invoice with its releases and credit note reversals."""
        self.ensure_one()
        retention_account = self._ra_get_retention_lines().account_id
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Retention of %s", self.name),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'domain': [
                ('account_id', 'in', retention_account.ids),
                '|', ('move_id', '=', self.id), ('retention_origin_move_id', '=', self.id),
            ],
            'context': {'create': False},
        }
