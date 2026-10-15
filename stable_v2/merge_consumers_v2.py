import csv
import re
import sys
from datetime import datetime
from collections import defaultdict

# Allow large longtext fields (such as multiline Address)
csv.field_size_limit(sys.maxsize)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# If False: IPR rows with shared NRIC take 100% of HKL's columns (except IPR keeps its own Id).
# If True:  IPR keeps its own ExternalMRN if it already has one, while taking all other fields from HKL.
PRESERVE_IPR_LOCAL_MRN = False

COLUMNS = [
    'Id', 'Nric', 'HonorificId', 'Name', 'Email', 'MobileNo', 'CountryCode',
    'BirthDate', 'GenderId', 'IsMalaysian', 'EthnicityId', 'ExternalMRN',
    'Address', 'Value', 'IsDeleted', 'CreatedTimestamp', 'CreatedBy',
    'UpdatedTimestamp', 'UpdatedBy', 'LastName', 'FirstName', 'nric_type', 'nationality'
]

NOT_NULL_INT_COLS = {'Id', 'HonorificId', 'GenderId', 'IsMalaysian', 'EthnicityId', 'Value', 'IsDeleted'}
NULLABLE_COLS = {
    'Nric', 'Name', 'Email', 'MobileNo', 'CountryCode', 'BirthDate',
    'ExternalMRN', 'Address', 'CreatedBy', 'UpdatedTimestamp',
    'UpdatedBy', 'LastName', 'FirstName', 'nric_type', 'nationality'
}
NULL_MARKERS = {'', r'\N', 'NULL', 'None', 'null'}

# Common placeholder strings entered by receptionists instead of a real NRIC
JUNK_NRIC_VALUES = {'-', '--', 'NA', 'N/A', 'NIL', 'NONE', '0', '000000000000', 'UNKNOWN', 'TEST'}

# Regex for valid MySQL datetime / datetime(6): YYYY-MM-DD HH:MM:SS[.ffffff] or YYYY-MM-DD
MYSQL_DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)?$')

def is_null(val):
    return val is None or str(val).strip() in NULL_MARKERS

def normalize_nric(nric_val):
    """
    Normalizes NRIC for matching.
    Junk placeholders ('N/A', '-', etc.) are treated as empty so unrelated patients are never merged.
    """
    if is_null(nric_val):
        return ''
    cleaned = str(nric_val).strip().upper()[:255]
    if cleaned in JUNK_NRIC_VALUES:
        return ''
    return cleaned

def strip_nric_dashes(nric_str):
    return re.sub(r'[\s\-]', '', nric_str)

def validate_datetime(val_str):
    if is_null(val_str):
        return True
    s = str(val_str).strip()
    if s.startswith('0000-00-00'):
        return False
    if not MYSQL_DATETIME_RE.match(s):
        return False
    try:
        datetime.strptime(s[:10], '%Y-%m-%d')
        return True
    except ValueError:
        return False

def sanitize_row_for_mysql(row):
    clean = {}
    for col in COLUMNS:
        val = row.get(col, '')
        if col in NULLABLE_COLS and is_null(val):
            clean[col] = ''
        else:
            clean[col] = str(val).strip() if col in NOT_NULL_INT_COLS else val
    return clean

def intra_db_sort_key(row):
    """
    Used ONLY when the SAME database (e.g. inside HKL itself) has multiple rows for the same NRIC.
    Picks the active (IsDeleted=0) and newest row as that database's primary record.
    """
    is_active = 1 if str(row.get('IsDeleted', '0')).strip() == '0' else 0
    upd = row.get('UpdatedTimestamp', '')
    cre = row.get('CreatedTimestamp', '')
    ts = cre if is_null(upd) else upd
    return (is_active, str(ts).strip(), -int(row['Id']))


