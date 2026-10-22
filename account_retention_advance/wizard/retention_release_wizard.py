from odoo import Command, api, fields, models
from odoo.exceptions import UserError


class RetentionReleaseWizard(models.TransientModel):
    _name = 'retention.release.wizard'
    _description = "Release Retention"
    _check_company_auto = True

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    partner_id = fields.Many2one('res.partner', string="Customer", required=True)
    order_ids = fields.Many2many(
        'sale.order', string="Projects",
        domain="[('partner_id', 'child_of', partner_id), ('company_id', '=', company_id)]",
        help="Limit the invoices to these projects.",
    )
    invoice_ids = fields.Many2many(
        'account.move', string="Invoices", check_company=True,
        domain="[('move_type', '=', 'out_invoice'), ('state', '=', 'posted'), ('company_id', '=', company_id),"
               " ('commercial_partner_id', '=', partner_id), ('retention_residual', '>', 0)]",
    )
    currency_id = fields.Many2one('res.currency', compute='_compute_currency_id')
    retention_held = fields.Monetary(string="Retention Held", compute='_compute_retention_held', currency_field='currency_id')
    amount = fields.Monetary(
        string="Amount to Release", currency_field='currency_id',
        compute='_compute_amount', store=True, readonly=False, precompute=True,
        help="Allocated to the selected invoices, oldest first. Adjust the lines to allocate differently.",
    )
    line_ids = fields.One2many(
        'retention.release.wizard.line', 'wizard_id', string="Allocation",
        compute='_compute_line_ids', store=True, readonly=False,
    )
    date = fields.Date(string="Release Date", required=True, default=fields.Date.context_today)
    date_maturity = fields.Date(
        string="Due Date", compute='_compute_date_maturity', store=True, readonly=False, precompute=True, required=True,
        help="Due date of the released amount in the customer receivable.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal", required=True, check_company=True,
        compute='_compute_journal_id', store=True, readonly=False, precompute=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
    )
    ref = fields.Char(string="Reference")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if res.get('partner_id'):
            res['partner_id'] = self.env['res.partner'].browse(res['partner_id']).commercial_partner_id.id
        return res

    @api.depends('invoice_ids')
    def _compute_currency_id(self):
        for wizard in self:
            currencies = wizard.invoice_ids.currency_id
            wizard.currency_id = currencies if len(currencies) == 1 else wizard.company_id.currency_id

    @api.depends('invoice_ids')
    def _compute_retention_held(self):
        for wizard in self:
            wizard.retention_held = sum(wizard.invoice_ids.mapped('retention_residual'))

    @api.depends('invoice_ids')
    def _compute_amount(self):
        for wizard in self:
            wizard.amount = sum(wizard.invoice_ids.mapped('retention_residual'))

    @api.depends('invoice_ids', 'amount')
    def _compute_line_ids(self):
        for wizard in self:
            remaining = wizard.amount
            commands = [Command.clear()]
            for invoice in wizard.invoice_ids.sorted(lambda move: (move.invoice_date or move.date, move.id)):
                amount = min(remaining, invoice.retention_residual) if remaining > 0 else 0.0
                remaining -= amount
                commands.append(Command.create({'invoice_id': invoice.id, 'amount': amount}))
            wizard.line_ids = commands

    @api.depends('date')
    def _compute_date_maturity(self):
        for wizard in self:
            wizard.date_maturity = wizard.date

    @api.depends('company_id')
    def _compute_journal_id(self):
        for wizard in self:
            wizard.journal_id = wizard.company_id._ra_get_release_journal()

    @api.onchange('partner_id', 'order_ids')
    def _onchange_partner_id(self):
        if self.partner_id != self.partner_id.commercial_partner_id:
            self.partner_id = self.partner_id.commercial_partner_id
        domain = [
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id),
            ('commercial_partner_id', '=', self.partner_id.id),
            ('retention_residual', '>', 0.0),
        ]
        if self.order_ids:
            domain.append(('project_sale_order_id', 'in', self.order_ids.ids))
        if self.partner_id and not (self.invoice_ids and self.invoice_ids.filtered_domain(domain) == self.invoice_ids):
            self.invoice_ids = self.env['account.move'].search(domain)

    def _check_release(self, lines):
        if not lines:
            raise UserError(self.env._("Enter an amount to release on at least one invoice."))
        if len(lines.invoice_id.currency_id) > 1:
            raise UserError(self.env._("Release retention of invoices in one currency at a time."))
        currency = lines.invoice_id.currency_id
        for line in lines:
            invoice = line.invoice_id
            if (
                invoice.state != 'posted'
                or invoice.company_id != self.company_id
                or invoice.commercial_partner_id != self.partner_id
            ):
                raise UserError(self.env._("%s is not a posted invoice of this customer and company.", invoice.display_name))
            if currency.compare_amounts(line.amount, 0.0) < 0:
                raise UserError(self.env._("The amount to release cannot be negative."))
            if currency.compare_amounts(line.amount, invoice.retention_residual) > 0:
                raise UserError(self.env._(
                    "You cannot release %(amount)s on %(invoice)s: only %(held)s of retention is held.",
                    amount=invoice._ra_format(line.amount), invoice=invoice.display_name,
                    held=invoice._ra_format(invoice.retention_residual),
                ))
        if currency.compare_amounts(sum(lines.mapped('amount')), self.amount):
            raise UserError(self.env._(
                "The allocation (%(allocated)s) differs from the amount to release (%(amount)s).",
                allocated=lines.invoice_id[:1]._ra_format(sum(lines.mapped('amount'))),
                amount=lines.invoice_id[:1]._ra_format(self.amount),
            ))
        if self.date_maturity < self.date:
            raise UserError(self.env._("The due date cannot be before the release date."))
        violated = self.company_id._get_violated_lock_dates(self.date, False, self.journal_id)
        if violated:
            raise UserError(self.env._(
                "The release date %(date)s is in a locked period (%(locks)s). Choose a later date.",
                date=self.date, locks=self.env['res.company']._format_lock_dates(violated),
            ))

    def _prepare_move_vals(self, lines):
        currency = lines.invoice_id.currency_id
        company_currency = self.company_id.currency_id
        line_vals = []
        for line in lines:
            invoice = line.invoice_id
            retention_line = invoice._ra_get_retention_lines()
            receivable_account = invoice.line_ids.filtered(
                lambda aml: aml.display_type == 'payment_term' and not aml.retention_advance_type
            )[:1].account_id or self.partner_id.with_company(self.company_id).property_account_receivable_id
            balance = currency._convert(line.amount, company_currency, self.company_id, self.date)
            label = self.env._("Retention release %s", invoice.name)
            common = {
                'name': label,
                'partner_id': self.partner_id.id,
                'currency_id': currency.id,
                'retention_advance_type': 'retention_release',
                'retention_origin_move_id': invoice.id,
            }
            line_vals += [
                Command.create({
                    **common,
                    'account_id': receivable_account.id,
                    'amount_currency': line.amount,
                    'balance': balance,
                    'date_maturity': self.date_maturity,
                }),
                Command.create({
                    **common,
                    'account_id': retention_line.account_id.id,
                    'amount_currency': -line.amount,
                    'balance': -balance,
                    'date_maturity': self.date,
                }),
            ]
        return {
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'journal_id': self.journal_id.id,
            'date': self.date,
            'partner_id': self.partner_id.id,
            'currency_id': currency.id,
            'ref': self.ref or self.env._("Retention release - %s", ', '.join(lines.invoice_id.mapped('name'))),
            'line_ids': line_vals,
        }

    def action_release(self):
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: not line.invoice_id.currency_id.is_zero(line.amount))
        self._check_release(lines)
        move = self.env['account.move'].create(self._prepare_move_vals(lines))
        move.action_post()
        return move._get_records_action(name=self.env._("Retention Release"))


class RetentionReleaseWizardLine(models.TransientModel):
    _name = 'retention.release.wizard.line'
    _description = "Release Retention - Invoice"

    wizard_id = fields.Many2one('retention.release.wizard', required=True, ondelete='cascade')
    invoice_id = fields.Many2one('account.move', string="Invoice", required=True)
    project_sale_order_id = fields.Many2one(related='invoice_id.project_sale_order_id')
    invoice_date = fields.Date(related='invoice_id.invoice_date', string="Date")
    retention_due_date = fields.Date(related='invoice_id.retention_due_date', string="Handover")
    currency_id = fields.Many2one(related='invoice_id.currency_id')
    retention_amount = fields.Monetary(related='invoice_id.retention_amount', string="Retention")
    retention_residual = fields.Monetary(related='invoice_id.retention_residual', string="Held")
    amount = fields.Monetary(string="To Release")
