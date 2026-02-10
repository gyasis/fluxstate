import snowflake.snowpark as snowpark
from snowflake.snowpark.functions import col, current_date, date_add, date_trunc
import pandas as pd

def main(session: snowpark.Session):
    # Step 1: Calculate the start and end of the previous week dynamically
    start_of_last_week = date_add('DAY', -7, date_trunc('WEEK', current_date()))  # Start of last week (Monday)
    end_of_last_week = date_add('DAY', -1, date_trunc('WEEK', current_date()))    # End of last week (Sunday)

    # Step 2: Placeholder for your SQL logic that populates the original_table DataFrame
    original_table = session.sql("""
        WITH LastCompletedAppointments AS (
            SELECT
                A.PATIENT_ID,
                A.ID AS APPOINTMENT_ID,
                A.APPT_TIME AS LAST_COMPLETED_APPT_TIME,
                A.APPT_TYPE,
                ROW_NUMBER() OVER (PARTITION BY A.PATIENT_ID ORDER BY A.APPT_TIME DESC) AS rn
            FROM
                ELATION.HERSELF_HEALTH.APPOINTMENT A
            INNER JOIN
                ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS AST
            ON
                A.ID = AST.APPOINTMENT_ID
            WHERE
                A.DELETION_TIME IS NULL
                AND AST.DELETION_TIME IS NULL
                AND (
            UPPER(AST.STATUS) = 'CHECKEDOUT' 
            OR (
                    NOT EXISTS (
                        SELECT 1
                        FROM ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS aps
                        WHERE aps.APPOINTMENT_ID = A.ID
                        AND LOWER(aps.STATUS) IN ('notseen', 'cancelled')
                )
                AND EXISTS (
                    SELECT 1
                    FROM ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS aps2
                    WHERE aps2.APPOINTMENT_ID = A.ID
                    AND LOWER(aps2.STATUS) != 'scheduled'
                )
                    )
                )
                        ), 
        EarliestCompletedInitialAppointments AS (
    SELECT
        A.PATIENT_ID,
        MIN(A.APPT_TIME) AS EARLIEST_COMPLETED_INITIAL_APPT_TIME
    FROM
        ELATION.HERSELF_HEALTH.APPOINTMENT A
    INNER JOIN
        ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS AST
    ON
        A.ID = AST.APPOINTMENT_ID
    WHERE
        A.DELETION_TIME IS NULL
        AND AST.DELETION_TIME IS NULL
        AND (
            UPPER(AST.STATUS) = 'CHECKEDOUT' 
            OR (
            NOT EXISTS (
                SELECT 1
                FROM ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS aps
                WHERE aps.APPOINTMENT_ID = A.ID
                AND LOWER(aps.STATUS) IN ('notseen', 'cancelled')
                )
                AND EXISTS (
                    SELECT 1
                    FROM ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS aps2
                    WHERE aps2.APPOINTMENT_ID = A.ID
                    AND LOWER(aps2.STATUS) != 'scheduled'
                )
            )
        )
    GROUP BY A.PATIENT_ID
        ), 
LastInitialComprehensiveAppointments AS (
    SELECT
        A.PATIENT_ID,
        MAX(A.APPT_TIME) AS LAST_INITIAL_COMPLETED_APPT_TIME
    FROM
        ELATION.HERSELF_HEALTH.APPOINTMENT A
    INNER JOIN
        ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS AST
    ON
        A.ID = AST.APPOINTMENT_ID
    WHERE
        A.DELETION_TIME IS NULL
        AND AST.DELETION_TIME IS NULL
        AND (
            UPPER(AST.STATUS) = 'CHECKEDOUT' 
            OR (
                NOT EXISTS (
                    SELECT 1
                    FROM ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS aps
                    WHERE aps.APPOINTMENT_ID = A.ID
                    AND LOWER(aps.STATUS) IN ('notseen', 'cancelled')
                )
                AND EXISTS (
                    SELECT 1
                    FROM ELATION.HERSELF_HEALTH.APPOINTMENT_STATUS aps2
                    WHERE aps2.APPOINTMENT_ID = A.ID
                    AND LOWER(aps2.STATUS) != 'scheduled'
                )
            )
        )
    GROUP BY A.PATIENT_ID
), 
        visit_notes AS (
            SELECT
                vn.ID AS VISIT_NOTE_ID,
                vn.PATIENT_ID,
                vn.DOCUMENT_DATE,
                vn.CREATION_TIME,
                vn.PHYSICIAN_USER_ID,
                vn.CREATED_BY_USER_ID,
                vn.SIGNED_TIME,
                vn.SIGNED_BY_USER_ID
            FROM elation.HERSELF_HEALTH.VISIT_NOTE vn
            WHERE vn.DELETION_TIME IS NULL AND vn.SIGNED_TIME IS NOT NULL
        ), 
        billing_codes AS (
            SELECT
                b.VISIT_NOTE_ID,
                LISTAGG(DISTINCT bi_dx.ICD10_CODE, ',') WITHIN GROUP (ORDER BY bi_dx.ICD10_CODE) AS BILLING_CODES
            FROM elation.HERSELF_HEALTH.BILL b
            JOIN elation.HERSELF_HEALTH.BILL_ITEM bi ON b.ID = bi.BILL_ID
            JOIN elation.HERSELF_HEALTH.BILL_ITEM_DX bi_dx ON bi.ID = bi_dx.BILL_ITEM_ID
            WHERE bi.DELETION_TIME IS NULL AND bi_dx.DELETELOG_ID IS NULL AND bi_dx.BILL_ITEM_DELETION_TIME IS NULL
            GROUP BY b.VISIT_NOTE_ID
        ),
        billing_hcc AS (
            SELECT
                bc.VISIT_NOTE_ID,
                LISTAGG(DISTINCT ppc_hcc.HCC_CODE, ',') WITHIN GROUP (ORDER BY ppc_hcc.HCC_CODE) AS HCC_CODES
            FROM billing_codes bc
            JOIN elation.HERSELF_HEALTH.PATIENT_PROBLEM_CODE_HCC ppc_hcc ON POSITION(ppc_hcc.ICD10_ID IN bc.BILLING_CODES) > 0
            GROUP BY bc.VISIT_NOTE_ID
        ),
        sorted_problems AS (
            SELECT 
                *,
                ROW_NUMBER() OVER (PARTITION BY PATIENT_ID, DESCRIPTION ORDER BY CREATION_TIME) AS rn_asc,
                ROW_NUMBER() OVER (PARTITION BY PATIENT_ID, DESCRIPTION ORDER BY CREATION_TIME DESC) AS rn_desc
            FROM 
                elation.HERSELF_HEALTH.PATIENT_PROBLEM
        ),
        first_problems AS (
            SELECT
                ID,
                PATIENT_ID,
                DESCRIPTION,
                CREATION_TIME AS FIRST_CREATION_DATE,
                LAST_MODIFIED AS FIRST_LAST_MODIFIED,
                CREATED_BY_USER_ID AS FIRST_CREATED_BY,
                DELETION_TIME AS FIRST_DELETION_TIME,
                DELETED_BY_USER_ID AS FIRST_DELETED_BY,
                STATUS AS FIRST_STATUS,
                SYNOPSIS AS FIRST_SYNOPSIS,
                RANK AS FIRST_RANK,
                START_DATE AS FIRST_START_DATE,
                RESOLVED_DATE AS FIRST_RESOLVED_DATE,
                WAREHOUSE_ID AS FIRST_WAREHOUSE_ID,
                HDB_LAST_SYNC AS FIRST_HDB_LAST_SYNC
            FROM
                sorted_problems
            WHERE
                rn_asc = 1
        ),
        last_problems AS (
            SELECT
                ID,
                PATIENT_ID,
                DESCRIPTION,
                CREATION_TIME AS LAST_CREATION_DATE,
                LAST_MODIFIED AS LAST_LAST_MODIFIED,
                CREATED_BY_USER_ID AS LAST_CREATED_BY,
                DELETION_TIME AS LAST_DELETION_TIME,
                DELETED_BY_USER_ID AS LAST_DELETED_BY,
                STATUS AS LAST_STATUS,
                SYNOPSIS AS LAST_SYNOPSIS,
                RANK AS LAST_RANK,
                START_DATE AS LAST_START_DATE,
                RESOLVED_DATE AS LAST_RESOLVED_DATE,
                WAREHOUSE_ID AS LAST_WAREHOUSE_ID,
                HDB_LAST_SYNC AS LAST_HDB_LAST_SYNC
            FROM
                sorted_problems
            WHERE
                rn_desc = 1
        ),
        first_last_problems AS (
            SELECT 
                fp.ID,
                fp.PATIENT_ID,
                fp.DESCRIPTION,
                fp.FIRST_CREATION_DATE,
                lp.LAST_CREATION_DATE,
                lp.LAST_LAST_MODIFIED AS LAST_MODIFICATION_DATE,
                fp.FIRST_CREATED_BY,
                lp.LAST_CREATED_BY AS LAST_MODIFIED_BY,
                lp.LAST_DELETION_TIME AS LAST_DELETION_DATE,
                lp.LAST_DELETED_BY AS LAST_DELETED_BY,
                lp.LAST_STATUS AS STATUS,
                lp.LAST_SYNOPSIS AS SYNOPSIS,
                lp.LAST_RANK AS RANK,
                lp.LAST_START_DATE AS START_DATE,
                lp.LAST_RESOLVED_DATE AS RESOLVED_DATE,
                lp.LAST_WAREHOUSE_ID AS WAREHOUSE_ID,
                lp.LAST_HDB_LAST_SYNC AS HDB_LAST_SYNC,
                ppc.ICD10_ID
            FROM 
                first_problems fp
            JOIN 
                last_problems lp
            ON 
                fp.PATIENT_ID = lp.PATIENT_ID AND fp.DESCRIPTION = lp.DESCRIPTION
            JOIN 
                elation.HERSELF_HEALTH.PATIENT_PROBLEM_CODE ppc
            ON 
                lp.ID = ppc.PATIENT_PROBLEM_ID
            GROUP BY 
                fp.ID, fp.PATIENT_ID, fp.DESCRIPTION, ppc.ICD10_ID, fp.FIRST_CREATION_DATE, lp.LAST_CREATION_DATE, lp.LAST_LAST_MODIFIED, fp.FIRST_CREATED_BY, lp.LAST_CREATED_BY, lp.LAST_DELETION_TIME, lp.LAST_DELETED_BY, lp.LAST_STATUS, lp.LAST_SYNOPSIS, lp.LAST_RANK, lp.LAST_START_DATE, lp.LAST_RESOLVED_DATE, lp.LAST_WAREHOUSE_ID, lp.LAST_HDB_LAST_SYNC
        )
        SELECT DISTINCT
            flp.PATIENT_ID,
            flp.DESCRIPTION,
            flp.FIRST_CREATION_DATE,
            flp.LAST_CREATION_DATE,
            flp.LAST_MODIFICATION_DATE,
            flp.FIRST_CREATED_BY,
            CONCAT(u1.FIRST_NAME, ' ', u1.LAST_NAME) AS FIRST_CREATED_BY_USER_NAME,
            flp.LAST_MODIFIED_BY,
            CONCAT(u2.FIRST_NAME, ' ', u2.LAST_NAME) AS LAST_MODIFIED_BY_USER_NAME,
            flp.LAST_DELETION_DATE,
            flp.LAST_DELETED_BY,
            CONCAT(u3.FIRST_NAME, ' ', u3.LAST_NAME) AS LAST_DELETED_BY_USER_NAME,
            flp.STATUS,
            flp.SYNOPSIS,
            flp.RANK,
            flp.START_DATE,
            flp.RESOLVED_DATE,
            flp.WAREHOUSE_ID,
            flp.HDB_LAST_SYNC,
            i10.CODE AS ICD10_CODE,
            i10.DESCRIPTION AS ICD10_DESCRIPTION,
            ppc_hcc.HCC_CODE,
            bc.BILLING_CODES,  -- Added column to display list of billed ICD-10 codes
            CASE 
                WHEN POSITION(i10.CODE IN bc.BILLING_CODES) > 0 THEN 'Yes'
                ELSE 'No'
            END AS ICD10_ON_BILL,
            lca.LAST_COMPLETED_APPT_TIME,
            lca.APPT_TYPE,
            lca.APPOINTMENT_ID,
            vn.VISIT_NOTE_ID,
            vn.DOCUMENT_DATE,
            vn.PHYSICIAN_USER_ID,
            CONCAT(u4.FIRST_NAME, ' ', u4.LAST_NAME) AS PHYSICIAN_USER_NAME,
            vn.CREATED_BY_USER_ID AS VN_CREATED_BY_USER_ID,
            CONCAT(u5.FIRST_NAME, ' ', u5.LAST_NAME) AS VN_CREATED_BY_USER_NAME,
            vn.SIGNED_TIME,
            vn.SIGNED_BY_USER_ID,
            CONCAT(u6.FIRST_NAME, ' ', u6.LAST_NAME) AS SIGNED_BY_USER_NAME,
    lica.LAST_INITIAL_COMPLETED_APPT_TIME AS LAST_INITIAL  -- Added column to display last initial comprehensive visit time
        FROM first_last_problems flp
        JOIN elation.HERSELF_HEALTH.PATIENT_PROBLEM_CODE ppc ON flp.ID = ppc.PATIENT_PROBLEM_ID
        JOIN elation.HERSELF_HEALTH.PATIENT_PROBLEM_CODE_HCC ppc_hcc ON ppc.ICD10_ID = ppc_hcc.ICD10_ID
        JOIN elation.HERSELF_HEALTH.ICD10 i10 ON ppc.ICD10_ID = i10.ID
        LEFT JOIN LastCompletedAppointments lca ON flp.PATIENT_ID = lca.PATIENT_ID AND lca.rn = 1
        LEFT JOIN visit_notes vn ON flp.PATIENT_ID = vn.PATIENT_ID AND vn.DOCUMENT_DATE = lca.LAST_COMPLETED_APPT_TIME  -- Ensure DOCUMENT_DATE matches LAST_COMPLETED_APPT_TIME
        LEFT JOIN billing_codes bc ON vn.VISIT_NOTE_ID = bc.VISIT_NOTE_ID
        LEFT JOIN billing_hcc bh ON vn.VISIT_NOTE_ID = bh.VISIT_NOTE_ID
        LEFT JOIN elation.HERSELF_HEALTH.USER u1 ON flp.FIRST_CREATED_BY = u1.ID
        LEFT JOIN elation.HERSELF_HEALTH.USER u2 ON flp.LAST_MODIFIED_BY = u2.ID
        LEFT JOIN elation.HERSELF_HEALTH.USER u3 ON flp.LAST_DELETED_BY = u3.ID
        LEFT JOIN elation.HERSELF_HEALTH.USER u4 ON vn.PHYSICIAN_USER_ID = u4.ID
        LEFT JOIN elation.HERSELF_HEALTH.USER u5 ON vn.CREATED_BY_USER_ID = u5.ID
        LEFT JOIN elation.HERSELF_HEALTH.USER u6 ON vn.SIGNED_BY_USER_ID = u6.ID
LEFT JOIN LastInitialComprehensiveAppointments lica ON flp.PATIENT_ID = lica.PATIENT_ID  -- Join the new CTE for last initial comprehensive visit time
        WHERE ppc.ICD10_ID IS NOT NULL
          AND ppc_hcc.VERSION = 2023
    """).collect()

    # Step 3: Create a Pandas DataFrame from the original table
    original_df = pd.DataFrame(original_table)

    # Step 4: Define helper functions for HCC code conversion and checking if the HCC code is on the bill
    def convert_to_hcc(billing_codes: str) -> str:
        if not billing_codes:
            return None
        icd10_codes = billing_codes.split(',')
        hcc_codes = set()
        for code in icd10_codes:
            if code in hcc_dict:
                hcc_codes.add(hcc_dict[code])
        return ','.join(hcc_codes) if hcc_codes else None

    def check_hcc_on_bill(hcc_code: str, billing_hcc_codes: str) -> str:
        if not hcc_code or not billing_hcc_codes:
            return 'No'
        return 'Yes' if hcc_code in billing_hcc_codes.split(',') else 'No'

    # Step 5: Apply the functions to process the data
    hcc_table = session.sql("""
        SELECT DISTINCT
            ppc.ICD10_ID,
            i10.CODE AS ICD10_CODE,
            i10.DESCRIPTION AS ICD10_DESCRIPTION,
            ppc_hcc.HCC_CODE
        FROM elation.HERSELF_HEALTH.PATIENT_PROBLEM_CODE ppc
        JOIN elation.HERSELF_HEALTH.PATIENT_PROBLEM_CODE_HCC ppc_hcc ON ppc.ICD10_ID = ppc_hcc.ICD10_ID
        JOIN elation.HERSELF_HEALTH.ICD10 i10 ON ppc.ICD10_ID = i10.ID
        WHERE ppc_hcc.VERSION = 2023
    """).collect()

    hcc_dict = {row['ICD10_CODE']: row['HCC_CODE'] for row in hcc_table}
    
    original_df['BILLING_HCC_CODES'] = original_df['BILLING_CODES'].apply(convert_to_hcc)
    original_df['HCC_ON_BILL'] = original_df.apply(lambda row: check_hcc_on_bill(row['HCC_CODE'], row['BILLING_HCC_CODES']), axis=1)

    # Step 6: Add 'Pending' column based on the 'SYNOPSIS' containing 'pending'
    original_df['Pending'] = original_df['SYNOPSIS'].str.contains('pending', case=False, na=False)

    # Step 7: Calculate the number of days between DOCUMENT_DATE and SIGNED_TIME
    original_df['DOCUMENT_DATE'] = pd.to_datetime(original_df['DOCUMENT_DATE'])
    original_df['SIGNED_TIME'] = pd.to_datetime(original_df['SIGNED_TIME'])
    original_df['DAYS_TO_SIGN'] = (original_df['SIGNED_TIME'] - original_df['DOCUMENT_DATE']).dt.days

    # Convert key date columns to Timestamps
    original_df['FIRST_CREATION_DATE'] = pd.to_datetime(original_df['FIRST_CREATION_DATE'])
    original_df['LAST_COMPLETED_APPT_TIME'] = pd.to_datetime(original_df['LAST_COMPLETED_APPT_TIME'])
    original_df['RESOLVED_DATE'] = pd.to_datetime(original_df['RESOLVED_DATE'], errors='coerce')

    # Step 8: Risk code creation checks
    original_df['Risk_Code_Created_Before_Visit'] = original_df.apply(
        lambda row: 'Yes' if row['FIRST_CREATION_DATE'] < row['LAST_COMPLETED_APPT_TIME'] else 'No', axis=1)

    original_df['Risk_Code_Created_On_Day_of_Visit'] = original_df.apply(
        lambda row: 'Yes' if row['FIRST_CREATION_DATE'].date() == row['LAST_COMPLETED_APPT_TIME'].date() else 'No', axis=1)

    # Step 9: Calculate 'Final_Status_Soon_After_Visit'
    original_df['Final_Status_Soon_After_Visit'] = original_df.apply(
        lambda row: 'Billed' if row['HCC_ON_BILL'] == 'Yes' and row['SIGNED_TIME'] <= row['LAST_COMPLETED_APPT_TIME'] + pd.Timedelta(days=7) else
                    'Resolved' if not pd.isnull(row['RESOLVED_DATE']) and row['RESOLVED_DATE'] <= row['LAST_COMPLETED_APPT_TIME'] + pd.Timedelta(days=3) else
                    'Pending' if row['Pending'] else 
                    'Disagree' if not pd.isnull(row['LAST_DELETION_DATE']) and row['LAST_DELETION_DATE'] <= row['LAST_COMPLETED_APPT_TIME'] + pd.Timedelta(days=3) else
                    'Ignored', axis=1)

    # Step 10: Create hot-encoded columns for each status
    statuses = ['Pending', 'Resolved', 'Billed', 'Disagree', 'Ignored']
    for status in statuses:
        original_df[f'Status_{status}'] = original_df['Final_Status_Soon_After_Visit'].apply(lambda x: 1 if x == status else 0)

    # Step 11: Filter DataFrame for the previous week's date range
    original_df['LAST_COMPLETED_APPT_DATE'] = original_df['LAST_COMPLETED_APPT_TIME'].dt.date
    original_df = original_df[(original_df['LAST_COMPLETED_APPT_DATE'] >= pd.to_datetime(start_of_last_week).date()) &
                              (original_df['LAST_COMPLETED_APPT_DATE'] <= pd.to_datetime(end_of_last_week).date())]

    # Step 12: Exclude rows where LAST_DELETION_DATE is before LAST_COMPLETED_APPT_TIME
    original_df = original_df[original_df['LAST_DELETION_DATE'].isnull() | 
                              (original_df['LAST_DELETION_DATE'] >= original_df['LAST_COMPLETED_APPT_TIME'])]

    # Step 13: Group by provider and calculate required metrics
    grouped_df = original_df.groupby('SIGNED_BY_USER_NAME').agg(
        total_codes=pd.NamedAgg(column='SIGNED_BY_USER_NAME', aggfunc='count'),
        unique_appointments=pd.NamedAgg(column='APPOINTMENT_ID', aggfunc=pd.Series.nunique),
        Status_Pending=pd.NamedAgg(column='Status_Pending', aggfunc='sum'),
        Status_Resolved=pd.NamedAgg(column='Status_Resolved', aggfunc='sum'),
        Status_Billed=pd.NamedAgg(column='Status_Billed', aggfunc='sum'),
        Status_Disagree=pd.NamedAgg(column='Status_Disagree', aggfunc='sum'),
        Status_Ignored=pd.NamedAgg(column='Status_Ignored', aggfunc='sum'),
        assessed_codes=pd.NamedAgg(column='Status_Ignored', aggfunc=lambda x: (x == 0).sum())
    ).reset_index()

    # Step 14: Calculate the percentage of codes assessed
    grouped_df['percentage_assessed'] = (grouped_df['assessed_codes'] / grouped_df['total_codes']) * 100

    # Step 15: Convert the processed DataFrame back to Snowpark DataFrame and return it
    processed_df = session.create_dataframe(original_df)

    return processed_df
