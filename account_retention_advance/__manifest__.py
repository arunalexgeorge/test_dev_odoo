{
    'name': 'Retention and Advance Recovery on Invoices',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'Withhold retention and recover customer advances on progress invoices',
    'description': """
Retention and Advance Recovery on Customer Invoices
===================================================
For contracting companies that bill monthly progress invoices:

* Retention (a % of the work done) is booked on a separate Retention Receivable
  account with its own due date (the expected handover date).
* Part of a customer advance payment is recovered on each invoice and matched
  automatically against that payment.
* Retention release wizard, credit note handling, per project report (Excel).
""",
    'author': 'Arun A George',
    'license': 'LGPL-3',
    'depends': ['account', 'sale'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/account_move_views.xml',
        'views/account_payment_views.xml',
        'views/sale_order_views.xml',
        'wizard/retention_release_wizard_views.xml',
        'wizard/retention_advance_report_views.xml',
        'views/menus.xml',
        'report/report_invoice.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
}
