#!/usr/bin/env python3
"""
HOSPITAL PATIENT DATA SYNCHRONIZATION AND RECONCILIATION
================================================================================
Source Datasets (never modified):
  - ConsumersHKL.csv : Hospital Kuala Lumpur patient records
  - ConsumersIPR.csv : Institut Perubatan Respiratori patient records

Outputs Generated:
  - Final_HKL.csv                    : Synchronized dataset for HKL
  - Final_IPR.csv                    : Synchronized dataset for IPR
  - REVIEW_ITEMS.csv                 : Ambiguous / conflicting records for manual review
  - FINAL_SYNC_GENERATION_REPORT.txt : Reconciliation, validation and audit report

Rules Applied:
  1. Same NRIC & Same ID: Keep HKL in Final_HKL, keep IPR in Final_IPR (unmodified).
  2. NRIC exists only in HKL: Keep in Final_HKL. Copy to Final_IPR if not deleted (IsDeleted != 1)
     and the NRIC is not already in IPR / Final_IPR. Prepend '111' repeatedly if the ID conflicts.
  3. NRIC exists only in IPR: Keep in Final_IPR. Copy to Final_HKL if not deleted (IsDeleted != 1)
     and the NRIC is not already in HKL / Final_HKL. Prepend '111' repeatedly if the ID conflicts.
  4. Same NRIC & Different IDs: Keep original records in both, preserving each hospital's ID.
  5. Deleted records (IsDeleted = 1): Retained in source hospital output; never copied.

Records with a blank / placeholder NRIC cannot be matched across hospitals; they are kept in
their source hospital's output, never copied, and listed in REVIEW_ITEMS.csv.

Usage:
  python3 generate_final_datasets.py [DATA_DIR]
  DATA_DIR defaults to the folder containing this script.
  Exit code 0 = all validations passed, 1 = a validation failed, 2 = input problem.
================================================================================
"""

import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict

# Tokens representing unidentifiable, missing, or placeholder NRICs
MISSING_TOKENS = {
    "", "NA", "N/A", "NIL", "NULL", "NONE", "UNKNOWN", "-", "--", "NAN",
    "NIL IC", "-NIL-", "--NIL--", "NO IC PROVIDED", "TIADA", "TIADA MAKLUMAT",
    "DUMMY", "DUMMY ADN"
}

# Heuristic for NRICs that look like placeholders (flagged for review only; matching is unchanged)
SUSPICIOUS_NRIC_MIN_LENGTH = 6

REQUIRED_COLUMNS = ["Id", "Nric", "Name", "BirthDate", "GenderId", "IsDeleted"]
ID_PREFIX = "111"


class InputError(Exception):
    """Raised when the source files cannot be processed safely."""


def normalize_nric(val):
    """
    Normalizes NRIC strings for cross-facility clinical record linkage:
      1. Strips leading/trailing whitespace and surrounding quotation marks.
      2. Converts to uppercase.
      3. Filters out recognized placeholder / missing tokens.
      4. Strips hyphens and inner whitespace while strictly preserving leading zeros.
    """
    if not val:
        return ""
    s = str(val).strip().strip("'\"")
    if not s:
        return ""
    u = s.upper()
    if u in MISSING_TOKENS:
        return ""
    cleaned = re.sub(r"[\s\-]", "", u)
    if cleaned in MISSING_TOKENS:
        return ""
    return cleaned


def is_suspicious_nric(norm):
    """True for NRICs that look like placeholders (too short, one repeated character, or 'TEST')."""
    return len(norm) < SUSPICIOUS_NRIC_MIN_LENGTH or len(set(norm)) == 1 or "TEST" in norm


def is_deleted(value):
    return value.strip() == "1"


def resolve_id_conflict(orig_id, existing_ids):
    """
    Repeatedly prepends '111' to orig_id until the ID is unique in existing_ids.
    Returns (new_id, prepends_count).
    """
    curr_id = orig_id
    prepends = 0
    while curr_id in existing_ids:
        curr_id = ID_PREFIX + curr_id
        prepends += 1
    return curr_id, prepends


def is_valid_prefixed_id(new_id, orig_id):
    """True if new_id == ('111' * k) + orig_id for some k >= 1."""
    if not new_id.endswith(orig_id) or len(new_id) == len(orig_id):
        return False
    prefix = new_id[: len(new_id) - len(orig_id)]
    return len(prefix) % len(ID_PREFIX) == 0 and prefix == ID_PREFIX * (len(prefix) // len(ID_PREFIX))


# Strict UTF-8 (no silent byte replacement); newline="" lets csv handle embedded newlines.
def open_csv_read(path):
    return open(path, mode="r", encoding="utf-8", newline="")


def open_csv_write(path):
    return open(path, mode="w", encoding="utf-8", newline="")


def make_writer(f):
    # Source files use LF line endings; keep the same.
    return csv.writer(f, lineterminator="\n")


def column_index(header):
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise InputError(f"Missing required column(s): {missing}")
    return {c: header.index(c) for c in REQUIRED_COLUMNS}


class Validator:
    """Collects validation results; each check is tagged with the rule numbers it proves."""

    def __init__(self):
        self.results = []  # (name, passed, detail, rules)

    def check(self, name, passed, detail="", rules=()):
        self.results.append((name, bool(passed), detail, tuple(rules)))
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}" + (f" -- {detail}" if detail else ""))

    @property
    def all_passed(self):
        return all(r[1] for r in self.results)

    def rule_status(self, rule):
        checks = [r for r in self.results if rule in r[3]]
        return bool(checks) and all(r[1] for r in checks), len(checks)


