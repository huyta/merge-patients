import csv
import sys
from datetime import datetime

# Support large longtext fields
csv.field_size_limit(sys.maxsize)

COLUMNS = [
    'Id', 'Nric', 'HonorificId', 'Name', 'Email', 'MobileNo', 'CountryCode',
    'BirthDate', 'GenderId', 'IsMalaysian', 'EthnicityId', 'ExternalMRN',
    'Address', 'Value', 'IsDeleted', 'CreatedTimestamp', 'CreatedBy',
    'UpdatedTimestamp', 'UpdatedBy', 'LastName', 'FirstName', 'nric_type', 'nationality'
]

NULL_MARKERS = {'', r'\N', 'NULL', 'None'}

def is_null(val):
    return val is None or str(val).strip() in NULL_MARKERS

def normalize_nric(nric_val):
    if is_null(nric_val):
        return ''
    return str(nric_val).strip().upper()[:255]

def sanitize_row_for_mysql(row):
    """Ensures nullable datetime/varchar columns use \\N when empty so LOAD DATA INFILE never fails on datetime(6)."""
    clean = {}
    nullable_cols = {'Nric', 'Name', 'Email', 'MobileNo', 'CountryCode', 'BirthDate',
                     'ExternalMRN', 'Address', 'CreatedBy', 'UpdatedTimestamp',
                     'UpdatedBy', 'LastName', 'FirstName', 'nric_type', 'nationality'}
    for col in COLUMNS:
        val = row.get(col, '')
        if col in nullable_cols and is_null(val):
            clean[col] = r'\N'
        else:
            clean[col] = val
    return clean

def row_sort_key(row):
    """
    Determines which record has the fresher/better demographic info:
    1. Active (IsDeleted == '0') beats Deleted ('1')
    2. Newer UpdatedTimestamp (falls back to CreatedTimestamp)
    3. Lower Id as tie-breaker
    """
    is_active = 1 if str(row.get('IsDeleted', '0')).strip() == '0' else 0
    upd = row.get('UpdatedTimestamp', '')
    cre = row.get('CreatedTimestamp', '')
    ts = cre if is_null(upd) else upd
    return (is_active, str(ts).strip(), -int(row['Id']))

