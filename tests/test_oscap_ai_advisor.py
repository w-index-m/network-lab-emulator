"""
tools/oscap_ai_advisor.py テスト

OpenSCAPのresults.xml(fail項目)とDataStream(title/description/rationale)、
公式remediationスクリプト(bash)を突き合わせて日本語アドバイスを生成する
処理を、最小のサンプルXML/bashで検証する。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.oscap_ai_advisor import (
    load_fail_rule_ids, load_rule_metadata, load_fix_snippets,
    build_findings, _template_advice, Finding,
)

_RESULTS_XML = """<?xml version="1.0"?>
<Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2" id="xccdf_test_benchmark">
  <TestResult id="xccdf_test_result">
    <rule-result idref="xccdf_org.ssgproject.content_rule_package_aide_installed" severity="medium">
      <result>fail</result>
    </rule-result>
    <rule-result idref="xccdf_org.ssgproject.content_rule_package_prelink_removed" severity="low">
      <result>pass</result>
    </rule-result>
    <rule-result idref="xccdf_org.ssgproject.content_rule_partition_for_tmp" severity="low">
      <result>fail</result>
    </rule-result>
  </TestResult>
</Benchmark>
"""

_DATASTREAM_XML = """<?xml version="1.0"?>
<Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2" id="xccdf_test_ds">
  <Rule id="xccdf_org.ssgproject.content_rule_package_aide_installed">
    <title>Install AIDE</title>
    <description>AIDE is a file integrity checker.</description>
    <rationale>File integrity checking helps detect unauthorized changes.</rationale>
  </Rule>
  <Rule id="xccdf_org.ssgproject.content_rule_partition_for_tmp">
    <title>Ensure /tmp Located On Separate Partition</title>
    <description>The /tmp partition should be separate.</description>
    <rationale>Isolating /tmp allows restrictive mount options.</rationale>
  </Rule>
</Benchmark>
"""

_FIX_SCRIPT = """
###############################################################################
# BEGIN fix (1 / 2) for 'xccdf_org.ssgproject.content_rule_package_aide_installed'
###############################################################################
(>&2 echo "Remediating rule 1/2: 'xccdf_org.ssgproject.content_rule_package_aide_installed'")
apt-get install -y aide
# END fix for 'xccdf_org.ssgproject.content_rule_package_aide_installed'
"""


@pytest.fixture
def results_path(tmp_path):
    p = tmp_path / 'results.xml'
    p.write_text(_RESULTS_XML, encoding='utf-8')
    return str(p)


@pytest.fixture
def datastream_path(tmp_path):
    p = tmp_path / 'ds.xml'
    p.write_text(_DATASTREAM_XML, encoding='utf-8')
    return str(p)


@pytest.fixture
def fix_script_path(tmp_path):
    p = tmp_path / 'fix.sh'
    p.write_text(_FIX_SCRIPT, encoding='utf-8')
    return str(p)


def test_load_fail_rule_ids_only_returns_fail(results_path):
    fails = load_fail_rule_ids(results_path)
    ids = {rid for rid, _ in fails}
    assert 'xccdf_org.ssgproject.content_rule_package_aide_installed' in ids
    assert 'xccdf_org.ssgproject.content_rule_partition_for_tmp' in ids
    # passのルールは含まれない
    assert 'xccdf_org.ssgproject.content_rule_package_prelink_removed' not in ids
    assert len(fails) == 2


def test_load_rule_metadata_extracts_title_and_rationale(datastream_path):
    meta = load_rule_metadata(datastream_path, {'xccdf_org.ssgproject.content_rule_package_aide_installed'})
    m = meta['xccdf_org.ssgproject.content_rule_package_aide_installed']
    assert m['title'] == 'Install AIDE'
    assert 'unauthorized changes' in m['rationale']


def test_load_fix_snippets_extracts_matching_block(fix_script_path):
    snippets = load_fix_snippets(fix_script_path, {'xccdf_org.ssgproject.content_rule_package_aide_installed'})
    assert 'apt-get install -y aide' in snippets['xccdf_org.ssgproject.content_rule_package_aide_installed']


def test_load_fix_snippets_missing_rule_returns_empty(fix_script_path):
    snippets = load_fix_snippets(fix_script_path, {'xccdf_org.ssgproject.content_rule_partition_for_tmp'})
    assert 'xccdf_org.ssgproject.content_rule_partition_for_tmp' not in snippets


def test_load_fix_snippets_no_path_returns_empty_dict():
    assert load_fix_snippets(None, {'anything'}) == {}
    assert load_fix_snippets('/nonexistent/path.sh', {'anything'}) == {}


def test_build_findings_combines_results_metadata_and_fix(results_path, datastream_path, fix_script_path):
    findings = build_findings(results_path, datastream_path, fix_script_path)
    assert len(findings) == 2
    aide = next(f for f in findings if f.rule_id == 'xccdf_org.ssgproject.content_rule_package_aide_installed')
    assert aide.title == 'Install AIDE'
    assert aide.severity == 'medium'
    assert 'apt-get install -y aide' in aide.fix_snippet

    tmp_finding = next(f for f in findings if f.rule_id == 'xccdf_org.ssgproject.content_rule_partition_for_tmp')
    assert tmp_finding.fix_snippet == ''  # このルールのfixはスクリプトに存在しない


def test_template_advice_includes_title_severity_rationale_and_fix():
    f = Finding(
        rule_id='xccdf_test_rule', title='Test Rule', severity='high',
        rationale='It matters because of X.', fix_snippet='echo fix-it',
    )
    advice = _template_advice(f)
    assert 'Test Rule' in advice
    assert '高' in advice  # severity=highの日本語表記
    assert 'It matters because of X.' in advice
    assert 'echo fix-it' in advice


def test_template_advice_without_fix_snippet_says_manual_check_needed():
    f = Finding(rule_id='xccdf_test_rule', title='Test Rule', severity='low')
    advice = _template_advice(f)
    assert '手動確認が必要です' in advice
