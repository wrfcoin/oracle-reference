import copy
import io
import json
import runpy
from dataclasses import replace
from pathlib import Path

import pytest

from app.services import forecast
from app import config

SUBSET = json.loads((Path(__file__).parent / 'fixtures/forecast_subset_v1.json').read_text())
QUERY = {
    'source': 'gefs_fx', 'cycle': '2026-01-01T00:00:00Z',
    'points': [{'lat': 0.0, 'lon': 1.0}], 'lead_hours': [6, 12],
    'variables': ['temperature_c'],
}


def test_fresh_configuration_defaults_live_fetch_off(client, monkeypatch):
    monkeypatch.delenv('FORECAST_LIVE_FETCH', raising=False)
    # Defaults are evaluated at import time; load the actual configuration
    # afresh without altering the module already used by the application.
    fresh = runpy.run_path(config.__file__)
    assert fresh['Settings']().forecast_live_fetch is False
    assert fresh['settings'].forecast_live_fetch is False
    monkeypatch.setattr(forecast, 'settings', replace(fresh['settings'],
        forecast_subset_dir=None, forecast_subset_url='https://example.invalid/subsets'))
    def forbidden_transport(*args, **kwargs):
        pytest.fail('default configuration attempted a remote fetch')
    monkeypatch.setattr(forecast, 'urlopen', forbidden_transport)
    assert client.post('/v1/forecast', json=QUERY).status_code == 503


@pytest.mark.parametrize('flag,expected', [('0', False), ('1', True), ('true', False)])
def test_fresh_configuration_requires_explicit_live_fetch_opt_in(monkeypatch, flag, expected):
    monkeypatch.setenv('FORECAST_LIVE_FETCH', flag)
    fresh = runpy.run_path(config.__file__)
    assert fresh['Settings']().forecast_live_fetch is expected
    assert fresh['settings'].forecast_live_fetch is expected


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(forecast, 'settings', replace(forecast.settings,
        forecast_subset_dir=str(tmp_path), forecast_live_fetch=False, forecast_subset_url=None))
    def write(data):
        path = tmp_path / data['source'] / '2026010100.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data))
        return path
    write(SUBSET)
    return write


def test_unconfigured_forecast_archive_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(forecast, 'settings', replace(forecast.settings,
        forecast_subset_dir=None, forecast_live_fetch=False, forecast_subset_url=None))
    response = client.post('/v1/forecast', json=QUERY)
    assert response.status_code == 503


def test_gefs_lead_member_matrix_known_answers(client, archive):
    response = client.post('/v1/forecast', json=QUERY)
    assert response.status_code == 200
    body = response.json()
    assert body['source'] == 'gefs_fx'
    assert body['cycle'] == '2026-01-01T00:00:00Z'
    leads = body['results'][0]['forecasts']
    assert [row['lead_hours'] for row in leads] == [6, 12]
    assert [row['valid_time'] for row in leads] == ['2026-01-01T06:00:00Z', '2026-01-01T12:00:00Z']
    assert [member['member'] for member in leads[0]['members']] == ['control', 'p01', 'p02']
    assert [[m['values']['temperature_c'] for m in lead['members']] for lead in leads] == [[13, 14, 12], [23, 24, 22]]


def test_explicit_order_and_points(client, archive):
    query = {**QUERY, 'lead_hours': [12, 6], 'members': ['p02', 'control'],
             'points': [{'lat': 0, 'lon': 1}, {'lat': -1, 'lon': 0}]}
    body = client.post('/v1/forecast', json=query).json()
    results = body['results']
    assert len(results) == 2
    assert results[0]['forecasts'][0]['members'][0]['values']['temperature_c'] == 22
    assert results[1]['forecasts'][1]['members'][0]['values']['temperature_c'] == 9
    assert [m['member'] for m in results[0]['forecasts'][0]['members']] == ['p02', 'control']


def test_longitude_normalization(client, archive):
    data = {**SUBSET, 'lons': [-2, 0]}
    archive(data)
    for lon in [-1, 359]:
        query = {**QUERY, 'points': [{'lat': 0, 'lon': lon}]}
        response = client.post('/v1/forecast', json=query)
        assert response.status_code == 200
        assert response.json()['results'][0]['forecasts'][0]['members'][0]['values']['temperature_c'] == 13


