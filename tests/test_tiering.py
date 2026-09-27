"""Fixtures are byte copies from core4 9ef92a235 shared-protocol/fixtures/.
Expected results are upstream data, including pre/post-activation differences.
"""
import importlib
import json
from pathlib import Path

import pytest

from app import bands, tiering

VECTORS = json.loads((Path(__file__).parent / 'fixtures/reference_tier_vectors_v3.json').read_text())
CASES = [
    ([], 'good'), (['good'], 'good'), (['degraded'], 'degraded'), (['outlier'], 'outlier'),
    (['good','degraded'], 'degraded'), (['degraded','degraded'], 'degraded'),
    (['good','outlier'], 'degraded'), (['degraded','outlier'], 'degraded'), (['outlier','outlier'], 'outlier'),
    (['good','good','good'], 'good'), (['good','good','degraded'], 'good'),
    (['good','degraded','degraded'], 'degraded'), (['good','good','outlier'], 'degraded'),
    (['good','degraded','outlier'], 'degraded'), (['degraded','degraded','outlier'], 'degraded'),
    (['good','outlier','outlier'], 'outlier'), (['outlier','outlier','outlier'], 'outlier'),
    (['good','good','good','outlier'], 'degraded'), (['good','good','outlier','outlier'], 'outlier'),
]


@pytest.mark.parametrize('tiers,expected', CASES)
def test_consensus_fold_truth_table_and_retained_worst_branch(tiers, expected):
    assert tiering.fold_variable_tiers(tiers) == expected
    assert tiering.fold_variable_tiers(list(reversed(tiers))) == expected
    rank = {'good': 0, 'degraded': 1, 'outlier': 2}
    assert tiering.fold_variable_tiers(tiers, 'worst') == max(tiers, key=rank.get, default='good')


@pytest.mark.parametrize('vector', VECTORS['vectors'], ids=lambda v: v['name'])
def test_deployed_golden_vectors_pre_and_post_activation(vector):
    assert VECTORS['bands_version'] == bands.BANDS_VERSION
    for height in [99, 100, 101]:
        active = tiering.gross_guard_active_at(height, 100)
        expected = vector.get('expected_tier_gross_guard', vector['expected_tier']) if active else vector['expected_tier']
        assert tiering.tier_residuals(vector['residuals'], gross_guard_active=active) == expected


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_known_residual_is_outlier_but_not_gross(value):
    residuals = [{'source':'nws_station','variable':'temperature_c','residual':value},
                 {'source':'nws_station','variable':'humidity_pct','residual':1}]
    assert tiering.variable_tier('nws_station','temperature_c',value) == 'outlier'
    assert tiering.tier_residuals(residuals, gross_guard_active=True) == 'degraded'


def test_unknown_pairs_and_unscheduled_activation():
    assert tiering.variable_tier('missing','temperature_c',999) is None
    assert tiering.variable_tier('hrrr','missing',999) is None
    assert not tiering.gross_guard_active_at(2**64 - 1, None)
    assert tiering.gross_guard_active_at(0, 0)


def test_wrong_fixture_version_fails_at_import(monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(bands, 'BANDS_VERSION', 1)
        with pytest.raises(RuntimeError, match='version/fold'):
            importlib.reload(tiering)
    importlib.reload(tiering)
