import importlib.util,json,sys,tempfile
from pathlib import Path
from contextlib import nullcontext
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).resolve().parents[1]
def notify_module():
 spec=importlib.util.spec_from_file_location('upgrade_notify',ROOT/'windows/hydra_oms_notify.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_v2_notice_sees_child_guard_failure():
 m=notify_module();message,alert=m.summarize('buy','20990105',0,{'status':'EXECUTED','plans':[{'instance_id':'hydra','status':'EXECUTED','orders':[{'client_order_id':'TEST','status':'GUARD_REJECTED','quantity':100}]}]})
 assert alert and 'GUARD_REJECTED' in message and 'TEST' in message

def test_notify_failure_preserves_child_exit(monkeypatch,capsys):
 m=notify_module();monkeypatch.setattr(m,'run_child',lambda args:(7,{'status':'FAILED'}))
 monkeypatch.setattr(m,'notify',lambda *args:(_ for _ in ()).throw(RuntimeError('secret must not leak')))
 monkeypatch.setattr(sys,'argv',['notify','--command','buy','--date','20990105','--','fake'])
 assert m.main()==7
 assert 'secret must not leak' not in capsys.readouterr().out

def test_intraday_busy_does_not_send_or_query(monkeypatch,tmp_path):
 m=notify_module();monkeypatch.setattr(m,'send',lambda *a:pytest.fail('must stay silent'));monkeypatch.setattr(m,'status_view',lambda:pytest.fail('must not query'))
 m.notify('intraday','20990105',0,{'status':'SKIPPED_ACCOUNT_BUSY'},tmp_path)
 assert not list(tmp_path.iterdir())

def test_original_notification_result_fields_retained():
 m=notify_module();message,alert=m.summarize('upload-spool','20990105',0,{'uploaded':['x'],'failed':None})
 assert not alert and '1' in message

def test_shared_install_gate_complete(tmp_path):
 from live_client.oms_offline_acceptance import run_acceptance
 result=run_acceptance(tmp_path)
 assert result['passed'] and not result['qmt_contacted'] and not result['server_contacted']

def test_main_uses_one_calibrated_clock_for_agent_and_journal(monkeypatch,tmp_path):
 import live_client.oms_agent as main
 import live_client.config as config
 import live_client.execution_queue as queue
 import live_client.gateway as gateway
 import live_client.http_client as http
 import live_client.oms_journal as journal
 import live_client.oms_clock as clock
 from live_client.offline_acceptance import _config
 cfg=_config(tmp_path,'synthetic');monkeypatch.setattr(config.LiveClientConfig,'from_env',classmethod(lambda cls:cfg))
 monkeypatch.setattr(type(cfg),'validate_startup',lambda self:None);cfg.log_dir.mkdir(parents=True,exist_ok=True)
 instant=datetime(2099,1,5,9,15,5,tzinfo=timezone(timedelta(hours=8)));seen={}
 class Clock:
  def __init__(self,**kw):seen['clock_created']=True
  def now(self):return instant
 class Journal:
  def __init__(self,path,clock):seen['journal_clock']=clock()
  def bind(self,**kw):seen['bound']=True
 class Gateway:
  def __init__(self,cfg):pass
  def connect(self):seen['connected']=True
  def close(self):seen['closed']=True
 class Agent:
  def __init__(self,**kw):seen['agent_clock']=kw['clock']()
  def pre(self,date):return {'synthetic':True}
 monkeypatch.setattr(clock,'RuntimeClock',Clock);monkeypatch.setattr(journal,'OmsJournal',Journal);monkeypatch.setattr(gateway,'XtQMTGateway',Gateway)
 monkeypatch.setattr(http,'LiveServerClient',lambda *a,**kw:object());monkeypatch.setattr(queue,'account_submission_lock',lambda *a,**kw:nullcontext());monkeypatch.setattr(main,'OmsAgent',Agent)
 assert main.main(['pre','--date','20990105'])==0
 assert seen['journal_clock']==seen['agent_clock']==instant and seen['closed'] and seen['bound']

def test_clock_failure_prevents_qmt_connection(monkeypatch,tmp_path):
 import live_client.oms_agent as main
 import live_client.config as config
 import live_client.execution_queue as queue
 import live_client.gateway as gateway
 import live_client.http_client as http
 import live_client.oms_clock as clock
 from live_client.offline_acceptance import _config
 cfg=_config(tmp_path,'synthetic');monkeypatch.setattr(config.LiveClientConfig,'from_env',classmethod(lambda cls:cfg));monkeypatch.setattr(type(cfg),'validate_startup',lambda self:None);cfg.log_dir.mkdir(parents=True,exist_ok=True)
 monkeypatch.setattr(http,'LiveServerClient',lambda *a,**kw:object());monkeypatch.setattr(queue,'account_submission_lock',lambda *a,**kw:nullcontext())
 monkeypatch.setattr(clock,'RuntimeClock',lambda **kw:(_ for _ in ()).throw(RuntimeError('time unavailable')))
 monkeypatch.setattr(gateway,'XtQMTGateway',lambda *a:pytest.fail('QMT must not be constructed'))
 with pytest.raises(RuntimeError,match='time unavailable'):main.main(['pre','--date','20990105'])
