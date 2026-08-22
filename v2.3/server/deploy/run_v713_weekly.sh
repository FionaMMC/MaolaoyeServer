#!/usr/bin/env bash
# Refresh V7.13_Base and its four small-cap shadow targets on the former V79 slot.
set -euo pipefail

cd /opt/qmt-refresh
set -a
source ./.env
set +a

PY=/opt/qmt-refresh/venv/bin/python
SERVER_PY=/opt/qmt-server/venv/bin/python
# Strategy commit 88c2cb1 plus the audited Tushare ROE PIT null-safety patch.
# The release manifest records the exact common.py/data-refresh hashes.
PEER=/opt/qmt-refresh/releases/small_cap_peer-88c2cb1-roe-pit-nullfix-20260809
HYDRA=/opt/qmt-refresh/releases/permenant_portfolio-66985a1
SERVER=/opt/qmt-server/v2.3/server
HYDRA_TARGET="$HYDRA/v48/output_forward_rate_bond/v713_hydra_latest.parquet"
SOURCE_COMMIT=88c2cb1050c7391ce84a9d524a9884dfefaf3ef4

mkdir -p logs tmp
# Must remain absolute: the workflow later changes cwd to PEER and SERVER.
LOG="/opt/qmt-refresh/logs/v713_weekly_$(date +%Y%m%d_%H%M).log"
exec 9>/opt/qmt-refresh/tmp/heavy_job.lock
flock -w 18000 9 || {
  printf "waited 5h for heavy_job.lock\n" >>"$LOG"
  exit 1
}

notify() {
  "$PY" /opt/qmt-refresh/bin/wecom_notify.py \
    --level "$1" --title "$2" --text "$3" \
    >>"$LOG" 2>&1 || true
}

fail() {
  printf "FAIL: %s\n" "$1" >>"$LOG"
  notify fail "V7.13 target refresh failed" "$1"
  exit 1
}

