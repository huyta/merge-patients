# HKL & IPR `consumers` Table Synchronization & Merge Guide

## 1. Overview & Objective
This document explains the step-by-step workflow of the Python ETL & Validation script (`merge_consumers.py`) used to synchronize patient records between the **HKL** (`~300 MB`) and **IPR** (`~3 MB`) `lims-staging.consumers` tables.

Think of the script as a **smart digital receptionist** comparing two hospital filing cabinets (**HKL** and **IPR**). Its goal is to ensure both hospitals have the exact same complete list of patients (`Nric`), with the most up-to-date demographic and contact information, **without ever changing a patient's existing primary key (`Id`)** in either hospital so child tables (orders, specimens, test results) never break.

---

## 2. Step-by-Step Workflow Explanation

### Step 1: Reading & Cleaning Each CSV File (`load_and_validate_csv`)
Before comparing the two hospitals, the script opens `hkl_consumers.csv` and `ipr_consumers.csv` one by one and cleans up messy data so MySQL will not fail during `LOAD DATA LOCAL INFILE`.

1. **Fixes Empty Values (`\N`):**
   In CSV files, an empty string `""` can cause errors in MySQL strict mode (especially for `datetime(6)` columns like `BirthDate` or `UpdatedTimestamp`). The script converts empty nullable fields into `\N`, which is MySQL's native indicator for `NULL`.
2. **Repairs Broken Dates (`validate_datetime`):**
   If a patient record contains an invalid date such as `0000-00-00` or `1985-02-31`, MySQL 8's `NO_ZERO_DATE` mode will reject the import. The script detects these broken dates, logs the offending `Id`s in `validation_report.txt`, and safely heals nullable dates to `NULL` (`\N`).
3. **Catches "Fake" Placeholder NRICs (`JUNK_NRIC_VALUES`):**
   Clinic staff occasionally enter placeholders like `-`, `NA`, `N/A`, `NIL`, or `0` when a patient does not have an ID card.
   * **Why this matters:** If 500 different patients have `Nric = "NA"`, a standard merge script would treat all 500 as the **same person** and overwrite their records.
   * **How the script handles it:** It detects placeholder strings and treats them as **blank**. Those rows are placed into a safe preservation list (`extra_rows`) so they remain in their original database untouched with their original `Id`.
4. **Handles Duplicates Inside the Same Database (`best_by_nric` vs. `extra_rows`):**
   If a single database already contains multiple rows for the same `Nric` (e.g., an older deleted profile `Id=10` and a newer active profile `Id=500`):
   * The script uses `row_sort_key` to pick the **Winner**:
     1. **Active (`IsDeleted = 0`) beats Deleted (`IsDeleted = 1`)**
     2. **Newer `UpdatedTimestamp` (or `CreatedTimestamp`) beats Older Timestamp**
     3. **Lower `Id` acts as a tie-breaker**
   * The winning row (`Id=500`) is placed into the primary lookup dictionary (`best_by_nric`) to be synced across hospitals.
   * The duplicate row (`Id=10`) is **never deleted**—it is kept in `extra_rows` so `Id=10` still exists in the database and historical foreign keys remain intact.
5. **Tracks Every Used `Id` (`used_ids` and `max_id`):**
   As the script reads each file, it records every `Id` currently in use in HKL and IPR, along with the highest `Id` (`max_id`), ensuring it never assigns a colliding `Id` to a newly inserted patient.

---

### Step 2: Checking Cross-Database NRIC Formatting (`check_cross_db_dash_anomalies`)
The script compares NRIC keys across both hospitals to see if one database stores NRICs with dashes (`850101-14-5566`) while the other stores the exact same NRIC without dashes (`850101145566`).
* It logs the count and sample pairs into `validation_report.txt` so the engineering team has full visibility into formatting differences before import.

---

### Step 3: Core Merge & ID-Preservation Logic (`run_pipeline`)
The script combines all unique valid NRICs from both HKL and IPR into one master sorted list (`all_nrics`) and processes each patient against **3 possible scenarios**:

| Scenario | State Before Merge | Action Taken by Script |
| :--- | :--- | :--- |
| **Case 1: Patient Exists in BOTH HKL & IPR** | The `Nric` exists in both CSVs (with either the **same `Id`** or **different `Id`s**). | 1. Compares timestamps (`row_sort_key`) to determine which hospital has the fresher demographic record.<br>2. **Strictly preserves each hospital's local `Id`** (e.g., if HKL is `Id=100` and IPR is `Id=999`, HKL keeps `100` and IPR keeps `999`).<br>3. Updates the older record with the newer demographic fields (`merge_into_existing`), while preserving each hospital's local `ExternalMRN` and any non-null contact info (`Email`, `MobileNo`, `BirthDate`, `Address`). |
| **Case 2: Patient Exists ONLY in HKL** | The `Nric` exists in HKL (e.g., `Id=250`), but is missing in IPR. | 1. HKL keeps its record (`Id=250`) unchanged.<br>2. IPR needs this patient inserted. The script checks IPR's `used_ids`: **"Is `Id=250` free in IPR?"**<br>• **If Free:** Assigns `Id=250` in IPR (keeping IDs aligned across both hospitals).<br>• **If Occupied:** Assigns a new sequential ID at the end of IPR's table (`ipr_max_id + 1`). |
| **Case 3: Patient Exists ONLY in IPR** | The `Nric` exists in IPR (e.g., `Id=45`), but is missing in HKL. | 1. IPR keeps its record (`Id=45`) unchanged.<br>2. HKL needs this patient inserted. The script checks HKL's `used_ids`: **"Is `Id=45` free in HKL?"**<br>• **If Free:** Assigns `Id=45` in HKL.<br>• **If Occupied:** Assigns a new sequential ID at the end of HKL's table (`hkl_max_id + 1`). |

