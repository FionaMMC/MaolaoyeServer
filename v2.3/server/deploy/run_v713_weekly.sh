#!/usr/bin/env bash
# Refresh V7.13_Base and its exact Base shadow. Retired experiments are not dependencies.
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
  printf "=== V7.13 monthly cycle check %s ===\n" "$(date)"
  CALENDAR_TODAY="$(date +%Y%m%d)"
  MARKET_DATE="$CALENDAR_TODAY"
  cd "$SERVER"
  CYCLE_JSON="$("$SERVER_PY" -m scripts.v713_cycle --today "$CALENDAR_TODAY" \
    --current-target "$SERVER/plugins/v713/data/v713_target_latest.parquet")"
  CYCLE_STATUS="$(CYCLE_JSON="$CYCLE_JSON" "$SERVER_PY" -c 'import json,os; print(json.loads(os.environ["CYCLE_JSON"])["status"])')"
  printf "cycle=%s\n" "$CYCLE_JSON"
  if [ "$CYCLE_STATUS" != "DUE" ] && [ -z "${V713_DECISION_DATE:-}" ] && [ -z "${V713_RESUME_OUT:-}" ]; then
    if [ "$CYCLE_STATUS" = "CURRENT" ]; then
      NEXT_SESSION="$(CYCLE_JSON="$CYCLE_JSON" "$SERVER_PY" -c 'import json,os; print(json.loads(os.environ["CYCLE_JSON"])["decision_date"])')"
      "$SERVER_PY" -m scripts.run_v713_orders --trade-date "$NEXT_SESSION"
    fi
    exit 0
  fi
  TODAY="${V713_DECISION_DATE:-$(CYCLE_JSON="$CYCLE_JSON" "$SERVER_PY" -c 'import json,os; print(json.loads(os.environ["CYCLE_JSON"])["decision_date"])')}"
  [[ "$TODAY" =~ ^[0-9]{8}$ ]] || fail "invalid decision date: $TODAY"
  DECISION_DATE="$(date -d "$TODAY" +%F)"
  DEADLINE="$(date -d "$(date +%F) 20:30" +%s)"
  exec 9>/opt/qmt-refresh/tmp/heavy_job.lock
  flock -w 900 9 || fail "heavy_job.lock busy; next timer retries"

  while :; do
    MAXD="$("$PY" -c "import pandas as pd; print(pd.read_parquet('$QMT_SERVER_STORE/indexes/000852.SH.parquet', columns=['trade_date'])['trade_date'].astype(str).max())")"
    printf "store max=%s want=%s\n" "$MAXD" "$MARKET_DATE"
    [ "$MAXD" = "$MARKET_DATE" ] && break
    [ -n "${V713_DECISION_DATE:-}" ] && fail "manual market date unavailable: store max=$MAXD want=$MARKET_DATE"
    [ "$(date +%s)" -gt "$DEADLINE" ] && fail "market store stale: max=$MAXD want=$MARKET_DATE"
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
      Shadow_Base_latest.json Shadow_Base_latest.parquet
    do
      [ -f "$OUT/$artifact" ] || fail "resume output missing artifact: $artifact"
    done
    printf "resuming validated publish from %s\n" "$OUT"
  else
    OUT="$(mktemp -d "/opt/qmt-refresh/tmp/v713-weekly.${TODAY}.XXXXXX")"

    cd "$PEER"
    "$PY" v79_data_refresh.py --with-fundamentals
    V713_SOURCE_COMMIT="$SOURCE_COMMIT" "$PY" round4/v7.13/execution.py \
      --decision-date "$DECISION_DATE" \
      --hydra-weights "$HYDRA_TARGET" \
      --output-dir "$OUT"
    cp "$OUT/v713_target_${TODAY}.parquet" "$OUT/v713_target_latest.parquet"
    cp "$OUT/v713_target_${TODAY}.json" "$OUT/v713_target_latest.json"

    # The Base comparison consumes the exact executable basket; never rerun
    # TOP50 with the shadow producer's different AUM default.
    cd "$SERVER"
    "$SERVER_PY" -m scripts.wrap_v713_shadow_base \
      --source-dir "$OUT" --source-version "$SOURCE_COMMIT"

  fi

  cd "$SERVER"
  CANDIDATE_SUMMARY="$(V713_TARGET_DIR="$OUT" "$SERVER_PY" -c \
    "import os; from pathlib import Path; from plugins.v713_relay import V713RelayAdapter; V713RelayAdapter.data_dir=Path(os.environ['V713_TARGET_DIR']); f=V713RelayAdapter()._read_latest_basket(); print(f\"decision={f.decision_date.iloc[0]} as_of={f.as_of_date.iloc[0]} sleeve={f.sleeve.iloc[0]} rows={len(f)} hash={f.basket_sha256.iloc[0][:12]}\")")"
  printf "candidate %s\n" "$CANDIDATE_SUMMARY"
  EXPECTED_AS_OF="$(CYCLE_JSON="$CYCLE_JSON" "$SERVER_PY" -c 'import json,os; print(json.loads(os.environ["CYCLE_JSON"]).get("as_of_date", ""))')"
  CANDIDATE_AS_OF="$(CANDIDATE_TARGET="$OUT/v713_target_latest.parquet" "$SERVER_PY" -c 'import os,pandas as pd; print(pd.read_parquet(os.environ["CANDIDATE_TARGET"]).as_of_date.iloc[0])')"
  [ "$CANDIDATE_AS_OF" = "$EXPECTED_AS_OF" ] || fail "candidate month $CANDIDATE_AS_OF != expected $EXPECTED_AS_OF"

  # Never replace a consumed monthly executable allocation on a retry.
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

  if [ -z "${V713_DECISION_DATE:-}" ]; then
    "$SERVER_PY" -m scripts.run_v713_orders --trade-date "$TODAY"
  fi

  if [ -n "${V713_DECISION_DATE:-}" ]; then
  for SHADOW_ID in Shadow_Base
  do
    "$SERVER_PY" -m scripts.stage_shadow_target \
      --source "$OUT/${SHADOW_ID}_latest.parquet" \
      --sidecar "$OUT/${SHADOW_ID}_latest.json" \
      --shadow-id "$SHADOW_ID" --trade-date "$TODAY" --install
  done
  fi

  INSTALLED_SUMMARY="$("$SERVER_PY" -c "import pandas as pd; f=pd.read_parquet('$SERVER/plugins/v713/data/v713_target_latest.parquet'); print(f\"decision={f.decision_date.iloc[0]} as_of={f.as_of_date.iloc[0]} sleeve={f.sleeve.iloc[0]} rows={len(f)} hash={f.basket_sha256.iloc[0][:12]}\")")"
  SUMMARY="main_publish=$MAIN_PUBLISH candidate=[$CANDIDATE_SUMMARY] installed=[$INSTALLED_SUMMARY]"
  printf "%s\n" "$SUMMARY"
  notify ok "V7.13 target refreshed" "$SUMMARY"
  printf "=== done %s ===\n" "$(date)"
} >>"$LOG" 2>&1
