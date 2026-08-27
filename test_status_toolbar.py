"""Exercise the compact Status toolbar helpers in the rendered JavaScript."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


DRIVER = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const region=js.slice(js.indexOf('function _fmtResetTime'),js.indexOf('async function refreshUsageLimits'));
const elements={
  'nav-usage-cap-summary':{title:''},
  'nav-usage-cap-value':{textContent:'',className:''},
};
const context=vm.createContext({
  Date,Math,Number,
  document:{getElementById:id=>elements[id]||null},
});
vm.runInContext(region,context);

const shortWindow={label:'5h',duration_minutes:300,utilization:61};
const capWindow={label:'7d',duration_minutes:10080,utilization:19};
const selected=context._selectUsageCapWindow([shortWindow,capWindow]);
context._setUsageCapSummary(selected);
const normal={
  label:selected.label,
  text:elements['nav-usage-cap-value'].textContent,
  className:elements['nav-usage-cap-value'].className,
  title:elements['nav-usage-cap-summary'].title,
};

context._setUsageCapSummary({...capWindow,utilization:92});
const critical={
  text:elements['nav-usage-cap-value'].textContent,
  className:elements['nav-usage-cap-value'].className,
};

context._setUsageCapSummary(null);
const unavailable={
  text:elements['nav-usage-cap-value'].textContent,
  className:elements['nav-usage-cap-value'].className,
  title:elements['nav-usage-cap-summary'].title,
};

process.stdout.write(JSON.stringify({normal,critical,unavailable}));
"""


def test_usage_cap_summary_uses_the_longest_plan_window():
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(APP)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["normal"] == {
        "label": "7d",
        "text": "19%",
        "className": "stat-val",
        "title": "Codex 7d cap · 19% used",
    }
    assert state["critical"] == {
        "text": "92%",
        "className": "stat-val crit",
    }
    assert state["unavailable"] == {
        "text": "-",
        "className": "stat-val",
        "title": "Codex plan usage cap unavailable",
    }