def load_csv_index(filepath, label):
    best_by_nric = {}
    extra_rows = []
    used_ids = set()
    max_id = 0
    total_rows = 0

    with open(filepath, mode='r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            total_rows += 1
            row = sanitize_row_for_mysql(raw_row)
            row_id = int(row['Id'])
            used_ids.add(row_id)
            if row_id > max_id:
                max_id = row_id

            clean_nric = normalize_nric(row['Nric'])
            if not clean_nric:
                extra_rows.append(row)
                continue

            if clean_nric not in best_by_nric:
                best_by_nric[clean_nric] = row
            else:
                # Duplicate NRIC inside the same DB: keep best as primary, keep duplicate in extra_rows so its Id isn't lost
                curr = best_by_nric[clean_nric]
                if row_sort_key(row) > row_sort_key(curr):
                    extra_rows.append(curr)
                    best_by_nric[clean_nric] = row
                else:
                    extra_rows.append(row)

    print(f"[{label}] Loaded {total_rows:,} rows | Unique NRICs: {len(best_by_nric):,} | Extra/Null/Dup rows: {len(extra_rows):,} | Max ID: {max_id}")
    return best_by_nric, extra_rows, used_ids, max_id

def merge_into_existing(local_row, winning_row, sync_ts):
    """Updates local_row demographics from winning_row while strictly preserving local Id and local ExternalMRN."""
    merged = dict(winning_row)
    merged['Id'] = local_row['Id']  # NEVER change existing Id
    if not is_null(local_row.get('ExternalMRN')):
        merged['ExternalMRN'] = local_row['ExternalMRN']
    # Preserve non-null contact info if winning record has nulls
    for col in ['Email', 'MobileNo', 'BirthDate', 'Address']:
        if is_null(merged.get(col)) and not is_null(local_row.get(col)):
            merged[col] = local_row[col]
    merged['UpdatedTimestamp'] = sync_ts
    merged['UpdatedBy'] = 'SYSTEM_SYNC'
    return merged

def run_merge(hkl_in, ipr_in):
    sync_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
    
    hkl_nrics, hkl_extras, hkl_used_ids, hkl_max_id = load_csv_index(hkl_in, "HKL")
    ipr_nrics, ipr_extras, ipr_used_ids, ipr_max_id = load_csv_index(ipr_in, "IPR")

    all_nrics = sorted(set(hkl_nrics.keys()).union(set(ipr_nrics.keys())))
    
    stats = {
        'both_same_id': 0,
        'both_diff_id': 0,
        'hkl_only_kept_id_in_ipr': 0,
        'hkl_only_new_id_in_ipr': 0,
        'ipr_only_kept_id_in_hkl': 0,
        'ipr_only_new_id_in_hkl': 0,
    }

    with open('hkl_final.csv', 'w', encoding='utf-8', newline='') as f_hkl_full, \
         open('ipr_final.csv', 'w', encoding='utf-8', newline='') as f_ipr_full, \
         open('hkl_delta.csv', 'w', encoding='utf-8', newline='') as f_hkl_delta, \
         open('ipr_delta.csv', 'w', encoding='utf-8', newline='') as f_ipr_delta:

        w_hkl_full  = csv.DictWriter(f_hkl_full, fieldnames=COLUMNS)
        w_ipr_full  = csv.DictWriter(f_ipr_full, fieldnames=COLUMNS)
        w_hkl_delta = csv.DictWriter(f_hkl_delta, fieldnames=COLUMNS)
        w_ipr_delta = csv.DictWriter(f_ipr_delta, fieldnames=COLUMNS)

        for w in (w_hkl_full, w_ipr_full, w_hkl_delta, w_ipr_delta):
            w.writeheader()

        for nric in all_nrics:
            in_hkl = nric in hkl_nrics
            in_ipr = nric in ipr_nrics

            # 1. NRIC EXISTS IN BOTH HKL & IPR
            if in_hkl and in_ipr:
                h_row = hkl_nrics[nric]
                i_row = ipr_nrics[nric]
                
                if int(h_row['Id']) == int(i_row['Id']):
                    stats['both_same_id'] += 1
                else:
                    stats['both_diff_id'] += 1

                if row_sort_key(i_row) > row_sort_key(h_row):
                    # IPR is newer -> Update HKL (keeping HKL Id), keep IPR as-is
                    updated_hkl = merge_into_existing(h_row, i_row, sync_ts)
                    w_hkl_full.writerow(updated_hkl)
                    w_hkl_delta.writerow(updated_hkl)
                    w_ipr_full.writerow(i_row)
                else:
                    # HKL is newer -> Keep HKL as-is, update IPR (keeping IPR Id)
                    updated_ipr = merge_into_existing(i_row, h_row, sync_ts)
                    w_hkl_full.writerow(h_row)
                    w_ipr_full.writerow(updated_ipr)
                    w_ipr_delta.writerow(updated_ipr)

            # 2. NRIC ONLY IN HKL -> Must be inserted into IPR
            elif in_hkl and not in_ipr:
                h_row = hkl_nrics[nric]
                w_hkl_full.writerow(h_row)

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
                new_ipr['UpdatedBy'] = 'SYSTEM_SYNC'
                w_ipr_full.writerow(new_ipr)
                w_ipr_delta.writerow(new_ipr)

            # 3. NRIC ONLY IN IPR -> Must be inserted into HKL
            elif in_ipr and not in_hkl:
                i_row = ipr_nrics[nric]
                w_ipr_full.writerow(i_row)

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
                new_hkl['UpdatedBy'] = 'SYSTEM_SYNC'
                w_hkl_full.writerow(new_hkl)
                w_hkl_delta.writerow(new_hkl)

        # Preserve all intra-DB duplicates and NULL-NRIC rows in the full files
        for r in hkl_extras:
            w_hkl_full.writerow(r)
        for r in ipr_extras:
            w_ipr_full.writerow(r)

    print("\n=== MERGE SUMMARY ===")
    for k, v in stats.items():
        print(f"  {k:30s}: {v:,}")

if __name__ == '__main__':
    run_merge('./ConsumersHKL.csv', './ConsumersIPR.csv')