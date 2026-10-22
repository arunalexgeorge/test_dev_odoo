from odoo import Command, _, api, fields, models
from odoo.exceptions import ValidationError

# Retention lines live on a reconcilable balance sheet account that is NOT of type receivable:
# Odoo counts every receivable item of an invoice as "amount due", which must stay the net amount.
RETENTION_ACCOUNT_TYPES = ('asset_current', 'asset_non_current')


class ResCompany(models.Model):
    _inherit = 'res.company'

    retention_account_id = fields.Many2one(
        comodel_name='account.account',
        string="Retention Receivable Account",
        domain=[('reconcile', '=', True), ('account_type', 'in', RETENTION_ACCOUNT_TYPES)],
        help="Account holding the retention withheld by customers until it is released at handover.",
    )
    retention_percent = fields.Float(
        string="Default Retention %",
        digits=(16, 2),
        help="Retention percentage proposed on new projects and customer invoices.",
    )
    retention_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Retention Release Journal",
        domain=[('type', '=', 'general')],
        help="Miscellaneous journal used for the entries moving released retention to the receivable.",
    )

    @api.constrains('retention_account_id', 'retention_journal_id', 'retention_percent')
    def _check_retention_settings(self):
        for company in self:
            account = company.retention_account_id
            if account:
                if not account.reconcile:
                    raise ValidationError(_("The Retention Receivable account %s must allow reconciliation.", account.display_name))
                if account.account_type not in RETENTION_ACCOUNT_TYPES:
                    raise ValidationError(_(
                        "The Retention Receivable account %s must be a Current Assets or Non-current Assets account. "
                        "A receivable type account would make the retention part of the amount due on the invoice.",
                        account.display_name,
                    ))
                if not account.sudo().company_ids & company.parent_ids:
                    raise ValidationError(_("The Retention Receivable account %s belongs to another company.", account.display_name))
            journal = company.retention_journal_id
            if journal and (journal.type != 'general' or journal.company_id not in company.parent_ids):
                raise ValidationError(_("The Retention Release Journal must be a miscellaneous journal of the company."))
            if not 0.0 <= company.retention_percent <= 100.0:
                raise ValidationError(_("The default retention percentage must be between 0 and 100."))

    def _ra_get_retention_account(self):
        self.ensure_one()
        return self.retention_account_id or self.root_id.retention_account_id

    def _ra_get_release_journal(self):
        self.ensure_one()
        if journal := self.retention_journal_id or self.root_id.retention_journal_id:
            return journal
        misc_journal = self.env['account.chart.template'].with_company(self.root_id).ref('general', raise_if_not_found=False)
        return misc_journal or self.env['account.journal'].search([
            *self.env['account.journal']._check_company_domain(self),
            ('type', '=', 'general'),
        ], limit=1)

    def _ra_setup_retention_defaults(self):
        """Create a 'Retention Receivable' account and pick a release journal for companies missing them."""
        Account = self.env['account.account']
        for company in self.filtered(lambda c: not c.parent_id and c.chart_template):
            Account = Account.with_company(company)
            vals = {}
            if not company.retention_account_id:
                account = Account.search([
                    *Account._check_company_domain(company),
                    ('account_type', 'in', RETENTION_ACCOUNT_TYPES),
                    ('reconcile', '=', True),
                    ('name', 'ilike', 'retention'),
                ], limit=1)
                if not account:
                    receivable = company.partner_id.with_company(company).property_account_receivable_id
                    start_code = receivable.code or '1200'
                    account = Account.create({
                        'name': _("Retention Receivable"),
                        'code': Account._search_new_account_code(start_code),
                        'account_type': 'asset_current',
                        'reconcile': True,
                        'company_ids': [Command.set(company.ids)],
                    })
                vals['retention_account_id'] = account.id
            if not company.retention_journal_id:
                journal = company._ra_get_release_journal()
                if journal:
                    vals['retention_journal_id'] = journal.id
            if vals:
                company.write(vals)
