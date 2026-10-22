from . import models
from . import wizard


def post_init_hook(env):
    """Give every company that already has a chart of accounts a Retention Receivable account."""
    env['res.company'].search([('chart_template', '!=', False)])._ra_setup_retention_defaults()