After processing all matched and unmatched NRICs, the script appends `extra_rows` (blank NRICs and intra-database duplicates) back into the final lists so no existing rows are ever lost.

---

### Step 4: Pre-Output Safety Audit (`verify_final_invariants`)
Before writing any CSV files to disk, the script runs **3 mathematical assertions** in memory:
1. **Zero ID Collisions:** Verifies that `hkl_final_rows` and `ipr_final_rows` contain zero duplicate primary key `Id` values.
2. **100% Original ID Preservation:** Verifies that every single `Id` present in the original HKL and IPR input files still exists in the final outputs.
3. **100% NRIC Parity:** Verifies that both final datasets contain the exact same set of unique NRICs.

If any check fails, the script aborts immediately before writing files.

---

### Step 5: Generating Full & Delta Output Files
Once verified, the script writes **5 files**:

1. **`validation_report.txt`**: Complete audit log of row counts, healed dates, junk NRICs ignored, and merge statistics.
2. **`hkl_final.csv` & `ipr_final.csv` (Full Datasets):** Complete merged tables containing 100% of rows for each hospital.
3. **`hkl_delta.csv` & `ipr_delta.csv` (Delta / Changes-Only Datasets):**
   * Because HKL is **~300 MB** (~1.5M rows) and IPR is only **~3 MB** (~15k rows), ~99% of HKL's rows do not change.
   * `hkl_delta.csv` contains **only** the rows that were newly inserted from IPR or updated by IPR. Importing `hkl_delta.csv` into HKL takes **less than 2 seconds** instead of re-importing 300 MB of unchanged rows.

---

## 3. Safe Testing Procedure (Sample Verification Before Full Run)

1. **Generate Targeted Test Samples:**
   Run `python3 create_test_sample.py` to extract `sample_hkl.csv` (~85 rows) and `sample_ipr.csv` (~65 rows) from the real CSV files, covering all scenarios (Same-ID, Diff-ID, Free-ID insert, Collided-ID insert, and Null-NRIC).
2. **Run Merge on Samples:**
   Point `merge_consumers.py` to `sample_hkl.csv` and `sample_ipr.csv` and execute:
   ```bash
   python3 merge_consumers.py
   ```
3. **Inspect Side-by-Side Diff:**
   Run `python3 inspect_test_results.py` to print a before-and-after comparison table verifying that `BOTH_DIFF_ID` records kept their respective local `Id`s while syncing demographic fields.
4. **Execute Full Production Merge:**
   Update `merge_consumers.py` to target `hkl_consumers.csv` and `ipr_consumers.csv`, run the script, review `validation_report.txt`, and proceed with the staging table `LOAD DATA LOCAL INFILE` upsert.

---

## 4. Database Import Commands (`LOAD DATA LOCAL INFILE`)

Run the following SQL commands on **HKL** (using `hkl_delta.csv`) and **IPR** (using `ipr_delta.csv`) to apply the changes without locking or truncating the live `consumers` table:

```sql
USE `lims-staging`;

DROP TABLE IF EXISTS `consumers_delta`;
CREATE TABLE `consumers_delta` LIKE `consumers`;

LOAD DATA LOCAL INFILE '/path/to/delta.csv'
INTO TABLE `consumers_delta`
CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n'
IGNORE 1 LINES;

INSERT INTO `consumers`
SELECT * FROM `consumers_delta`
ON DUPLICATE KEY UPDATE
    `HonorificId`      = VALUES(`HonorificId`),
    `Name`             = VALUES(`Name`),
    `Email`            = VALUES(`Email`),
    `MobileNo`         = VALUES(`MobileNo`),
    `CountryCode`      = VALUES(`CountryCode`),
    `BirthDate`        = VALUES(`BirthDate`),
    `GenderId`         = VALUES(`GenderId`),
    `IsMalaysian`      = VALUES(`IsMalaysian`),
    `EthnicityId`      = VALUES(`EthnicityId`),
    `ExternalMRN`      = VALUES(`ExternalMRN`),
    `Address`          = VALUES(`Address`),
    `IsDeleted`        = VALUES(`IsDeleted`),
    `UpdatedTimestamp` = VALUES(`UpdatedTimestamp`),
    `UpdatedBy`        = VALUES(`UpdatedBy`),
    `LastName`         = VALUES(`LastName`),
    `FirstName`        = VALUES(`FirstName`),
    `nric_type`        = VALUES(`nric_type`),
    `nationality`      = VALUES(`nationality`);

DROP TABLE `consumers_delta`;
```