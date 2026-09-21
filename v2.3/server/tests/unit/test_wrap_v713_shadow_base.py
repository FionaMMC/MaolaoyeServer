import pandas as pd
import pytest
from plugins.v713_relay import basket_hash
from scripts.wrap_v713_shadow_base import wrap
from app.services.shadow_ledger import ShadowLedgerService, validate_shadow_sidecar


def test_base_wrap_has_exact_executable_weights_and_provenance(tmp_path):
    frame = pd.DataFrame({'code':['000001.SZ','511260.SH'],'weight':[0.37,0.63],
        'strategy_version':'v7.13-base','sleeve':'T1_5050',
        'decision_date':'20260901','as_of_date':'20260831'})
    frame['basket_sha256'] = basket_hash(frame)
    source = tmp_path/'v713_target_latest.parquet'
    frame.to_parquet(source,index=False)
    manifest = wrap(tmp_path,'audited-source')
    shadow = pd.read_parquet(tmp_path/'Shadow_Base_latest.parquet')
    assert shadow.set_index('code').weight.to_dict() == frame.set_index('code').weight.to_dict()
    assert manifest['input_hash'] == frame.basket_sha256.iloc[0]
    target,_ = ShadowLedgerService(None,None,tmp_path/'strategies.yaml').validate_target(shadow,'Shadow_Base',20260901)
    validate_shadow_sidecar(tmp_path/'Shadow_Base_latest.json',target)
    frame.loc[0,'weight'] += .01
    frame.to_parquet(source,index=False)
    with pytest.raises(ValueError):
        wrap(tmp_path,'audited-source')