{
  printf "=== V7.13 weekly start %s ===\n" "$(date)"
  CALENDAR_TODAY="$(date +%Y%m%d)"
  TODAY="${V713_DECISION_DATE:-$CALENDAR_TODAY}"
  [[ "$TODAY" =~ ^[0-9]{8}$ ]] || fail "invalid V713_DECISION_DATE: $TODAY"
  DECISION_DATE="$(date -d "$TODAY" +%F)"
  DEADLINE="$(date -d "$(date +%F) 20:30" +%s)"

  while :; do
    MAXD="$("$PY" -c "import pandas as pd; print(pd.read_parquet('$QMT_SERVER_STORE/indexes/000852.SH.parquet', columns=['trade_date'])['trade_date'].astype(str).max())")"
    printf "store max=%s want=%s\n" "$MAXD" "$TODAY"
    [ "$MAXD" = "$TODAY" ] && break
    [ -n "${V713_DECISION_DATE:-}" ] && fail "manual decision date unavailable: store max=$MAXD want=$TODAY"
    [ "$(date +%s)" -gt "$DEADLINE" ] && fail "market store stale: max=$MAXD want=$TODAY"
    sleep 600
  done

  [ -f "$HYDRA_TARGET" ] || fail "missing audited Hydra target: $HYDRA_TARGET"
  if [ -n "${V713_RESUME_OUT:-}" ]; then
    OUT="$(realpath -e "$V713_RESUME_OUT")"
    case "$OUT" in
      /opt/qmt-refresh/tmp/v713-weekly.*) ;;
      *) fail "resume output is outside the v713 temporary root: $OUT" ;;
    esac
    for artifact in \
      v713_target_latest.json v713_target_latest.parquet \
      Shadow_Base_latest.json Shadow_Base_latest.parquet \
      Shadow_Aux_Hard_TOP2_latest.json Shadow_Aux_Hard_TOP2_latest.parquet \
      Shadow_Aux_Hard_TOP2_ShortCredit_latest.json Shadow_Aux_Hard_TOP2_ShortCredit_latest.parquet \
      Shadow_ML_TOP2_latest.json Shadow_ML_TOP2_latest.parquet
    do
      [ -f "$OUT/$artifact" ] || fail "resume output missing artifact: $artifact"
    done
    printf "resuming validated publish from %s\n" "$OUT"
  else
    OUT="$(mktemp -d "/opt/qmt-refresh/tmp/v713-weekly.${TODAY}.XXXXXX")"

    cd "$PEER"
    WITH_FUNDAMENTALS=()
    if [ "$(date +%d)" -le 07 ] \
        || [ -n "${V713_DECISION_DATE:-}" ] \
        || [ "${V713_FORCE_FUNDAMENTALS:-0}" = "1" ]; then
      WITH_FUNDAMENTALS=(--with-fundamentals)
    fi
    "$PY" v79_data_refresh.py "${WITH_FUNDAMENTALS[@]}"
    "$PY" round4/v7.9/ml_switch_phase8_size_tail_structure.py
    "$PY" round4/v7.9/ml_switch_phase12_size_tail_state_classifier.py
    "$PY" round4/v7.13/refresh_shadow_inputs.py --decision-date "$DECISION_DATE"

    V713_SOURCE_COMMIT="$SOURCE_COMMIT" "$PY" round4/v7.13/execution.py \
      --decision-date "$DECISION_DATE" \
      --hydra-weights "$HYDRA_TARGET" \
      --output-dir "$OUT"
    cp "$OUT/v713_target_${TODAY}.parquet" "$OUT/v713_target_latest.parquet"
    cp "$OUT/v713_target_${TODAY}.json" "$OUT/v713_target_latest.json"

    V713_SOURCE_COMMIT="$SOURCE_COMMIT" "$PY" \
      round4/v7.13/produce_shadow_base_target.py \
      --decision-date "$DECISION_DATE" --hydra-weights "$HYDRA_TARGET" \
      --output-dir "$OUT"
    V713_SOURCE_COMMIT="$SOURCE_COMMIT" "$PY" \
      round4/v7.13/produce_shadow_aux_hard_top2_target.py \
      --decision-date "$DECISION_DATE" --hydra-weights "$HYDRA_TARGET" \
      --signal-csv round4/v7.13/shadow_inputs/aux_hard_logistic_signal.csv \
      --industry-etf-map round4/v7.13/shadow/industry_etf_map.csv \
      --shadow-id Shadow_Aux_Hard_TOP2 --top2-stop-code 511880.SH \
      --output-dir "$OUT"
    V713_SOURCE_COMMIT="$SOURCE_COMMIT" "$PY" \
      round4/v7.13/produce_shadow_aux_hard_top2_target.py \
      --decision-date "$DECISION_DATE" --hydra-weights "$HYDRA_TARGET" \
      --signal-csv round4/v7.13/shadow_inputs/aux_hard_logistic_signal.csv \
      --industry-etf-map round4/v7.13/shadow/industry_etf_map.csv \
      --shadow-id Shadow_Aux_Hard_TOP2_ShortCredit --top2-stop-code 511360.SH \
      --output-dir "$OUT"
    V713_SOURCE_COMMIT="$SOURCE_COMMIT" "$PY" \
      round4/v7.13/produce_shadow_ml_top2_target.py \
      --decision-date "$DECISION_DATE" --hydra-weights "$HYDRA_TARGET" \
      --model round4/v7.13/shadow/ml_top2/frozen/sw2021-aux-top2-logistic-20260626-r1.joblib \
      --model-manifest round4/v7.13/shadow/ml_top2/frozen/sw2021-aux-top2-logistic-20260626-r1.json \
      --feature-csv round4/v7.13/shadow_inputs/ml_top2_feature_panel.csv \
      --industry-etf-map round4/v7.13/shadow/industry_etf_map.csv \
      --output-dir "$OUT"
  fi

  cd "$SERVER"
  CANDIDATE_SUMMARY="$(V713_TARGET_DIR="$OUT" "$SERVER_PY" -c \
    "import os; from pathlib import Path; from plugins.v713_relay import V713RelayAdapter; V713RelayAdapter.data_dir=Path(os.environ['V713_TARGET_DIR']); f=V713RelayAdapter()._read_latest_basket(); print(f\"decision={f.decision_date.iloc[0]} as_of={f.as_of_date.iloc[0]} sleeve={f.sleeve.iloc[0]} rows={len(f)} hash={f.basket_sha256.iloc[0][:12]}\")")"
  printf "candidate %s\n" "$CANDIDATE_SUMMARY"

  # V7.13 is monthly even though this refresh job runs weekly.  Never replace
  # the executable main target with another artifact for the same completed
  # month.  Shadow targets may still refresh below.
  CURRENT_TARGET="$SERVER/plugins/v713/data/v713_target_latest.parquet"
  MAIN_PUBLISH="installed_first_target"
  if [ -f "$CURRENT_TARGET" ]; then
    MAIN_RELATION="$(CANDIDATE_TARGET="$OUT/v713_target_latest.parquet" \
      CURRENT_TARGET="$CURRENT_TARGET" "$SERVER_PY" -c \
      "import os, pandas as pd; from plugins.v713_relay import allocation_hash; c=pd.read_parquet(os.environ['CANDIDATE_TARGET']); p=pd.read_parquet(os.environ['CURRENT_TARGET']); ca=str(c.as_of_date.iloc[0]); pa=str(p.as_of_date.iloc[0]); print('newer_month' if ca > pa else 'older_month' if ca < pa else 'same_month_same_allocation' if allocation_hash(c) == allocation_hash(p) else 'same_month_changed_allocation')")"
    case "$MAIN_RELATION" in
      newer_month)
        MAIN_PUBLISH="installed_new_month"
        ;;
      same_month_same_allocation)
        MAIN_PUBLISH="skipped_same_month"
        ;;
      same_month_changed_allocation)
        fail "main target changed within consumed month; manual review required"
        ;;
      older_month)
        fail "main target as_of_date rolled backward; refusing publish"
        ;;
      *)
        fail "unknown main target relation: $MAIN_RELATION"
        ;;
    esac
  fi

  if [[ "$MAIN_PUBLISH" == installed_* ]]; then
    MAIN_JSON="$(mktemp "$SERVER/plugins/v713/data/.v713_target_latest.XXXXXX.json")"
    MAIN_PARQUET="$(mktemp "$SERVER/plugins/v713/data/.v713_target_latest.XXXXXX.parquet")"
    install -o qmtserver -g qmtserver -m 0644 "$OUT/v713_target_latest.json" "$MAIN_JSON"
    install -o qmtserver -g qmtserver -m 0644 "$OUT/v713_target_latest.parquet" "$MAIN_PARQUET"
    mv "$MAIN_JSON" "$SERVER/plugins/v713/data/v713_target_latest.json"
    mv "$MAIN_PARQUET" "$SERVER/plugins/v713/data/v713_target_latest.parquet"
  fi
  printf "main_publish=%s\n" "$MAIN_PUBLISH"

  for SHADOW_ID in \
    Shadow_Base \
    Shadow_Aux_Hard_TOP2 \
    Shadow_Aux_Hard_TOP2_ShortCredit \
    Shadow_ML_TOP2
  do
    "$SERVER_PY" -m scripts.stage_shadow_target \
      --source "$OUT/${SHADOW_ID}_latest.parquet" \
      --sidecar "$OUT/${SHADOW_ID}_latest.json" \
      --shadow-id "$SHADOW_ID" --trade-date "$TODAY" --install
  done

  INSTALLED_SUMMARY="$("$SERVER_PY" -c "import pandas as pd; f=pd.read_parquet('$SERVER/plugins/v713/data/v713_target_latest.parquet'); print(f\"decision={f.decision_date.iloc[0]} as_of={f.as_of_date.iloc[0]} sleeve={f.sleeve.iloc[0]} rows={len(f)} hash={f.basket_sha256.iloc[0][:12]}\")")"
  SUMMARY="main_publish=$MAIN_PUBLISH candidate=[$CANDIDATE_SUMMARY] installed=[$INSTALLED_SUMMARY]"
  printf "%s\n" "$SUMMARY"
  notify ok "V7.13 target refreshed" "$SUMMARY"
  printf "=== done %s ===\n" "$(date)"
} >>"$LOG" 2>&1
