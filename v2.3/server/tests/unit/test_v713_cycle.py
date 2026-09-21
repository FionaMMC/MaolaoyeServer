import pytest
from scripts.v713_cycle import plan_cycle


def test_month_end_uses_next_session_month():
    dates = ['20260731','20260828','20260831','20260901','20260902']
    assert plan_cycle('20260831',dates,'20260731') == {
        'status':'DUE','market_date':'20260831','decision_date':'20260901','as_of_date':'20260831'}
    assert plan_cycle('20260901',dates,'20260831')['status'] == 'CURRENT'


def test_holiday_month_end_uses_confirmed_calendar_not_weekdays():
    result = plan_cycle('20260930',['20260831','20260929','20260930','20261009'])
    assert result['decision_date'] == '20261009' and result['as_of_date'] == '20260930'
    assert plan_cycle('20261001',['20260930','20261009']) == {'status':'CLOSED'}
    with pytest.raises(ValueError,match='confirmed next'):
        plan_cycle('20260930',['20260831','20260930'])


def test_exchange_calendar_uses_standard_library_transport(monkeypatch):
    import io
    import json
    from scripts.v713_cycle import exchange_dates
    monkeypatch.setenv('TUSHARE_TOKEN', 'calendar-test-token')
    def respond(request, timeout):
        assert request.full_url == 'https://api.tushare.pro'
        assert request.method == 'POST' and timeout == 30
        body = json.loads(request.data)
        assert body['api_name'] == 'trade_cal'
        assert body['params']['is_open'] == '1'
        assert body['token'] == 'calendar-test-token'
        return io.BytesIO(json.dumps({'code':0,'data':{'items':[['20260921'],['20260922']]}}).encode())
    monkeypatch.setattr('scripts.v713_cycle.urllib.request.urlopen', respond)
    assert exchange_dates('20260921') == ['20260921','20260922']
