import base64
import io
from collections import defaultdict

import xlsxwriter

from odoo import api, fields, models
from odoo.tools import format_date

AMOUNT_FIELDS = [
    'total_invoiced',
    'retention_held',
    'retention_released',
    'retention_balance',
    'advance_received',
    'advance_recovered',
    'advance_balance',
]


class RetentionAdvanceReport(models.TransientModel):
    _name = 'retention.advance.report'
    _description = "Retention and Advance Report"

    date = fields.Date(string="As at", required=True, default=fields.Date.context_today)
    company_ids = fields.Many2many(
        'res.company', string="Companies", required=True,
        default=lambda self: self.env.companies,
        domain=lambda self: [('id', 'in', self.env.user.company_ids.ids)],
    )
    partner_ids = fields.Many2many('res.partner', string="Customers")
    include_all = fields.Boolean(
        string="Include projects without retention or advance",
        help="Also list customers / projects that were invoiced without any retention or advance.",
    )
    line_ids = fields.One2many('retention.advance.report.line', 'report_id', string="Lines")

    # -------------------------------------------------------------------------
    # DATA
    # -------------------------------------------------------------------------

    @api.model
    def _retention_rate(self, invoice):
        """Company currency value of one unit of the invoice retention (historical rate of the invoice)."""
        line = invoice._ra_get_retention_lines()[:1]
        return line.balance / line.amount_currency if line and line.amount_currency else 1.0

    @api.model
    def _advance_value(self, line, payment):
        """Company currency value of a recovery item, at the rate of the advance it consumes."""
        if (
            payment
            and payment.amount
            and line.currency_id == payment.currency_id
            and payment.currency_id != line.company_currency_id
        ):
            return line.amount_currency * payment.amount_company_currency_signed / payment.amount
        return line.balance

    def _get_rows(self):
        """Return one dict per (company, customer, project) with the amounts in company currency as at the date."""
        self.ensure_one()
        companies = self.company_ids & self.env.user.company_ids
        partners = self.partner_ids.commercial_partner_id
        rows = defaultdict(lambda: dict.fromkeys(AMOUNT_FIELDS + ['has_activity'], 0.0))

        move_domain = [
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('date', '<=', self.date),
            ('company_id', 'in', companies.ids),
        ]
        if partners:
            move_domain.append(('commercial_partner_id', 'in', partners.ids))
        for move in self.env['account.move'].search(move_domain):
            row = rows[(move.company_id.id, move.commercial_partner_id.id, move.project_sale_order_id.id)]
            row['total_invoiced'] += move.amount_total_signed
            for line in move._ra_get_retention_lines():
                if move.move_type == 'out_refund':
                    row['retention_held'] += line.amount_currency * self._retention_rate(move.reversed_entry_id)
                else:
                    row['retention_held'] += line.balance
                row['has_activity'] = 1.0
            for line in move._ra_get_recovery_lines():
                row['advance_recovered'] += self._advance_value(line, move.advance_payment_id)
                row['has_activity'] = 1.0

        release_domain = [
            ('retention_advance_type', '=', 'retention_release'),
            ('account_id.account_type', '!=', 'asset_receivable'),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.date),
            ('company_id', 'in', companies.ids),
        ]
        if partners:
            release_domain.append(('partner_id', 'in', partners.ids))
        for line in self.env['account.move.line'].search(release_domain):
            invoice = line.retention_origin_move_id
            row = rows[(line.company_id.id, invoice.commercial_partner_id.id, invoice.project_sale_order_id.id)]
            row['retention_released'] -= line.amount_currency * self._retention_rate(invoice)
            row['has_activity'] = 1.0

        payment_domain = [
            ('is_advance_payment', '=', True),
            ('payment_type', '=', 'inbound'),
            ('state', 'in', ('in_process', 'paid')),
            ('date', '<=', self.date),
            ('company_id', 'in', companies.ids),
        ]
        if partners:
            payment_domain.append(('partner_id', 'child_of', partners.ids))
        for payment in self.env['account.payment'].search(payment_domain):
            row = rows[(payment.company_id.id, payment.partner_id.commercial_partner_id.id, payment.advance_order_id.id)]
            row['advance_received'] += payment.amount_company_currency_signed
            row['has_activity'] = 1.0

        result = []
        for (company_id, partner_id, order_id), row in rows.items():
            if not row.pop('has_activity') and not self.include_all:
                continue
            currency = self.env['res.company'].browse(company_id).currency_id
            row['retention_balance'] = row['retention_held'] - row['retention_released']
            row['advance_balance'] = row['advance_received'] - row['advance_recovered']
            result.append({
                'company_id': company_id,
                'partner_id': partner_id,
                'order_id': order_id or False,
                **{fname: currency.round(row[fname]) for fname in AMOUNT_FIELDS},
            })
        partner_names = {p.id: p.display_name for p in self.env['res.partner'].browse({r['partner_id'] for r in result})}
        order_names = {o.id: o.name for o in self.env['sale.order'].browse({r['order_id'] for r in result if r['order_id']})}
        result.sort(key=lambda r: (r['company_id'], partner_names[r['partner_id']].lower(), order_names.get(r['order_id'], '')))
        return result

    def _generate_lines(self):
        self.ensure_one()
        self.line_ids.unlink()
        self.env['retention.advance.report.line'].create([
            {'report_id': self.id, **row} for row in self._get_rows()
        ])

    # -------------------------------------------------------------------------
    # ACTIONS
    # -------------------------------------------------------------------------

    def action_view(self):
        self.ensure_one()
        self._generate_lines()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Retention & Advance as at %s", format_date(self.env, self.date)),
            'res_model': 'retention.advance.report.line',
            'view_mode': 'list,pivot',
            'domain': [('report_id', '=', self.id)],
            'context': {'create': False},
        }

    def action_export_xlsx(self):
        self.ensure_one()
        self._generate_lines()
        content = self._get_xlsx_content()
        filename = f"retention_advance_{self.date}.xlsx"
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'datas': base64.b64encode(content),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def _get_xlsx_content(self):
        self.ensure_one()
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet(self.env._("Retention & Advance"))
        title = workbook.add_format({'bold': True, 'font_size': 14})
        header = workbook.add_format({'bold': True, 'bg_color': '#D9E1F2', 'border': 1, 'text_wrap': True, 'valign': 'top'})
        text = workbook.add_format({'border': 1})
        number = workbook.add_format({'border': 1, 'num_format': '#,##0.00'})
        total_text = workbook.add_format({'bold': True, 'top': 2})
        total_number = workbook.add_format({'bold': True, 'top': 2, 'num_format': '#,##0.00'})

        labels = self.env['retention.advance.report.line'].fields_get(AMOUNT_FIELDS, ['string'])
        columns = [
            self.env._("Company"), self.env._("Customer"), self.env._("Project"), self.env._("Currency"),
            *(labels[fname]['string'] for fname in AMOUNT_FIELDS),
        ]
        sheet.write(0, 0, self.env._("Retention and Advance Report"), title)
        sheet.write(1, 0, self.env._("As at %s", format_date(self.env, self.date)))
        sheet.write_row(3, 0, columns, header)
        sheet.set_row(3, 30)
        sheet.set_column(0, 1, 28)
        sheet.set_column(2, 2, 16)
        sheet.set_column(3, 3, 9)
        sheet.set_column(4, 4 + len(AMOUNT_FIELDS), 16)
        sheet.freeze_panes(4, 3)

        row_index = 4
        for line in self.line_ids:
            sheet.write(row_index, 0, line.company_id.name, text)
            sheet.write(row_index, 1, line.partner_id.display_name, text)
            sheet.write(row_index, 2, line.order_id.name or '', text)
            sheet.write(row_index, 3, line.currency_id.name, text)
            for offset, fname in enumerate(AMOUNT_FIELDS):
                sheet.write_number(row_index, 4 + offset, line[fname], number)
            row_index += 1
        if len(self.line_ids.currency_id) == 1 and self.line_ids:
            sheet.write(row_index, 0, self.env._("Total"), total_text)
            for col in range(1, 4):
                sheet.write_blank(row_index, col, None, total_text)
            for offset, fname in enumerate(AMOUNT_FIELDS):
                sheet.write_number(row_index, 4 + offset, sum(self.line_ids.mapped(fname)), total_number)
        workbook.close()
        return output.getvalue()


class RetentionAdvanceReportLine(models.TransientModel):
    _name = 'retention.advance.report.line'
    _description = "Retention and Advance Report Line"
    _order = 'id'

    report_id = fields.Many2one('retention.advance.report', required=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', string="Company", readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id', string="Currency")
    partner_id = fields.Many2one('res.partner', string="Customer", readonly=True)
    order_id = fields.Many2one('sale.order', string="Project", readonly=True)
    total_invoiced = fields.Monetary(string="Total Invoiced", readonly=True, aggregator='sum')
    retention_held = fields.Monetary(string="Retention Held", readonly=True, aggregator='sum')
    retention_released = fields.Monetary(string="Retention Released", readonly=True, aggregator='sum')
    retention_balance = fields.Monetary(string="Retention Balance", readonly=True, aggregator='sum')
    advance_received = fields.Monetary(string="Advance Received", readonly=True, aggregator='sum')
    advance_recovered = fields.Monetary(string="Advance Recovered", readonly=True, aggregator='sum')
    advance_balance = fields.Monetary(string="Advance Balance", readonly=True, aggregator='sum')
