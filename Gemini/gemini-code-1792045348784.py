import csv
import sys

# Support large longtext fields (such as multiline Address)
csv.field_size_limit(sys.maxsize)

NULL_OR_JUNK_NRICS = {
    '', r'\N', 'NULL', 'NONE', 'NIL', 'NA', 'N/A', '-', '--', '0', '000000000000', 'UNKNOWN', 'TEST'
}

OUTPUT_COLUMNS = [
    'Clean_NRIC',
    'HKL_Id',
    'IPR_Id',
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
    'HKL_IsDeleted',
    'IPR_IsDeleted',
    'HKL_UpdatedTimestamp',
    'IPR_UpdatedTimestamp',
    'HKL_CreatedTimestamp',
    'IPR_CreatedTimestamp'
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
    Determines which record wins during sync:
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

def load_best_by_nric(filepath, label):
    """
    Loads CSV and keeps the best active/latest record per NRIC (in case of intra-DB duplicates).
    Also tracks all IDs associated with that NRIC if duplicates exist inside the same DB.
    """
    best_by_nric = {}
    all_ids_by_nric = {}
    total_rows = 0

    with open(filepath, mode='r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rows += 1
            nric = normalize_nric(row.get('Nric'))
            if not nric:
                continue

            row_id = clean_val(row.get('Id'))
            all_ids_by_nric.setdefault(nric, []).append(row_id)

            if nric not in best_by_nric:
                best_by_nric[nric] = row
            else:
                if row_sort_key(row) > row_sort_key(best_by_nric[nric]):
                    best_by_nric[nric] = row

    print(f"[{label}] Scanned {total_rows:,} rows -> Found {len(best_by_nric):,} valid unique NRICs.")
    return best_by_nric, all_ids_by_nric

def export_same_nric_diff_id(hkl_csv, ipr_csv, output_csv='same_nric_diff_id_report.csv'):
    # 1. Load IPR first (small 3MB file)
    ipr_best, ipr_all_ids = load_best_by_nric(ipr_csv, 'IPR')

    # 2. Load HKL (300MB file)
    hkl_best, hkl_all_ids = load_best_by_nric(hkl_csv, 'HKL')

    # 3. Find overlapping NRICs and filter for different IDs
    common_nrics = sorted(set(ipr_best.keys()).intersection(set(hkl_best.keys())))
    
    same_id_count = 0
    diff_id_count = 0

    with open(output_csv, mode='w', encoding='utf-8', newline='') as out_f:
        writer = csv.DictWriter(out_f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()

        for nric in common_nrics:
            h_row = hkl_best[nric]
            i_row = ipr_best[nric]

            h_id = clean_val(h_row.get('Id'))
            i_id = clean_val(i_row.get('Id'))

            if int(h_id) == int(i_id):
                same_id_count += 1
                continue

            diff_id_count += 1
            winner = 'IPR' if row_sort_key(i_row) > row_sort_key(h_row) else 'HKL'

            writer.writerow({
                'Clean_NRIC':           nric,
                'HKL_Id':               h_id,
                'IPR_Id':               i_id,
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
                'HKL_IsDeleted':        clean_val(h_row.get('IsDeleted')),
                'IPR_IsDeleted':        clean_val(i_row.get('IsDeleted')),
                'HKL_UpdatedTimestamp': clean_val(h_row.get('UpdatedTimestamp')),
                'IPR_UpdatedTimestamp': clean_val(i_row.get('UpdatedTimestamp')),
                'HKL_CreatedTimestamp': clean_val(h_row.get('CreatedTimestamp')),
                'IPR_CreatedTimestamp': clean_val(i_row.get('CreatedTimestamp')),
            })

    print("\n=== NRIC OVERLAP SUMMARY ===")
    print(f"  Total Shared NRICs (In Both DBs) : {len(common_nrics):,}")
    print(f"  Shared NRICs with SAME ID        : {same_id_count:,}")
    print(f"  Shared NRICs with DIFFERENT ID   : {diff_id_count:,}")
    print(f"\nExported {diff_id_count:,} rows to '{output_csv}'.")

if __name__ == '__main__':
    export_same_nric_diff_id('./ConsumersHKL.csv', './ConsumersIPR.csv')