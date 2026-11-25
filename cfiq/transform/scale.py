"""Normalization of numeric model features.

DMDW topic: data transformation - normalization.
  min-max : x' = (x - min) / (max - min)        -> range [0, 1]
  z-score : x' = (x - mean) / std               -> mean 0, std 1

fit_scaler() learns the parameters on TRAINING data only; apply_scaler() reuses
them on test data. Fitting on all data would leak test statistics into training.
"""

import numpy as np
import pandas as pd


def fit_scaler(df: pd.DataFrame, columns: list[str], method: str = "zscore") -> dict:
    params = {"method": method, "columns": list(columns)}
    if method == "minmax":
        params["a"] = df[columns].min()
        params["b"] = df[columns].max() - df[columns].min()
    elif method == "zscore":
        params["a"] = df[columns].mean()
        params["b"] = df[columns].std(ddof=0)
    else:
        raise ValueError(f"unknown scaling method: {method!r}")
    params["b"] = params["b"].replace(0, 1.0)  # constant column: avoid division by zero
    return params


def apply_scaler(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    out = df.copy()
    cols = params["columns"]
    out[cols] = (df[cols] - params["a"]) / params["b"]
    return out


def minmax_scale(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return apply_scaler(df, fit_scaler(df, columns, "minmax"))


def zscore_scale(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return apply_scaler(df, fit_scaler(df, columns, "zscore"))


__all__ = ["fit_scaler", "apply_scaler", "minmax_scale", "zscore_scale", "np"]