class ReviewLog:
    """Ambiguous or conflicting records that must not be resolved silently."""

    COLUMNS = [
        "Category", "SourceHospital", "Id", "Nric", "NormalizedNric", "Name", "BirthDate",
        "GenderId", "IsDeleted", "RelatedHospital", "RelatedId", "RelatedNric", "Reason", "ActionTaken",
    ]

    def __init__(self, cols):
        self.cols = cols
        self.items = []

    def add(self, category, hospital, row, reason, action,
            related_hospital="", related_id="", related_nric=""):
        c = self.cols
        self.items.append([
            category, hospital, row[c["Id"]].strip(), row[c["Nric"]], normalize_nric(row[c["Nric"]]),
            row[c["Name"]], row[c["BirthDate"]], row[c["GenderId"]], row[c["IsDeleted"]].strip(),
            related_hospital, related_id, related_nric, reason, action,
        ])

    def counts(self):
        return Counter(item[0] for item in self.items)

    def write(self, path):
        with open_csv_write(path) as f:
            w = make_writer(f)
            w.writerow(self.COLUMNS)
            # Stable sort keeps the original order within each category
            w.writerows(sorted(self.items, key=lambda item: item[0]))


def sync_records(source_rows, cols, dest_ids, dest_orig_nrics, writer,
                 source_name, dest_name, review):
    """
    Applies Rule 2 (HKL -> IPR) or Rule 3 (IPR -> HKL) plus Rule 5 to every source row and
    appends eligible copies to the destination writer.

    dest_ids        : IDs already in the destination (original + copied); mutated in place.
    dest_orig_nrics : normalized NRICs in the ORIGINAL destination dataset (incl. deleted records).

    Check order (the set of copied records is the same whichever order the skip checks run in,
    because only active, copied records are ever added to the destination):
      1. IsDeleted = 1          -> never copied (Rule 5)
      2. blank/placeholder NRIC -> never copied, flagged for review
      3. NRIC in original dest  -> patient already exists there (Rules 1 / 4)
      4. NRIC already copied    -> duplicate record of the same patient in the source, flagged
      5. otherwise copy, prepending '111' to the ID until it is unique
    """
    id_i, nric_i, del_i = cols["Id"], cols["Nric"], cols["IsDeleted"]
    st = {
        "evaluated": 0, "copied": 0, "skipped_deleted": 0, "skipped_missing_nric": 0,
        "skipped_nric_in_dest": 0, "skipped_duplicate_in_source": 0,
        "id_changes": [], "copied_suspicious": 0,
    }
    copied_nrics = {}  # normalized NRIC -> ID used in destination

    for row in source_rows:
        st["evaluated"] += 1
        if st["evaluated"] % 400000 == 0:
            print(f"    Evaluated {st['evaluated']:,} {source_name} records; copied {st['copied']:,} so far...")

        orig_id = row[id_i].strip()

        if is_deleted(row[del_i]):
            st["skipped_deleted"] += 1
            continue

        norm = normalize_nric(row[nric_i])
        if not norm:
            st["skipped_missing_nric"] += 1
            review.add(
                "MISSING_OR_PLACEHOLDER_NRIC", source_name, row,
                reason="NRIC is blank or a placeholder; patient cannot be matched across hospitals",
                action=f"Kept in Final_{source_name} only; not copied to Final_{dest_name}",
            )
            continue

        if norm in dest_orig_nrics:
            st["skipped_nric_in_dest"] += 1
            continue

        if norm in copied_nrics:
            st["skipped_duplicate_in_source"] += 1
            review.add(
                "DUPLICATE_PATIENT_RECORD_IN_SOURCE", source_name, row,
                reason=f"Another active {source_name} record with the same NRIC was already copied "
                       f"(first in file order)",
                action=f"Kept in Final_{source_name}; not copied to Final_{dest_name} "
                       f"to avoid a duplicate patient",
                related_hospital=dest_name, related_id=copied_nrics[norm],
            )
            continue

        new_id, prepends = resolve_id_conflict(orig_id, dest_ids)
        if new_id != orig_id:
            st["id_changes"].append({
                "original_id": orig_id, "new_id": new_id, "prepends": prepends,
                "nric": row[nric_i], "name": row[cols["Name"]], "destination": f"Final_{dest_name}.csv",
            })

        if is_suspicious_nric(norm):
            st["copied_suspicious"] += 1
            review.add(
                "SUSPICIOUS_NRIC_COPIED", source_name, row,
                reason="NRIC looks like a placeholder (short, repeated character, or TEST)",
                action=f"Copied to Final_{dest_name} as Id={new_id} per Rule {'2' if source_name == 'HKL' else '3'}; "
                       f"verify identity",
            )

        new_row = list(row)
        new_row[id_i] = new_id
        writer.writerow(new_row)
        dest_ids.add(new_id)
        copied_nrics[norm] = new_id
        st["copied"] += 1

    return st