def test_gfs_single_control_and_exact_cycle_no_latest_fallback(client, archive):
    data = copy.deepcopy(SUBSET)
    data['source'], data['members'] = 'gfs_fx', ['control']
    data['variables']['temperature_c'] = [[lead[0]] for lead in data['variables']['temperature_c']]
    archive(data)
    query = {**QUERY, 'source': 'gfs_fx'}
    response = client.post('/v1/forecast', json=query)
    assert response.status_code == 200
    assert len(response.json()['results'][0]['forecasts'][0]['members']) == 1
    assert client.post('/v1/forecast', json={**query, 'cycle': '2026-01-02T00:00:00Z'}).status_code == 503


@pytest.mark.parametrize('change', [
    {'source': 'hrrr'}, {'cycle': '2026-01-01T01:00:00Z'}, {'cycle': '2026-01-01T00:00:00'},
    {'lead_hours': [-1]}, {'lead_hours': [385]}, {'lead_hours': [True]}, {'lead_hours': [6, 6]},
    {'variables': ['not_a_variable']}, {'members': ['control', 'control']}, {'points': []},
])
def test_request_validation(client, archive, change):
    assert client.post('/v1/forecast', json={**QUERY, **change}).status_code == 422


@pytest.mark.parametrize('change', [
    {'lead_hours': [3]}, {'members': ['p99']}, {'variables': ['pressure_hpa']},
    {'points': [{'lat': 2, 'lon': 1}]},
])
def test_missing_axes_or_outside_tile_are_refused(client, archive, change):
    assert client.post('/v1/forecast', json={**QUERY, **change}).status_code == 422


@pytest.mark.parametrize('change', [
    {'cycle': '2026-01-02T00:00:00Z'}, {'version': 2}, {'lats': [1, -1]},
    {'members': ['control', 'control', 'p02']}, {'lead_hours': [6]},
    {'variables': {'temperature_c': [[1]]}}, {'variables': {'temperature_c': [[float('nan')]]}},
])
def test_malformed_archive_is_sanitized(client, archive, change):
    data = {**SUBSET, **change}
    archive(data)
    response = client.post('/v1/forecast', json=QUERY)
    assert response.status_code == 502
    assert 'malformed' in response.json()['detail']


def test_local_subset_byte_limit(client, archive, monkeypatch):
    monkeypatch.setattr(forecast, 'settings', replace(forecast.settings, forecast_max_bytes=100))
    response = client.post('/v1/forecast', json=QUERY)
    assert response.status_code == 502
    assert 'byte limit' in response.json()['detail']


def test_remote_subset_opt_in_and_limit_plus_one_read(client, monkeypatch):
    calls = []
    raw = json.dumps(SUBSET).encode()
    class Stream(io.BytesIO):
        def read(self, size=-1):
            calls.append(size)
            return super().read(size)
    def transport(url, timeout):
        assert url == 'https://example.invalid/subsets/gefs_fx/2026010100.json'
        return Stream(raw)
    monkeypatch.setattr(forecast, 'urlopen', transport)
    config = replace(forecast.settings, forecast_subset_dir=None,
                     forecast_subset_url='https://example.invalid/subsets', forecast_live_fetch=False)
    monkeypatch.setattr(forecast, 'settings', config)
    assert client.post('/v1/forecast', json=QUERY).status_code == 503
    assert not calls
    monkeypatch.setattr(forecast, 'settings', replace(config, forecast_live_fetch=True, forecast_max_bytes=len(raw)))
    assert client.post('/v1/forecast', json=QUERY).status_code == 200
    assert calls == [len(raw) + 1]
    monkeypatch.setattr(forecast, 'settings', replace(config, forecast_live_fetch=True, forecast_max_bytes=len(raw) - 1))
    assert client.post('/v1/forecast', json=QUERY).status_code == 502


def test_point_value_budget_refuses_large_matrix(client, archive):
    data = copy.deepcopy(SUBSET)
    data['members'] = [f'p{i:02}' for i in range(31)]
    grid = [[1, 1], [1, 1]]
    data['variables']['temperature_c'] = [[grid] * 31] * 2
    archive(data)
    query = {**QUERY, 'points': QUERY['points'] * 100}
    # 100*2*31 < 10000, still bounded and accepted.
    assert client.post('/v1/forecast', json=query).status_code == 200
    data['variables']['humidity_pct'] = data['variables']['temperature_c']
    archive(data)
    query['variables'] = ['temperature_c', 'humidity_pct']
    assert client.post('/v1/forecast', json=query).status_code == 422
