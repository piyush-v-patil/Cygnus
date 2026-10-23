"""Checkboxes and checkbox groups end up exactly as intended, including unticking."""
import asyncio
from pathlib import Path

import pytest

from autoapply.browser.fields import enumerate_fields, fill_field

from .conftest import CHROMIUM

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="needs Playwright Chromium")]

PAGE = """<form>
<div class="field"><label for="sp">I will require visa sponsorship</label><input type="checkbox" id="sp" name="sp" checked></div>
<fieldset><legend>Which locations?</legend>
  <label><input type="checkbox" name="loc" value="NYC" checked> NYC</label>
  <label><input type="checkbox" name="loc" value="SF"> SF</label>
  <label><input type="checkbox" name="loc" value="Austin" checked> Austin</label></fieldset>
<fieldset><legend>Relocate?</legend>
  <label><input type="radio" name="rel" value="Yes" checked> Yes</label>
  <label><input type="radio" name="rel" value="No"> No</label></fieldset>
</form>"""


def test_untick_and_exact_groups():
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROMIUM)
            page = await b.new_page()
            await page.set_content(PAGE)
            fields = {f.kind: f for f in await enumerate_fields(page)}
            await fill_field(page, fields["checkbox"], "No")
            await fill_field(page, fields["checkbox_group"], "SF")
            await fill_field(page, fields["radio"], "No")
            out = await page.evaluate("""() => ({sp: document.getElementById('sp').checked,
                loc: [...document.querySelectorAll('input[name=loc]:checked')].map(i => i.value),
                rel: document.querySelector('input[name=rel]:checked').value})""")
            await b.close()
            return out
    assert asyncio.run(go()) == {"sp": False, "loc": ["SF"], "rel": "No"}