# ==============================================================================
# STAGE 1: PRE-MERGE CSV VALIDATION & ANOMALY DETECTION
# ==============================================================================
def load_and_validate_csv(filepath, label, report_file):
    best_by_nric = {}
    extra_rows = []
    used_ids = set()
    max_id = 0
    total_rows = 0

    anomalies = {
        'duplicate_pk_id': [],
        'invalid_not_null_int': [],
        'broken_birthdate': [],
        'broken_created_ts': [],
        'broken_updated_ts': [],
        'varchar_overflow': [],
        'junk_nric_ignored': 0,
        'intra_db_duplicate_nric': 0,
        'null_nric_count': 0
    }

    with open(filepath, mode='r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        
        missing_cols = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing_cols:
            raise ValueError(f"[{label}] CRITICAL: Missing columns in CSV header: {missing_cols}")

        for line_num, raw_row in enumerate(reader, start=2):
            total_rows += 1

            # 1. Validate NOT NULL Integer columns
            int_error = False
            for int_col in NOT_NULL_INT_COLS:
                val = raw_row.get(int_col, '')
                try:
                    if '.' in str(val):
                        raw_row[int_col] = str(int(float(val)))
                    else:
                        int(str(val).strip())
                except (ValueError, TypeError):
                    anomalies['invalid_not_null_int'].append((line_num, raw_row.get('Id'), int_col, val))
                    int_error = True
            if int_error:
                continue

            row = sanitize_row_for_mysql(raw_row)
            row_id = int(row['Id'])

            # 2. Check for Duplicate Primary Key Id in source CSV
            if row_id in used_ids:
                anomalies['duplicate_pk_id'].append((line_num, row_id))
                continue
            used_ids.add(row_id)
            if row_id > max_id:
                max_id = row_id

            # 3. Validate Datetime(6) columns
            if not validate_datetime(row['BirthDate']):
                anomalies['broken_birthdate'].append((row_id, row['BirthDate']))
                row['BirthDate'] = r'\N'

            if is_null(row['CreatedTimestamp']) or not validate_datetime(row['CreatedTimestamp']):
                anomalies['broken_created_ts'].append((row_id, row['CreatedTimestamp']))
                row['CreatedTimestamp'] = '1970-01-01 00:00:00.000000'

            if not validate_datetime(row['UpdatedTimestamp']):
                anomalies['broken_updated_ts'].append((row_id, row['UpdatedTimestamp']))
                row['UpdatedTimestamp'] = r'\N'

            # 4. Validate VARCHAR length constraints (nric_type <= 50, nationality <= 10)
            if not is_null(row['nric_type']) and len(str(row['nric_type'])) > 50:
                anomalies['varchar_overflow'].append((row_id, 'nric_type', len(str(row['nric_type']))))
                row['nric_type'] = str(row['nric_type'])[:50]

            if not is_null(row['nationality']) and len(str(row['nationality'])) > 10:
                anomalies['varchar_overflow'].append((row_id, 'nationality', len(str(row['nationality']))))
                row['nationality'] = str(row['nationality'])[:10]

            # 5. Check NRIC validity & Junk placeholders
            raw_nric_str = '' if is_null(row['Nric']) else str(row['Nric']).strip().upper()
            if raw_nric_str in JUNK_NRIC_VALUES and raw_nric_str != '':
                anomalies['junk_nric_ignored'] += 1

            clean_nric = normalize_nric(row['Nric'])
            if not clean_nric:
                anomalies['null_nric_count'] += 1
                extra_rows.append(row)
                continue

            if clean_nric not in best_by_nric:
                best_by_nric[clean_nric] = row
            else:
                anomalies['intra_db_duplicate_nric'] += 1
                curr = best_by_nric[clean_nric]
                if intra_db_sort_key(row) > intra_db_sort_key(curr):
                    extra_rows.append(curr)
                    best_by_nric[clean_nric] = row
                else:
                    extra_rows.append(row)

    summary = (
        f"\n--- [{label}] PRE-MERGE VALIDATION REPORT ---\n"
        f"  Total Rows Scanned          : {total_rows:,}\n"
        f"  Valid Unique NRICs          : {len(best_by_nric):,}\n"
        f"  Intra-DB Duplicate NRICs    : {anomalies['intra_db_duplicate_nric']:,} (preserved in extra_rows)\n"
        f"  Null/Blank NRIC Rows        : {anomalies['null_nric_count']:,} (preserved in extra_rows)\n"
        f"  Junk NRICs ('NA','-',etc.)  : {anomalies['junk_nric_ignored']:,} (treated as blank to prevent false merges)\n"
        f"  Duplicate PK Ids Found      : {len(anomalies['duplicate_pk_id']):,}\n"
        f"  Invalid NOT NULL Ints       : {len(anomalies['invalid_not_null_int']):,}\n"
        f"  Broken BirthDates (Healed)  : {len(anomalies['broken_birthdate']):,}\n"
        f"  Broken CreatedTS (Healed)   : {len(anomalies['broken_created_ts']):,}\n"
        f"  Broken UpdatedTS (Healed)   : {len(anomalies['broken_updated_ts']):,}\n"
        f"  VARCHAR Overflows (Trimmed) : {len(anomalies['varchar_overflow']):,}\n"
    )
    print(summary)
    report_file.write(summary)

    for key in ['duplicate_pk_id', 'invalid_not_null_int', 'broken_birthdate', 'broken_created_ts', 'varchar_overflow']:
        if anomalies[key]:
            report_file.write(f"  Sample {key} (up to 10): {anomalies[key][:10]}\n")

    if anomalies['duplicate_pk_id'] or anomalies['invalid_not_null_int']:
        raise RuntimeError(f"[{label}] Aborting merge due to critical PK or NOT NULL integer corruption. Check validation_report.txt.")

    return best_by_nric, extra_rows, used_ids, max_id


def check_cross_db_dash_anomalies(hkl_nrics, ipr_nrics, report_file):
    hkl_stripped = {strip_nric_dashes(k): k for k in hkl_nrics.keys()}
    dash_mismatches = []
    for ipr_k in ipr_nrics.keys():
        if ipr_k not in hkl_nrics:
            s_key = strip_nric_dashes(ipr_k)
            if s_key and s_key in hkl_stripped:
                dash_mismatches.append((hkl_stripped[s_key], ipr_k))

    msg = f"\n[CROSS-DB NRIC FORMAT CHECK] NRICs differing only by dashes/spaces: {len(dash_mismatches):,}\n"
    print(msg.strip())
    report_file.write(msg)
    if dash_mismatches:
        report_file.write(f"  Samples (HKL vs IPR): {dash_mismatches[:20]}\n")


# ==============================================================================
# STAGE 2: POST-MERGE INVARIANT VERIFICATION
# ==============================================================================
def verify_final_invariants(orig_hkl_ids, orig_ipr_ids, hkl_final_rows, ipr_final_rows):
    print("\n--- RUNNING POST-MERGE INVARIANT CHECKS ---")
    
    final_hkl_ids = [int(r['Id']) for r in hkl_final_rows]
    final_ipr_ids = [int(r['Id']) for r in ipr_final_rows]

    if len(final_hkl_ids) != len(set(final_hkl_ids)):
        raise AssertionError("FATAL: Duplicate Id detected in hkl_final!")
    if len(final_ipr_ids) != len(set(final_ipr_ids)):
        raise AssertionError("FATAL: Duplicate Id detected in ipr_final!")

    missing_hkl = orig_hkl_ids - set(final_hkl_ids)
    missing_ipr = orig_ipr_ids - set(final_ipr_ids)
    if missing_hkl:
        raise AssertionError(f"FATAL: {len(missing_hkl)} existing HKL IDs were lost!")
    if missing_ipr:
        raise AssertionError(f"FATAL: {len(missing_ipr)} existing IPR IDs were lost!")

    final_hkl_nrics = {normalize_nric(r['Nric']) for r in hkl_final_rows if normalize_nric(r['Nric'])}
    final_ipr_nrics = {normalize_nric(r['Nric']) for r in ipr_final_rows if normalize_nric(r['Nric'])}
    if final_hkl_nrics != final_ipr_nrics:
        diff = final_hkl_nrics.symmetric_difference(final_ipr_nrics)
        raise AssertionError(f"FATAL: NRIC mismatch between final HKL and IPR! Diff count: {len(diff)}")

    print("  [PASS] Zero ID collisions in HKL and IPR final datasets.")
    print(f"  [PASS] 100% of original IDs preserved (HKL: {len(orig_hkl_ids):,}, IPR: {len(orig_ipr_ids):,}).")
    print(f"  [PASS] Both databases now contain the exact same {len(final_hkl_nrics):,} unique NRICs.")


# ==============================================================================
# STAGE 3: MAIN MERGE PIPELINE (HKL AS MASTER SOURCE OF TRUTH)
# ==============================================================================
def apply_hkl_master_to_ipr(ipr_existing_row, hkl_master_row, sync_ts):
    """
    Overwrites IPR row with HKL Master data (HKL = Source of Truth),
    while strictly keeping IPR's existing primary key `Id`.
    """
    updated_ipr = dict(hkl_master_row)
    updated_ipr['Id'] = ipr_existing_row['Id']  # NEVER alter IPR's existing Id!

    if PRESERVE_IPR_LOCAL_MRN and not is_null(ipr_existing_row.get('ExternalMRN')):
        updated_ipr['ExternalMRN'] = ipr_existing_row['ExternalMRN']

    updated_ipr['UpdatedTimestamp'] = sync_ts
    updated_ipr['UpdatedBy'] = 'HKL_MASTER_SYNC'
    return updated_ipr

def run_pipeline(hkl_in, ipr_in):
    sync_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')

    with open('validation_report.txt', 'w', encoding='utf-8') as report_file:
        report_file.write(f"Validation & Merge Run (HKL = Master Source of Truth): {sync_ts}\n")

        # 1. Load & Validate Both CSVs
        hkl_nrics, hkl_extras, hkl_used_ids, hkl_max_id = load_and_validate_csv(hkl_in, "HKL", report_file)
        ipr_nrics, ipr_extras, ipr_used_ids, ipr_max_id = load_and_validate_csv(ipr_in, "IPR", report_file)

        orig_hkl_ids_snapshot = set(hkl_used_ids)
        orig_ipr_ids_snapshot = set(ipr_used_ids)

        # 2. Check Cross-DB NRIC Dash Mismatches
        check_cross_db_dash_anomalies(hkl_nrics, ipr_nrics, report_file)

        # 3. Perform In-Memory Merge (HKL Always Wins Conflicts)
        all_nrics = sorted(set(hkl_nrics.keys()).union(set(ipr_nrics.keys())))
        hkl_final_rows, ipr_final_rows = [], []
        hkl_delta_rows, ipr_delta_rows = [], []

        stats = defaultdict(int)

        for nric in all_nrics:
            in_hkl = nric in hkl_nrics
            in_ipr = nric in ipr_nrics

            # CASE 1: NRIC EXISTS IN BOTH HKL & IPR -> HKL ALWAYS WINS
            if in_hkl and in_ipr:
                h_row = hkl_nrics[nric]
                i_row = ipr_nrics[nric]

                if int(h_row['Id']) == int(i_row['Id']):
                    stats['both_same_id_hkl_master_applied'] += 1
                else:
                    stats['both_diff_id_hkl_master_applied'] += 1

                # HKL stays 100% untouched (it is the source of truth)
                hkl_final_rows.append(h_row)

                # IPR gets updated with HKL's data, while keeping IPR's existing Id
                updated_ipr = apply_hkl_master_to_ipr(i_row, h_row, sync_ts)
                ipr_final_rows.append(updated_ipr)
                ipr_delta_rows.append(updated_ipr)

            # CASE 2: NRIC ONLY IN HKL -> Keep in HKL, Insert into IPR
            elif in_hkl and not in_ipr:
                h_row = hkl_nrics[nric]
                hkl_final_rows.append(h_row)

                new_ipr = dict(h_row)
                cand_id = int(h_row['Id'])
                if cand_id not in ipr_used_ids:
                    new_ipr['Id'] = str(cand_id)
                    ipr_used_ids.add(cand_id)
                    stats['hkl_only_kept_id_in_ipr'] += 1
                else:
                    ipr_max_id += 1
                    new_ipr['Id'] = str(ipr_max_id)
                    ipr_used_ids.add(ipr_max_id)
                    stats['hkl_only_new_id_in_ipr'] += 1

                new_ipr['UpdatedTimestamp'] = sync_ts
                new_ipr['UpdatedBy'] = 'HKL_MASTER_SYNC'
                ipr_final_rows.append(new_ipr)
                ipr_delta_rows.append(new_ipr)

            # CASE 3: NRIC ONLY IN IPR -> Keep in IPR, Insert into HKL
            elif in_ipr and not in_hkl:
                i_row = ipr_nrics[nric]
                ipr_final_rows.append(i_row)

                new_hkl = dict(i_row)
                cand_id = int(i_row['Id'])
                if cand_id not in hkl_used_ids:
                    new_hkl['Id'] = str(cand_id)
                    hkl_used_ids.add(cand_id)
                    stats['ipr_only_kept_id_in_hkl'] += 1
                else:
                    hkl_max_id += 1
                    new_hkl['Id'] = str(hkl_max_id)
                    hkl_used_ids.add(hkl_max_id)
                    stats['ipr_only_new_id_in_hkl'] += 1

                new_hkl['UpdatedTimestamp'] = sync_ts
                new_hkl['UpdatedBy'] = 'IPR_TO_HKL_SYNC'
                hkl_final_rows.append(new_hkl)
                hkl_delta_rows.append(new_hkl)

        # Preserve all intra-DB duplicates and NULL-NRIC rows
        hkl_final_rows.extend(hkl_extras)
        ipr_final_rows.extend(ipr_extras)

        # 4. Run Post-Merge Invariant Verification BEFORE writing files
        verify_final_invariants(orig_hkl_ids_snapshot, orig_ipr_ids_snapshot, hkl_final_rows, ipr_final_rows)

        # 5. Write Verified Outputs to Disk
        print("\n--- WRITING OUTPUT CSV FILES ---")
        for filename, rows in [
            ('hkl_final.csv', hkl_final_rows),
            ('ipr_final.csv', ipr_final_rows),
            ('hkl_delta.csv', hkl_delta_rows),
            ('ipr_delta.csv', ipr_delta_rows)
        ]:
            with open(filename, 'w', encoding='utf-8', newline='') as out_f:
                writer = csv.DictWriter(out_f, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            print(f"  Wrote {filename:15s} : {len(rows):,} rows")

        print("\n=== MERGE SUMMARY (HKL AS SOURCE OF TRUTH) ===")
        for k, v in stats.items():
            print(f"  {k:38s}: {v:,}")
            report_file.write(f"  {k:38s}: {v:,}\n")

if __name__ == '__main__':
    run_pipeline('ConsumersHKL.csv', 'ConsumersIPR.csv')