def validate_final(v, label, final_path, expected_header, original_rows, n_original,
                   copy_source_rows, stats, cols, dest_orig_nrics, dest_deleted_expected,
                   dest_dup_ids_expected, expected_copied, keep_rule, copy_rule):
    """
    Re-reads a final file and proves:
      - the first n_original rows are the destination's original records, unchanged
      - every copied row is identical to its source row except the Id
      - copied rows: not deleted, valid unique NRIC not already in destination, valid Id prefix
      - no duplicate IDs introduced; deleted-record count equals the original destination
    copy_source_rows must be in the same order the rows were copied (source file order).
    """
    id_i, nric_i, del_i = cols["Id"], cols["Nric"], cols["IsDeleted"]
    change_map = {c["new_id"]: c["original_id"] for c in stats["id_changes"]}

    ids = set()
    dup_ids = deleted_total = 0
    orig_seen = orig_changed = 0
    copied_seen = copied_deleted = copied_bad_nric = copied_dup_nric = 0
    copied_content_mismatch = copied_source_missing = copied_bad_id = 0
    copied_nrics = set()

    orig_iter = iter(original_rows)
    src_iter = iter(copy_source_rows)

    with open_csv_read(final_path) as f:
        reader = csv.reader(f)
        header = next(reader)
        for idx, row in enumerate(reader):
            rid = row[id_i].strip()
            if rid in ids:
                dup_ids += 1
            else:
                ids.add(rid)
            deleted = is_deleted(row[del_i])
            if deleted:
                deleted_total += 1

            if idx < n_original:
                orig_seen += 1
                if next(orig_iter, None) != row:
                    orig_changed += 1
                continue

            copied_seen += 1
            if deleted:
                copied_deleted += 1

            norm = normalize_nric(row[nric_i])
            if not norm or norm in dest_orig_nrics:
                copied_bad_nric += 1
            elif norm in copied_nrics:
                copied_dup_nric += 1
            else:
                copied_nrics.add(norm)

            orig_id = change_map.get(rid, rid)
            if rid != orig_id and not is_valid_prefixed_id(rid, orig_id):
                copied_bad_id += 1

            src = None
            for candidate in src_iter:
                if candidate[id_i].strip() == orig_id:
                    src = candidate
                    break
            if src is None:
                copied_source_missing += 1
            elif src[:id_i] + src[id_i + 1:] != row[:id_i] + row[id_i + 1:]:
                copied_content_mismatch += 1

    total = orig_seen + copied_seen
    both = sorted({keep_rule, copy_rule, 1, 4, 5})

    v.check(f"{label}: header identical to source", header == expected_header, rules=both)
    v.check(f"{label}: all {n_original:,} original records present and unchanged",
            orig_seen == n_original and orig_changed == 0,
            f"{orig_seen:,} present, {orig_changed:,} changed", rules=[1, 4, 5, keep_rule])
    v.check(f"{label}: copied record count = source-only active patients",
            copied_seen == stats["copied"] == expected_copied,
            f"written {copied_seen:,}, synced {stats['copied']:,}, expected {expected_copied:,}",
            rules=[copy_rule])
    v.check(f"{label}: copied records identical to source except Id",
            copied_content_mismatch == 0 and copied_source_missing == 0,
            f"{copied_content_mismatch} mismatched, {copied_source_missing} not found", rules=[copy_rule])
    v.check(f"{label}: no copied record has IsDeleted = 1", copied_deleted == 0,
            f"{copied_deleted} found", rules=[5, copy_rule])
    v.check(f"{label}: deleted records = original destination deleted records",
            deleted_total == dest_deleted_expected,
            f"{deleted_total:,} vs {dest_deleted_expected:,}", rules=[5])
    v.check(f"{label}: no copied record duplicates an NRIC already in the file",
            copied_bad_nric == 0 and copied_dup_nric == 0,
            f"{copied_bad_nric} existed in destination, {copied_dup_nric} duplicated among copies",
            rules=[1, 4, copy_rule])
    v.check(f"{label}: no duplicate IDs introduced", dup_ids == dest_dup_ids_expected,
            f"{len(ids):,} unique IDs in {total:,} records", rules=[copy_rule])
    v.check(f"{label}: changed IDs follow the repeated '111' prefix rule", copied_bad_id == 0,
            f"{len(change_map)} changed, {copied_bad_id} invalid", rules=[copy_rule])

    return {"total": total, "unique_ids": len(ids), "deleted": deleted_total,
            "original": orig_seen, "copied": copied_seen}


