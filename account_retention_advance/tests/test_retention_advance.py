import base64

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged('post_install', '-at_install')
class TestRetentionAdvance(AccountTestInvoicingCommon):

    @classmethod
    @AccountTestInvoicingCommon.setup_country('sa')
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        cls.company.retention_percent = 10.0
        cls.retention_account = cls.company.retention_account_id
        cls.receivable = cls.company_data['default_account_receivable']
        cls.tax_15 = cls.company_data['default_tax_sale']
        cls.customer = cls.partner_a
        cls.handover = fields.Date.from_string('2027-06-30')
        cls.usd = cls.setup_other_currency('USD', rates=[('2025-01-01', 0.25), ('2026-06-01', 0.20)])
        cls.env.user.group_ids |= cls.env.ref('sales_team.group_sale_manager')

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _create_advance(self, amount, date='2026-01-05', currency=None, company_data=None, **kwargs):
        company_data = company_data or self.company_data
        payment = self.env['account.payment'].with_company(company_data['company']).create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.customer.id,
            'amount': amount,
            'date': date,
            'journal_id': company_data['default_journal_bank'].id,
            'currency_id': (currency or company_data['currency']).id,
            'is_advance_payment': True,
            **kwargs,
        })
        payment.action_post()
        return payment

    def _create_progress_invoice(self, work_done, date='2026-01-31', advance=None, recovery_percent=20.0, post=True, **kwargs):
        vals = {
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
            'invoice_date': date,
            'date': date,
            'retention_due_date': self.handover,
            'invoice_line_ids': [Command.create({
                'name': "Work done",
                'price_unit': work_done,
                'tax_ids': [Command.set(self.tax_15.ids)],
            })],
        }
        if advance:
            vals.update({
                'advance_payment_id': advance.id,
                'advance_recovery_method': 'percent',
                'advance_recovery_percent': recovery_percent,
            })
        vals.update(kwargs)
        invoice = self.env['account.move'].create(vals)
        if post:
            invoice.action_post()
        return invoice

    def _release(self, partner=None, invoices=None, amount=None, date='2026-09-30', company=None):
        context = {'default_partner_id': (partner or self.customer).id}
        if company:
            context['default_company_id'] = company.id
        if invoices:
            context['default_invoice_ids'] = invoices.ids
        with Form(self.env['retention.release.wizard'].with_context(**context)) as wizard_form:
            if amount is not None:
                wizard_form.amount = amount
            wizard_form.date = date
        action = wizard_form.record.action_release()
        return self.env['account.move'].browse(action['res_id'])

    def _reverse_invoice(self, invoice, date, is_modify=False):
        action = self.env['account.move.reversal'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({'journal_id': invoice.journal_id.id, 'date': date}).reverse_moves(is_modify=is_modify)
        return invoice.reversal_move_ids.sorted('id')[-1:], action

    def _report_row(self, date, **kwargs):
        report = self.env['retention.advance.report'].create({'date': date, **kwargs})
        rows = report._get_rows()
        return rows[0] if len(rows) == 1 else rows

    @staticmethod
    def _retention_line(move):
        return move.line_ids.filtered(lambda line: line.display_type == 'retention')

    @staticmethod
    def _recovery_line(move):
        return move.line_ids.filtered(lambda line: line.retention_advance_type == 'advance_recovery')

    @staticmethod
    def _due_lines(move):
        return move.line_ids.filtered(lambda line: line.display_type == 'payment_term' and not line.retention_advance_type)

    # -------------------------------------------------------------------------
    # TESTS
    # -------------------------------------------------------------------------

    def test_settings_default_account(self):
        """A Retention Receivable account is created with the chart of accounts and can't be a receivable."""
        self.assertRecordValues(self.retention_account, [{'account_type': 'asset_current', 'reconcile': True}])
        with self.assertRaises(ValidationError):
            self.company.retention_account_id = self.receivable

    def test_example_first_invoice(self):
        """The example of the specification: 100,000 of work, 10% retention, 20% advance recovery."""
        advance = self._create_advance(200000)
        invoice = self._create_progress_invoice(100000, advance=advance)

        self.assertRecordValues(invoice, [{
            'amount_untaxed': 100000.0,
            'amount_tax': 15000.0,
            'amount_total': 115000.0,
            'retention_amount': 10000.0,
            'advance_recovery_amount': 20000.0,
            'amount_to_pay_now': 85000.0,
            'amount_residual': 85000.0,
            'retention_residual': 10000.0,
            'payment_state': 'partial',
        }])
        self.assertRecordValues(self._retention_line(invoice), [{
            'account_id': self.retention_account.id,
            'partner_id': self.customer.id,
            'balance': 10000.0,
            'date_maturity': self.handover,
            'tax_ids': [],
            'reconciled': False,
        }])
        self.assertRecordValues(self._recovery_line(invoice), [{
            'account_id': self.receivable.id,
            'balance': 20000.0,
            'amount_residual': 0.0,
            'reconciled': True,
        }])
        self.assertRecordValues(self._due_lines(invoice), [{
            'account_id': self.receivable.id,
            'balance': 85000.0,
            'amount_residual': 85000.0,
        }])
        self.assertEqual(advance.advance_residual, 180000.0)
        # Only the 85,000 of this invoice is open in the receivable (the rest of the advance stays a credit).
        open_receivable = self.env['account.move.line'].search([
            ('move_id', '=', invoice.id), ('account_id.account_type', '=', 'asset_receivable'), ('reconciled', '=', False),
        ])
        self.assertEqual(sum(open_receivable.mapped('amount_residual')), 85000.0)

    def test_form_shows_amount_to_pay_now(self):
        """The deductions are computed live in the invoice form, before saving."""
        advance = self._create_advance(200000)
        with Form(self.env['account.move'].with_context(default_move_type='out_invoice')) as move_form:
            move_form.partner_id = self.customer
            move_form.invoice_date = fields.Date.from_string('2026-01-31')
            with move_form.invoice_line_ids.new() as line_form:
                line_form.name = "Work done"
                line_form.price_unit = 100000.0
                line_form.tax_ids.clear()
                line_form.tax_ids.add(self.tax_15)
            move_form.retention_due_date = self.handover
            move_form.advance_payment_id = advance
            move_form.advance_recovery_percent = 20.0
            self.assertEqual(move_form.retention_amount, 10000.0)
            self.assertEqual(move_form.advance_recovery_amount, 20000.0)
            self.assertEqual(move_form.amount_to_pay_now, 85000.0)
            self.assertEqual(move_form.advance_available_amount, 200000.0)
        invoice = move_form.record
        invoice.action_post()
        self.assertEqual(invoice.amount_residual, 85000.0)

    def test_ten_invoices_use_up_the_advance(self):
        advance = self._create_advance(200000)
        invoices = self.env['account.move']
        for month in range(1, 11):
            invoices |= self._create_progress_invoice(100000, date=f'2026-{month:02d}-28', advance=advance)
        self.assertEqual(advance.advance_residual, 0.0)
        self.assertEqual(sum(invoices.mapped('retention_residual')), 100000.0)
        self.assertEqual(sum(invoices.mapped('amount_residual')), 850000.0)
        with self.assertRaisesRegex(UserError, "only"):
            self._create_progress_invoice(100000, date='2026-11-28', advance=advance)

    def test_cannot_recover_more_than_left(self):
        advance = self._create_advance(50000)
        invoice = self._create_progress_invoice(
            100000, advance=advance, post=False, advance_recovery_method='fixed', advance_recovery_fixed_amount=30000,
        )
        self.assertRecordValues(invoice, [{'advance_recovery_amount': 30000.0, 'amount_to_pay_now': 75000.0}])
        invoice.action_post()
        self.assertEqual(advance.advance_residual, 20000.0)

        invoice_2 = self._create_progress_invoice(
            100000, date='2026-02-28', advance=advance, post=False,
            advance_recovery_method='fixed', advance_recovery_fixed_amount=25000,
        )
        self.assertEqual(invoice_2.advance_available_amount, 20000.0)
        with self.assertRaisesRegex(UserError, "only"):
            invoice_2.action_post()
        invoice_2.advance_recovery_fixed_amount = 20000
        invoice_2.action_post()
        self.assertEqual(advance.advance_residual, 0.0)

        # Deductions can't exceed the invoice total.
        invoice_3 = self._create_progress_invoice(
            10000, date='2026-03-31', advance=self._create_advance(500000), post=False,
            advance_recovery_method='fixed', advance_recovery_fixed_amount=20000,
        )
        with self.assertRaisesRegex(UserError, "higher than the total"):
            invoice_3.action_post()

    def test_release_retention_partial_then_full(self):
        invoice_1 = self._create_progress_invoice(100000, date='2026-01-31')
        invoice_2 = self._create_progress_invoice(100000, date='2026-02-28')
        self.assertEqual(invoice_1.amount_residual, 105000.0)

        release = self._release(amount=15000)
        self.assertEqual(release.state, 'posted')
        self.assertEqual(release.journal_id, self.company._ra_get_release_journal())
        released_receivable = release.line_ids.filtered(lambda line: line.account_id == self.receivable)
        self.assertRecordValues(released_receivable.sorted('balance'), [
            {'balance': 5000.0, 'date_maturity': fields.Date.from_string('2026-09-30'), 'retention_origin_move_id': invoice_2.id, 'reconciled': False},
            {'balance': 10000.0, 'date_maturity': fields.Date.from_string('2026-09-30'), 'retention_origin_move_id': invoice_1.id, 'reconciled': False},
        ])
        self.assertRecordValues(release.line_ids.filtered(lambda line: line.account_id == self.retention_account).sorted('balance'), [
            {'balance': -10000.0, 'reconciled': True},
            {'balance': -5000.0, 'reconciled': True},
        ])
        self.assertRecordValues(invoice_1 + invoice_2, [{'retention_residual': 0.0}, {'retention_residual': 5000.0}])
        retention_items = self.env['account.move.line'].search(invoice_1.action_view_retention_items()['domain'])
        self.assertEqual(sorted(retention_items.mapped('balance')), [-10000.0, 10000.0])
        # The released amount can be paid like any other receivable.
        self.assertEqual(invoice_1.amount_residual, 105000.0)

        with self.assertRaisesRegex(UserError, "only"):
            wizard = self.env['retention.release.wizard'].create({
                'partner_id': self.customer.id, 'invoice_ids': [Command.set(invoice_2.ids)], 'date': '2026-10-01',
            })
            wizard.amount = 6000
            wizard.line_ids.amount = 6000
            wizard.action_release()

        self._release(invoices=invoice_2, date='2026-10-01')
        self.assertEqual(invoice_2.retention_residual, 0.0)
        self.assertTrue(self._retention_line(invoice_2).reconciled)

    def test_release_respects_lock_date(self):
        invoice = self._create_progress_invoice(100000, date='2026-01-31')
        self.company.fiscalyear_lock_date = fields.Date.from_string('2026-06-30')
        with self.assertRaisesRegex(UserError, "locked period"):
            self._release(invoices=invoice, date='2026-05-31')
        release = self._release(invoices=invoice, date='2026-07-15')
        self.assertEqual(release.date, fields.Date.from_string('2026-07-15'))

    def test_partial_credit_note(self):
        advance = self._create_advance(200000)
        invoice = self._create_progress_invoice(100000, advance=advance)
        credit_note, _action = self._reverse_invoice(invoice, date=fields.Date.from_string('2026-02-10'))
        self.assertRecordValues(credit_note, [{'retention_amount': 10000.0, 'advance_recovery_amount': 20000.0}])

        credit_note.invoice_line_ids.price_unit = 50000
        self.assertRecordValues(credit_note, [{
            'amount_total': 57500.0,
            'retention_amount': 5000.0,
            'advance_recovery_amount': 10000.0,
            'amount_to_pay_now': 42500.0,
        }])
        self.assertRecordValues(self._retention_line(credit_note), [{'balance': -5000.0, 'account_id': self.retention_account.id}])
        credit_note.action_post()

        self.assertRecordValues(invoice, [{'retention_residual': 5000.0, 'amount_residual': 42500.0}])
        self.assertEqual(advance.advance_residual, 190000.0)
        self.assertEqual(credit_note.amount_residual, 0.0)
        self.assertTrue(self._retention_line(credit_note).reconciled)
        self.assertTrue(self._recovery_line(credit_note).reconciled)
        self.assertTrue(self._recovery_line(invoice).reconciled)
        self.assertTrue(self._recovery_line(invoice).matching_number)
        self.assertTrue(self._recovery_line(credit_note).matching_number)

        # The advance given back can be recovered again.
        invoice_2 = self._create_progress_invoice(100000, date='2026-02-28', advance=advance)
        self.assertEqual(advance.advance_residual, 170000.0)
        self.assertEqual(invoice_2.amount_residual, 85000.0)

    def test_full_reversal_and_new_invoice(self):
        advance = self._create_advance(200000)
        invoice = self._create_progress_invoice(100000, advance=advance)
        credit_note, action = self._reverse_invoice(invoice, date=fields.Date.from_string('2026-02-10'), is_modify=True)
        self.assertRecordValues(credit_note, [{'state': 'posted', 'retention_amount': 10000.0, 'advance_recovery_amount': 20000.0}])
        self.assertRecordValues(invoice, [{'payment_state': 'reversed', 'retention_residual': 0.0, 'amount_residual': 0.0}])
        self.assertEqual(advance.advance_residual, 200000.0)

        new_invoice = self.env['account.move'].browse(action['res_id'])
        self.assertRecordValues(new_invoice, [{
            'state': 'draft', 'retention_percent': 10.0, 'advance_payment_id': advance.id, 'advance_recovery_percent': 20.0,
        }])
        new_invoice.action_post()
        self.assertEqual(advance.advance_residual, 180000.0)
        self.assertEqual(new_invoice.amount_residual, 85000.0)

    def test_reset_to_draft_does_not_duplicate(self):
        advance = self._create_advance(200000)
        invoice = self._create_progress_invoice(100000, advance=advance)
        line_count = len(invoice.line_ids)

        invoice.button_draft()
        invoice.action_post()
        self.assertEqual(len(invoice.line_ids), line_count)
        self.assertEqual(len(self._retention_line(invoice)), 1)
        self.assertEqual(len(self._recovery_line(invoice).matched_credit_ids), 1)
        self.assertEqual(advance.advance_residual, 180000.0)
        self.assertEqual(invoice.amount_residual, 85000.0)

        # Changing the amounts while in draft re-computes everything once.
        invoice.button_draft()
        invoice.invoice_line_ids.price_unit = 120000
        invoice.action_post()
        self.assertEqual(len(invoice.line_ids), line_count)
        self.assertRecordValues(invoice, [{'retention_amount': 12000.0, 'advance_recovery_amount': 24000.0, 'amount_residual': 102000.0}])
        self.assertEqual(advance.advance_residual, 176000.0)

    def test_reset_to_draft_after_release(self):
        invoice = self._create_progress_invoice(100000)
        self._release(invoices=invoice, amount=4000)
        self.assertEqual(invoice.retention_residual, 6000.0)

        invoice.button_draft()
        invoice.action_post()
        self.assertEqual(invoice.retention_residual, 6000.0)

        invoice.button_draft()
        invoice.invoice_line_ids.price_unit = 30000
        with self.assertRaisesRegex(UserError, "already released"):
            invoice.action_post()
        with self.assertRaisesRegex(UserError, "released"):
            invoice.button_cancel()

    def test_credit_note_after_release(self):
        """Retention already released can't be reversed on the retention account: it is credited on the receivable."""
        invoice = self._create_progress_invoice(100000)
        release = self._release(invoices=invoice, amount=4000)
        credit_note, _action = self._reverse_invoice(invoice, date=fields.Date.from_string('2026-10-15'))
        self.assertRecordValues(credit_note, [{'retention_amount': 6000.0, 'amount_to_pay_now': 109000.0}])
        credit_note.action_post()

        self.assertEqual(invoice.retention_residual, 0.0)
        self.assertEqual(credit_note.amount_residual, 0.0)
        self.assertEqual(invoice.amount_residual, 0.0)
        released_receivable = release.line_ids.filtered(lambda line: line.account_id == self.receivable)
        self.assertTrue(released_receivable.reconciled)

    def test_foreign_currency_same_currency_advance(self):
        """USD invoice on a SAR company, USD advance, release at another rate."""
        advance = self._create_advance(20000, currency=self.usd)
        invoice = self._create_progress_invoice(10000, advance=advance, currency_id=self.usd.id)

        self.assertRecordValues(invoice, [{
            'currency_id': self.usd.id,
            'retention_amount': 1000.0,
            'advance_recovery_amount': 2000.0,
            'amount_to_pay_now': 8500.0,
            'amount_residual': 8500.0,
            'retention_residual': 1000.0,
        }])
        self.assertRecordValues(self._retention_line(invoice), [{'amount_currency': 1000.0, 'balance': 4000.0, 'currency_id': self.usd.id}])
        self.assertRecordValues(self._recovery_line(invoice), [{'amount_currency': 2000.0, 'balance': 8000.0, 'reconciled': True}])
        self.assertEqual(advance.advance_residual, 18000.0)

        # Release in July at 5 SAR / USD: the 1,000 SAR difference is booked as exchange difference.
        release = self._release(invoices=invoice, date='2026-07-01')
        self.assertRecordValues(
            release.line_ids.filtered(lambda line: line.account_id == self.retention_account),
            [{'amount_currency': -1000.0, 'balance': -5000.0, 'currency_id': self.usd.id, 'reconciled': True}],
        )
        self.assertEqual(invoice.retention_residual, 0.0)
        self.assertTrue(self._retention_line(invoice).full_reconcile_id)

        # Report values the release at the historical rate of the retention.
        row = self._report_row('2026-12-31')
        self.assertRecordValues(self.env['res.partner'].browse(row['partner_id']), [{'id': self.customer.id}])
        self.assertEqual(row['retention_held'], 4000.0)
        self.assertEqual(row['retention_released'], 4000.0)
        self.assertEqual(row['retention_balance'], 0.0)
        self.assertEqual(row['advance_received'], 80000.0)
        self.assertEqual(row['advance_recovered'], 8000.0)
        self.assertEqual(row['advance_balance'], 72000.0)

    def test_foreign_currency_invoice_with_company_currency_advance(self):
        advance = self._create_advance(100000)  # SAR
        invoice = self._create_progress_invoice(10000, advance=advance, currency_id=self.usd.id)  # USD 2,000 = SAR 8,000
        self.assertTrue(self._recovery_line(invoice).reconciled)
        self.assertEqual(advance.advance_residual, 92000.0)

        invoice_2 = self._create_progress_invoice(
            100000, date='2026-02-28', advance=advance, currency_id=self.usd.id, post=False,
        )  # USD 20,000 = SAR 80,000 > 92,000? no: fits
        invoice_2.action_post()
        self.assertEqual(advance.advance_residual, 12000.0)
        invoice_3 = self._create_progress_invoice(100000, date='2026-03-31', advance=advance, currency_id=self.usd.id, post=False)
        with self.assertRaisesRegex(UserError, "only"):
            invoice_3.action_post()

    def test_multi_company(self):
        company_2_data = self.setup_other_company(name="Company 2")
        company_2 = company_2_data['company']
        self.assertTrue(company_2.retention_account_id)
        self.assertNotEqual(company_2.retention_account_id, self.retention_account)
        company_2.retention_percent = 5.0

        self.env.user.company_ids |= company_2
        invoice = self.env['account.move'].with_company(company_2).create({
            'move_type': 'out_invoice',
            'partner_id': self.customer.id,
            'invoice_date': '2026-01-31',
            'retention_due_date': self.handover,
            'invoice_line_ids': [Command.create({'name': "Work", 'price_unit': 100000, 'tax_ids': [Command.set(company_2_data['default_tax_sale'].ids)]})],
        })
        invoice.action_post()
        self.assertRecordValues(self._retention_line(invoice), [{'account_id': company_2.retention_account_id.id, 'balance': 5000.0}])

        advance_company_1 = self._create_advance(100000)
        with self.assertRaises(UserError):
            self.env['account.move'].with_company(company_2).create({
                'move_type': 'out_invoice',
                'partner_id': self.customer.id,
                'invoice_date': '2026-01-31',
                'retention_due_date': self.handover,
                'advance_payment_id': advance_company_1.id,
                'advance_recovery_percent': 10.0,
                'invoice_line_ids': [Command.create({'name': "Work", 'price_unit': 100000, 'tax_ids': [Command.set(company_2_data['default_tax_sale'].ids)]})],
            }).action_post()

        release = self._release(invoices=invoice, company=company_2)
        self.assertEqual(release.company_id, company_2)
        self.assertEqual(invoice.retention_residual, 0.0)

        rows = self._report_row('2026-12-31', company_ids=[Command.set((self.company + company_2).ids)])
        self.assertEqual({row['company_id'] for row in rows}, {self.company.id, company_2.id})

    def test_report_as_at_dates_and_excel(self):
        advance = self._create_advance(200000, date='2026-01-05')
        invoice_1 = self._create_progress_invoice(100000, date='2026-01-31', advance=advance)
        invoice_2 = self._create_progress_invoice(100000, date='2026-02-28', advance=advance)
        self._release(invoices=invoice_1, amount=5000, date='2026-03-15')
        credit_note, _action = self._reverse_invoice(invoice_2, date=fields.Date.from_string('2026-03-20'))
        credit_note.invoice_line_ids.price_unit = 50000
        credit_note.action_post()

        row = self._report_row('2026-02-15')
        self.assertEqual(
            {key: row[key] for key in ('total_invoiced', 'retention_held', 'retention_released', 'retention_balance',
                                       'advance_received', 'advance_recovered', 'advance_balance')},
            {'total_invoiced': 115000.0, 'retention_held': 10000.0, 'retention_released': 0.0, 'retention_balance': 10000.0,
             'advance_received': 200000.0, 'advance_recovered': 20000.0, 'advance_balance': 180000.0},
        )
        row = self._report_row('2026-12-31')
        self.assertEqual(
            {key: row[key] for key in ('total_invoiced', 'retention_held', 'retention_released', 'retention_balance',
                                       'advance_received', 'advance_recovered', 'advance_balance')},
            {'total_invoiced': 172500.0, 'retention_held': 15000.0, 'retention_released': 5000.0, 'retention_balance': 10000.0,
             'advance_received': 200000.0, 'advance_recovered': 30000.0, 'advance_balance': 170000.0},
        )
        # Balances tie to the open items.
        self.assertEqual(row['retention_balance'], sum((invoice_1 + invoice_2).mapped('retention_residual')))
        self.assertEqual(row['advance_balance'], advance.advance_residual)

        report = self.env['retention.advance.report'].create({'date': '2026-12-31'})
        action = report.action_export_xlsx()
        attachment = self.env['ir.attachment'].browse(int(action['url'].split('/')[3].split('?')[0]))
        self.assertTrue(base64.b64decode(attachment.datas).startswith(b'PK'))
        self.assertEqual(len(report.line_ids), 1)
        self.assertEqual(report.action_view()['res_model'], 'retention.advance.report.line')

    def test_sale_order_project(self):
        product = self._create_product(name="Construction work", type='service', invoice_policy='delivery', lst_price=1000000, taxes_id=self.tax_15)
        order = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'advance_recovery_percent': 20.0,
            'expected_handover_date': self.handover,
            'order_line': [Command.create({'product_id': product.id, 'product_uom_qty': 1, 'price_unit': 1000000})],
        })
        order.action_confirm()
        self.assertEqual(order.retention_percent, 10.0)
        advance = self._create_advance(200000, advance_order_id=order.id)

        order.order_line.qty_delivered = 0.1
        invoice = order._create_invoices()
        self.assertRecordValues(invoice, [{
            'project_sale_order_id': order.id,
            'retention_percent': 10.0,
            'retention_due_date': self.handover,
            'advance_payment_id': advance.id,
            'advance_recovery_percent': 20.0,
            'amount_to_pay_now': 85000.0,
        }])
        invoice.action_post()
        self.assertEqual(invoice.amount_residual, 85000.0)

        other_invoice = self._create_progress_invoice(50000, date='2026-02-28')  # no project
        rows = self._report_row(fields.Date.today())
        by_project = {row['order_id']: row for row in rows}
        self.assertEqual(by_project[order.id]['retention_balance'], 10000.0)
        self.assertEqual(by_project[order.id]['advance_balance'], 180000.0)
        self.assertEqual(by_project[False]['retention_balance'], other_invoice.retention_amount)

    def test_advance_not_proposed_as_outstanding_credit(self):
        advance = self._create_advance(200000)
        regular_payment = self.init_payment(1000.0, post=True, date='2026-01-05', partner=self.customer)
        invoice = self._create_progress_invoice(100000)
        widget_lines = [vals['id'] for vals in invoice.invoice_outstanding_credits_debits_widget['content']]
        self.assertNotIn(advance.move_id.line_ids.filtered(lambda line: line.balance < 0).id, widget_lines)
        self.assertIn(regular_payment.move_id.line_ids.filtered(lambda line: line.balance < 0).id, widget_lines)

    def test_printed_invoice(self):
        advance = self._create_advance(200000)
        invoice = self._create_progress_invoice(100000, advance=advance)
        html = str(self.env['ir.actions.report']._render_qweb_html('account.report_invoice', invoice.ids)[0])
        self.assertIn('Amount to Pay Now', html)
        self.assertIn('Less: Retention', html)
        self.assertIn('Less: Advance Recovery', html)
        self.assertIn('85,000.00', html)

    def test_installments(self):
        """The amount to pay now is spread over the payment term installments."""
        advance = self._create_advance(200000)
        invoice = self._create_progress_invoice(100000, advance=advance, invoice_payment_term_id=self.pay_terms_b.id)
        installments = self._due_lines(invoice).sorted('date_maturity')
        self.assertEqual(installments.mapped('balance'), [25500.0, 59500.0])
        self.assertRegex(installments[1].name, '#2$')
        self.assertEqual(invoice.amount_residual, 85000.0)
        self.assertEqual(advance.advance_residual, 180000.0)

    def test_reverse_retention_release(self):
        invoice = self._create_progress_invoice(100000)
        release = self._release(invoices=invoice, amount=4000)
        self.assertEqual(invoice.retention_residual, 6000.0)

        reversal, _action = self._reverse_invoice(release, date=fields.Date.from_string('2026-10-01'))
        self.assertEqual(reversal.state, 'posted')
        self.assertEqual(invoice.retention_residual, 10000.0)
        row = self._report_row('2026-12-31')
        self.assertEqual((row['retention_released'], row['retention_balance']), (0.0, 10000.0))

        self._release(invoices=invoice, date='2026-10-02')
        self.assertEqual(invoice.retention_residual, 0.0)

    def test_sale_down_payment_is_not_work_done(self):
        product = self._create_product(name="Construction work", type='service', invoice_policy='delivery', lst_price=1000000, taxes_id=self.tax_15)
        order = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'expected_handover_date': self.handover,
            'order_line': [Command.create({'product_id': product.id, 'product_uom_qty': 1, 'price_unit': 1000000})],
        })
        order.action_confirm()
        self.env['sale.advance.payment.inv'].with_context(active_model='sale.order', active_ids=order.ids).create({
            'advance_payment_method': 'percentage',
            'amount': 20,
        }).create_invoices()
        down_payment_invoice = order.invoice_ids
        self.assertEqual(down_payment_invoice.retention_amount, 0.0)
        down_payment_invoice.action_post()

        order.order_line.filtered(lambda line: not line.is_downpayment).qty_delivered = 0.5
        final_invoice = order._create_invoices(final=True)
        self.assertEqual(final_invoice.amount_untaxed, 300000.0)
        self.assertEqual(final_invoice.retention_amount, 50000.0)

    def test_change_advance_in_draft(self):
        advance_1 = self._create_advance(200000)
        advance_2 = self._create_advance(50000, date='2026-01-06')
        invoice = self._create_progress_invoice(100000, advance=advance_1)

        invoice.button_draft()
        invoice.advance_payment_id = advance_2
        invoice.action_post()
        self.assertEqual((advance_1.advance_residual, advance_2.advance_residual), (200000.0, 30000.0))
        recovery_line = self._recovery_line(invoice)
        self.assertTrue(recovery_line.matching_number)
        self.assertEqual(recovery_line.matching_number, advance_2.move_id.line_ids.filtered(lambda line: line.balance < 0).matching_number)

        invoice.button_draft()
        invoice.advance_payment_id = False
        invoice.action_post()
        self.assertEqual((advance_1.advance_residual, advance_2.advance_residual), (200000.0, 50000.0))
        self.assertEqual(invoice.amount_residual, 105000.0)
