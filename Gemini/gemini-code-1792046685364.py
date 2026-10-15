import csv
import sys
from datetime import datetime

# Support large longtext fields (such as multiline Address)
csv.field_size_limit(sys.maxsize)

NULL_OR_JUNK_NRICS = {
    '', r'\N', 'NULL', 'NONE', 'NIL', 'NA', 'N/A', '-', '--', '0', '000000000000', 'UNKNOWN', 'TEST'
}

# Columns for Report 1 (Same NRIC, Same ID) & Report 2 (Same NRIC, Diff ID)
SHARED_REPORT_COLS = [
    'Clean_NRIC',
    'HKL_Id',
    'IPR_Id',
    'Has_Demographic_Diff',
    'Winning_Source',
    'HKL_Name',
    'IPR_Name',
    'HKL_ExternalMRN',
    'IPR_ExternalMRN',
    'HKL_BirthDate',
    'IPR_BirthDate',
    'HKL_GenderId',
    'IPR_GenderId',
    'HKL_MobileNo',
    'IPR_MobileNo',
    'HKL_Email',
    'IPR_Email',
    'HKL_IsDeleted',
    'IPR_IsDeleted',
    'HKL_UpdatedTimestamp',
    'IPR_UpdatedTimestamp',
    'HKL_CreatedTimestamp',
    'IPR_CreatedTimestamp'
]

# Columns for Report 3 (HKL Only) & Report 4 (IPR Only)
SINGLE_DB_REPORT_COLS = [
    'Clean_NRIC',
    'Source_DB',
    'Source_Id',
    'ID_Status_In_Target_DB',
    'Name',
    'ExternalMRN',
    'BirthDate',
    'GenderId',
    'MobileNo',
    'Email',
    'IsDeleted',
    'UpdatedTimestamp',
    'CreatedTimestamp'
]

# Columns for Report 5 (Intra-DB Duplicates inside HKL or IPR)
INTRA_DB_DUP_COLS = [
    'Source_DB',
    'Clean_NRIC',
    'Total_Copies_In_DB',
    'Resolution_Role',
    'Winning_Id_For_NRIC',
    'Id',
    'Raw_Nric',
    'Name',
    'ExternalMRN',
    'BirthDate',
    'GenderId',
    'MobileNo',
    'Email',
    'IsDeleted',
    'UpdatedTimestamp',
    'CreatedTimestamp'
]

def normalize_nric(val):
    if val is None:
        return ''
    cleaned = str(val).strip().upper()[:255]
    if cleaned in NULL_OR_JUNK_NRICS:
        return ''
    return cleaned

def clean_val(val):
    if val is None or str(val).strip() == r'\N':
        return ''
    return str(val).strip()

def row_sort_key(row):
    """
    Determines which record wins during deduplication/sync:
    1. Active (IsDeleted == '0') beats Deleted ('1')
    2. Newer UpdatedTimestamp (or CreatedTimestamp if Updated is empty)
    3. Lower Id as tie-breaker
    """
    is_active = 1 if clean_val(row.get('IsDeleted', '0')) == '0' else 0
    upd = clean_val(row.get('UpdatedTimestamp'))
    cre = clean_val(row.get('CreatedTimestamp'))
    ts = upd if upd else cre
    try:
        row_id = int(clean_val(row.get('Id', '0')))
    except ValueError:
        row_id = 0
    return (is_active, ts, -row_id)

def has_demographic_diff(h_row, i_row):
    compare_cols = ['Name', 'BirthDate', 'GenderId', 'MobileNo', 'Email', 'Address', 'IsDeleted']
    for col in compare_cols:
        if clean_val(h_row.get(col)) != clean_val(i_row.get(col)):
            return 'YES'
    return 'NO'