def main():
    start_time = time.time()

    base_dir = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    hkl_path = os.path.join(base_dir, "ConsumersHKL.csv")
    ipr_path = os.path.join(base_dir, "ConsumersIPR.csv")
    final_hkl_path = os.path.join(base_dir, "Final_HKL.csv")
    final_ipr_path = os.path.join(base_dir, "Final_IPR.csv")
    review_path = os.path.join(base_dir, "REVIEW_ITEMS.csv")
    report_path = os.path.join(base_dir, "FINAL_SYNC_GENERATION_REPORT.txt")

    print("=" * 80)
    print("STARTING HOSPITAL PATIENT RECONCILIATION DATASET GENERATION")
    print(f"Data Directory: {base_dir}")
    print("=" * 80)

    for p in (hkl_path, ipr_path):
        if not os.path.exists(p):
            raise InputError(f"{p} not found")

    # -------------------------------------------------------------------------
    # STEP 1: Load and index ConsumersIPR.csv (small; kept in memory)
    # -------------------------------------------------------------------------
    print("\n[Step 1/7] Loading and indexing ConsumersIPR.csv...")
    with open_csv_read(ipr_path) as f:
        reader = csv.reader(f)
        ipr_header = next(reader)
        ipr_records = list(reader)

    cols = column_index(ipr_header)
    ID, NRIC, DEL = cols["Id"], cols["Nric"], cols["IsDeleted"]

    ipr_ids, ipr_by_id = set(), {}
    ipr_dup_ids = ipr_deleted = ipr_missing = 0
    ipr_nric_set, ipr_active_nrics = set(), set()
    ipr_rows_by_nric = defaultdict(list)

    for line_no, row in enumerate(ipr_records, start=2):
        if len(row) != len(ipr_header):
            raise InputError(f"ConsumersIPR.csv row {line_no}: {len(row)} fields, expected {len(ipr_header)}")
        rid = row[ID].strip()
        if rid in ipr_ids:
            ipr_dup_ids += 1
        else:
            ipr_ids.add(rid)
            ipr_by_id[rid] = row
        deleted = is_deleted(row[DEL])
        if deleted:
            ipr_deleted += 1
        norm = normalize_nric(row[NRIC])
        if not norm:
            ipr_missing += 1
            continue
        ipr_nric_set.add(norm)
        ipr_rows_by_nric[norm].append(row)
        if not deleted:
            ipr_active_nrics.add(norm)

    ipr_dup_nrics = sum(1 for rows in ipr_rows_by_nric.values() if len(rows) > 1)
    print(f"  * Records                 : {len(ipr_records):,}")
    print(f"  * Unique IDs              : {len(ipr_ids):,} (duplicate IDs: {ipr_dup_ids})")
    print(f"  * Unique valid NRICs      : {len(ipr_nric_set):,} (NRICs with >1 record: {ipr_dup_nrics})")
    print(f"  * IsDeleted = 1           : {ipr_deleted:,}")
    print(f"  * Missing/placeholder NRIC: {ipr_missing}")

    # -------------------------------------------------------------------------
    # STEP 2: Stream ConsumersHKL.csv to index IDs, NRICs and cross-hospital matches
    # -------------------------------------------------------------------------
    print("\n[Step 2/7] Scanning ConsumersHKL.csv...")
    hkl_ids = set()
    hkl_total = hkl_dup_ids = hkl_deleted = hkl_missing = 0
    hkl_nric_set, hkl_active_nrics = set(), set()
    hkl_nric_counts_multi = 0
    hkl_rows_by_matched_nric = defaultdict(list)
    hkl_rows_sharing_ipr_id = {}
    hkl_matched_records = hkl_only_records = 0
    seen_once = set()

    with open_csv_read(hkl_path) as f:
        reader = csv.reader(f)
        hkl_header = next(reader)
        if hkl_header != ipr_header:
            raise InputError("ConsumersHKL.csv and ConsumersIPR.csv have different columns/column order; "
                             "copied rows would be misaligned")
        for row in reader:
            hkl_total += 1
            if len(row) != len(hkl_header):
                raise InputError(f"ConsumersHKL.csv record {hkl_total}: {len(row)} fields, expected {len(hkl_header)}")
            rid = row[ID].strip()
            if rid in hkl_ids:
                hkl_dup_ids += 1
            else:
                hkl_ids.add(rid)
            if rid in ipr_by_id:
                hkl_rows_sharing_ipr_id[rid] = row
            deleted = is_deleted(row[DEL])
            if deleted:
                hkl_deleted += 1
            norm = normalize_nric(row[NRIC])
            if not norm:
                hkl_missing += 1
                continue
            if norm in hkl_nric_set and norm not in seen_once:
                seen_once.add(norm)
                hkl_nric_counts_multi += 1
            hkl_nric_set.add(norm)
            if not deleted:
                hkl_active_nrics.add(norm)
            if norm in ipr_nric_set:
                hkl_rows_by_matched_nric[norm].append(row)
                hkl_matched_records += 1
            else:
                hkl_only_records += 1
            if hkl_total % 400000 == 0:
                print(f"    Scanned {hkl_total:,} HKL records...")
    del seen_once

    print(f"  * Records                 : {hkl_total:,}")
    print(f"  * Unique IDs              : {len(hkl_ids):,} (duplicate IDs: {hkl_dup_ids})")
    print(f"  * Unique valid NRICs      : {len(hkl_nric_set):,} (NRICs with >1 record: {hkl_nric_counts_multi:,})")
    print(f"  * IsDeleted = 1           : {hkl_deleted:,}")
    print(f"  * Missing/placeholder NRIC: {hkl_missing}")

    review = ReviewLog(cols)

    # -------------------------------------------------------------------------
    # STEP 3: Cross-hospital matching analysis (Rules 1 & 4) + review flags
    # -------------------------------------------------------------------------
    print("\n[Step 3/7] Analyzing cross-hospital matches...")
    matched_nrics = sorted(hkl_rows_by_matched_nric)
    m_same = m_diff = m_mixed = same_pairs = diff_pairs = 0
    ipr_matched_records = 0
    only_deleted_in_ipr = only_deleted_in_hkl = suspicious_matched = 0

    for norm in matched_nrics:
        i_rows, h_rows = ipr_rows_by_nric[norm], hkl_rows_by_matched_nric[norm]
        ipr_matched_records += len(i_rows)
        i_ids = {r[ID].strip() for r in i_rows}
        h_ids = {r[ID].strip() for r in h_rows}
        common = i_ids & h_ids
        if common and i_ids == h_ids:
            m_same += 1
        elif not common:
            m_diff += 1
        else:
            m_mixed += 1
        same_pairs += len(common)
        diff_pairs += len(i_rows) * len(h_rows) - len(common)

        i_active = any(not is_deleted(r[DEL]) for r in i_rows)
        h_active = any(not is_deleted(r[DEL]) for r in h_rows)
        for src_name, dst_name, src_rows, dst_rows, src_active, dst_active in (
            ("HKL", "IPR", h_rows, i_rows, h_active, i_active),
            ("IPR", "HKL", i_rows, h_rows, i_active, h_active),
        ):
            if src_active and not dst_active:
                if src_name == "HKL":
                    only_deleted_in_ipr += 1
                else:
                    only_deleted_in_hkl += 1
                for r in src_rows:
                    if not is_deleted(r[DEL]):
                        review.add(
                            "DESTINATION_HAS_ONLY_DELETED_RECORDS", src_name, r,
                            reason=f"Same NRIC exists in {dst_name} but every {dst_name} record is IsDeleted = 1",
                            action=f"Not copied to Final_{dst_name} because the NRIC already exists there "
                                   f"(Rules 1/4); {dst_name} has no active record for this patient",
                            related_hospital=dst_name,
                            related_id=";".join(sorted(x[ID].strip() for x in dst_rows)),
                            related_nric=dst_rows[0][NRIC],
                        )

        if is_suspicious_nric(norm):
            suspicious_matched += 1
            for src_name, dst_name, src_rows, dst_rows in (("HKL", "IPR", h_rows, i_rows),
                                                           ("IPR", "HKL", i_rows, h_rows)):
                for r in src_rows:
                    review.add(
                        "SUSPICIOUS_NRIC_MATCHED", src_name, r,
                        reason="NRIC looks like a placeholder (short, repeated character, or TEST) "
                               "but matches across hospitals",
                        action="Treated as the same patient (Rules 1/4); not copied either way",
                        related_hospital=dst_name,
                        related_id=";".join(sorted(x[ID].strip() for x in dst_rows)),
                        related_nric=dst_rows[0][NRIC],
                    )

    hkl_only_nrics = len(hkl_nric_set) - len(matched_nrics)
    ipr_only_nrics = len(ipr_nric_set) - len(matched_nrics)
    hkl_only_active = sum(1 for n in hkl_active_nrics if n not in ipr_nric_set)
    ipr_only_active = sum(1 for n in ipr_active_nrics if n not in hkl_nric_set)
    ipr_only_records = len(ipr_records) - ipr_missing - ipr_matched_records

    print(f"  * Matched NRICs           : {len(matched_nrics):,}")
    print(f"      same ID {m_same:,} | different ID {m_diff:,} | mixed {m_mixed:,}")
    print(f"  * HKL-only NRICs          : {hkl_only_nrics:,} ({hkl_only_active:,} with an active record)")
    print(f"  * IPR-only NRICs          : {ipr_only_nrics:,} ({ipr_only_active:,} with an active record)")

    # -------------------------------------------------------------------------
    # STEP 4: Generate Final_HKL.csv  (original HKL + eligible IPR copies, Rule 3)
    # -------------------------------------------------------------------------
    print("\n[Step 4/7] Generating Final_HKL.csv...")
    final_hkl_ids = set(hkl_ids)
    with open_csv_write(final_hkl_path) as f_out:
        writer = make_writer(f_out)
        writer.writerow(hkl_header)
        with open_csv_read(hkl_path) as f_in:
            reader = csv.reader(f_in)
            next(reader)
            for row in reader:
                writer.writerow(row)
        ipr_to_hkl = sync_records(ipr_records, cols, final_hkl_ids, hkl_nric_set,
                                  writer, "IPR", "HKL", review)
    print(f"  * Original HKL kept {hkl_total:,} | copied from IPR {ipr_to_hkl['copied']:,} | "
          f"ID changes {len(ipr_to_hkl['id_changes'])}")

    # -------------------------------------------------------------------------
    # STEP 5: Generate Final_IPR.csv  (original IPR + eligible HKL copies, Rule 2)
    # -------------------------------------------------------------------------
    print("\n[Step 5/7] Generating Final_IPR.csv...")
    final_ipr_ids = set(ipr_ids)
    with open_csv_write(final_ipr_path) as f_out:
        writer = make_writer(f_out)
        writer.writerow(ipr_header)
        writer.writerows(ipr_records)
        with open_csv_read(hkl_path) as f_in:
            reader = csv.reader(f_in)
            next(reader)
            hkl_to_ipr = sync_records(reader, cols, final_ipr_ids, ipr_nric_set,
                                      writer, "HKL", "IPR", review)
    print(f"  * Original IPR kept {len(ipr_records):,} | copied from HKL {hkl_to_ipr['copied']:,} | "
          f"ID changes {len(hkl_to_ipr['id_changes'])}")

    # Same ID in both hospitals but different NRIC (possible NRIC typo -> duplicate patient)
    ipr_copy_new_id = {c["original_id"]: c["new_id"] for c in ipr_to_hkl["id_changes"]}
    hkl_copy_new_id = {c["original_id"]: c["new_id"] for c in hkl_to_ipr["id_changes"]}
    same_id_diff_nric = 0
    for rid, h_row in hkl_rows_sharing_ipr_id.items():
        i_row = ipr_by_id[rid]
        if normalize_nric(h_row[NRIC]) == normalize_nric(i_row[NRIC]):
            continue
        same_id_diff_nric += 1
        dob = "same DOB" if h_row[cols["BirthDate"]].strip() == i_row[cols["BirthDate"]].strip() else "different DOB"
        gender = "same gender" if h_row[cols["GenderId"]].strip() == i_row[cols["GenderId"]].strip() else "different gender"
        actions = [
            f"IPR record copied to Final_HKL as Id={ipr_copy_new_id[rid]}" if rid in ipr_copy_new_id
            else "IPR record not copied to Final_HKL",
            f"HKL record copied to Final_IPR as Id={hkl_copy_new_id[rid]}" if rid in hkl_copy_new_id
            else "HKL record not copied to Final_IPR",
        ]
        review.add(
            "SAME_ID_DIFFERENT_NRIC", "HKL", h_row,
            reason=f"Same Id in both hospitals but NRIC differs ({dob}, {gender}); possible NRIC typo",
            action="; ".join(actions),
            related_hospital="IPR", related_id=rid, related_nric=i_row[NRIC],
        )

    # -------------------------------------------------------------------------
    # STEP 6: Validate the generated files
    # -------------------------------------------------------------------------
    print("\n[Step 6/7] Validating generated files...")
    v = Validator()
    v.check("HKL -> IPR: every HKL deleted record excluded from copying",
            hkl_to_ipr["skipped_deleted"] == hkl_deleted,
            f"{hkl_to_ipr['skipped_deleted']:,} of {hkl_deleted:,}", rules=[5])
    v.check("IPR -> HKL: every IPR deleted record excluded from copying",
            ipr_to_hkl["skipped_deleted"] == ipr_deleted,
            f"{ipr_to_hkl['skipped_deleted']:,} of {ipr_deleted:,}", rules=[5])
    for label, st, total in (("IPR -> HKL", ipr_to_hkl, len(ipr_records)), ("HKL -> IPR", hkl_to_ipr, hkl_total)):
        accounted = (st["copied"] + st["skipped_deleted"] + st["skipped_missing_nric"]
                     + st["skipped_nric_in_dest"] + st["skipped_duplicate_in_source"])
        v.check(f"{label}: every source record accounted for", accounted == st["evaluated"] == total,
                f"{accounted:,} accounted / {total:,} records", rules=[2 if label.startswith("HKL") else 3])

    with open_csv_read(hkl_path) as f_src:
        src = csv.reader(f_src)
        next(src)
        hkl_val = validate_final(v, "Final_HKL.csv", final_hkl_path, hkl_header, src, hkl_total,
                                 ipr_records, ipr_to_hkl, cols, hkl_nric_set, hkl_deleted,
                                 hkl_dup_ids, ipr_only_active, keep_rule=2, copy_rule=3)
    with open_csv_read(hkl_path) as f_src:
        src = csv.reader(f_src)
        next(src)
        ipr_val = validate_final(v, "Final_IPR.csv", final_ipr_path, ipr_header, ipr_records,
                                 len(ipr_records), src, hkl_to_ipr, cols, ipr_nric_set, ipr_deleted,
                                 ipr_dup_ids, hkl_only_active, keep_rule=3, copy_rule=2)

    # -------------------------------------------------------------------------
    # STEP 7: Review file + audit report
    # -------------------------------------------------------------------------
    print("\n[Step 7/7] Writing REVIEW_ITEMS.csv and audit report...")
    review.write(review_path)
    review_counts = review.counts()
    elapsed = time.time() - start_time

    L = []
    add = L.append
    rule_text = {
        1: "Same NRIC & same ID: each hospital keeps its own original record",
        2: "HKL-only NRIC: kept in HKL; active records copied to IPR ('111' prefix on ID conflict)",
        3: "IPR-only NRIC: kept in IPR; active records copied to HKL ('111' prefix on ID conflict)",
        4: "Same NRIC & different IDs: each hospital keeps its own original ID",
        5: "IsDeleted = 1: kept at origin, never copied",
    }

    add("=" * 80)
    add("FINAL HOSPITAL PATIENT SYNCHRONIZATION DATASET GENERATION REPORT")
    add("Hospital Kuala Lumpur (HKL) & Institut Perubatan Respiratori (IPR)")
    add(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}   Data directory: {base_dir}")
    add("=" * 80)
    add(f"OVERALL VALIDATION: {'PASSED' if v.all_passed else 'FAILED'}")
    add("")

    add("1. RECORD COUNTS")
    add("-" * 80)
    add(f"  {'File':<22}{'Records':>12}{'Unique IDs':>14}{'IsDeleted=1':>14}{'Missing NRIC':>14}")
    add(f"  {'ConsumersHKL.csv':<22}{hkl_total:>12,}{len(hkl_ids):>14,}{hkl_deleted:>14,}{hkl_missing:>14,}")
    add(f"  {'ConsumersIPR.csv':<22}{len(ipr_records):>12,}{len(ipr_ids):>14,}{ipr_deleted:>14,}{ipr_missing:>14,}")
    add(f"  {'Final_HKL.csv':<22}{hkl_val['total']:>12,}{hkl_val['unique_ids']:>14,}{hkl_val['deleted']:>14,}")
    add(f"  {'Final_IPR.csv':<22}{ipr_val['total']:>12,}{ipr_val['unique_ids']:>14,}{ipr_val['deleted']:>14,}")
    add(f"  Final_HKL = {hkl_val['original']:,} original HKL + {hkl_val['copied']:,} copied from IPR")
    add(f"  Final_IPR = {ipr_val['original']:,} original IPR + {ipr_val['copied']:,} copied from HKL")
    add(f"  Unique valid NRICs: HKL {len(hkl_nric_set):,} ({hkl_nric_counts_multi:,} with >1 record) | "
        f"IPR {len(ipr_nric_set):,} ({ipr_dup_nrics:,} with >1 record)")
    add("")

    add("2. PATIENT MATCHING SUMMARY")
    add("-" * 80)
    add(f"  * NRICs found in both hospitals              : {len(matched_nrics):,} "
        f"(HKL records {hkl_matched_records:,}, IPR records {ipr_matched_records:,})")
    add(f"    - Same NRIC and same ID(s)      (Rule 1)    : {m_same:,} NRICs")
    add(f"    - Same NRIC, different ID(s)    (Rule 4)    : {m_diff:,} NRICs")
    add(f"    - Mixed (some IDs match, some differ)       : {m_mixed:,} NRICs")
    add(f"    - Record pairs with same ID / different ID  : {same_pairs:,} / {diff_pairs:,}")
    add(f"  * NRICs found only in HKL                    : {hkl_only_nrics:,} ({hkl_only_records:,} records)")
    add(f"    - with an active record (copied to IPR)     : {hkl_only_active:,}")
    add(f"    - with only deleted records (not copied)    : {hkl_only_nrics - hkl_only_active:,}")
    add(f"  * NRICs found only in IPR                    : {ipr_only_nrics:,} ({ipr_only_records:,} records)")
    add(f"    - with an active record (copied to HKL)     : {ipr_only_active:,}")
    add(f"    - with only deleted records (not copied)    : {ipr_only_nrics - ipr_only_active:,}")
    add("")

    add("3. COPIED RECORDS WITH CHANGED IDs ('111' PREFIX)")
    add("-" * 80)
    for st in (ipr_to_hkl, hkl_to_ipr):
        dest = "Final_HKL.csv" if st is ipr_to_hkl else "Final_IPR.csv"
        add(f"  Destination {dest}: {len(st['id_changes'])} record(s)")
        add(f"    {'Original ID':<14}{'New ID':<18}{'Prefixes':>9}  NRIC")
        for ch in st["id_changes"]:
            add(f"    {ch['original_id']:<14}{ch['new_id']:<18}{ch['prepends']:>9}  {ch['nric']}")
    add("")

    add("4. SKIPPED RECORDS (NOT COPIED)")
    add("-" * 80)
    for label, st, total in (("IPR -> Final_HKL", ipr_to_hkl, len(ipr_records)),
                             ("HKL -> Final_IPR", hkl_to_ipr, hkl_total)):
        dest = "HKL" if label.startswith("IPR") else "IPR"
        src_name = "IPR" if dest == "HKL" else "HKL"
        add(f"  {label} ({total:,} records evaluated)")
        add(f"    - Copied                                         : {st['copied']:,}")
        add(f"    - Skipped: NRIC already exists in {dest} dataset     : {st['skipped_nric_in_dest']:,}")
        add(f"    - Skipped: NRIC already copied (duplicate {src_name} record): {st['skipped_duplicate_in_source']:,}")
        add(f"    - Skipped: IsDeleted = 1                         : {st['skipped_deleted']:,}")
        add(f"    - Skipped: missing/placeholder NRIC              : {st['skipped_missing_nric']:,}")
    add("")

    add("5. DELETED RECORDS (IsDeleted = 1)")
    add("-" * 80)
    add(f"  * HKL: {hkl_deleted:,} in source | {hkl_val['deleted']:,} retained in Final_HKL | "
        f"{hkl_to_ipr['skipped_deleted']:,} excluded from Final_IPR")
    add(f"  * IPR: {ipr_deleted:,} in source | {ipr_val['deleted']:,} retained in Final_IPR | "
        f"{ipr_to_hkl['skipped_deleted']:,} excluded from Final_HKL")
    add("")

    add("6. VALIDATION CHECKS")
    add("-" * 80)
    for name, passed, detail, _ in v.results:
        add(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    add("")

    add("7. RULE CONFIRMATION (derived from the checks above)")
    add("-" * 80)
    for rule in range(1, 6):
        ok, n = v.rule_status(rule)
        add(f"  Rule {rule}: {'CONFIRMED' if ok else 'NOT CONFIRMED'} ({n} checks) - {rule_text[rule]}")
    add("")

    add("8. ITEMS FOR MANUAL REVIEW (details in REVIEW_ITEMS.csv)")
    add("-" * 80)
    descriptions = {
        "SAME_ID_DIFFERENT_NRIC": "Same ID in both hospitals, different NRIC (possible typo)",
        "DESTINATION_HAS_ONLY_DELETED_RECORDS": "Active patient whose only records in the other hospital are deleted",
        "DUPLICATE_PATIENT_RECORD_IN_SOURCE": "Extra active record of a patient already copied (not copied)",
        "MISSING_OR_PLACEHOLDER_NRIC": "Blank/placeholder NRIC (kept at origin, not copied)",
        "SUSPICIOUS_NRIC_MATCHED": "Placeholder-like NRIC matched across hospitals (records)",
        "SUSPICIOUS_NRIC_COPIED": "Placeholder-like NRIC copied to the other hospital",
    }
    for cat, desc in descriptions.items():
        add(f"  {review_counts.get(cat, 0):>8,}  {cat:<38} {desc}")
    add(f"  {len(review.items):>8,}  TOTAL")
    add(f"  (Same-ID/different-NRIC pairs: {same_id_diff_nric}; matched placeholder-like NRICs: {suspicious_matched}; "
        f"patients active in HKL but deleted-only in IPR: {only_deleted_in_ipr}; "
        f"active in IPR but deleted-only in HKL: {only_deleted_in_hkl})")
    add("")
    add(f"Execution completed in {elapsed:.2f} seconds.")

    with open(report_path, "w", encoding="utf-8") as rep:
        rep.write("\n".join(L) + "\n")

    print(f"\nReview items ({len(review.items):,}) saved to: {review_path}")
    print(f"Audit report saved to: {report_path}")
    print(f"OVERALL VALIDATION: {'PASSED' if v.all_passed else 'FAILED'}")
    print(f"Total execution time: {elapsed:.2f} seconds.")
    return 0 if v.all_passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (InputError, UnicodeDecodeError) as e:
        print(f"\nINPUT ERROR: {e}", file=sys.stderr)
        sys.exit(2)
