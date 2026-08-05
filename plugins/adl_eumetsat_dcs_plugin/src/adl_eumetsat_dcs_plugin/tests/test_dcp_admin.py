from types import SimpleNamespace

from django.test import SimpleTestCase

from adl_eumetsat_dcs_plugin.client import (
    DCSWebServiceConnectionError,
    parse_dcp_admin_page,
)
from adl_eumetsat_dcs_plugin.utils import get_registered_dcp_options

# Trimmed from a real DCP_ADMIN response (operator EMI): the summary
# heading, the button tables, the header row, one never-transmitting DCP
# row (empty dates, no checkbox/links) and one active row (checkbox,
# dates, List/Download links).
DCP_ADMIN_FIXTURE = """
<html><body>
<h3 class="contentMiddleHead">
    Operator:
    EMI
</h3>
<form name="dcpAdminForm" method="POST" action="/dcswebservice/dcpAdmin.do">
    <input type="hidden" name="action" value="">
    <table class="summary">
        <tr>
            <td><input type="submit" name="submit" value="Download"></td>
            <td class="contentMiddleText"><input type="button" value="Mark All" name="ALL"/></td>
            <td class="contentMiddleText"><input type="reset" name="reset" value="Reset"></td>
        </tr>
    </table>
    <table><tr><td>
    <table class="contentTable" cellpadding="0" cellspacing="0" border="0">
        <tr><td colspan="17" class="separator"></td></tr>
        <tr>
            <th class="separatorV">&nbsp;</th>
            <th>&nbsp;</th>
            <th class="separatorV">&nbsp;</th>
            <th class="def">DCP ID<br/></th>
            <th class="separatorV">&nbsp;</th>
            <th class="def">Name<br/></th>
            <th class="separatorV">&nbsp;</th>
            <th class="def">Downloaded<br/></th>
            <th class="separatorV">&nbsp;</th>
            <th align="center">Num Messages</th>
            <th class="separatorV">&nbsp;</th>
            <th align="center">Most Recent Message</th>
            <th class="separatorV">&nbsp;</th>
            <th>&nbsp;</th>
            <th class="separatorV">&nbsp;</th>
            <th>&nbsp;</th>
            <th class="separatorVRight">&nbsp;</th>
        </tr>
        <tr><td colspan="17" class="separator"></td></tr>
        <tr>
            <td class="separatorV">&nbsp;</td>
            <td align="center"></td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>1813F452</td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>ET/MAJI</td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap></td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>0</td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap></td>
            <td class="separatorV">&nbsp;</td>
            <td nowrap></td>
            <td class="separatorV">&nbsp;</td>
            <td nowrap></td>
            <td class="separatorVRight">&nbsp;</td>
        </tr>
        <tr><td colspan="17" class="separator"></td></tr>
        <tr>
            <td class="separatorV">&nbsp;</td>
            <td align="center"><input type="checkbox" name="MARKED_FOR_DOWNLOAD" value="188990C0"></td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>188990C0</td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>ET/NEGHELE</td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>05/08/2026 14:36:35</td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>670</td>
            <td class="separatorV">&nbsp;</td>
            <td align="center" nowrap>05/08/2026 15:25:47</td>
            <td class="separatorV">&nbsp;</td>
            <td nowrap><a href="/dcswebservice/dcpAdmin.do?action=ACTION_LIST&amp;id=188990C0">
                List DCPs
            </a></td>
            <td class="separatorV">&nbsp;</td>
            <td nowrap><a href="/dcswebservice/dcpAdmin.do?action=ACTION_DOWNLOAD&amp;id=188990C0">
                Download DCPs
            </a></td>
            <td class="separatorVRight">&nbsp;</td>
        </tr>
        <tr><td colspan="17" class="separator"></td></tr>
    </table>
    </td></tr></table>
</form>
</body></html>
"""


class ParseDcpAdminPageTests(SimpleTestCase):

    def test_parses_operator_and_dcps(self):
        result = parse_dcp_admin_page(DCP_ADMIN_FIXTURE)

        self.assertEqual(result["operator"], "EMI")
        self.assertEqual(len(result["dcps"]), 2)

        maji, neghele = result["dcps"]
        self.assertEqual(maji, {
            "dcp_id": "1813F452",
            "name": "ET/MAJI",
            "downloaded_date": None,
            "num_messages": 0,
            "most_recent_message": None,
        })
        self.assertEqual(neghele, {
            "dcp_id": "188990C0",
            "name": "ET/NEGHELE",
            "downloaded_date": "05/08/2026 14:36:35",
            "num_messages": 670,
            "most_recent_message": "05/08/2026 15:25:47",
        })

    def test_missing_table_raises(self):
        # e.g. the session bounced to a login page
        with self.assertRaises(DCSWebServiceConnectionError):
            parse_dcp_admin_page("<html><body><form>login</form></body></html>")


class RegisteredDcpOptionsTests(SimpleTestCase):

    def test_options_annotated_and_sorted(self):
        roster = parse_dcp_admin_page(DCP_ADMIN_FIXTURE)
        client = SimpleNamespace(list_registered_dcps=lambda: roster)
        connection = SimpleNamespace(get_api_client=lambda: client)

        options = get_registered_dcp_options(connection)

        self.assertEqual(options, [
            {"label": "1813F452 — ET/MAJI (no messages)", "value": "1813F452"},
            {"label": "188990C0 — ET/NEGHELE (670 msgs)", "value": "188990C0"},
        ])