def load_csv_once(filepath, label, dup_writer):
    """
    Reads CSV in a single pass:
    - Keeps the winning row per NRIC in `best_by_nric`
    - Tracks any NRIC that appears more than once in `dup_groups` and writes them to Report 5
    - Tracks all integer IDs in `used_ids`
    """
    best_by_nric = {}
    dup_groups = {}  # { clean_nric: [list of all rows sharing this NRIC] }
    used_ids = set()
    stats = {
        'total_rows': 0,
        'null_or_junk_nric': 0,
        'dup_nric_groups': 0,
        'dup_extra_rows': 0
    }

    with open(filepath, mode='r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats['total_rows'] += 1
            raw_id = clean_val(row.get('Id', '0'))
            try:
                used_ids.add(int(raw_id))
            except ValueError:
                pass

            nric = normalize_nric(row.get('Nric'))
            if not nric:
                stats['null_or_junk_nric'] += 1
                continue

            if nric not in best_by_nric:
                best_by_nric[nric] = row
            else:
                # We found an intra-DB duplicate NRIC!
                stats['dup_extra_rows'] += 1
                if nric not in dup_groups:
                    # First time seeing a second copy: add the initial row + current row
                    dup_groups[nric] = [best_by_nric[nric], row]
                else:
                    dup_groups[nric].append(row)

                # Update best_by_nric if the new row is fresher/active
                if row_sort_key(row) > row_sort_key(best_by_nric[nric]):
                    best_by_nric[nric] = row

    # Write all intra-DB duplicate groups for this database to Report 5
    stats['dup_nric_groups'] = len(dup_groups)
    for nric in sorted(dup_groups.keys()):
        group_rows = dup_groups[nric]
        # Sort highest priority first (Index 0 is the Winner)
        group_rows.sort(key=row_sort_key, reverse=True)
        winning_id = clean_val(group_rows[0].get('Id'))
        total_copies = len(group_rows)

        for idx, r in enumerate(group_rows):
            role = '1_PRIMARY_WINNER' if idx == 0 else f'2_DUPLICATE_COPY_#{idx}'
            dup_writer.writerow({
                'Source_DB':           label,
                'Clean_NRIC':          nric,
                'Total_Copies_In_DB':  total_copies,
                'Resolution_Role':     role,
                'Winning_Id_For_NRIC': winning_id,
                'Id':                  clean_val(r.get('Id')),
                'Raw_Nric':            clean_val(r.get('Nric')),
                'Name':                clean_val(r.get('Name')),
                'ExternalMRN':         clean_val(r.get('ExternalMRN')),
                'BirthDate':           clean_val(r.get('BirthDate')),
                'GenderId':            clean_val(r.get('GenderId')),
                'MobileNo':            clean_val(r.get('MobileNo')),
                'Email':               clean_val(r.get('Email')),
                'IsDeleted':           clean_val(r.get('IsDeleted')),
                'UpdatedTimestamp':    clean_val(r.get('UpdatedTimestamp')),
                'CreatedTimestamp':    clean_val(r.get('CreatedTimestamp')),
            })

    print(f"[{label}] Scanned {stats['total_rows']:,} rows | "
          f"Unique Valid NRICs: {len(best_by_nric):,} | "
          f"Intra-DB Dup NRIC Groups: {stats['dup_nric_groups']:,} ({stats['dup_extra_rows']:,} extra rows) | "
          f"Blank/Junk NRICs: {stats['null_or_junk_nric']:,}")
    return best_by_nric, used_ids, stats

def build_shared_row_dict(nric, h_row, i_row, h_id, i_id):
    diff_flag = has_demographic_diff(h_row, i_row)
    winner = 'IPR' if row_sort_key(i_row) > row_sort_key(h_row) else 'HKL'
    return {
        'Clean_NRIC':           nric,
        'HKL_Id':               h_id,
        'IPR_Id':               i_id,
        'Has_Demographic_Diff': diff_flag,
        'Winning_Source':       winner,
        'HKL_Name':             clean_val(h_row.get('Name')),
        'IPR_Name':             clean_val(i_row.get('Name')),
        'HKL_ExternalMRN':      clean_val(h_row.get('ExternalMRN')),
        'IPR_ExternalMRN':      clean_val(i_row.get('ExternalMRN')),
        'HKL_BirthDate':        clean_val(h_row.get('BirthDate')),
        'IPR_BirthDate':        clean_val(i_row.get('BirthDate')),
        'HKL_GenderId':         clean_val(h_row.get('GenderId')),
        'IPR_GenderId':         clean_val(i_row.get('GenderId')),
        'HKL_MobileNo':         clean_val(h_row.get('MobileNo')),
        'IPR_MobileNo':         clean_val(i_row.get('MobileNo')),
        'HKL_Email':            clean_val(h_row.get('Email')),
        'IPR_Email':            clean_val(i_row.get('Email')),
        'HKL_IsDeleted':        clean_val(h_row.get('IsDeleted')),
        'IPR_IsDeleted':        clean_val(i_row.get('IsDeleted')),
        'HKL_UpdatedTimestamp': clean_val(h_row.get('UpdatedTimestamp')),
        'IPR_UpdatedTimestamp': clean_val(i_row.get('UpdatedTimestamp')),
        'HKL_CreatedTimestamp': clean_val(h_row.get('CreatedTimestamp')),
        'IPR_CreatedTimestamp': clean_val(i_row.get('CreatedTimestamp')),
    }

def build_single_db_row_dict(nric, source_db, row, target_used_ids):
    src_id_str = clean_val(row.get('Id', '0'))
    try:
        id_occupied = int(src_id_str) in target_used_ids
    except ValueError:
        id_occupied = True

    id_status = 'OCCUPIED_NEEDS_NEW_ID' if id_occupied else 'FREE_CAN_KEEP_SAME_ID'
    return {
        'Clean_NRIC':             nric,
        'Source_DB':              source_db,
        'Source_Id':              src_id_str,
        'ID_Status_In_Target_DB': id_status,
        'Name':                   clean_val(row.get('Name')),
        'ExternalMRN':            clean_val(row.get('ExternalMRN')),
        'BirthDate':              clean_val(row.get('BirthDate')),
        'GenderId':               clean_val(row.get('GenderId')),
        'MobileNo':               clean_val(row.get('MobileNo')),
        'Email':                  clean_val(row.get('Email')),
        'IsDeleted':              clean_val(row.get('IsDeleted')),
        'UpdatedTimestamp':       clean_val(row.get('UpdatedTimestamp')),
        'CreatedTimestamp':       clean_val(row.get('CreatedTimestamp')),
    }, id_occupied

def run_all_5_audits(hkl_csv, ipr_csv):
    start_time = datetime.now()
    print("Starting Single-Pass Audit (5 Reports) across HKL and IPR CSVs...\n")

    with open('1_same_nric_same_id.csv', 'w', encoding='utf-8', newline='') as f_same, \
         open('2_same_nric_diff_id.csv', 'w', encoding='utf-8', newline='') as f_diff, \
         open('3_hkl_only_nric.csv', 'w', encoding='utf-8', newline='') as f_hkl_only, \
         open('4_ipr_only_nric.csv', 'w', encoding='utf-8', newline='') as f_ipr_only, \
         open('5_intra_db_duplicates.csv', 'w', encoding='utf-8', newline='') as f_intra_dup:

        w_same      = csv.DictWriter(f_same, fieldnames=SHARED_REPORT_COLS)
        w_diff      = csv.DictWriter(f_diff, fieldnames=SHARED_REPORT_COLS)
        w_hkl_only  = csv.DictWriter(f_hkl_only, fieldnames=SINGLE_DB_REPORT_COLS)
        w_ipr_only  = csv.DictWriter(f_ipr_only, fieldnames=SINGLE_DB_REPORT_COLS)
        w_intra_dup = csv.DictWriter(f_intra_dup, fieldnames=INTRA_DB_DUP_COLS)

        w_same.writeheader()
        w_diff.writeheader()
        w_hkl_only.writeheader()
        w_ipr_only.writeheader()
        w_intra_dup.writeheader()

        # 1. Single pass over IPR (3 MB) and HKL (300 MB) -> Also populates 5_intra_db_duplicates.csv
        ipr_best, ipr_used_ids, ipr_stats = load_csv_once(ipr_csv, 'IPR', w_intra_dup)
        hkl_best, hkl_used_ids, hkl_stats = load_csv_once(hkl_csv, 'HKL', w_intra_dup)

        all_nrics = sorted(set(hkl_best.keys()).union(set(ipr_best.keys())))

        # Counters for Reports 1 to 4
        c_same_id = 0
        c_diff_id = 0
        c_hkl_only_free = 0
        c_hkl_only_occupied = 0
        c_ipr_only_free = 0
        c_ipr_only_occupied = 0

        # 2. Populate Reports 1, 2, 3, and 4
        for nric in all_nrics:
            in_hkl = nric in hkl_best
            in_ipr = nric in ipr_best

            if in_hkl and in_ipr:
                h_row = hkl_best[nric]
                i_row = ipr_best[nric]
                h_id = clean_val(h_row.get('Id'))
                i_id = clean_val(i_row.get('Id'))

                out_row = build_shared_row_dict(nric, h_row, i_row, h_id, i_id)
                if int(h_id) == int(i_id):
                    w_same.writerow(out_row)
                    c_same_id += 1
                else:
                    w_diff.writerow(out_row)
                    c_diff_id += 1

            elif in_hkl and not in_ipr:
                out_row, occupied = build_single_db_row_dict(nric, 'HKL', hkl_best[nric], ipr_used_ids)
                w_hkl_only.writerow(out_row)
                if occupied:
                    c_hkl_only_occupied += 1
                else:
                    c_hkl_only_free += 1

            elif in_ipr and not in_hkl:
                out_row, occupied = build_single_db_row_dict(nric, 'IPR', ipr_best[nric], hkl_used_ids)
                w_ipr_only.writerow(out_row)
                if occupied:
                    c_ipr_only_occupied += 1
                else:
                    c_ipr_only_free += 1

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n==================================================================")
    print(f"AUDIT COMPLETED IN {elapsed:.2f} SECONDS")
    print(f"==================================================================")
    print(f"Total Combined Unique NRICs Across Both DBs : {len(all_nrics):,}")
    print(f"  1. [1_same_nric_same_id.csv]   (In Both, Same ID) : {c_same_id:,}")
    print(f"  2. [2_same_nric_diff_id.csv]   (In Both, Diff ID) : {c_diff_id:,}")
    print(f"  3. [3_hkl_only_nric.csv]       (Only in HKL)      : {c_hkl_only_free + c_hkl_only_occupied:,}")
    print(f"       ├── ID is FREE in IPR (Can keep HKL ID)      : {c_hkl_only_free:,}")
    print(f"       └── ID is OCCUPIED in IPR (Needs new ID)     : {c_hkl_only_occupied:,}")
    print(f"  4. [4_ipr_only_nric.csv]       (Only in IPR)      : {c_ipr_only_free + c_ipr_only_occupied:,}")
    print(f"       ├── ID is FREE in HKL (Can keep IPR ID)      : {c_ipr_only_free:,}")
    print(f"       └── ID is OCCUPIED in HKL (Needs new ID)     : {c_ipr_only_occupied:,}")
    print(f"  5. [5_intra_db_duplicates.csv] (Same-DB Dupes)")
    print(f"       ├── HKL Duplicate NRIC Groups                : {hkl_stats['dup_nric_groups']:,} ({hkl_stats['dup_extra_rows']:,} extra rows)")
    print(f"       └── IPR Duplicate NRIC Groups                : {ipr_stats['dup_nric_groups']:,} ({ipr_stats['dup_extra_rows']:,} extra rows)")
    print(f"==================================================================")

if __name__ == '__main__':
    run_all_5_audits('./ConsumersHKL.csv', './ConsumersIPR.csv')