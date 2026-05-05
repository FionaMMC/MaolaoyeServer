"""
数据加载层 — 从 data/ 目录读取所有必要的 parquet 文件
"""
from pathlib import Path
from typing import Tuple
import pandas as pd


class DataLoader:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.required_files = [
            'pred_csi1000.parquet',
            'v12_exp_hs300.parquet',
            'stock_close.parquet',
            'stock_returns.parquet',
            'index_csi1000.parquet',
        ]

    def verify(self) -> bool:
        """检查所有必需文件是否存在"""
        missing = []
        for f in self.required_files:
            if not (self.data_dir / f).exists():
                missing.append(f)
        if missing:
            raise FileNotFoundError(f"缺少数据文件: {missing}")
        return True

    def load_all(self) -> dict:
        """加载所有数据, 返回字典"""
        self.verify()

        pred = pd.read_parquet(self.data_dir / 'pred_csi1000.parquet')
        if not pd.api.types.is_datetime64_any_dtype(pred['date']):
            pred['date'] = pd.to_datetime(pred['date'])

        v12 = pd.read_parquet(self.data_dir / 'v12_exp_hs300.parquet').squeeze()
        v12.index = pd.to_datetime(v12.index)

        close_df = pd.read_parquet(self.data_dir / 'stock_close.parquet')
        close_df.index = pd.to_datetime(close_df.index)

        rets_df = pd.read_parquet(self.data_dir / 'stock_returns.parquet')
        rets_df.index = pd.to_datetime(rets_df.index)

        idx_df = pd.read_parquet(self.data_dir / 'index_csi1000.parquet')
        idx_df.index = pd.to_datetime(idx_df.index)

        return {
            'pred': pred,
            'v12': v12,
            'close_df': close_df,
            'rets_df': rets_df,
            'idx': idx_df,
        }

    def info(self) -> dict:
        """数据概况"""
        data = self.load_all()
        return {
            'pred': {
                'rows': len(data['pred']),
                'dates': data['pred']['date'].nunique(),
                'codes': data['pred']['code'].nunique(),
                'date_range': (str(data['pred']['date'].min().date()),
                               str(data['pred']['date'].max().date())),
            },
            'v12': {
                'days': len(data['v12']),
                'mean': float(data['v12'].mean()),
                'range': (float(data['v12'].min()), float(data['v12'].max())),
            },
            'close_df': {'shape': data['close_df'].shape},
            'idx': {
                'days': len(data['idx']),
                'date_range': (str(data['idx'].index.min().date()),
                               str(data['idx'].index.max().date())),
            },
        }
